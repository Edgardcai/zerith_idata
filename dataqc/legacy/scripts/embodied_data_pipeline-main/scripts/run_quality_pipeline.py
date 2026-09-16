#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quality_pipeline.annotations import (
    build_pyramid,
    iter_frame_annotations,
)
from quality_pipeline.annotations.pyramid import write_pyramid
from quality_pipeline.annotations.schema import to_jsonable
from quality_pipeline.episode_io import read_raw_episode
from quality_pipeline.profiles import load_profile
from quality_pipeline.qc import run_quality_checks
from quality_pipeline.rtml import build_rtml_report, build_rtml_spec


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run a CoRobot/RTML-inspired QC and annotation pass over one raw robot "
            "episode or a directory containing multiple episodes."
        ),
    )
    parser.add_argument("--profile", required=True, help="Path to robot_profiles/*.yaml")
    parser.add_argument(
        "--input",
        required=True,
        help=(
            "Raw episode directory, parquet file, HDF5 episode directory/file, "
            "or a directory containing episode subdirectories."
        ),
    )
    parser.add_argument("--output", required=True, help="Output processing directory")
    parser.add_argument(
        "--actions-json",
        default=None,
        help="Optional action_debug payload JSON to attach to the episode QC.",
    )
    parser.add_argument(
        "--manual-json",
        default=None,
        help="Optional annotations.manual.json with human overrides. "
        "If omitted, looks for <episode>/annotations.manual.json.",
    )
    parser.add_argument(
        "--manual-failure-json",
        default=None,
        help=(
            "Optional manual_failure_annotations.json. Matching failed episodes are "
            "reported as 采集失败 after normal QC is still generated."
        ),
    )
    parser.add_argument(
        "--task",
        default=None,
        help="Optional task instruction override for inputs that do not carry prompt metadata. "
        "G2 HDF5 episodes read meta/episode_meta.json:text instead.",
    )
    parser.add_argument(
        "--vision",
        action="store_true",
        default=False,
        help="Enable vision pipeline (YOLO + optional VLM) regardless of profile setting.",
    )
    parser.add_argument(
        "--vlm",
        action="store_true",
        default=False,
        help="Enable VLM scene description and hand-target assignment (requires configured API key env).",
    )
    parser.add_argument(
        "--frame-sample",
        type=int,
        default=1,
        help="Write every Nth frame annotation. Default: 1 (all frames).",
    )
    parser.add_argument(
        "--artifact-mode",
        choices=("full", "compact"),
        default="full",
        help=(
            "full writes all debug/canonical artifacts. compact writes only "
            "annotations.json, qc_report.json, final_report_table.md, and manifest.json."
        ),
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        default=False,
        help="Shortcut for --artifact-mode compact.",
    )
    parser.add_argument(
        "--num-workers",
        type=int,
        default=1,
        help="Number of episode-level QC worker processes for batch input. Default: 1.",
    )
    parser.add_argument(
        "--stationary-threshold",
        type=int,
        choices=(20, 40, 60),
        default=None,
        help=(
            "Override max_stationary_action_frames for this QC run. "
            "Supported web-console levels: 20, 40, or 60 frames."
        ),
    )
    args = parser.parse_args()
    if args.compact:
        args.artifact_mode = "compact"
    args.num_workers = max(1, int(args.num_workers))

    profile = _profile_with_cli_overrides(load_profile(args.profile), args)
    input_path = Path(args.input).expanduser().resolve()
    out_path = Path(args.output).expanduser().resolve()
    episode_inputs, batch_mode = _discover_episode_inputs(input_path)

    if batch_mode:
        return _run_batch(args, profile, input_path, out_path, episode_inputs)

    manual_failures = _load_manual_failure_map(args.manual_failure_json)
    manual = _manual_failure_for_episode_input(episode_inputs[0], manual_failures)
    result = _process_episode(
        episode_input=episode_inputs[0],
        out_dir=out_path,
        profile=profile,
        args=args,
    )
    result = _apply_manual_failure_override(result, episode_inputs[0], out_path, manual)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _profile_with_cli_overrides(profile: Any, args: argparse.Namespace) -> Any:
    # Allow CLI flags to override profile vision/vlm and QC threshold settings.
    stationary_threshold = getattr(args, "stationary_threshold", None)
    if not (args.vision or args.vlm or stationary_threshold is not None):
        return profile
    import copy

    raw = copy.deepcopy(profile.raw)
    ann_cfg = raw.setdefault("processing", {}).setdefault("annotations", {})
    if args.vision:
        ann_cfg.setdefault("vision_pipeline", {})["enabled"] = True
    if args.vlm:
        ann_cfg.setdefault("vlm", {})["enabled"] = True
    if stationary_threshold is not None:
        raw.setdefault("processing", {}).setdefault("rtml", {}).setdefault(
            "global_constraints", {}
        )["max_stationary_action_frames"] = int(stationary_threshold)
    from quality_pipeline.profiles import RobotProfile

    return RobotProfile(path=profile.path, raw=raw)


def _process_episode(
    *,
    episode_input: Path,
    out_dir: Path,
    profile: Any,
    args: argparse.Namespace,
) -> dict[str, Any]:
    from dataqc.incremental import reuse_cli, remember_cli
    cached= reuse_cli(episode_input,out_dir,profile,args)
    if cached:return cached
    episode = read_raw_episode(episode_input, profile, actions_json=args.actions_json)
    if args.task and episode.meta.get("source_format") != "g2_hdf5":
        episode.meta["prompt"] = str(args.task)
        episode.meta["task_override_source"] = "cli"
    out_dir.mkdir(parents=True, exist_ok=True)
    episode.meta["processed_dir"] = str(out_dir)
    qc_report = run_quality_checks(episode, profile)
    pyramid = build_pyramid(episode, profile, manual_path=args.manual_json, repo_root=ROOT)
    annotations = _legacy_annotations(episode, pyramid)
    artifacts = _write_outputs(
        out_dir=out_dir,
        episode=episode,
        profile=profile,
        qc_report=qc_report,
        annotations=annotations,
        pyramid=pyramid,
        artifact_mode=str(args.artifact_mode),
        frame_sample=args.frame_sample,
    )
    _write_json(
        out_dir / "manifest.json",
        _manifest(
            episode,
            profile,
            qc_report,
            out_dir,
            pyramid,
            artifacts=artifacts,
            artifact_mode=str(args.artifact_mode),
        ),
    )

    quality_issues = _quality_issue_summary(qc_report)
    delete_status = _delete_status_from_qc(qc_report)
    result = {
        "ok": True,
        "episode_id": episode.episode_id,
        "profile_id": profile.profile_id,
        "input": str(episode_input),
        "accepted": qc_report["accepted"],
        "quality_grade": qc_report.get("quality_grade", ""),
        "rules_version": qc_report.get("rules_version", ""),
        "review_required": qc_report.get("review_required", False),
        "quality_score": qc_report["quality_score"],
        "fps": qc_report.get("summary", {}).get("fps"),
        "quality_issues": quality_issues,
        "delete_status": delete_status,
        "error": "" if qc_report["accepted"] else quality_issues,
        "artifact_mode": str(args.artifact_mode),
        "artifacts": artifacts,
        "output": str(out_dir),
    }
    remember_cli(episode_input,profile,args,result)
    return result


