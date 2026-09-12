#!/usr/bin/env python3
# -- coding: UTF-8
"""
Convert MCAP data into an ICRA-WBC-like episode directory.

Output layout:
    episode_00000/
      states/aligned_joints.h5
      videos/head_color.mp4
      videos/hand_left_color.mp4
      videos/hand_right_color.mp4
      meta/episode_meta.json

Only videos that exist in the source MCAP are generated. The current AgileX
MCAP contains three color cameras, so depth videos are omitted.
"""

import argparse
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore

from mcap_to_hdf5 import collect_mcap_files, infer_episode_name, resolve_topic_yaml
from camera_layouts import CAMERA_LAYOUTS, CameraLayout, THREE_CAMERA, get_camera_layout


SCRIPT_DIR = Path(__file__).resolve().parent
DATA_TOOLS_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = SCRIPT_DIR.parents[1]
DEFAULT_ALOHA_YAML = DATA_TOOLS_DIR / "topic_configs" / "aloha_data_params.yaml"
DEFAULT_PROFILE = PROJECT_ROOT / "robot_profiles" / "aloha.yaml"

QUALITY_GRADES = {"A", "B", "C", "F"}
REASON_CODE_LABELS_ZH = {
    "minor_collision": "轻微碰撞",
    "unsmooth_motion": "轨迹不平滑",
    "retry_success": "重试后成功",
    "minor_visual_issue": "轻微视觉异常",
    "grasp_failure": "抓取失败",
    "object_dropped": "物体掉落",
    "wrong_placement": "放置错误",
    "wrong_target": "目标错误",
    "object_knocked_over": "物体碰倒",
    "task_abandoned": "任务中止",
    "others": "其他",
}
REASON_CODE_LABELS_EN = {
    "minor_collision": "minor collision",
    "unsmooth_motion": "unsmooth motion",
    "retry_success": "retry success",
    "minor_visual_issue": "minor visual issue",
    "grasp_failure": "grasp failure",
    "object_dropped": "object dropped",
    "wrong_placement": "wrong placement",
    "wrong_target": "wrong target",
    "object_knocked_over": "object knocked over",
    "task_abandoned": "task abandoned",
    "others": "other",
}

VIDEO_SOURCES = THREE_CAMERA.video_sources
REFERENCE_VIDEO_NAMES = list(THREE_CAMERA.reference_video_names)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Convert MCAP data into an episode directory like the ICRA-WBC dataset."
    )
    parser.add_argument(
        "--mcapPath",
        required=True,
        help="Input .mcap file or directory containing .mcap files.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output episode directory, for example /path/to/data/episode_00000.",
    )
    parser.add_argument(
        "--episodeName",
        default="",
        help="Episode name used for intermediate conversion. Default: input directory name.",
    )
    parser.add_argument(
        "--workDir",
        default="",
        help="Intermediate work directory. Default: <output>/_work.",
    )
    parser.add_argument(
        "--type",
        default="aloha",
        help="Dataset config type passed to data_sync.py and data_to_hdf5.py. Default: aloha.",
    )
    parser.add_argument(
        "--robot",
        choices=["auto", "aloha", "g2"],
        default="auto",
        help="Source robot layout for aligned_joints conversion. Default: auto.",
    )
    parser.add_argument(
        "--alohaYaml",
        default=str(DEFAULT_ALOHA_YAML),
        help="YAML topic mapping passed to mcap_to_hdf5.py.",
    )
    parser.add_argument(
        "--profile",
        default=str(DEFAULT_PROFILE),
        help="Robot profile YAML used for default task text. Default: robot_profiles/aloha.yaml.",
    )
    parser.add_argument(
        "--cameraLayout",
        choices=tuple(CAMERA_LAYOUTS),
        default="three_camera",
        help="Camera/video layout. Default: three_camera.",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable used to run conversion scripts. Default: current Python.",
    )
    parser.add_argument(
        "--timeDiffLimit",
        type=float,
        default=0.03,
        help="Timestamp sync tolerance passed to data_sync.py. Default: 0.03.",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=0.0,
        help="Output video FPS. Default: infer FPS from aligned_joints.h5 timestamps. Set a positive value to override.",
    )
    parser.add_argument(
        "--taskId",
        type=int,
        default=0,
        help="Optional task_id written to meta/episode_meta.json.",
    )
    parser.add_argument(
        "--jobId",
        type=int,
        default=0,
        help="Optional job_id written to meta/episode_meta.json.",
    )
    parser.add_argument(
        "--aid",
        default="",
        help="Optional AID written to meta/episode_meta.json.",
    )
    parser.add_argument(
        "--episodeId",
        type=int,
        default=0,
        help="Optional episode_id written to meta/episode_meta.json.",
    )
    parser.add_argument(
        "--text",
        default="",
        help="Optional text field written to meta/episode_meta.json.",
    )
    parser.add_argument(
        "--textZh",
        default="",
        help="Optional text_zh field written to meta/episode_meta.json.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite managed outputs if they already exist.",
    )
    parser.add_argument(
        "--cleanupIntermediate",
        action="store_true",
        help="Remove <output>/_work after successful conversion.",
    )
    parser.add_argument(
        "--allowPartialRecover",
        action="store_true",
        help=(
            "Allow conversion from a partially recovered MCAP if rosbag2_py cannot read the original. "
            "Default refuses partial recovery."
        ),
    )
    parser.add_argument(
        "--recordTimeTopic",
        dest="record_time_topics",
        action="append",
        default=[],
        help="Use MCAP record time for this exact topic; repeatable.",
    )
    parser.add_argument(
        "--gpu-encode-videos",
        action="store_true",
        help="Use ffmpeg NVENC for output MP4 transcode, with CPU H.264 fallback.",
    )
    parser.add_argument(
        "--gpu-device",
        default="0",
        help="GPU index used by NVENC when --gpu-encode-videos is set. Default: 0.",
    )
    parser.add_argument(
        "--gpu-video-encoder",
        choices=("h264_nvenc", "hevc_nvenc", "av1_nvenc"),
        default="h264_nvenc",
        help="ffmpeg NVENC encoder used with --gpu-encode-videos. Default: h264_nvenc.",
    )
    parser.add_argument(
        "--dryRun",
        action="store_true",
        help="Print paths and commands without running conversion.",
    )
    return parser.parse_args(argv)


