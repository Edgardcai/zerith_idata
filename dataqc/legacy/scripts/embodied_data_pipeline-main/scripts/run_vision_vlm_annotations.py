#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quality_pipeline.annotations import build_pyramid, iter_frame_annotations
from quality_pipeline.annotations.schema import to_jsonable
from quality_pipeline.episode_io import read_raw_episode
from quality_pipeline.profiles import RobotProfile, load_profile


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run only the vision/VLM annotation pass over one HDF5 episode or a "
            "directory containing multiple episodes. This writes visual detections, "
            "scene descriptions, hand-target assignments, and segment annotations "
            "without running QC checks."
        ),
    )
    parser.add_argument("--profile", required=True, help="Path to robot_profiles/*.yaml")
    parser.add_argument(
        "--input",
        required=True,
        help="HDF5 episode directory/file, or a directory containing episode subdirectories.",
    )
    parser.add_argument("--output", required=True, help="Output annotation directory")
    parser.add_argument(
        "--actions-json",
        default=None,
        help="Optional action_debug payload JSON to attach to the episode.",
    )
    parser.add_argument(
        "--manual-json",
        default=None,
        help="Optional annotations.manual.json with human overrides. "
        "If omitted, looks for <episode>/annotations.manual.json.",
    )
    parser.add_argument(
        "--task",
        default=None,
        help="Optional task instruction override for inputs that do not carry prompt metadata. "
        "G2 HDF5 episodes read meta/episode_meta.json:text instead.",
    )
    parser.add_argument(
        "--vision",
        dest="vision",
        action="store_true",
        default=True,
        help="Enable vision detection. Enabled by default for this script.",
    )
    parser.add_argument(
        "--no-vision",
        dest="vision",
        action="store_false",
        help="Disable vision detection.",
    )
    parser.add_argument(
        "--vlm",
        dest="vlm",
        action="store_true",
        default=True,
        help="Enable VLM scene description and hand-target assignment. Enabled by default.",
    )
    parser.add_argument(
        "--no-vlm",
        dest="vlm",
        action="store_false",
        help="Disable VLM calls.",
    )
    parser.add_argument(
        "--yolo-conf-threshold",
        type=float,
        default=None,
        help="Override processing.annotations.vision_pipeline.conf_threshold for local_yolo.",
    )
    parser.add_argument(
        "--yolo-key-frames",
        default=None,
        help="Comma-separated YOLO key frames to run, for example: first,middle,last.",
    )
    parser.add_argument(
        "--frame-sample",
        type=int,
        default=1,
        help="Write every Nth legacy frame annotation in full mode. Default: 1.",
    )
    parser.add_argument(
        "--artifact-mode",
        choices=("full", "compact"),
        default="full",
        help=(
            "full writes trajectory, segments, frames, frame_annotations, report, and manifest. "
            "compact writes trajectory, segments, annotations, report, and manifest."
        ),
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        default=False,
        help="Shortcut for --artifact-mode compact.",
    )
    args = parser.parse_args()
    if args.compact:
        args.artifact_mode = "compact"

    profile = _profile_with_annotation_overrides(load_profile(args.profile), args)
    input_path = Path(args.input).expanduser().resolve()
    out_path = Path(args.output).expanduser().resolve()
    episode_inputs, batch_mode = _discover_episode_inputs(input_path)

    if batch_mode:
        return _run_batch(args, profile, input_path, out_path, episode_inputs)

    result = _process_episode(
        episode_input=episode_inputs[0],
        out_dir=out_path,
        profile=profile,
        args=args,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _profile_with_annotation_overrides(profile: Any, args: argparse.Namespace) -> Any:
    raw = copy.deepcopy(profile.raw)
    ann_cfg = raw.setdefault("processing", {}).setdefault("annotations", {})
    vp_cfg = ann_cfg.setdefault("vision_pipeline", {})
    vp_cfg["enabled"] = bool(args.vision)
    if args.yolo_conf_threshold is not None:
        vp_cfg["conf_threshold"] = float(args.yolo_conf_threshold)
    if args.yolo_key_frames:
        vp_cfg["key_frames"] = [
            item.strip()
            for item in str(args.yolo_key_frames).split(",")
            if item.strip()
        ]
    ann_cfg.setdefault("vlm", {})["enabled"] = bool(args.vlm)
    return RobotProfile(path=profile.path, raw=raw)


def _process_episode(
    *,
    episode_input: Path,
    out_dir: Path,
    profile: Any,
    args: argparse.Namespace,
) -> dict[str, Any]:
    episode = read_raw_episode(episode_input, profile, actions_json=args.actions_json)
    if args.task and episode.meta.get("source_format") != "g2_hdf5":
        episode.meta["prompt"] = str(args.task)
        episode.meta["task_override_source"] = "cli"

    out_dir.mkdir(parents=True, exist_ok=True)
    episode.meta["processed_dir"] = str(out_dir)
    pyramid = build_pyramid(episode, profile, manual_path=args.manual_json, repo_root=ROOT)
    artifacts = _write_outputs(
        out_dir=out_dir,
        episode=episode,
        profile=profile,
        pyramid=pyramid,
        artifact_mode=str(args.artifact_mode),
        frame_sample=args.frame_sample,
    )
    manifest = _manifest(
        episode=episode,
        profile=profile,
        episode_input=episode_input,
        out_dir=out_dir,
        pyramid=pyramid,
        artifacts=artifacts,
        artifact_mode=str(args.artifact_mode),
    )
    _write_json(out_dir / "manifest.json", manifest)

    trajectory = to_jsonable(pyramid["trajectory"])
    annotation = trajectory.get("annotation", {})
    if not isinstance(annotation, dict):
        annotation = {}
    return {
        "ok": True,
        "episode_id": episode.episode_id,
        "profile_id": profile.profile_id,
        "input": str(episode_input),
        "output": str(out_dir),
        "artifact_mode": str(args.artifact_mode),
        "scene_description": trajectory.get("scene_description", "not_generated"),
        "target_objects": _target_labels(trajectory),
        "hand_target_assignments": annotation.get("hand_target_assignments", {}),
        "artifacts": artifacts,
    }


def _run_batch(
    args: argparse.Namespace,
    profile: Any,
    input_root: Path,
    out_root: Path,
    episode_inputs: list[Path],
) -> int:
    out_root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for idx, episode_input in enumerate(episode_inputs, start=1):
        out_dir = out_root / _episode_output_name(episode_input)
        print(
            f"[{idx}/{len(episode_inputs)}] {episode_input} -> {out_dir}",
            file=sys.stderr,
        )
        try:
            results.append(
                _process_episode(
                    episode_input=episode_input,
                    out_dir=out_dir,
                    profile=profile,
                    args=args,
                )
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            print(f"[{idx}/{len(episode_inputs)}] failed: {error}", file=sys.stderr)
            results.append(
                {
                    "ok": False,
                    "episode_id": _episode_output_name(episode_input),
                    "profile_id": profile.profile_id,
                    "input": str(episode_input),
                    "output": str(out_dir),
                    "error": error,
                }
            )

    summary = _batch_summary(profile, input_root, out_root, results)
    _write_json(out_root / "batch_annotations_summary.json", summary)
    _write_batch_summary_table(out_root / "batch_annotations_summary.md", summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["failed_to_process"] == 0 else 1


def _discover_episode_inputs(input_path: Path) -> tuple[list[Path], bool]:
    if not input_path.exists():
        raise FileNotFoundError(input_path)
    if input_path.is_file() or _looks_like_episode_dir(input_path):
        return [input_path], False

    episode_inputs = [
        child
        for child in input_path.iterdir()
        if not child.name.startswith(".")
        and (
            (child.is_dir() and _looks_like_episode_dir(child))
            or (child.is_file() and _looks_like_episode_file(child))
        )
    ]
    episode_inputs = sorted(episode_inputs, key=_natural_path_key)
    if episode_inputs:
        return episode_inputs, True
    return [input_path], False


def _looks_like_episode_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    markers = (
        path / "state.csv",
        path / "states" / "aligned_joints.h5",
        path / "states" / "aligned_joints.hdf5",
        path / "aligned_joints.h5",
        path / "aligned_joints.hdf5",
    )
    return any(marker.is_file() for marker in markers)


def _looks_like_episode_file(path: Path) -> bool:
    return path.suffix.lower() in {".parquet", ".h5", ".hdf5"}


def _natural_path_key(path: Path) -> list[Any]:
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", path.name)
    ]


def _episode_output_name(path: Path) -> str:
    if path.is_file():
        if path.name.startswith("aligned_joints.") and path.parent.name == "states":
            return path.parent.parent.name
        return path.stem
    return path.name


def _write_outputs(
    *,
    out_dir: Path,
    episode: Any,
    profile: Any,
    pyramid: dict[str, Any],
    artifact_mode: str,
    frame_sample: int,
) -> dict[str, str]:
    trajectory = to_jsonable(pyramid["trajectory"])
    segments = [to_jsonable(seg) for seg in pyramid["segments"]]

    artifacts = {
        "trajectory": "trajectory.json",
        "segments": "segments.jsonl",
        "annotations": "annotations.json",
        "annotation_report": "annotation_report.md",
    }
    _write_json(out_dir / "trajectory.json", trajectory)
    _write_jsonl(out_dir / "segments.jsonl", segments)
    _write_json(out_dir / "annotations.json", _annotation_payload(episode, trajectory, segments, artifact_mode))
    _write_annotation_report(out_dir / "annotation_report.md", episode=episode, trajectory=trajectory, segments=segments)

    if artifact_mode != "compact":
        frames = [to_jsonable(frame) for frame in pyramid["frames"]]
        _write_jsonl(out_dir / "frames.jsonl", frames)
        _write_frame_annotations(out_dir / "frame_annotations.jsonl", episode, profile, frame_sample)
        artifacts["frames"] = "frames.jsonl"
        artifacts["frame_annotations"] = "frame_annotations.jsonl"
    return artifacts


def _annotation_payload(
    episode: Any,
    trajectory: dict[str, Any],
    segments: list[dict[str, Any]],
    artifact_mode: str,
) -> dict[str, Any]:
    annotation = trajectory.get("annotation", {})
    if not isinstance(annotation, dict):
        annotation = {}
    payload = {
        "trajectory": {
            "episode_id": trajectory.get("episode_id"),
            "profile_id": trajectory.get("profile_id"),
            "robot": trajectory.get("robot"),
            "instruction": trajectory.get("instruction", ""),
            "target_objects": _target_labels(trajectory),
            "scene_description": trajectory.get("scene_description", "not_generated"),
            "annotation_source": annotation.get("source", "auto"),
            "vision_pipeline": annotation.get("vision_pipeline", ""),
            "vision_keyframe_detections": annotation.get("vision_keyframe_detections", []),
            "vlm_model": annotation.get("vlm_model", ""),
            "hand_target_assignment_source": annotation.get("hand_target_assignment_source", ""),
            "hand_target_assignments": annotation.get("hand_target_assignments", {}),
            "hand_target_assignment_vlm": annotation.get("hand_target_assignment_vlm", {}),
        },
        "segments": segments,
        "frame_annotation_count": episode.n_frames,
    }
    if artifact_mode != "compact":
        payload["frame_annotation_file"] = "frame_annotations.jsonl"
    return payload


def _manifest(
    *,
    episode: Any,
    profile: Any,
    episode_input: Path,
    out_dir: Path,
    pyramid: dict[str, Any],
    artifacts: dict[str, str],
    artifact_mode: str,
) -> dict[str, Any]:
    trajectory = to_jsonable(pyramid["trajectory"])
    annotation = trajectory.get("annotation", {})
    if not isinstance(annotation, dict):
        annotation = {}
    return {
        "episode_id": episode.episode_id,
        "profile_id": profile.profile_id,
        "input": str(episode_input),
        "raw_episode_dir": str(episode.root),
        "raw_source_path": str(
            episode.meta.get("raw_hdf5_path")
            or episode.meta.get("raw_parquet_path")
            or episode.root
        ),
        "processed_dir": str(out_dir),
        "artifact_mode": artifact_mode,
        "artifacts": artifacts,
        "annotation": {
            "scene_description": trajectory.get("scene_description", "not_generated"),
            "target_objects": _target_labels(trajectory),
            "hand_target_assignments": annotation.get("hand_target_assignments", {}),
            "hand_target_assignment_source": annotation.get("hand_target_assignment_source", ""),
            "vision_keyframe_detection_count": _vision_detection_count(annotation),
            "vlm_model": annotation.get("vlm_model", ""),
        },
    }


def _batch_summary(
    profile: Any,
    input_root: Path,
    out_root: Path,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    processed = [item for item in results if item.get("ok")]
    return {
        "ok": len(processed) == len(results),
        "profile_id": profile.profile_id,
        "input": str(input_root),
        "output": str(out_root),
        "episode_count": len(results),
        "processed": len(processed),
        "failed_to_process": len(results) - len(processed),
        "artifacts": {
            "batch_summary": "batch_annotations_summary.json",
            "batch_summary_table": "batch_annotations_summary.md",
        },
        "episodes": results,
    }


def _write_annotation_report(
    path: Path,
    *,
    episode: Any,
    trajectory: dict[str, Any],
    segments: list[dict[str, Any]],
) -> None:
    annotation = trajectory.get("annotation", {})
    if not isinstance(annotation, dict):
        annotation = {}

    lines = [
        "## 视觉/VLM 标注结果",
        "",
        "| 信息项 | 内容 |",
        "|---|---|",
        f"| episode_id | {_escape_table_cell(str(trajectory.get('episode_id', episode.episode_id)))} |",
        f"| instruction | {_escape_table_cell(str(trajectory.get('instruction', '')))} |",
        f"| scene_description | {_escape_table_cell(str(trajectory.get('scene_description', 'not_generated')))} |",
        f"| target_objects | {_escape_table_cell(', '.join(_target_labels(trajectory)))} |",
        f"| vision_pipeline | {_escape_table_cell(str(annotation.get('vision_pipeline', '')))} |",
        f"| vision_error | {_escape_table_cell(str(annotation.get('vision_error', '')))} |",
        f"| vlm_model | {_escape_table_cell(str(annotation.get('vlm_model', '')))} |",
        "",
        "## 目标检测",
        "",
        "| target_object | bbox_xyxy_norm | source |",
        "|---|---|---|",
    ]
    for obj in trajectory.get("objects", []):
        if not isinstance(obj, dict):
            continue
        lines.append(
            "| "
            + " | ".join(
                _escape_table_cell(value)
                for value in (
                    str(obj.get("label", "")),
                    _format_bbox(obj.get("bbox")),
                    str(obj.get("source", "")),
                )
            )
            + " |"
        )

    _append_keyframe_detection_table(lines, annotation)
    _append_hand_target_table(lines, annotation)
    _append_segment_table(lines, segments)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _append_keyframe_detection_table(lines: list[str], annotation: dict[str, Any]) -> None:
    keyframes = annotation.get("vision_keyframe_detections")
    if not isinstance(keyframes, list) or not keyframes:
        return

    lines.extend(
        [
            "",
            "## YOLO 原始检测",
            "",
            "| frame | frame_idx | label | raw_label | confidence | bbox_xyxy_norm | image_path |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for frame in keyframes:
        if not isinstance(frame, dict):
            continue
        detections = frame.get("detections")
        if not isinstance(detections, list) or not detections:
            lines.append(
                "| "
                + " | ".join(
                    _escape_table_cell(value)
                    for value in (
                        str(frame.get("frame_key", "")),
                        str(frame.get("frame_idx", "")),
                        "",
                        str(frame.get("error", "")),
                        "",
                        "",
                        str(frame.get("image_path", "")),
                    )
                )
                + " |"
            )
            continue
        for det in detections:
            if not isinstance(det, dict):
                continue
            lines.append(
                "| "
                + " | ".join(
                    _escape_table_cell(value)
                    for value in (
                        str(frame.get("frame_key", "")),
                        str(frame.get("frame_idx", "")),
                        str(det.get("label", "")),
                        str(det.get("raw_label", "")),
                        _format_number(det.get("confidence")),
                        _format_bbox(det.get("bbox_xyxy_norm")),
                        str(frame.get("image_path", "")),
                    )
                )
                + " |"
            )


def _append_hand_target_table(lines: list[str], annotation: dict[str, Any]) -> None:
    assignments = annotation.get("hand_target_assignments", {})
    vlm = annotation.get("hand_target_assignment_vlm", {})
    if not isinstance(assignments, dict):
        assignments = {}
    if not isinstance(vlm, dict):
        vlm = {}
    hands = sorted({*assignments.keys(), *vlm.keys()})
    if not hands:
        return

    lines.extend(
        [
            "",
            "## 左右手目标分配",
            "",
            "| hand | assigned_target | vlm_target | vlm_confidence | sample_frame | grasp_start_frame |",
            "|---|---|---|---|---|---|",
        ]
    )
    for hand in hands:
        info = vlm.get(hand, {})
        if not isinstance(info, dict):
            info = {}
        lines.append(
            "| "
            + " | ".join(
                _escape_table_cell(value)
                for value in (
                    str(hand),
                    str(assignments.get(hand, "")),
                    str(info.get("target_object", "")),
                    str(info.get("confidence", "")),
                    str(info.get("sample_frame_idx", "")),
                    str(info.get("grasp_start_frame", "")),
                )
            )
            + " |"
        )


def _append_segment_table(lines: list[str], segments: list[dict[str, Any]]) -> None:
    lines.extend(
        [
            "",
            "## Subtask 明细",
            "",
            "| id | hand | target_object | subtask | 帧范围 | 时间范围(s) |",
            "|---|---|---|---|---|---|",
        ]
    )
    for segment in segments:
        start_frame = segment.get("start_frame", "")
        end_frame = segment.get("end_frame", "")
        start_time = segment.get("start_time", "")
        end_time = segment.get("end_time", "")
        lines.append(
            "| "
            + " | ".join(
                _escape_table_cell(value)
                for value in (
                    str(segment.get("segment_id", "")),
                    str(segment.get("hand", "")),
                    str(segment.get("target_object", "")),
                    str(segment.get("subtask", "")),
                    f"{start_frame}-{end_frame}",
                    f"{_format_number(start_time)}-{_format_number(end_time)}",
                )
            )
            + " |"
        )


def _write_batch_summary_table(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "## 批量视觉/VLM 标注汇总",
        "",
        "| 信息项 | 内容 |",
        "|---|---|",
        f"| profile_id | {_escape_table_cell(str(summary.get('profile_id', '')))} |",
        f"| input | {_escape_table_cell(str(summary.get('input', '')))} |",
        f"| output | {_escape_table_cell(str(summary.get('output', '')))} |",
        f"| episode_count | {_escape_table_cell(str(summary.get('episode_count', '')))} |",
        f"| processed | {_escape_table_cell(str(summary.get('processed', '')))} |",
        f"| failed_to_process | {_escape_table_cell(str(summary.get('failed_to_process', '')))} |",
        "",
        "## Episode 明细",
        "",
        "| episode_id | 处理结果 | scene_description | target_objects | hand_target_assignments | output | error |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in summary.get("episodes", []):
        if not isinstance(item, dict):
            continue
        lines.append(
            "| "
            + " | ".join(
                _escape_table_cell(value)
                for value in (
                    str(item.get("episode_id", "")),
                    "成功" if item.get("ok") else "失败",
                    str(item.get("scene_description", "")),
                    ", ".join(str(value) for value in item.get("target_objects", []))
                    if isinstance(item.get("target_objects"), list)
                    else str(item.get("target_objects", "")),
                    json.dumps(item.get("hand_target_assignments", {}), ensure_ascii=False),
                    str(item.get("output", "")),
                    str(item.get("error", "")),
                )
            )
            + " |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _target_labels(trajectory: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for obj in trajectory.get("objects", []):
        if isinstance(obj, dict) and obj.get("label"):
            labels.append(str(obj["label"]))
    return labels


def _vision_detection_count(annotation: dict[str, Any]) -> int:
    keyframes = annotation.get("vision_keyframe_detections")
    if not isinstance(keyframes, list):
        return 0
    count = 0
    for frame in keyframes:
        if not isinstance(frame, dict):
            continue
        detections = frame.get("detections")
        if isinstance(detections, list):
            count += len(detections)
    return count


def _format_bbox(value: Any) -> str:
    if not isinstance(value, list):
        return str(value)
    return "[" + ", ".join(_format_number(item) for item in value) + "]"


def _format_number(value: Any) -> str:
    try:
        return f"{float(value):.3f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(value)


def _escape_table_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: list[Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _write_frame_annotations(
    path: Path,
    episode: Any,
    profile: Any,
    frame_sample: int,
) -> None:
    sample = max(1, int(frame_sample))
    rows = iter_frame_annotations(episode, profile)
    with path.open("w", encoding="utf-8") as f:
        for idx, row in enumerate(rows):
            if idx % sample != 0:
                continue
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