def _worker_args_from_namespace(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "profile": str(args.profile),
        "actions_json": args.actions_json,
        "manual_json": args.manual_json,
        "task": args.task,
        "vision": bool(args.vision),
        "vlm": bool(args.vlm),
        "frame_sample": int(args.frame_sample),
        "artifact_mode": str(args.artifact_mode),
        "compact": bool(args.compact),
        "manual_failure_json": args.manual_failure_json,
        "stationary_threshold": getattr(args, "stationary_threshold", None),
        "num_workers": 1,
    }


def _process_episode_worker(payload: tuple[int, int, str, str, dict[str, Any]]) -> tuple[int, dict[str, Any]]:
    idx, total, episode_input_text, out_dir_text, args_dict = payload
    args = argparse.Namespace(**args_dict)
    profile = _profile_with_cli_overrides(load_profile(args.profile), args)
    episode_input = Path(episode_input_text)
    out_dir = Path(out_dir_text)
    manual_failures = _load_manual_failure_map(args.manual_failure_json)
    manual = _manual_failure_for_episode_input(episode_input, manual_failures)
    try:
        result = _process_episode(
            episode_input=episode_input,
            out_dir=out_dir,
            profile=profile,
            args=args,
        )
        result = _apply_manual_failure_override(result, episode_input, out_dir, manual)
    except Exception as exc:
        result = {
            "ok": False,
            "episode_id": _episode_output_name(episode_input),
            "profile_id": profile.profile_id,
            "input": str(episode_input),
            "output": str(out_dir),
            "error": f"{type(exc).__name__}: {exc}",
        }
        result = _apply_manual_failure_override(result, episode_input, out_dir, manual)
    result["_batch_index"] = idx
    result["_batch_total"] = total
    return idx, result


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
        path / "episode.hdf5",
        path / "episode.h5",
        path / "states" / "aligned_joints.h5",
        path / "states" / "aligned_joints.hdf5",
        path / "aligned_joints.h5",
        path / "aligned_joints.hdf5",
    )
    return any(marker.is_file() for marker in markers)


def _looks_like_episode_file(path: Path) -> bool:
    return path.suffix.lower() in {".parquet", ".h5", ".hdf5"}


def _natural_path_key(path: Path) -> list[tuple[int, Any]]:
    return [
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.split(r"(\d+)", path.name)
        if part
    ]


def _episode_output_name(path: Path) -> str:
    if path.is_file():
        if path.name.startswith("aligned_joints.") and path.parent.name == "states":
            return path.parent.parent.name
        return path.stem
    return path.name


def _manual_failure_truthy(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "fail", "failed", "failure", "失败", "采集失败"}