def resolve_profile_path(profile_path):
    requested = Path(profile_path).expanduser()
    candidates = [requested]
    if not requested.is_absolute():
        candidates.append((PROJECT_ROOT / requested).resolve())
    if DEFAULT_PROFILE not in candidates:
        candidates.append(DEFAULT_PROFILE)

    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return requested


def read_profile_default_task(profile_path):
    profile_path = resolve_profile_path(profile_path)
    if not profile_path.exists():
        raise FileNotFoundError(
            f"Robot profile not found: {profile_path}. "
            "Mount robot_profiles/ into the conversion container or pass --profile."
        )
    if yaml is None:
        return read_simple_yaml_default_task(profile_path)
    with profile_path.open("r", encoding="utf-8") as file_obj:
        raw = yaml.safe_load(file_obj) or {}
    tasks = raw.get("tasks")
    if isinstance(tasks, dict):
        for key in ("default", "default_task", "instruction"):
            value = tasks.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    value = raw.get("default_task")
    return value.strip() if isinstance(value, str) else ""


def resolve_task_text(args, required=True):
    task_text = args.text.strip()
    if task_text:
        return task_text

    task_text = read_profile_default_task(args.profile)
    if not task_text and required:
        raise ValueError(
            f"No task text found. Set --text or add tasks.default to {resolve_profile_path(args.profile)}."
        )
    return task_text


def resolve_robot_kind(args):
    if args.robot != "auto":
        return args.robot
    if args.type.lower() == "g2":
        return "g2"

    profile_path = resolve_profile_path(args.profile)
    if yaml is not None and profile_path.exists():
        with profile_path.open("r", encoding="utf-8") as file_obj:
            raw = yaml.safe_load(file_obj) or {}
        marker = f"{raw.get('adapter', '')} {raw.get('profile_id', '')}".lower()
        if "g2" in marker:
            return "g2"
    return "aloha"


def read_simple_yaml_default_task(profile_path):
    in_tasks = False
    for raw_line in Path(profile_path).read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        stripped = raw_line.strip()
        if indent == 0:
            in_tasks = stripped == "tasks:"
            if stripped.startswith("default_task:"):
                return stripped.split(":", 1)[1].strip().strip("\"'")
            continue
        if in_tasks and stripped.startswith(("default:", "default_task:", "instruction:")):
            return stripped.split(":", 1)[1].strip().strip("\"'")
    return ""


def run_command(cmd, dry_run=False):
    print("+ " + " ".join(str(part) for part in cmd))
    if dry_run:
        return
    subprocess.run(cmd, cwd=str(SCRIPT_DIR), check=True)


def remove_path(path):
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def drop_adjacent_equal_timestamp_frames(aligned_h5_path, dry_run=False):
    import h5py

    aligned_h5_path = Path(aligned_h5_path)
    with h5py.File(aligned_h5_path, "r") as src:
        frame_keys = sorted((key for key in src.keys() if key.isdigit()), key=lambda key: int(key))
        timestamps = [int(src[key]["main_timestamp"][()]) for key in frame_keys]

    if not frame_keys:
        return {
            "enabled": True,
            "original_frame_count": 0,
            "removed_frame_count": 0,
            "kept_frame_count": 0,
            "kept_source_positions": [],
            "removed_source_frames": [],
        }

    keep_positions = [0]
    removed_frames = []
    for pos in range(1, len(frame_keys)):
        if timestamps[pos] == timestamps[pos - 1]:
            removed_frames.append(int(frame_keys[pos]))
        else:
            keep_positions.append(pos)

    info = {
        "enabled": True,
        "original_frame_count": len(frame_keys),
        "removed_frame_count": len(removed_frames),
        "kept_frame_count": len(keep_positions),
        "kept_source_positions": keep_positions,
        "removed_source_frames": removed_frames,
    }
    if not removed_frames:
        return info

    print(
        "Drop adjacent equal-timestamp frames: "
        f"{len(removed_frames)}/{len(frame_keys)} removed from {aligned_h5_path}"
    )
    if dry_run:
        return info

    tmp_path = aligned_h5_path.with_name(f"{aligned_h5_path.name}.dedupe_tmp")
    remove_path(tmp_path)
    try:
        with h5py.File(aligned_h5_path, "r") as src, h5py.File(tmp_path, "w") as dst:
            for attr_key, attr_value in src.attrs.items():
                dst.attrs[attr_key] = attr_value
            dst.attrs["adjacent_equal_timestamp_deduped"] = True
            dst.attrs["adjacent_equal_timestamp_original_frame_count"] = len(frame_keys)
            dst.attrs["adjacent_equal_timestamp_removed_frame_count"] = len(removed_frames)

            numeric_keys = set(frame_keys)
            for key in src.keys():
                if key not in numeric_keys:
                    src.copy(key, dst, name=key)
            for new_idx, source_pos in enumerate(keep_positions):
                src.copy(frame_keys[source_pos], dst, name=str(new_idx))
        tmp_path.replace(aligned_h5_path)
    except Exception:
        remove_path(tmp_path)
        raise
    return info