def _normalise_quality_grade(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text if text in {"A", "B", "C", "F"} else ""


def _normalise_manual_failure_entries(payload: Any) -> dict[str, dict[str, Any]]:
    if isinstance(payload, dict):
        raw_entries = payload.get("episodes") or payload.get("failures") or payload.get("annotations")
        if raw_entries is None:
            raw_entries = []
            for key, value in payload.items():
                if key in {"version", "updated_at", "source"}:
                    continue
                if isinstance(value, dict):
                    raw_entries.append({"episode_name": key, **value})
        elif isinstance(raw_entries, dict):
            raw_entries = [
                {"episode_name": key, **value}
                if isinstance(value, dict)
                else {"episode_name": key, "is_failure": value}
                for key, value in raw_entries.items()
            ]
    elif isinstance(payload, list):
        raw_entries = payload
    else:
        raw_entries = []

    entries: dict[str, dict[str, Any]] = {}
    for item in raw_entries:
        if isinstance(item, str):
            name = item.strip()
            reason = ""
            is_failure = True
            quality_grade = "F"
        elif isinstance(item, dict):
            name = str(item.get("episode_name") or item.get("episode_id") or item.get("name") or "").strip()
            reason = str(item.get("reason_label") or item.get("reason") or item.get("remark") or "").strip()
            quality_grade = _normalise_quality_grade(item.get("quality_grade") or item.get("grade"))
            failure_value = (
                item.get("is_failure")
                if "is_failure" in item
                else item.get("failure", item.get("failed", item.get("manual_failure")))
            )
            if quality_grade:
                is_failure = quality_grade == "F"
            else:
                is_failure = _manual_failure_truthy(failure_value, default=bool(reason))
                quality_grade = "F" if is_failure else ""
        else:
            continue
        if name:
            entries[name] = {
                "episode_name": name,
                "is_failure": bool(is_failure),
                "reason_label": reason,
                "quality_grade": quality_grade,
            }
    return entries


def _load_manual_failure_map(path_text: str | None) -> dict[str, dict[str, Any]]:
    if not path_text:
        return {}
    path = Path(path_text).expanduser().resolve()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return _normalise_manual_failure_entries(payload)


def _read_episode_meta_for_match(path: Path) -> dict[str, Any]:
    if path.is_file() and path.name.startswith("episode."):
        candidates = (path.parent / "episode_meta.json", path.parent / "meta" / "episode_meta.json")
    elif path.is_file() and path.name.startswith("aligned_joints.") and path.parent.name == "states":
        candidates = (path.parent.parent / "meta" / "episode_meta.json",)
    elif path.is_dir():
        candidates = (path / "episode_meta.json", path / "meta" / "episode_meta.json")
    else:
        candidates = (path.parent / "episode_meta.json", path.parent / "meta" / "episode_meta.json")
    meta_path = next((candidate for candidate in candidates if candidate.is_file()), candidates[0])
    if not meta_path.is_file():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _manual_failure_match_keys(path: Path) -> list[str]:
    keys = [_episode_output_name(path), path.stem, path.name]
    meta = _read_episode_meta_for_match(path)
    for key in (
        "episode_id",
        "source_episode_id",
        "raw_episode_id",
        "episode_name",
        "source_episode_name",
        "source_mcap_episode_name",
    ):
        value = str(meta.get(key) or "").strip()
        if value:
            keys.append(value)
    source_files = meta.get("source_mcap_files")
    if isinstance(source_files, list):
        for item in source_files:
            item_path = Path(str(item))
            keys.extend([item_path.stem, item_path.name])
    unique: list[str] = []
    seen: set[str] = set()
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def _manual_failure_for_episode_input(
    episode_input: Path,
    manual_failures: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    for key in _manual_failure_match_keys(episode_input):
        if key in manual_failures:
            return manual_failures[key]
    return None


def _manual_failure_reason(manual: dict[str, Any] | None) -> str:
    if not manual:
        return ""
    return str(manual.get("reason_label") or "").strip() or "人工标注采集失败"


def _write_manual_failure_marker(
    episode_input: Path,
    out_dir: Path,
    manual: dict[str, Any],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        out_dir / "manual_failure.json",
        {
            "episode_name": str(manual.get("episode_name") or _episode_output_name(episode_input)),
            "reason_label": _manual_failure_reason(manual),
            "input": str(episode_input),
        },
    )


def _apply_manual_failure_override(
    result: dict[str, Any],
    episode_input: Path,
    out_dir: Path,
    manual: dict[str, Any] | None,
) -> dict[str, Any]:
    if not manual or not manual.get("is_failure"):
        return result
    reason = _manual_failure_reason(manual)
    _write_manual_failure_marker(episode_input, out_dir, manual)
    updated = dict(result)
    artifacts = updated.get("artifacts")
    artifacts = dict(artifacts) if isinstance(artifacts, dict) else {}
    artifacts["manual_failure"] = "manual_failure.json"
    qc_issues = str(updated.get("quality_issues") or "").strip()
    processing_error = str(updated.get("error") or "").strip()
    error_text = reason if not processing_error else f"{reason}；处理失败: {processing_error}"
    updated.update(
        {
            "accepted": False,
            "delete_status": "采集失败",
            "error": error_text,
            "manual_failure": True,
            "manual_failure_reason": reason,
            "quality_grade": "F",
            "quality_issues": reason if not qc_issues else f"{reason}；{qc_issues}",
            "artifacts": artifacts,
            "output": str(out_dir),
        }
    )
    return updated


def _run_batch(args, profile, input_root, out_root, episode_inputs):
    if str(profile.raw.get('adapter') or '').lower() != 'zerith_columnar':
        return _run_batch_after_prepare(args, profile, input_root, out_root, episode_inputs)
    from integrations.batch_collection import prepare_collection
    with prepare_collection(episode_inputs, profile, args, out_root):
        return _run_batch_after_prepare(args, profile, input_root, out_root, episode_inputs)


def _run_batch_after_prepare(
    args: argparse.Namespace,
    profile: Any,
    input_root: Path,
    out_root: Path,
    episode_inputs: list[Path],
) -> int:
    out_root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    manual_failures = _load_manual_failure_map(args.manual_failure_json)
    if int(args.num_workers) <= 1 or len(episode_inputs) <= 1:
        for idx, episode_input in enumerate(episode_inputs, start=1):
            out_dir = out_root / _episode_output_name(episode_input)
            print(
                f"[{idx}/{len(episode_inputs)}] {episode_input} -> {out_dir}",
                file=sys.stderr,
            )
            manual = _manual_failure_for_episode_input(episode_input, manual_failures)
            if manual and manual.get("is_failure"):
                reason = str(manual.get("reason_label") or "").strip() or "人工标注采集失败"
                print(
                    f"[{idx}/{len(episode_inputs)}] manual failure annotation, run QC anyway: {reason}",
                    file=sys.stderr,
                )
            try:
                result = _process_episode(
                    episode_input=episode_input,
                    out_dir=out_dir,
                    profile=profile,
                    args=args,
                )
                results.append(_apply_manual_failure_override(result, episode_input, out_dir, manual))
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                print(f"[{idx}/{len(episode_inputs)}] failed: {error}", file=sys.stderr)
                result = {
                    "ok": False,
                    "episode_id": _episode_output_name(episode_input),
                    "profile_id": profile.profile_id,
                    "input": str(episode_input),
                    "output": str(out_dir),
                    "error": error,
                }
                results.append(_apply_manual_failure_override(result, episode_input, out_dir, manual))
    else:
        max_workers = min(int(args.num_workers), len(episode_inputs))
        print(
            f"Batch QC workers: {max_workers}, episodes: {len(episode_inputs)}",
            file=sys.stderr,
        )
        worker_args = _worker_args_from_namespace(args)
        ordered_results: dict[int, dict[str, Any]] = {}
        futures = []
        with ProcessPoolExecutor(max_workers=max_workers) as pool:
            for idx, episode_input in enumerate(episode_inputs, start=1):
                out_dir = out_root / _episode_output_name(episode_input)
                manual = _manual_failure_for_episode_input(episode_input, manual_failures)
                if manual and manual.get("is_failure"):
                    reason = str(manual.get("reason_label") or "").strip() or "人工标注采集失败"
                    print(
                        f"[{idx}/{len(episode_inputs)}] manual failure annotation, submit QC anyway {episode_input}: {reason}",
                        file=sys.stderr,
                    )
                print(
                    f"[{idx}/{len(episode_inputs)}] submit {episode_input} -> {out_dir}",
                    file=sys.stderr,
                )
                futures.append(
                    pool.submit(
                        _process_episode_worker,
                        (idx, len(episode_inputs), str(episode_input), str(out_dir), worker_args),
                    )
                )
            for future in as_completed(futures):
                idx, result = future.result()
                result.pop("_batch_index", None)
                result.pop("_batch_total", None)
                ordered_results[idx] = result
                status = "ok" if result.get("ok") else f"failed: {result.get('error', '')}"
                print(f"[{idx}/{len(episode_inputs)}] {status}", file=sys.stderr)
        results = [
            ordered_results[idx]
            for idx in range(1, len(episode_inputs) + 1)
            if idx in ordered_results
        ]
        if len(results) != len(episode_inputs):
            completed = set(ordered_results)
            for idx, episode_input in enumerate(episode_inputs, start=1):
                if idx in completed:
                    continue
                out_dir = out_root / _episode_output_name(episode_input)
                results.append(
                    {
                        "ok": False,
                        "episode_id": _episode_output_name(episode_input),
                        "profile_id": profile.profile_id,
                        "input": str(episode_input),
                        "output": str(out_dir),
                        "error": "worker did not return a result",
                    }
                )

    summary = _batch_summary(profile, input_root, out_root, results)
    _write_json(out_root / "batch_summary.json", summary)
    _write_batch_summary_table(out_root / "batch_summary.md", summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["failed_to_process"] == 0 else 1


def _batch_summary(
    profile: Any,
    input_root: Path,
    out_root: Path,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    processed = [item for item in results if item.get("ok")]
    delete_items = [item for item in processed if item.get("delete_status") == "删除"]
    repair_items = [item for item in processed if item.get("delete_status") == "修复"]
    manual_failure_items = [
        item
        for item in processed
        if item.get("delete_status") == "采集失败" or item.get("manual_failure") is True
    ]
    delete_ids = [_summary_episode_id(item.get("episode_id", "")) for item in delete_items]
    repair_ids = [_summary_episode_id(item.get("episode_id", "")) for item in repair_items]
    manual_failure_ids = [
        _summary_episode_id(item.get("episode_id", "")) for item in manual_failure_items
    ]
    scores = [
        float(item["quality_score"])
        for item in processed
        if isinstance(item.get("quality_score"), (int, float))
    ]
    return {
        "ok": len(processed) == len(results),
        "profile_id": profile.profile_id,
        "input": str(input_root),
        "output": str(out_root),
        "episode_count": len(results),
        "processed": len(processed),
        "accepted": sum(1 for item in processed if item.get("accepted") is True),
        "rejected": sum(1 for item in processed if item.get("accepted") is False),
        "need_delete": len(delete_items),
        "need_delete_ids": delete_ids,
        "need_repair": len(repair_items),
        "need_repair_ids": repair_ids,
        "manual_failure": len(manual_failure_items),
        "manual_failure_ids": manual_failure_ids,
        "failed_to_process": len(results) - len(processed),
        "mean_quality_score": round(sum(scores) / len(scores), 2) if scores else None,
        "artifacts": {
            "batch_summary": "batch_summary.json",
            "batch_summary_table": "batch_summary.md",
        },
        "episodes": results,
    }


def _write_batch_summary_table(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "## 批量质检汇总",
        "",
        "| 信息项 | 内容 |",
        "|---|---|",
        f"| profile_id | {_escape_table_cell(str(summary.get('profile_id', '')))} |",
        f"| input | {_escape_table_cell(str(summary.get('input', '')))} |",
        f"| output | {_escape_table_cell(str(summary.get('output', '')))} |",
        f"| episode_count | {_escape_table_cell(str(summary.get('episode_count', '')))} |",
        f"| processed | {_escape_table_cell(str(summary.get('processed', '')))} |",
        f"| accepted | {_escape_table_cell(str(summary.get('accepted', '')))} |",
        f"| rejected | {_escape_table_cell(str(summary.get('rejected', '')))} |",
        f"| need_delete | {_escape_table_cell(_summary_count_with_ids(summary, 'need_delete', 'need_delete_ids'))} |",
        f"| need_repair | {_escape_table_cell(_summary_count_with_ids(summary, 'need_repair', 'need_repair_ids'))} |",
        f"| manual_failure | {_escape_table_cell(_summary_count_with_ids(summary, 'manual_failure', 'manual_failure_ids'))} |",
        f"| failed_to_process | {_escape_table_cell(str(summary.get('failed_to_process', '')))} |",
        f"| mean_quality_score | {_escape_table_cell(str(summary.get('mean_quality_score', '')))} |",
        "",
        "## Episode 明细",
        "",
        "| episode_id | 处理结果 | accepted | quality_score | fps | 是否删除 | output | error |",
        "|---|---|---|---|---|---|---|---|",
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
                    _bool_label(item.get("accepted")) if item.get("ok") else "",
                    str(item.get("quality_score", "")),
                    str(item.get("fps", "")),
                    str(item.get("delete_status", "")),
                    str(item.get("output", "")),
                    _batch_error_text(item),
                )
            )
            + " |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _summary_count_with_ids(summary: dict[str, Any], count_key: str, ids_key: str) -> str:
    ids = [_summary_episode_id(item) for item in summary.get(ids_key, []) if str(item)]
    if ids:
        return f"count={summary.get(count_key, 0)}; ids={','.join(ids)}"
    return f"count={summary.get(count_key, 0)}; ids="


def _summary_episode_id(value: Any) -> str:
    text = str(value)
    numbers = re.findall(r"\d+", text)
    if not numbers:
        return text
    return str(int(numbers[-1]))


def _delete_status_from_qc(qc_report: dict[str, Any]) -> str:
    return "成功"


def _quality_issue_summary(qc_report: dict[str, Any]) -> str:
    if qc_report.get("rules_version"):
        return str(qc_report.get("reason") or "")
    checks = [check for check in qc_report.get("checks", []) if isinstance(check, dict)]
    failed = [check for check in checks if str(check.get("status") or "") == "fail"]
    if failed:
        return "未通过: " + "；".join(_quality_issue_text(check) for check in failed)
    if qc_report.get("accepted") is False:
        warn = [check for check in checks if str(check.get("status") or "") == "warn"]
        score = qc_report.get("quality_score")
        threshold = qc_report.get("accept_score")
        parts = [f"质量分 {score}/100 低于阈值 {threshold}"]
        if warn:
            parts.append("警告项: " + "；".join(_quality_issue_text(check) for check in warn))
        return "；".join(parts)
    return ""


def _quality_issue_text(check: dict[str, Any]) -> str:
    if check.get("detail", {}).get("shared_text"):
        return check["detail"]["shared_text"]
    name = str(check.get("name") or "")
    detail = check.get("detail") if isinstance(check.get("detail"), dict) else {}
    label = _check_label(name)
    if name == "timestamp_monotonic":
        non_monotonic = int(detail.get("non_monotonic") or 0)
        max_gap = detail.get("max_gap_sec")
        limit = detail.get("max_allowed_gap_sec")
        start = detail.get("max_gap_start_frame")
        end = detail.get("max_gap_end_frame")
        try:
            relation = ">" if float(max_gap) > float(limit) else "<="
        except (TypeError, ValueError):
            relation = ">"
        gap_text = (
            f"{label}: 最大间隔 {max_gap}s {relation} "
            f"阈值 {limit}s，"
            f"发生帧 {start}-{end}"
        )
        if non_monotonic:
            return f"{label}: 重复或逆序时间戳 {non_monotonic} 处；" + gap_text.split(": ", 1)[-1]
        return gap_text
    if name == "fps":
        return (
            f"{label}: {detail.get('fps')}Hz < "
            f"阈值 {detail.get('min_fps')}Hz"
        )
    if name == "motion_stability":
        frame_range = _frame_range_from_detail(
            detail,
            "max_state_step_start_frame",
            "max_state_step_end_frame",
        )
        frame_text = f"，发生帧 {frame_range}" if frame_range else ""
        return (
            f"{label}: 最大 state 跳变 {detail.get('max_state_step')}，"
            f"阈值 {detail.get('max_allowed_state_step', '见配置')}"
            f"{frame_text}"
        )
    if name == "action_stationary_frames":
        return (
            f"{label}: 最长连续静止 {detail.get('max_stationary_run_frames', detail.get('max_stationary_run'))} 帧 > "
            f"阈值 {detail.get('max_allowed_stationary_run')} 帧，"
            f"发生帧 {detail.get('max_stationary_run_start_frame')}-"
            f"{detail.get('max_stationary_run_end_frame')}"
        )
    if name == "camera_completeness":
        summary = detail.get("_summary") if isinstance(detail.get("_summary"), dict) else {}
        expected = summary.get("expected_required_view_count")
        present = summary.get("present_required_view_count")
        if expected is not None:
            return f"{label}: 期望视角 {expected}，实际视角 {present}"
        return f"{label}: 相机视角不足"
    if name == "zerith_external_videos":
        issues = detail.get("issues") if isinstance(detail.get("issues"), list) else []
        parts = []
        for item in issues:
            if not isinstance(item, dict):
                continue
            reasons = item.get("issues") if isinstance(item.get("issues"), list) else []
            parts.append(f"{item.get('camera')}: {','.join(str(reason) for reason in reasons)}")
        return f"{label}: " + ("；".join(parts) or "缺失或不可解码")
    if name == "camera_repair_frames":
        warnings = detail.get("warnings") if isinstance(detail.get("warnings"), list) else []
        parts = [_camera_repair_warning_text(item, detail) for item in warnings if isinstance(item, dict)]
        return "；".join(part for part in parts if part) or f"{label}: 连续修复帧超过阈值"
    if name == "duration":
        reason = str(detail.get("reason") or "")
        if reason == "too_short":
            return (
                f"轨迹过短: {detail.get('duration_sec')}s < "
                f"最短 {detail.get('min_duration_sec')}s"
            )
        if reason == "too_long":
            return (
                f"轨迹过长: {detail.get('duration_sec')}s > "
                f"最长 {detail.get('max_duration_sec')}s"
            )
        return label
    if name == "state_dim":
        return f"{label}: 异常帧 {detail.get('bad_frames')}，期望维度 {detail.get('expected')}"
    if name == "action_dim":
        return f"{label}: 异常 action {detail.get('bad_actions')}，期望维度 {detail.get('expected')}"
    if name == "finite_values":
        return (
            f"{label}: 异常 state 帧 {detail.get('bad_state_frames')}，"
            f"异常 action {detail.get('bad_actions')}"
        )
    return label


def _frame_range_from_detail(detail: dict[str, Any], start_key: str, end_key: str) -> str:
    start = detail.get(start_key)
    end = detail.get(end_key)
    if start is None or end is None:
        return ""
    return f"{start}-{end}"


def _batch_error_text(item: dict[str, Any]) -> str:
    error = str(item.get("error") or "")
    if error:
        return error
    if item.get("ok") and item.get("accepted") is False:
        return str(item.get("quality_issues") or "质检不通过")
    return ""


def _manifest(
    episode: Any,
    profile: Any,
    qc_report: dict[str, Any],
    out_dir: Path,
    pyramid: dict[str, Any],
    *,
    artifacts: dict[str, str],
    artifact_mode: str,
) -> dict[str, Any]:
    trajectory = pyramid["trajectory"]
    return {
        "episode_id": episode.episode_id,
        "profile_id": profile.profile_id,
        "raw_episode_dir": str(episode.root),
        "raw_source_path": str(
            episode.meta.get("raw_hdf5_path")
            or episode.meta.get("raw_parquet_path")
            or episode.root
        ),
        "processed_dir": str(out_dir),
        "accepted": qc_report["accepted"],
        "quality_grade": qc_report.get("quality_grade", ""),
        "rules_version": qc_report.get("rules_version", ""),
        "review_required": qc_report.get("review_required", False),
        "quality_score": qc_report["quality_score"],
        "artifact_mode": artifact_mode,
        "artifacts": artifacts,
        "pyramid": {
            "schema_version": trajectory.schema_version,
            "trajectory_verbs": trajectory.task_taxonomy.verbs,
            "hands_used": trajectory.hands_used,
            "segment_count": len(pyramid["segments"]),
            "frame_count": len(pyramid["frames"]),
            "annotation_source": trajectory.annotation.get("source", "auto"),
        },
        "lerobot_mapping": {
            "state_feature": profile.raw["state"]["feature"],
            "action_feature": profile.raw["action"]["feature"],
            "cameras": {
                camera.raw_key: camera.lerobot_key
                for camera in profile.cameras
            },
        },
    }


def _write_outputs(
    *,
    out_dir: Path,
    episode: Any,
    profile: Any,
    qc_report: dict[str, Any],
    annotations: dict[str, Any],
    pyramid: dict[str, Any],
    artifact_mode: str,
    frame_sample: int,
) -> dict[str, str]:
    artifacts = {
        "qc_report": "qc_report.json",
        "final_report_table": "final_report_table.md",
        "annotations": "annotations.json",
    }
    _write_json(out_dir / "qc_report.json", qc_report)
    _write_final_report_table(
        out_dir / "final_report_table.md",
        episode=episode,
        profile=profile,
        qc_report=qc_report,
        annotations=annotations,
        pyramid=pyramid,
    )
    _write_json(out_dir / "annotations.json", annotations)

    if artifact_mode == "compact":
        return artifacts

    rtml_spec = build_rtml_spec(profile)
    rtml_report = build_rtml_report(episode, profile, qc_report, annotations)
    _write_json(out_dir / "profile_snapshot.json", profile.raw)
    _write_json(out_dir / "rtml_spec.json", rtml_spec)
    _write_json(out_dir / "rtml_report.json", rtml_report)
    pyramid_artifacts = write_pyramid(out_dir, pyramid)
    _write_frame_annotations(out_dir / "frame_annotations.jsonl", episode, profile, frame_sample)
    return {
        "profile_snapshot": "profile_snapshot.json",
        "rtml_spec": "rtml_spec.json",
        **artifacts,
        "frame_annotations": "frame_annotations.jsonl",
        "rtml_report": "rtml_report.json",
        **pyramid_artifacts,
    }


def _legacy_annotations(episode: Any, pyramid: dict[str, Any]) -> dict[str, Any]:
    trajectory_dict = to_jsonable(pyramid["trajectory"])
    return {
        "trajectory": {
            "episode_id": trajectory_dict["episode_id"],
            "profile_id": trajectory_dict["profile_id"],
            "robot": trajectory_dict["robot"],
            "instruction": trajectory_dict.get("instruction", ""),
            "target_objects": [obj["label"] for obj in trajectory_dict.get("objects", [])],
            "scene_description": trajectory_dict.get("scene_description", "not_generated"),
            "annotation_source": trajectory_dict.get("annotation", {}).get("source", "auto"),
        },
        "segments": [to_jsonable(seg) for seg in pyramid["segments"]],
        "frame_annotation_file": "frame_annotations.jsonl",
        "frame_annotation_count": episode.n_frames,
    }


def _write_qc_table(
    path: Path,
    qc_report: dict[str, Any],
    annotations: dict[str, Any] | None = None,
) -> None:
    lines: list[str] = []
    if annotations:
        _append_annotation_tables(lines, annotations)
    _append_quality_check_table(lines, qc_report)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_final_report_table(
    path: Path,
    *,
    episode: Any,
    profile: Any,
    qc_report: dict[str, Any],
    annotations: dict[str, Any],
    pyramid: dict[str, Any],
) -> None:
    trajectory = to_jsonable(pyramid["trajectory"])
    annotation = trajectory.get("annotation", {})
    summary = qc_report.get("summary", {})
    failed_checks = [
        _check_label(str(check.get("name") or ""))
        for check in qc_report.get("checks", [])
        if isinstance(check, dict) and str(check.get("status") or "") == "fail"
    ]
    raw_source_path = (
        episode.meta.get("raw_hdf5_path")
        or episode.meta.get("raw_parquet_path")
        or episode.root
    )

    lines = [
        "## 最终结论",
        "",
        "| 信息项 | 内容 |",
        "|---|---|",
        f"| accepted | {_escape_table_cell(_bool_label(qc_report.get('accepted')))} |",
        f"| quality_score | {_escape_table_cell(str(qc_report.get('quality_score')))}/100 |",
        f"| 未通过检查项 | {_escape_table_cell(', '.join(failed_checks) if failed_checks else '无')} |",
        f"| episode_id | {_escape_table_cell(str(trajectory.get('episode_id', episode.episode_id)))} |",
        f"| profile_id | {_escape_table_cell(str(trajectory.get('profile_id', profile.profile_id)))} |",
        f"| raw_source_path | {_escape_table_cell(str(raw_source_path))} |",
        "",
        "## 轨迹信息",
        "",
        "| 信息项 | 内容 |",
        "|---|---|",
        f"| robot | {_escape_table_cell(str(trajectory.get('robot', '')))} |",
        f"| instruction | {_escape_table_cell(str(trajectory.get('instruction', '')))} |",
        f"| scene_description | {_escape_table_cell(str(trajectory.get('scene_description', '')))} |",
        f"| target_objects | {_escape_table_cell(_join_labels(trajectory.get('objects', [])))} |",
        f"| fps | {_escape_table_cell(str(trajectory.get('fps', '')))} |",
        f"| frame_count | {_escape_table_cell(str(trajectory.get('n_frames', summary.get('frames', ''))))} |",
        f"| duration_sec | {_escape_table_cell(str(trajectory.get('duration_sec', summary.get('duration_sec', ''))))} |",
        "",
        "## 视觉与目标识别",
        "",
        "| 信息项 | 内容 |",
        "|---|---|",
        f"| vision_pipeline | {_escape_table_cell(str(annotation.get('vision_pipeline', '')))} |",
        f"| vlm_model | {_escape_table_cell(str(annotation.get('vlm_model', '')))} |",
        "",
        "| target_object | bbox_xyxy_norm |",
        "|---|---|",
    ]
    for obj in trajectory.get("objects", []):
        if not isinstance(obj, dict):
            continue
        lines.append(
            "| "
            + _escape_table_cell(str(obj.get("label", "")))
            + " | "
            + _escape_table_cell(_format_bbox(obj.get("bbox")))
            + " |"
        )

    _append_hand_target_table(lines, annotation)
    _append_final_subtask_table(lines, annotations)
    _append_data_overview_table(lines, qc_report, annotations)
    _append_quality_check_table(
        lines,
        qc_report,
        include_summary=False,
        compact_details=True,
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _append_hand_target_table(lines: list[str], annotation: dict[str, Any]) -> None:
    assignments = annotation.get("hand_target_assignments", {})
    vlm = annotation.get("hand_target_assignment_vlm", {})
    if not isinstance(assignments, dict) and not isinstance(vlm, dict):
        return
    hands = sorted(
        {
            *(assignments.keys() if isinstance(assignments, dict) else []),
            *(vlm.keys() if isinstance(vlm, dict) else []),
        }
    )
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
        info = vlm.get(hand, {}) if isinstance(vlm, dict) else {}
        if not isinstance(info, dict):
            info = {}
        assigned = assignments.get(hand, "") if isinstance(assignments, dict) else ""
        lines.append(
            "| "
            + _escape_table_cell(str(hand))
            + " | "
            + _escape_table_cell(str(assigned))
            + " | "
            + _escape_table_cell(str(info.get("target_object", "")))
            + " | "
            + _escape_table_cell(str(info.get("confidence", "")))
            + " | "
            + _escape_table_cell(str(info.get("sample_frame_idx", "")))
            + " | "
            + _escape_table_cell(str(info.get("grasp_start_frame", "")))
            + " |"
        )


def _append_final_subtask_table(lines: list[str], annotations: dict[str, Any]) -> None:
    segments = annotations.get("segments")
    if not isinstance(segments, list):
        return
    lines.extend(
        [
            "",
            "## Subtask 明细",
            "",
            "| id | subtask | 帧范围 | 时间范围(s) | confidence |",
            "|---|---|---|---|---|",
        ]
    )
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        start_frame = segment.get("start_frame", "")
        end_frame = segment.get("end_frame", "")
        start_time = segment.get("start_time", "")
        end_time = segment.get("end_time", "")
        row = [
            str(segment.get("segment_id", "")),
            str(segment.get("subtask", "")),
            f"{start_frame}-{end_frame}",
            _format_time_range(start_time, end_time),
            str(segment.get("confidence", "")),
        ]
        lines.append("| " + " | ".join(_escape_table_cell(item) for item in row) + " |")


def _append_data_overview_table(
    lines: list[str],
    qc_report: dict[str, Any],
    annotations: dict[str, Any],
) -> None:
    summary = qc_report.get("summary", {})
    camera_counts = summary.get("camera_counts", {}) if isinstance(summary, dict) else {}
    lines.extend(
        [
            "",
            "## 数据概览",
            "",
            "| 信息项 | 内容 |",
            "|---|---|",
            f"| actions | {_escape_table_cell(str(summary.get('actions', '')))} |",
            f"| frame_annotation_file | {_escape_table_cell(str(annotations.get('frame_annotation_file', '')))} |",
            f"| camera_counts | {_escape_table_cell(_format_camera_counts(camera_counts))} |",
        ]
    )


def _bool_label(value: Any) -> str:
    if value is True:
        return "通过"
    if value is False:
        return "不通过"
    return str(value)


def _join_labels(objects: Any) -> str:
    if not isinstance(objects, list):
        return str(objects)
    labels = []
    for obj in objects:
        if isinstance(obj, dict):
            label = obj.get("label")
            if label:
                labels.append(str(label))
        elif obj:
            labels.append(str(obj))
    return ", ".join(labels)


def _join_values(values: Any) -> str:
    if isinstance(values, list):
        return ", ".join(str(value) for value in values)
    return str(values)


def _format_bbox(value: Any) -> str:
    if not isinstance(value, list):
        return str(value)
    return "[" + ", ".join(_format_number(item) for item in value) + "]"


def _format_time_range(start_time: Any, end_time: Any) -> str:
    return f"{_format_number(start_time)}-{_format_number(end_time)}"


def _segment_duration(start_time: Any, end_time: Any) -> str:
    try:
        return _format_number(float(end_time) - float(start_time))
    except (TypeError, ValueError):
        return ""


def _format_quality_flags(value: Any) -> str:
    if not isinstance(value, dict):
        return str(value)
    return "；".join(f"{key}={_bool_label(flag)}" for key, flag in value.items())


def _format_camera_counts(value: Any) -> str:
    if not isinstance(value, dict):
        return str(value)
    return "；".join(f"{camera}: {count}" for camera, count in value.items())


def _format_number(value: Any) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{float(value):.6g}"
    return str(value)


def _append_annotation_tables(lines: list[str], annotations: dict[str, Any]) -> None:
    trajectory = annotations.get("trajectory")
    if isinstance(trajectory, dict):
        target_objects = trajectory.get("target_objects", [])
        if isinstance(target_objects, list):
            target_objects_text = ", ".join(str(obj) for obj in target_objects)
        else:
            target_objects_text = str(target_objects)
        lines.extend(
            [
                "## 轨迹信息",
                "",
                "| 信息项 | 内容 |",
                "|---|---|",
                f"| episode_id | {_escape_table_cell(str(trajectory.get('episode_id', '')))} |",
                f"| profile_id | {_escape_table_cell(str(trajectory.get('profile_id', '')))} |",
                f"| instruction | {_escape_table_cell(str(trajectory.get('instruction', '')))} |",
                f"| target_objects | {_escape_table_cell(target_objects_text)} |",
                f"| annotation_source | {_escape_table_cell(str(trajectory.get('annotation_source', '')))} |",
                f"| frame_annotation_file | {_escape_table_cell(str(annotations.get('frame_annotation_file', '')))} |",
                f"| frame_annotation_count | {_escape_table_cell(str(annotations.get('frame_annotation_count', '')))} |",
            ]
        )

    segments = annotations.get("segments")
    if not isinstance(segments, list):
        return
    if lines:
        lines.append("")
    lines.extend(
        [
            "## Subtask 信息",
            "",
            "| subtask | 帧范围 | confidence |",
            "|---|---|---|",
        ]
    )
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        subtask = str(segment.get("subtask", ""))
        start_frame = segment.get("start_frame", "")
        end_frame = segment.get("end_frame", "")
        confidence = segment.get("confidence", "")
        frame_range = f"{start_frame}-{end_frame}"
        lines.append(
            "| "
            + _escape_table_cell(subtask)
            + " | "
            + _escape_table_cell(frame_range)
            + " | "
            + _escape_table_cell(str(confidence))
            + " |"
        )


def _append_quality_check_table(
    lines: list[str],
    qc_report: dict[str, Any],
    *,
    include_summary: bool = True,
    compact_details: bool = False,
) -> None:
    if lines:
        lines.append("")
    lines.extend(["## 数据质量检查", ""])
    if include_summary:
        lines.extend(
            [
                "| 指标 | 值 |",
                "|---|---|",
                f"| accepted | {_escape_table_cell(str(qc_report.get('accepted')))} |",
                f"| quality_score | {_escape_table_cell(str(qc_report.get('quality_score')))}/100 |",
                "",
            ]
        )
    lines.extend(["| 检查项 | 是否通过 | 相关细节 |", "|---|---|---|"])
    for check in qc_report.get("checks", []):
        if not isinstance(check, dict):
            continue
        name = str(check.get("name") or "")
        status = str(check.get("status") or "")
        detail = check.get("detail") if isinstance(check.get("detail"), dict) else {}
        lines.append(
            "| "
            + _escape_table_cell(_check_label(name))
            + " | "
            + _escape_table_cell(_status_label(status))
            + " | "
            + _escape_table_cell(_check_detail(name, detail, compact=compact_details))
            + " |"
        )


def _check_label(name: str) -> str:
    labels = {
        "state_dim": "state 维度",
        "action_dim": "action 维度",
        "finite_values": "有限数值",
        "zerith_hdf5_schema": "零次方 HDF5 Schema",
        "zerith_external_videos": "零次方外部 MP4",
        "timestamp_monotonic": "时间戳连续性",
        "fps": "FPS",
        "camera_completeness": "相机视角",
        "camera_repair_frames": "相机修复帧",
        "duration": "轨迹时长",
        "motion_stability": "运动稳定性",
        "action_stationary_frames": "action 静止帧",
        "gripper_activity": "夹爪活动",
    }
    return labels.get(name, name)


def _status_label(status: str) -> str:
    return {
        "pass": "通过",
        "warn": "警告",
        "fail": "未通过",
    }.get(status, status)


def _check_detail(
    name: str,
    detail: dict[str, Any],
    *,
    compact: bool = False,
) -> str:
    if detail.get("shared_text"):
        return str(detail["shared_text"])
    if name == "state_dim":
        if compact:
            return (
                f"期望维度 {detail.get('expected')}；"
                f"异常帧 {detail.get('bad_frames')}"
            )
        return (
            f"期望维度 {detail.get('expected')}；总帧数 {detail.get('frames')}；"
            f"异常帧 {detail.get('bad_frames')}"
        )
    if name == "action_dim":
        if compact:
            return (
                f"期望维度 {detail.get('expected')}；"
                f"异常 action {detail.get('bad_actions')}"
            )
        return (
            f"期望维度 {detail.get('expected')}；action 数 {detail.get('actions')}；"
            f"异常 action {detail.get('bad_actions')}"
        )
    if name == "finite_values":
        return (
            f"异常 state 帧 {detail.get('bad_state_frames')}；"
            f"异常 action {detail.get('bad_actions')}"
        )
    if name == "timestamp_monotonic":
        return (
            f"重复或逆序时间戳 {detail.get('non_monotonic')} 处；"
            f"平均间隔 {detail.get('mean_dt_sec')}s；"
            f"估计频率 {detail.get('estimated_hz')}Hz；最大间隔 {detail.get('max_gap_sec')}s；"
            f"最大间隔帧 {detail.get('max_gap_start_frame')}-{detail.get('max_gap_end_frame')}；"
            f"阈值 {detail.get('max_allowed_gap_sec')}s"
        )
    if name == "fps":
        return (
            f"fps {detail.get('fps')}Hz；"
            f"最低要求 {detail.get('min_fps')}Hz；"
            f"帧数 {detail.get('frames')}；"
            f"时长 {detail.get('duration_sec')}s"
        )
    if name == "camera_completeness":
        summary = detail.get("_summary") if isinstance(detail.get("_summary"), dict) else {}
        expected = summary.get("expected_required_view_count")
        present = summary.get("present_required_view_count")
        parts = []
        if expected is not None or present is not None:
            parts.append(f"期望视角 {expected}；实际视角 {present}")
        for camera, info in detail.items():
            if str(camera).startswith("_"):
                continue
            if not isinstance(info, dict):
                continue
            if compact:
                parts.append(f"{camera}: {info.get('count')} 帧，缺失 {info.get('missing')}")
                continue
            parts.append(f"{camera}: {info.get('count')} 帧，缺失 {info.get('missing')}")
        return "；".join(parts)
    if name == "zerith_external_videos":
        cameras = detail.get("cameras") if isinstance(detail.get("cameras"), dict) else {}
        parts = []
        for camera, info in cameras.items():
            if not isinstance(info, dict):
                continue
            issues = info.get("issues") if isinstance(info.get("issues"), list) else []
            suffix = f"，问题 {','.join(str(item) for item in issues)}" if issues else ""
            parts.append(
                f"{camera}: 解码 {info.get('decoded_frame_count')}/{info.get('expected_frames')} 帧{suffix}"
            )
        return "；".join(parts)
    if name == "camera_repair_frames":
        threshold = detail.get("max_allowed_consecutive_repair_frames")
        warnings = detail.get("warnings") if isinstance(detail.get("warnings"), list) else []
        if warnings:
            return "；".join(
                _camera_repair_warning_text(item, detail)
                for item in warnings
                if isinstance(item, dict)
            )
        cameras = detail.get("cameras") if isinstance(detail.get("cameras"), list) else []
        if not cameras:
            return f"无相机修复统计；阈值 > {threshold} 帧"
        return "；".join(
            f"{item.get('camera')}: 最长连续修复/复用 {item.get('max_consecutive_repair_or_reuse_frames')} 帧"
            for item in cameras
            if isinstance(item, dict)
        ) + f"；阈值 > {threshold} 帧"
    if name == "duration":
        duration = detail.get("duration_sec")
        min_duration = detail.get("min_duration_sec")
        max_duration = detail.get("max_duration_sec")
        reason = str(detail.get("reason") or "")
        if max_duration is None:
            return f"时长 {duration}s；最低要求 {min_duration}s"
        reason_text = {
            "too_short": "；轨迹过短",
            "too_long": "；轨迹过长",
        }.get(reason, "")
        return (
            f"时长 {duration}s；允许范围 {min_duration}-{max_duration}s"
            f"{reason_text}"
        )
    if name == "motion_stability":
        parts = [f"最大 state 跳变 {detail.get('max_state_step')}"]
        state_frame_range = _frame_range_from_detail(
            detail,
            "max_state_step_start_frame",
            "max_state_step_end_frame",
        )
        if state_frame_range:
            parts.append(f"最大 state 跳变帧 {state_frame_range}")
        parts.append(f"state 阈值 {detail.get('max_allowed_state_step')}")
        parts.append(f"最大 joint 跳变 {detail.get('max_joint_step')}")
        joint_frame_range = _frame_range_from_detail(
            detail,
            "max_joint_step_start_frame",
            "max_joint_step_end_frame",
        )
        if joint_frame_range:
            parts.append(f"最大 joint 跳变帧 {joint_frame_range}")
        parts.extend(
            [
                f"joint 阈值 {detail.get('max_allowed_joint_step')}",
                f"平均 joint 变化 {detail.get('mean_joint_delta')}",
                f"平均 joint 最小阈值 {detail.get('min_joint_motion_mean')}",
            ]
        )
        return "；".join(parts)
    if name == "action_stationary_frames":
        return (
            f"静止帧 {detail.get('stationary_frames')}；"
            f"静止帧对 {detail.get('stationary_pairs', '')}；"
            f"action 不变帧 {detail.get('unchanged_action_frames')}；"
            f"最长连续静止帧数 {detail.get('max_stationary_run_frames', detail.get('max_stationary_run'))}；"
            f"最长连续范围 {detail.get('max_stationary_run_start_frame')}-"
            f"{detail.get('max_stationary_run_end_frame')}；"
            f"超过阈值静止段 {detail.get('over_threshold_stationary_runs', '')}；"
            f"通过阈值最长连续 <= {detail.get('max_allowed_stationary_run')} 帧；"
            f"action 近似不变阈值 {detail.get('epsilon')}；"
            f"state 静止阈值 {detail.get('state_stationary_epsilon')}；"
            f"底盘速度静止阈值 {detail.get('base_velocity_epsilon')}；"
            f"最大底盘速度 {detail.get('max_base_velocity_abs')}；"
            f"比较帧对 {detail.get('compared_action_pairs')}；"
            f"静止判定来源 {detail.get('stationary_source', '')}；"
            f"静止 state/action 维度 {detail.get('stationary_state_dim', '')}/"
            f"{detail.get('stationary_action_dim', '')}"
        )
    if name == "gripper_activity":
        parts = []
        for side, info in detail.items():
            if not isinstance(info, dict):
                continue
            parts.append(
                f"{side}: 统计源 {info.get('source', 'state')}，"
                f"夹取次数 {info.get('grasp_events', info.get('close_events'))}，"
                f"开闭切换次数 {info.get('transitions')}，"
                f"闭合次数 {info.get('close_events')}，"
                f"张开次数 {info.get('open_events')}，"
                f"初始/最终 {info.get('initial_state')}/{info.get('final_state')}，"
                f"逐帧差分次数 {info.get('delta_transitions')}，"
                f"闭合值 {info.get('closed_value')}，张开值 {info.get('open_value')}，"
                f"阈值 {info.get('threshold')}"
            )
        return "；".join(parts)
    return json.dumps(detail, ensure_ascii=False, separators=(",", ":"))


def _camera_repair_warning_text(item: dict[str, Any], parent_detail: dict[str, Any]) -> str:
    camera = item.get("camera") or item.get("camera_color_name") or "unknown"
    threshold = parent_detail.get("max_allowed_consecutive_repair_frames")
    repair_run = item.get("max_consecutive_repair_or_reuse_frames")
    repair_start = item.get("max_consecutive_repair_or_reuse_start")
    repair_end = item.get("max_consecutive_repair_or_reuse_end")
    return (
        f"相机修复帧: {camera} 连续修复/复用 {repair_run} 帧 > "
        f"阈值 {threshold} 帧，帧 {repair_start}-{repair_end}"
    )


def _escape_table_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


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