def prepare_output_dirs(episode_dir, work_dir, overwrite, dry_run=False):
    states_dir = episode_dir / "states"
    videos_dir = episode_dir / "videos"
    meta_dir = episode_dir / "meta"
    managed_paths = [
        states_dir / "aligned_joints.h5",
        videos_dir,
        meta_dir,
        work_dir,
    ]
    existing = [path for path in managed_paths if path.exists() or path.is_symlink()]
    if existing and not overwrite:
        joined = "\n  ".join(str(path) for path in existing)
        raise FileExistsError(f"Managed outputs already exist, use --overwrite:\n  {joined}")

    if dry_run:
        return states_dir, videos_dir, meta_dir

    if overwrite:
        for path in existing:
            remove_path(path)

    states_dir.mkdir(parents=True, exist_ok=True)
    videos_dir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)
    return states_dir, videos_dir, meta_dir


def read_hdf5_info(aligned_h5_path):
    import h5py

    with h5py.File(aligned_h5_path, "r") as file_obj:
        frame_indices = sorted(int(key) for key in file_obj.keys() if key.isdigit())
        if not frame_indices:
            raise ValueError(f"No frame groups found in {aligned_h5_path}")

        first_ts = int(file_obj[str(frame_indices[0])]["main_timestamp"][()])
        last_ts = int(file_obj[str(frame_indices[-1])]["main_timestamp"][()])

    duration = max(0.0, (last_ts - first_ts) / 1_000_000_000.0)
    frame_count = len(frame_indices)
    inferred_fps = 0.0
    if frame_count > 1 and duration > 0.0:
        inferred_fps = (frame_count - 1) / duration

    return {
        "frame_count": frame_count,
        "first_timestamp_ns": first_ts,
        "last_timestamp_ns": last_ts,
        "duration": duration,
        "inferred_fps": inferred_fps,
    }


def read_hdf5_frame_timestamps(aligned_h5_path):
    import h5py

    with h5py.File(aligned_h5_path, "r") as file_obj:
        frame_indices = sorted(int(key) for key in file_obj.keys() if key.isdigit())
        return [
            (frame_idx, int(file_obj[str(frame_idx)]["main_timestamp"][()]))
            for frame_idx in frame_indices
        ]


def text_list(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def joined_text(value):
    return " ".join(text_list(value)).strip()


def normalise_quality_grade(value):
    text = str(value or "").strip().upper()
    return text if text in QUALITY_GRADES else ""


def reason_code_list(value):
    out = []
    raw_items = value if isinstance(value, list) else [value]
    for raw_item in raw_items:
        if raw_item is None:
            continue
        for item in str(raw_item).replace("；", ",").replace(";", ",").split(","):
            text = item.strip()
            if text and text not in out:
                out.append(text)
    return out


def labels_from_reason_codes(reason_codes, language="zh"):
    mapping = REASON_CODE_LABELS_ZH if language == "zh" else REASON_CODE_LABELS_EN
    return [mapping.get(str(code), str(code)) for code in reason_codes if str(code)]


def collection_quality_from_info(payload):
    if not isinstance(payload, dict):
        return {}
    raw_quality = payload.get("collection_quality")
    if not isinstance(raw_quality, dict):
        raw_quality = {}

    grade = normalise_quality_grade(raw_quality.get("grade") or payload.get("grade"))
    reason_codes = reason_code_list(raw_quality.get("reason_codes") or payload.get("reason_codes"))
    for single_reason_code in reason_code_list(raw_quality.get("reason_code") or payload.get("reason_code")):
        if single_reason_code not in reason_codes:
            reason_codes.append(single_reason_code)
    reason_labels_zh = labels_from_reason_codes(reason_codes, "zh")
    reason_labels_en = labels_from_reason_codes(reason_codes, "en")
    reason_note = str(raw_quality.get("reason_note") or payload.get("reason_note") or "").strip()
    if reason_note and reason_note not in reason_labels_zh:
        reason_labels_zh.append(reason_note)

    out = dict(raw_quality)
    if grade:
        out["grade"] = grade
    out["reason_codes"] = reason_codes
    out["reason_labels"] = reason_labels_zh
    out["reason_labels_zh"] = reason_labels_zh
    out["reason_labels_en"] = reason_labels_en
    if reason_note:
        out["reason_note"] = reason_note
    return {key: value for key, value in out.items() if value not in ("", [], None)}


def info_payload_metadata(payload):
    if not isinstance(payload, dict):
        return {}

    collection_quality = collection_quality_from_info(payload)
    reason_labels_zh = collection_quality.get("reason_labels_zh") or collection_quality.get("reason_labels")
    if not isinstance(reason_labels_zh, list):
        reason_labels_zh = []
    reason_labels_en = collection_quality.get("reason_labels_en")
    if not isinstance(reason_labels_en, list):
        reason_labels_en = []
    reason_codes = collection_quality.get("reason_codes")
    if not isinstance(reason_codes, list):
        reason_codes = []

    meta = {}
    if collection_quality:
        meta["collection_quality"] = collection_quality
    grade = normalise_quality_grade(collection_quality.get("grade"))
    if grade:
        meta["quality_grade"] = grade
        meta["manual_quality_grade"] = grade
        meta["manual_failure"] = grade == "F"
    if reason_codes:
        meta["reason_codes"] = list(reason_codes)
    if reason_labels_zh or reason_labels_en:
        meta["reason_labels"] = [str(item) for item in reason_labels_zh if str(item).strip()]
        meta["reason_labels_zh"] = [str(item) for item in reason_labels_zh if str(item).strip()]
        meta["reason_labels_en"] = [str(item) for item in reason_labels_en if str(item).strip()]
        meta["quality_description"] = "；".join(meta["reason_labels"])
        meta["manual_review_reason"] = meta["quality_description"]
        if grade == "F":
            meta["manual_failure_reason"] = meta["quality_description"]

    items = payload.get("items")
    if isinstance(items, list):
        meta["items"] = items
    for key in ("scene", "level_definition"):
        value = payload.get(key)
        if value not in ("", [], {}, None):
            meta[key] = value
    return meta


def instruction_info_candidates(mcap_files, source_episode_name):
    candidates = []
    for mcap_file in mcap_files:
        if mcap_file.suffix.lower() == ".mcap":
            candidates.append(mcap_file.with_name(f"{mcap_file.stem}_info.json"))
        if source_episode_name:
            candidates.append(mcap_file.parent / f"{source_episode_name}_info.json")

    seen = set()
    unique = []
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def resolve_instruction_info_path(mcap_files, source_episode_name):
    for candidate in instruction_info_candidates(mcap_files, source_episode_name):
        if candidate.is_file():
            return candidate
    return None


def timestamp_text_to_ns(value):
    try:
        seconds = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return int((seconds * Decimal(1_000_000_000)).to_integral_value(rounding=ROUND_HALF_UP))


def nearest_frame_index(frame_timestamps, timestamp_ns):
    if not frame_timestamps:
        return 0
    best_frame, best_diff = frame_timestamps[0][0], abs(frame_timestamps[0][1] - timestamp_ns)
    for frame_idx, frame_timestamp_ns in frame_timestamps[1:]:
        diff = abs(frame_timestamp_ns - timestamp_ns)
        if diff < best_diff:
            best_frame, best_diff = frame_idx, diff
    return int(best_frame)


def subtask_segments_from_info(segment_instructions, frame_timestamps):
    if not isinstance(segment_instructions, list):
        return []
    frame_count = len(frame_timestamps)
    if frame_count == 0:
        return []

    raw_segments = []
    for item in segment_instructions:
        if not isinstance(item, dict):
            continue
        start_ns = timestamp_text_to_ns(item.get("start_time"))
        end_ns = timestamp_text_to_ns(item.get("end_time"))
        if start_ns is None or end_ns is None:
            continue
        start_frame = nearest_frame_index(frame_timestamps, start_ns)
        end_frame = nearest_frame_index(frame_timestamps, end_ns)
        if end_frame < start_frame:
            start_frame, end_frame = end_frame, start_frame

        description_en = text_list(item.get("description_en"))
        segment = {
            "start": max(0, min(start_frame, frame_count - 1)),
            "end": max(0, min(end_frame, frame_count - 1)),
            "subtask": joined_text(description_en) or joined_text(item.get("description_zh")) or str(item.get("id") or ""),
            "description_en": description_en,
            "description_zh": text_list(item.get("description_zh")),
            "raw_start_time": str(item.get("start_time") or ""),
            "raw_end_time": str(item.get("end_time") or ""),
        }
        if item.get("id"):
            segment["id"] = str(item["id"])
        raw_segments.append(segment)

    raw_segments.sort(key=lambda segment: (segment["start"], segment["end"]))
    segments = []
    previous_end = -1
    for segment in raw_segments:
        start = max(int(segment["start"]), previous_end + 1)
        if start >= frame_count:
            continue
        end = max(start, int(segment["end"]))
        end = min(end, frame_count - 1)
        updated = dict(segment)
        updated["start"] = start
        updated["end"] = end
        segments.append(updated)
        previous_end = end
    return segments


def frame_segment_instructions_from_subtasks(subtask_segments):
    segment_instructions = []
    for segment in subtask_segments:
        item = {
            "start_time": int(segment["start"]),
            "end_time": int(segment["end"]),
            "description_en": list(segment.get("description_en") or []),
            "description_zh": list(segment.get("description_zh") or []),
        }
        if segment.get("id"):
            item["id"] = str(segment["id"])
        if segment.get("raw_start_time"):
            item["raw_start_time"] = str(segment["raw_start_time"])
        if segment.get("raw_end_time"):
            item["raw_end_time"] = str(segment["raw_end_time"])
        segment_instructions.append(item)
    return segment_instructions


def read_instruction_meta(mcap_files, source_episode_name, aligned_h5_path, explicit_task_text="", dry_run=False):
    info_path = resolve_instruction_info_path(mcap_files, source_episode_name)
    if info_path is None:
        return {}
    print(f"Read instruction info: {info_path}")

    with info_path.open("r", encoding="utf-8") as file_obj:
        payload = json.load(file_obj)
    mark = payload.get("mark") if isinstance(payload, dict) else {}
    if not isinstance(mark, dict):
        mark = {}

    frame_timestamps = [] if dry_run else read_hdf5_frame_timestamps(aligned_h5_path)
    full_instructions_en = [explicit_task_text] if explicit_task_text else text_list(mark.get("full-instructions-en"))
    full_instructions_zh = text_list(mark.get("full-instructions-zh"))
    segment_instructions = mark.get("segment-instructions")
    subtask_segments = subtask_segments_from_info(segment_instructions, frame_timestamps)
    frame_segment_instructions = frame_segment_instructions_from_subtasks(subtask_segments)
    description_en = []
    for segment in subtask_segments:
        description_en.extend(segment.get("description_en") or [])

    meta = {
        "instruction_info_file": str(info_path),
        "full_instructions_en": full_instructions_en,
        "full_instructions_zh": full_instructions_zh,
        "segment_instructions": frame_segment_instructions,
        "subtask_segments": subtask_segments,
        "description_en": description_en,
    }
    meta.update(info_payload_metadata(payload))
    return meta


def task_text_from_instruction_meta(instruction_meta):
    full_instructions_en = instruction_meta.get("full_instructions_en") if instruction_meta else None
    if isinstance(full_instructions_en, list):
        for value in full_instructions_en:
            text = str(value).strip()
            if text:
                return text
    return ""


def write_hdf5_task_attrs(aligned_h5_path, task_text, dry_run=False):
    if not task_text:
        return
    print(f"Write task attrs: {aligned_h5_path}")
    if dry_run:
        return

    import h5py

    with h5py.File(aligned_h5_path, "a") as file_obj:
        file_obj.attrs["task"] = task_text


def resolve_sync_images(sync_path):
    if not sync_path.exists():
        return []

    image_paths = []
    for raw_line in sync_path.read_text().splitlines():
        line = raw_line.strip()
        if not line:
            continue
        image_path = Path(line)
        if not image_path.is_absolute():
            image_path = sync_path.parent / image_path
        image_paths.append(image_path)
    return image_paths


def read_sync_summary(aloha_episode_dir):
    summary_path = aloha_episode_dir / "sync_summary.json"
    if not summary_path.exists():
        return {}
    try:
        with summary_path.open("r", encoding="utf-8") as file_obj:
            data = json.load(file_obj)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[WARN] Could not read sync summary {summary_path}: {exc}", flush=True)
        return {}
    if isinstance(data, dict):
        return data
    return {}


def add_sync_stats_to_video_infos(video_infos, sync_summary, video_sources=VIDEO_SOURCES):
    if not sync_summary:
        return video_infos
    camera_entries = sync_summary.get("camera_color", [])
    if not isinstance(camera_entries, list):
        return video_infos
    stats_by_name = {
        str(entry.get("name")): entry
        for entry in camera_entries
        if isinstance(entry, dict) and entry.get("name") is not None
    }
    annotated = []
    for info in video_infos:
        copied = dict(info)
        relative_sync_path = video_sources.get(str(info.get("file", "")))
        if relative_sync_path is not None and len(relative_sync_path.parts) >= 3:
            camera_name = relative_sync_path.parts[-2]
            sync_stats = stats_by_name.get(camera_name)
            if sync_stats:
                copied["camera_color_name"] = camera_name
                copied["sync_stats"] = sync_stats
        annotated.append(copied)
    return annotated


def open_video_writer(cv2, video_path, fps, frame_size):
    codecs = ["avc1", "H264", "mp4v"]
    for codec in codecs:
        fourcc = cv2.VideoWriter_fourcc(*codec)
        writer = cv2.VideoWriter(str(video_path), fourcc, fps, frame_size)
        if writer.isOpened():
            return writer, codec
        writer.release()
    raise RuntimeError(f"Could not open a video writer for {video_path}")


def requested_gpu_device_index(gpu_device):
    text = str(gpu_device).strip()
    if text.startswith("cuda:"):
        return text.split(":", 1)[1].strip() or "0"
    if text == "cuda":
        return "0"
    return text or "0"


def gpu_device_index(gpu_device):
    requested = requested_gpu_device_index(gpu_device)
    visible_devices = [
        item.strip()
        for item in os.environ.get("CUDA_VISIBLE_DEVICES", "").split(",")
        if item.strip()
    ]
    if visible_devices:
        if requested in visible_devices:
            return str(visible_devices.index(requested))
        try:
            requested_index = int(requested)
        except ValueError:
            return requested
        if 0 <= requested_index < len(visible_devices):
            return str(requested_index)
    return requested


def ffmpeg_h264_cpu_command(ffmpeg_path, input_path, output_path):
    return [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(input_path),
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-tag:v",
        "avc1",
        str(output_path),
    ]


def ffmpeg_h264_gpu_command(ffmpeg_path, input_path, output_path, gpu_device, gpu_video_encoder):
    return [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(input_path),
        "-an",
        "-c:v",
        gpu_video_encoder,
        "-gpu",
        gpu_device_index(gpu_device),
        "-preset",
        "fast",
        "-cq",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-tag:v",
        "avc1",
        str(output_path),
    ]


def transcode_video_to_h264(
    input_path,
    output_path,
    gpu_encode=False,
    gpu_device="0",
    gpu_video_encoder="h264_nvenc",
):
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        return None

    tmp_output = output_path.with_name(f"{output_path.stem}.h264.tmp{output_path.suffix}")
    remove_path(tmp_output)
    codec = "h264"
    if gpu_encode:
        try:
            subprocess.run(
                ffmpeg_h264_gpu_command(
                    ffmpeg_path,
                    input_path,
                    tmp_output,
                    gpu_device,
                    gpu_video_encoder,
                ),
                check=True,
            )
            codec = gpu_video_encoder
        except subprocess.CalledProcessError as exc:
            remove_path(tmp_output)
            print(
                f"[WARN] GPU video transcode failed on GPU {gpu_device_index(gpu_device)} "
                f"with {gpu_video_encoder}: {exc}. Falling back to libx264.",
                flush=True,
            )
    if not tmp_output.exists():
        subprocess.run(ffmpeg_h264_cpu_command(ffmpeg_path, input_path, tmp_output), check=True)
    tmp_output.replace(output_path)
    if input_path != output_path:
        remove_path(input_path)
    return codec


def write_video_from_sync(
    sync_path,
    video_path,
    fps,
    expected_frame_count,
    frame_positions=None,
    gpu_encode=False,
    gpu_device="0",
    gpu_video_encoder="h264_nvenc",
):
    import cv2

    image_paths = resolve_sync_images(sync_path)
    if not image_paths:
        raise FileNotFoundError(f"No synced images found from {sync_path}")
    if frame_positions is not None:
        invalid_positions = [pos for pos in frame_positions if pos < 0 or pos >= len(image_paths)]
        if invalid_positions:
            raise ValueError(
                f"Frame selection for {sync_path} is outside image list range: "
                f"{invalid_positions[:5]} / {len(image_paths)} images"
            )
        image_paths = [image_paths[pos] for pos in frame_positions]
    if expected_frame_count and len(image_paths) != expected_frame_count:
        raise ValueError(
            f"Frame count mismatch for {sync_path}: images={len(image_paths)}, hdf5={expected_frame_count}"
        )

    first_frame = cv2.imread(str(image_paths[0]), cv2.IMREAD_COLOR)
    if first_frame is None:
        raise FileNotFoundError(f"Could not read first image: {image_paths[0]}")
    height, width = first_frame.shape[:2]
    frame_size = (width, height)
    ffmpeg_available = shutil.which("ffmpeg") is not None
    raw_video_path = video_path.with_name(f"{video_path.stem}.opencv.tmp{video_path.suffix}") if ffmpeg_available else video_path
    remove_path(raw_video_path)
    writer, codec = open_video_writer(cv2, raw_video_path, fps, frame_size)

    try:
        for idx, image_path in enumerate(image_paths):
            frame = first_frame if idx == 0 else cv2.imread(str(image_path), cv2.IMREAD_COLOR)
            if frame is None:
                raise FileNotFoundError(f"Could not read image: {image_path}")
            if frame.shape[:2] != (height, width):
                raise ValueError(
                    f"Image size mismatch in {sync_path}: {image_path} has {frame.shape[:2]}, expected {(height, width)}"
                )
            writer.write(frame)
            if idx and idx % 300 == 0:
                print(f"wrote {idx}/{len(image_paths)} frames to {video_path.name}")
    finally:
        writer.release()

    transcoded_codec = (
        transcode_video_to_h264(raw_video_path, video_path, gpu_encode, gpu_device, gpu_video_encoder)
        if ffmpeg_available
        else None
    )
    if not transcoded_codec:
        if codec in {"avc1", "H264"}:
            transcoded_codec = "h264"
        else:
            raise RuntimeError(
                "Could not create a browser-playable H.264 MP4. Install ffmpeg "
                "in the runtime image, or use an OpenCV build with H.264/avc1 encoding."
            )

    return {
        "file": video_path.name,
        "frame_count": len(image_paths),
        "width": width,
        "height": height,
        "fps": fps,
        "codec": transcoded_codec or codec,
    }


def write_available_videos(
    aloha_episode_dir,
    videos_dir,
    fps,
    frame_count,
    frame_positions=None,
    dry_run=False,
    gpu_encode=False,
    gpu_device="0",
    gpu_video_encoder="h264_nvenc",
    camera_layout: CameraLayout = THREE_CAMERA,
):
    video_infos = []
    missing_video_names = []

    for video_name, relative_sync_path in camera_layout.video_sources.items():
        sync_path = aloha_episode_dir / relative_sync_path
        video_path = videos_dir / video_name
        if not sync_path.exists():
            missing_video_names.append(video_name)
            continue

        print(f"Create video: {video_path}")
        if dry_run:
            video_infos.append({"file": video_name})
            continue
        video_infos.append(
            write_video_from_sync(
                sync_path,
                video_path,
                fps,
                frame_count,
                frame_positions,
                gpu_encode,
                gpu_device,
                gpu_video_encoder,
            )
        )

    generated_names = {info["file"] for info in video_infos}
    for video_name in camera_layout.reference_video_names:
        if video_name not in generated_names and video_name not in missing_video_names:
            missing_video_names.append(video_name)

    return video_infos, missing_video_names


def validate_required_videos(camera_layout: CameraLayout, missing_video_names):
    missing_required = [
        video_name
        for video_name in camera_layout.required_video_names
        if video_name in missing_video_names
    ]
    if missing_required:
        raise FileNotFoundError(
            "Missing required camera videos for "
            f"{camera_layout.name}: {', '.join(missing_required)}"
        )


def write_meta(
    meta_path,
    args,
    source_episode_name,
    mcap_files,
    h5_info,
    video_infos,
    missing_videos,
    fps,
    task_text,
    instruction_meta=None,
    sync_summary=None,
    dry_run=False,
):
    camera_layout = get_camera_layout(args.cameraLayout)
    video_infos = add_sync_stats_to_video_infos(
        video_infos,
        sync_summary or {},
        camera_layout.video_sources,
    )
    meta = {
        "task_id": args.taskId,
        "job_id": args.jobId,
        "AID": args.aid,
        "episode_id": args.episodeId,
        "duration": h5_info["duration"],
        "task": task_text,
        "text_zh": args.textZh,
        "episode_name": meta_path.parent.parent.name,
        "source_episode_name": source_episode_name,
        "source_mcap_files": [str(path) for path in mcap_files],
        "frame_count": h5_info["frame_count"],
        "video_fps": fps,
        "inferred_state_fps": h5_info["inferred_fps"],
        "first_timestamp_ns": h5_info["first_timestamp_ns"],
        "last_timestamp_ns": h5_info["last_timestamp_ns"],
        "adjacent_equal_timestamp_deduplication": h5_info.get(
            "adjacent_equal_timestamp_deduplication",
            {
                "enabled": True,
                "original_frame_count": h5_info["frame_count"],
                "removed_frame_count": 0,
                "kept_frame_count": h5_info["frame_count"],
            },
        ),
        "available_videos": video_infos,
        "missing_reference_videos": sorted(missing_videos),
        "states_file": "states/aligned_joints.h5",
    }
    record_time_topics = sorted(set(getattr(args, "record_time_topics", [])))
    if record_time_topics:
        meta["timestamp_source_overrides"] = {
            topic: "mcap_record_time" for topic in record_time_topics
        }
    if sync_summary:
        meta["camera_sync_summary"] = sync_summary
    if instruction_meta:
        meta.update(instruction_meta)
        full_instructions_en = meta.get("full_instructions_en")
        if isinstance(full_instructions_en, list) and full_instructions_en:
            meta["task"] = str(full_instructions_en[0])
            meta["tasks"] = [str(value) for value in full_instructions_en if str(value).strip()]
    explicit_task_text = str(args.text or "").strip()
    if explicit_task_text:
        meta["task"] = explicit_task_text
        meta["tasks"] = [explicit_task_text]
        meta["full_instructions_en"] = [explicit_task_text]
    if not meta.get("tasks") and meta.get("task"):
        meta["tasks"] = [str(meta["task"])]

    print(f"Create meta: {meta_path}")
    if dry_run:
        print(json.dumps(meta, indent=2, ensure_ascii=False))
        return

    with meta_path.open("w", encoding="utf-8") as file_obj:
        json.dump(meta, file_obj, indent=2, ensure_ascii=False)
        file_obj.write("\n")


def build_mcap_to_hdf5_command(
    *,
    args,
    mcap_path,
    intermediate_hdf5_dir,
    hdf5_work_dir,
    source_episode_name,
    dataset_type,
    topic_yaml,
):
    command = [
        args.python,
        "mcap_to_hdf5.py",
        "--mcapPath",
        str(mcap_path),
        "--output",
        str(intermediate_hdf5_dir),
        "--workDir",
        str(hdf5_work_dir),
        "--episodeName",
        source_episode_name,
        "--type",
        dataset_type,
        "--alohaYaml",
        str(topic_yaml),
        "--timeDiffLimit",
        str(args.timeDiffLimit),
        "--overwrite",
    ]
    if args.allowPartialRecover:
        command.append("--allowPartialRecover")
    for topic in args.record_time_topics:
        command.extend(["--recordTimeTopic", topic])
    return command


def main(argv=None):
    args = parse_args(argv)
    camera_layout = get_camera_layout(args.cameraLayout)

    mcap_path = Path(args.mcapPath).expanduser().resolve()
    mcap_files = collect_mcap_files(mcap_path)
    source_episode_name = args.episodeName or infer_episode_name(mcap_path, mcap_files)
    robot_kind = resolve_robot_kind(args)
    dataset_type = "g2" if robot_kind == "g2" and args.type == "aloha" else args.type
    topic_yaml = resolve_topic_yaml(dataset_type, args.alohaYaml)

    episode_dir = Path(args.output).expanduser().resolve()
    work_dir = Path(args.workDir).expanduser().resolve() if args.workDir else episode_dir / "_work"
    states_dir, videos_dir, meta_dir = prepare_output_dirs(episode_dir, work_dir, args.overwrite, args.dryRun)

    aligned_h5 = states_dir / "aligned_joints.h5"
    intermediate_hdf5_dir = work_dir / "ario_hdf5"
    intermediate_hdf5 = intermediate_hdf5_dir / f"{source_episode_name}.hdf5"
    hdf5_work_dir = work_dir / "mcap_to_hdf5_work"
    aloha_episode_dir = hdf5_work_dir / "aloha" / source_episode_name

    print(f"Input MCAP files: {len(mcap_files)}")
    for file_path in mcap_files:
        print(f"  {file_path}")
    print(f"Output episode dir: {episode_dir}")
    print(f"Source episode name: {source_episode_name}")
    print(f"Robot layout: {robot_kind}")
    print(f"Dataset type: {dataset_type}")
    print(f"Topic YAML: {topic_yaml}")
    print(f"Work dir: {work_dir}")

    run_command(
        build_mcap_to_hdf5_command(
            args=args,
            mcap_path=mcap_path,
            intermediate_hdf5_dir=intermediate_hdf5_dir,
            hdf5_work_dir=hdf5_work_dir,
            source_episode_name=source_episode_name,
            dataset_type=dataset_type,
            topic_yaml=topic_yaml,
        ),
        args.dryRun,
    )

    run_command(
        [
            args.python,
            "ario_hdf5_to_aligned_joints.py",
            "--input",
            str(intermediate_hdf5),
            "--output",
            str(aligned_h5),
            "--robot",
            robot_kind,
        ],
        args.dryRun,
    )

    dedupe_info = {
        "enabled": False,
        "original_frame_count": 0,
        "removed_frame_count": 0,
        "kept_frame_count": 0,
        "kept_source_positions": None,
        "removed_source_frames": [],
    }

    h5_info = {
        "frame_count": 0,
        "first_timestamp_ns": 0,
        "last_timestamp_ns": 0,
        "duration": 0.0,
        "inferred_fps": 0.0,
    }
    if not args.dryRun:
        try:
            h5_info = read_hdf5_info(aligned_h5)
        except ValueError:
            if aligned_h5.exists():
                aligned_h5.unlink()
            raise
        h5_info["adjacent_equal_timestamp_deduplication"] = {
            key: value for key, value in dedupe_info.items() if key != "kept_source_positions"
        }

    fps = args.fps
    if fps <= 0.0:
        fps = h5_info["inferred_fps"] or 30.0
    if fps <= 0.0:
        raise ValueError(f"Invalid FPS: {fps}")

    explicit_task_text = args.text.strip()
    instruction_meta = read_instruction_meta(
        mcap_files,
        source_episode_name,
        aligned_h5,
        explicit_task_text,
        args.dryRun,
    )
    default_task_text = "" if explicit_task_text else read_profile_default_task(args.profile)
    task_text = explicit_task_text or task_text_from_instruction_meta(instruction_meta) or default_task_text
    if not task_text:
        raise ValueError(
            "No task text found. Set --text, add tasks.default to the robot profile, "
            "or provide an MCAP sidecar *_info.json with mark.full-instructions-en."
        )
    write_hdf5_task_attrs(aligned_h5, task_text, args.dryRun)

    video_infos, missing_videos = write_available_videos(
        aloha_episode_dir,
        videos_dir,
        fps,
        h5_info["frame_count"],
        dedupe_info.get("kept_source_positions"),
        args.dryRun,
        args.gpu_encode_videos,
        args.gpu_device,
        args.gpu_video_encoder,
        camera_layout,
    )
    if not args.dryRun:
        validate_required_videos(camera_layout, missing_videos)
    sync_summary = read_sync_summary(aloha_episode_dir)
    write_meta(
        meta_dir / "episode_meta.json",
        args,
        source_episode_name,
        mcap_files,
        h5_info,
        video_infos,
        missing_videos,
        fps,
        task_text,
        instruction_meta,
        sync_summary,
        args.dryRun,
    )

    if args.cleanupIntermediate:
        print(f"+ remove {work_dir}")
        if not args.dryRun:
            shutil.rmtree(work_dir)

    print(f"Done: {episode_dir}")


if __name__ == "__main__":
    raise SystemExit(main())
