#!/usr/bin/env python3
# -- coding: UTF-8
"""
Regenerate episodes marked as need_repair in batch_summary.md, then insert
interpolated frames so aligned_joints.h5 timestamp gaps are below a target.

Typical use inside the data-tools ROS2 container:
    REPO_ROOT=$(pwd)
    DATA_ROOT=data
    DATASET_NAME=grasp_bottle2
    QC_REPORT_NAME=grasp_bottle2_YYYYmmdd_HHMMSS

    docker run --rm -it \
      --user "$(id -u):$(id -g)" \
      -e HOME=/tmp \
      -e DATA_ROOT=/workspace/data \
      -e DATASET_NAME="$DATASET_NAME" \
      -e QC_REPORT_NAME="$QC_REPORT_NAME" \
      -w /workspace/mcap_conversion/scripts \
      -v /etc/passwd:/etc/passwd:ro \
      -v /etc/group:/etc/group:ro \
      -v "$REPO_ROOT/mcap_conversion:/workspace/mcap_conversion" \
      -v "$REPO_ROOT/$DATA_ROOT:/workspace/data" \
      data-tools-ros2:jazzy \
      bash

    cd /workspace/mcap_conversion/scripts
    python3 repair_need_repair_episodes.py \
      --qcSummary "$DATA_ROOT/qc_reports/$QC_REPORT_NAME/batch_summary.md" \
      --mcapRoot "$DATA_ROOT/raw_mcap/$DATASET_NAME" \
      --outputRoot "$DATA_ROOT/hdf5_episodes/$DATASET_NAME" \
      --maxGap 0.25 \
      --overwrite \
      --cleanupIntermediate
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from mcap_to_icra_episode import read_hdf5_info, transcode_video_to_h264


SCRIPT_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class FrameStep:
    left_idx: int
    right_idx: int
    alpha: float
    timestamp_ns: int
    is_original: bool


@dataclass(frozen=True)
class SeriesEntry:
    time_sec: float
    path: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Repair episodes listed in batch_summary.md need_repair by "
            "regenerating from MCAP and inserting interpolated HDF5/video frames."
        )
    )
    parser.add_argument(
        "--qcSummary",
        type=Path,
        required=True,
        help=(
            "Path to batch_summary.md, for example "
            "data/qc_reports/<dataset_name>_<timestamp>/batch_summary.md."
        ),
    )
    parser.add_argument(
        "--mcapRoot",
        type=Path,
        default=None,
        help="Root containing episode MCAP directories. Default: derive from the summary input root.",
    )
    parser.add_argument(
        "--outputRoot",
        type=Path,
        default=None,
        help="Root containing output episode directories. Default: input root from batch_summary.md.",
    )
    parser.add_argument(
        "--episodeIds",
        default="",
        help="Optional comma-separated episode ids to repair, for example 1,3,9. Default: need_repair from summary.",
    )
    parser.add_argument(
        "--episodeNames",
        default="",
        help=(
            "Optional comma-separated exact episode directory names to repair, "
            "for example recording_20260604_105941. Takes precedence over --episodeIds."
        ),
    )
    parser.add_argument(
        "--maxGap",
        type=float,
        default=0.25,
        help="Maximum allowed timestamp gap after interpolation. Default: 0.25 seconds.",
    )
    parser.add_argument(
        "--timeDiffLimit",
        type=float,
        default=0.03,
        help="Timestamp sync tolerance passed to mcap_to_icra_episode.py. Default: 0.03.",
    )
    parser.add_argument(
        "--rawTimeTolerance",
        type=float,
        default=0.03,
        help=(
            "Tolerance for using an original MCAP/Aloha sensor sample at an "
            "inserted timestamp. If no sample is within this window, interpolate. "
            "Default: 0.03 seconds."
        ),
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable used to call mcap_to_icra_episode.py.",
    )
    parser.add_argument("--type", default="aloha", help="Dataset config type passed to mcap_to_icra_episode.py.")
    parser.add_argument("--profile", default="", help="Robot profile YAML passed to mcap_to_icra_episode.py.")
    parser.add_argument("--text", default="", help="Task text passed to mcap_to_icra_episode.py.")
    parser.add_argument("--robot", default="auto", help="Robot layout passed to mcap_to_icra_episode.py.")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Pass --overwrite to mcap_to_icra_episode.py.",
    )
    parser.add_argument(
        "--cleanupIntermediate",
        action="store_true",
        help="Remove each episode _work directory after raw-sensor-priority repair completes.",
    )
    parser.add_argument(
        "--gpu-encode-videos",
        action="store_true",
        help="Use ffmpeg NVENC for regenerated/repaired MP4 transcode, with CPU H.264 fallback.",
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
        "--skipConvert",
        action="store_true",
        help="Only interpolate existing episode outputs; do not regenerate from MCAP.",
    )
    parser.add_argument(
        "--dryRun",
        action="store_true",
        help="Print planned repair commands without changing files.",
    )
    return parser.parse_args()


def split_markdown_row(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]

    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for char in text:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    cells.append("".join(current).strip())
    return cells


def parse_summary(qc_summary: Path) -> tuple[dict[str, str], list[dict[str, str]]]:
    info: dict[str, str] = {}
    episodes: list[dict[str, str]] = []
    lines = qc_summary.expanduser().resolve().read_text(encoding="utf-8").splitlines()

    in_info = False
    in_episode_table = False
    episode_header: list[str] | None = None

    for line in lines:
        stripped = line.strip()
        if stripped == "## 批量质检汇总":
            in_info = True
            in_episode_table = False
            continue
        if stripped == "## Episode 明细":
            in_info = False
            in_episode_table = True
            continue
        if not stripped.startswith("|"):
            continue

        cells = split_markdown_row(stripped)
        if not cells or all(set(cell) <= {"-", ":"} for cell in cells):
            continue

        if in_info:
            if len(cells) >= 2 and cells[0] != "信息项":
                info[cells[0]] = cells[1]
            continue

        if in_episode_table:
            if "episode_id" in cells:
                episode_header = cells
                continue
            if episode_header is None:
                continue
            episodes.append(dict(zip(episode_header, cells)))

    return info, episodes


def numeric_episode_id(value: Any) -> str:
    numbers = re.findall(r"\d+", str(value))
    if not numbers:
        return str(value)
    return str(int(numbers[-1]))


def episode_name_from_id(value: Any) -> str:
    text = str(value).strip()
    if text.startswith("episode"):
        return text
    number = numeric_episode_id(text)
    return f"episode{number}" if number else text


def parse_ids_cell(value: str) -> list[str]:
    match = re.search(r"ids=([^;|]+)", value)
    if not match:
        return []
    return [
        numeric_episode_id(part)
        for part in re.split(r"[,\s]+", match.group(1).strip())
        if part.strip()
    ]


def repair_episode_names(info: dict[str, str], episodes: list[dict[str, str]]) -> list[str]:
    names = [
        str(row.get("episode_id", "")).strip()
        for row in episodes
        if str(row.get("是否删除", "")).strip() == "修复"
    ]
    if names:
        return sorted({episode_name_from_id(name) for name in names}, key=natural_episode_key)

    ids = parse_ids_cell(info.get("need_repair", ""))
    return sorted({episode_name_from_id(item) for item in ids}, key=natural_episode_key)


def natural_episode_key(value: str) -> list[Any]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value)]


def infer_roots(qc_summary: Path, info: dict[str, str]) -> tuple[Path, Path]:
    output_root_text = info.get("input", "")
    output_root = Path(output_root_text).expanduser().resolve() if output_root_text else None
    if output_root is None:
        qc_name = qc_summary.parent.name
        if qc_name.endswith("_qc_report"):
            output_root = qc_summary.parent.with_name(qc_name[: -len("_qc_report")] + "_h5")
        else:
            output_root = qc_summary.parent.with_name(qc_name + "_h5")

    if output_root.name.endswith("_h5"):
        mcap_root = output_root.with_name(output_root.name[: -len("_h5")] + "_mcap")
    else:
        mcap_root = output_root.with_name(output_root.name + "_mcap")
    return output_root, mcap_root


def run_command(cmd: list[str], dry_run: bool) -> None:
    print("+ " + " ".join(cmd), flush=True)
    if not dry_run:
        subprocess.run(cmd, cwd=str(SCRIPT_DIR), check=True)


def regenerate_episode(args: argparse.Namespace, episode_name: str, mcap_dir: Path, episode_dir: Path) -> None:
    cmd = [
        args.python,
        str(SCRIPT_DIR / "mcap_to_icra_episode.py"),
        "--mcapPath",
        str(mcap_dir),
        "--output",
        str(episode_dir),
        "--episodeName",
        episode_name,
        "--timeDiffLimit",
        str(args.timeDiffLimit),
        "--type",
        args.type,
        "--robot",
        args.robot,
    ]
    if args.profile:
        cmd.extend(["--profile", str(args.profile)])
    if args.text:
        cmd.extend(["--text", args.text])
    if args.gpu_encode_videos:
        cmd.extend(
            [
                "--gpu-encode-videos",
                "--gpu-device",
                args.gpu_device,
                "--gpu-video-encoder",
                args.gpu_video_encoder,
            ]
        )
    if args.overwrite:
        cmd.append("--overwrite")
    run_command(cmd, args.dryRun)


def cleanup_episode_work(episode_dir: Path, dry_run: bool) -> None:
    work_dir = episode_dir / "_work"
    if not work_dir.exists():
        return
    print(f"+ remove {work_dir}", flush=True)
    if not dry_run:
        shutil.rmtree(work_dir)


def aloha_episode_dir(episode_dir: Path, episode_name: str) -> Path:
    return episode_dir / "_work" / "mcap_to_hdf5_work" / "aloha" / episode_name


def parse_time_from_path(path: Path) -> float | None:
    try:
        return float(path.stem)
    except ValueError:
        return None


def list_series(directory: Path, suffixes: tuple[str, ...]) -> list[SeriesEntry]:
    if not directory.exists():
        return []
    entries: list[SeriesEntry] = []
    for suffix in suffixes:
        for path in directory.glob(f"*{suffix}"):
            if path.name == "config.json" or path.name == "sync.txt":
                continue
            time_sec = parse_time_from_path(path)
            if time_sec is not None:
                entries.append(SeriesEntry(time_sec=time_sec, path=path))
    return sorted(entries, key=lambda item: item.time_sec)


def nearest_entry(series: list[SeriesEntry], target_sec: float, tolerance_sec: float) -> SeriesEntry | None:
    if not series:
        return None
    times = [item.time_sec for item in series]
    idx = bisect.bisect_left(times, target_sec)
    candidates: list[SeriesEntry] = []
    if idx > 0:
        candidates.append(series[idx - 1])
    if idx < len(series):
        candidates.append(series[idx])
    if not candidates:
        return None
    best = min(candidates, key=lambda item: abs(item.time_sec - target_sec))
    if abs(best.time_sec - target_sec) <= tolerance_sec:
        return best
    return None


def quat_from_rpy(roll: float, pitch: float, yaw: float) -> np.ndarray:
    cr = math.cos(roll * 0.5)
    sr = math.sin(roll * 0.5)
    cp = math.cos(pitch * 0.5)
    sp = math.sin(pitch * 0.5)
    cy = math.cos(yaw * 0.5)
    sy = math.sin(yaw * 0.5)
    return np.array(
        [
            sr * cp * cy - cr * sp * sy,
            cr * sp * cy + sr * cp * sy,
            cr * cp * sy - sr * sp * cy,
            cr * cp * cy + sr * sp * sy,
        ],
        dtype=np.float64,
    )


def pose_dict_to_position_orientation(data: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    position = np.array([data["x"], data["y"], data["z"]], dtype=np.float64)
    orientation = quat_from_rpy(float(data["roll"]), float(data["pitch"]), float(data["yaw"]))
    return position, orientation


class RawEpisodeCache:
    def __init__(self, root: Path):
        self.root = root
        self.joint_series = {
            name: list_series(root / "arm" / "jointState" / name, (".json",))
            for name in ("masterLeft", "masterRight", "puppetLeft", "puppetRight")
        }
        self.pose_series = {
            name: list_series(root / "localization" / "pose" / name, (".json",))
            for name in ("puppetLeft", "puppetRight")
        }
        self.camera_series = {
            name: list_series(root / "camera" / "color" / name, (".jpg", ".png"))
            for name in ("left", "front", "right")
        }
        self._json_cache: dict[Path, dict[str, Any]] = {}

    @property
    def available(self) -> bool:
        return self.root.exists()

    def nearest_joint(self, name: str, target_sec: float, tolerance_sec: float) -> tuple[dict[str, Any], float] | None:
        entry = nearest_entry(self.joint_series.get(name, []), target_sec, tolerance_sec)
        if entry is None:
            return None
        return self.read_json(entry.path), entry.time_sec

    def nearest_pose(self, name: str, target_sec: float, tolerance_sec: float) -> tuple[dict[str, Any], float] | None:
        entry = nearest_entry(self.pose_series.get(name, []), target_sec, tolerance_sec)
        if entry is None:
            return None
        return self.read_json(entry.path), entry.time_sec

    def nearest_camera(self, name: str, target_sec: float, tolerance_sec: float) -> SeriesEntry | None:
        return nearest_entry(self.camera_series.get(name, []), target_sec, tolerance_sec)

    def read_json(self, path: Path) -> dict[str, Any]:
        if path not in self._json_cache:
            self._json_cache[path] = json.loads(path.read_text(encoding="utf-8"))
        return self._json_cache[path]


def hdf5_timestamps(h5_path: Path) -> tuple[list[int], list[int]]:
    with h5py.File(h5_path, "r") as file_obj:
        frame_indices = sorted(int(key) for key in file_obj.keys() if key.isdigit())
        timestamps = [int(file_obj[str(idx)]["main_timestamp"][()]) for idx in frame_indices]
    return frame_indices, timestamps


def build_steps(timestamps: list[int], max_gap_sec: float) -> tuple[list[FrameStep], list[int]]:
    if len(timestamps) < 2:
        return [FrameStep(0, 0, 0.0, timestamps[0] if timestamps else 0, True)], []

    max_gap_ns = int(max_gap_sec * 1_000_000_000)
    steps = [FrameStep(0, 0, 0.0, timestamps[0], True)]
    interval_segments: list[int] = []

    for idx, (left_ts, right_ts) in enumerate(zip(timestamps, timestamps[1:])):
        gap = max(0, right_ts - left_ts)
        segments = max(1, math.ceil(gap / max_gap_ns)) if max_gap_ns > 0 else 1
        interval_segments.append(segments)
        for step_idx in range(1, segments + 1):
            alpha = step_idx / segments
            is_original = step_idx == segments
            timestamp_ns = right_ts if is_original else int(round(left_ts + gap * alpha))
            steps.append(FrameStep(idx, idx + 1, alpha, timestamp_ns, is_original))

    return steps, interval_segments


def dataset_paths(group: h5py.Group, prefix: str = "") -> list[str]:
    paths: list[str] = []
    for key, item in group.items():
        item_path = f"{prefix}/{key}" if prefix else key
        if isinstance(item, h5py.Dataset):
            paths.append(item_path)
        elif isinstance(item, h5py.Group):
            paths.extend(dataset_paths(item, item_path))
    return paths


def ensure_parent_group(group: h5py.Group, dataset_path: str) -> h5py.Group:
    parent = group
    parts = dataset_path.split("/")
    for part in parts[:-1]:
        parent = parent.require_group(part)
    return parent


def read_dataset(root: h5py.File, frame_idx: int, path: str) -> np.ndarray:
    return np.asarray(root[str(frame_idx)][path][()])


def default_interpolated_value(
    root: h5py.File,
    step: FrameStep,
    dataset_path: str,
    source_dtype: np.dtype,
) -> Any:
    if dataset_path == "main_timestamp":
        return np.asarray(step.timestamp_ns, dtype=np.uint64)
    if step.is_original:
        return read_dataset(root, step.right_idx, dataset_path)

    left = read_dataset(root, step.left_idx, dataset_path)
    right = read_dataset(root, step.right_idx, dataset_path)
    if left.shape == right.shape and np.issubdtype(left.dtype, np.number):
        value = left.astype(np.float64) * (1.0 - step.alpha) + right.astype(np.float64) * step.alpha
        if np.issubdtype(source_dtype, np.integer):
            return np.rint(value).astype(source_dtype)
        return value.astype(source_dtype)

    return left if step.alpha < 0.5 else right


def array_from_joint(data: dict[str, Any], field: str) -> np.ndarray:
    return np.asarray(data[field], dtype=np.float64)


def raw_joint_part(
    raw_cache: RawEpisodeCache,
    name: str,
    field: str,
    target_sec: float,
    tolerance_sec: float,
) -> np.ndarray | None:
    item = raw_cache.nearest_joint(name, target_sec, tolerance_sec)
    if item is None:
        return None
    data, _ = item
    if field not in data:
        return None
    return array_from_joint(data, field)


def apply_joint_pair_override(
    base: np.ndarray,
    raw_cache: RawEpisodeCache,
    left_name: str,
    right_name: str,
    field: str,
    target_sec: float,
    tolerance_sec: float,
) -> np.ndarray:
    value = np.asarray(base).copy()
    left = raw_joint_part(raw_cache, left_name, field, target_sec, tolerance_sec)
    right = raw_joint_part(raw_cache, right_name, field, target_sec, tolerance_sec)
    half = value.shape[0] // 2 if value.ndim == 1 else 0
    if left is not None and half:
        value[:half] = left[:half]
    if right is not None and half:
        value[half : half + min(half, len(right))] = right[:half]
    return value


def raw_pose_part(
    raw_cache: RawEpisodeCache,
    name: str,
    target_sec: float,
    tolerance_sec: float,
) -> tuple[np.ndarray, np.ndarray] | None:
    item = raw_cache.nearest_pose(name, target_sec, tolerance_sec)
    if item is None:
        return None
    data, _ = item
    return pose_dict_to_position_orientation(data)


def apply_end_pose_override(
    base: np.ndarray,
    raw_cache: RawEpisodeCache,
    dataset_path: str,
    target_sec: float,
    tolerance_sec: float,
) -> np.ndarray:
    value = np.asarray(base).copy()
    left = raw_pose_part(raw_cache, "puppetLeft", target_sec, tolerance_sec)
    right = raw_pose_part(raw_cache, "puppetRight", target_sec, tolerance_sec)
    part_idx = 0 if dataset_path == "state/end/position" else 1
    if left is not None:
        value[0] = left[part_idx]
    if right is not None:
        value[1] = right[part_idx]
    return value


def raw_camera_timestamp(
    raw_cache: RawEpisodeCache,
    dataset_path: str,
    target_sec: float,
    tolerance_sec: float,
) -> np.ndarray | None:
    camera_name = {
        "timestamp/camera/head_stereo_left": "left",
        "timestamp/camera/head_color": "front",
        "timestamp/camera/head_stereo_right": "right",
        "timestamp/camera/head": "head",
    }.get(dataset_path)
    if camera_name is None:
        return None
    entry = raw_cache.nearest_camera(camera_name, target_sec, tolerance_sec)
    if entry is None:
        return None
    return np.asarray([int(round(entry.time_sec * 1_000_000_000))], dtype=np.uint64)


def value_with_raw_sensor_priority(
    root: h5py.File,
    step: FrameStep,
    dataset_path: str,
    source_dtype: np.dtype,
    raw_cache: RawEpisodeCache | None,
    raw_tolerance_sec: float,
) -> Any:
    base = default_interpolated_value(root, step, dataset_path, source_dtype)
    if step.is_original or raw_cache is None or not raw_cache.available:
        return base

    target_sec = step.timestamp_ns / 1_000_000_000.0
    if dataset_path == "state/joint/position":
        return apply_joint_pair_override(
            base, raw_cache, "puppetLeft", "puppetRight", "position", target_sec, raw_tolerance_sec
        ).astype(source_dtype)
    if dataset_path == "state/joint/velocity":
        return apply_joint_pair_override(
            base, raw_cache, "puppetLeft", "puppetRight", "velocity", target_sec, raw_tolerance_sec
        ).astype(source_dtype)
    if dataset_path == "state/joint/effort":
        return apply_joint_pair_override(
            base, raw_cache, "puppetLeft", "puppetRight", "effort", target_sec, raw_tolerance_sec
        ).astype(source_dtype)
    if dataset_path == "action/joint/position":
        return apply_joint_pair_override(
            base, raw_cache, "masterLeft", "masterRight", "position", target_sec, raw_tolerance_sec
        ).astype(source_dtype)
    if dataset_path == "state/left_effector/position":
        left = raw_joint_part(raw_cache, "puppetLeft", "position", target_sec, raw_tolerance_sec)
        return np.asarray([left[6]], dtype=source_dtype) if left is not None and len(left) > 6 else base
    if dataset_path == "state/right_effector/position":
        right = raw_joint_part(raw_cache, "puppetRight", "position", target_sec, raw_tolerance_sec)
        return np.asarray([right[6]], dtype=source_dtype) if right is not None and len(right) > 6 else base
    if dataset_path == "action/left_effector/position":
        left = raw_joint_part(raw_cache, "masterLeft", "position", target_sec, raw_tolerance_sec)
        return np.asarray([left[6]], dtype=source_dtype) if left is not None and len(left) > 6 else base
    if dataset_path == "action/right_effector/position":
        right = raw_joint_part(raw_cache, "masterRight", "position", target_sec, raw_tolerance_sec)
        return np.asarray([right[6]], dtype=source_dtype) if right is not None and len(right) > 6 else base
    if dataset_path in {"state/end/position", "state/end/orientation"}:
        return apply_end_pose_override(base, raw_cache, dataset_path, target_sec, raw_tolerance_sec).astype(source_dtype)
    if dataset_path.startswith("timestamp/camera/"):
        raw_ts = raw_camera_timestamp(raw_cache, dataset_path, target_sec, raw_tolerance_sec)
        return raw_ts if raw_ts is not None else base
    return base


def write_repaired_hdf5(
    h5_path: Path,
    steps: list[FrameStep],
    max_gap_sec: float,
    raw_cache: RawEpisodeCache | None,
    raw_tolerance_sec: float,
) -> dict[str, Any]:
    tmp_path = h5_path.with_name(f"{h5_path.stem}.repair_tmp{h5_path.suffix}")
    if tmp_path.exists():
        tmp_path.unlink()

    with h5py.File(h5_path, "r") as src, h5py.File(tmp_path, "w") as dst:
        src_frame_indices = sorted(int(key) for key in src.keys() if key.isdigit())
        if not src_frame_indices:
            raise ValueError(f"No frame groups in {h5_path}")
        paths = dataset_paths(src[str(src_frame_indices[0])])
        dtypes = {path: src[str(src_frame_indices[0])][path].dtype for path in paths}

        for key, value in src.attrs.items():
            dst.attrs[key] = value
        dst.attrs["timestamp_gap_repaired"] = True
        dst.attrs["timestamp_gap_target_sec"] = max_gap_sec
        dst.attrs["source_frame_count"] = len(src_frame_indices)
        dst.attrs["repaired_frame_count"] = len(steps)
        dst.attrs["interpolated_frame_count"] = len(steps) - len(src_frame_indices)
        dst.attrs["raw_sensor_priority"] = raw_cache is not None and raw_cache.available
        dst.attrs["raw_sensor_tolerance_sec"] = raw_tolerance_sec

        for out_idx, step in enumerate(steps):
            group = dst.create_group(str(out_idx))
            source_group = src[str(step.left_idx)]
            for key, value in source_group.attrs.items():
                group.attrs[key] = value
            for path in paths:
                parent = ensure_parent_group(group, path)
                name = path.split("/")[-1]
                value = value_with_raw_sensor_priority(
                    src, step, path, dtypes[path], raw_cache, raw_tolerance_sec
                )
                parent.create_dataset(name, data=value)

    tmp_path.replace(h5_path)
    return read_hdf5_info(h5_path)


def max_timestamp_gap_sec(timestamps: list[int]) -> float:
    if len(timestamps) < 2:
        return 0.0
    return max((b - a) / 1_000_000_000.0 for a, b in zip(timestamps, timestamps[1:]))


def open_video_writer(cv2: Any, video_path: Path, fps: float, frame_size: tuple[int, int]) -> Any:
    codecs = ["avc1", "H264", "mp4v"]
    for codec in codecs:
        fourcc = cv2.VideoWriter_fourcc(*codec)
        writer = cv2.VideoWriter(str(video_path), fourcc, fps, frame_size)
        if writer.isOpened():
            return writer
        writer.release()
    raise RuntimeError(f"Could not open video writer for {video_path}")


def read_exact_frame(cap: Any, video_path: Path, idx: int) -> np.ndarray:
    ok, frame = cap.read()
    if not ok or frame is None:
        raise ValueError(f"Could not read frame {idx} from {video_path}")
    return frame


def video_camera_name(video_path: Path) -> str | None:
    return {
        "hand_left_color.mp4": "left",
        "head_color.mp4": "front",
        "hand_right_color.mp4": "right",
        "head.mp4": "head",
    }.get(video_path.name)


def raw_camera_frame(
    cv2: Any,
    raw_cache: RawEpisodeCache | None,
    camera_name: str | None,
    target_sec: float,
    tolerance_sec: float,
    frame_size: tuple[int, int],
) -> np.ndarray | None:
    if raw_cache is None or not raw_cache.available or camera_name is None:
        return None
    entry = raw_cache.nearest_camera(camera_name, target_sec, tolerance_sec)
    if entry is None:
        return None
    frame = cv2.imread(str(entry.path), cv2.IMREAD_COLOR)
    if frame is None:
        return None
    width, height = frame_size
    if frame.shape[:2] != (height, width):
        frame = cv2.resize(frame, frame_size, interpolation=cv2.INTER_AREA)
    return frame


def repair_video(
    video_path: Path,
    steps: list[FrameStep],
    interval_segments: list[int],
    fps: float,
    raw_cache: RawEpisodeCache | None,
    raw_tolerance_sec: float,
    dry_run: bool,
    gpu_encode: bool,
    gpu_device: str,
    gpu_video_encoder: str,
) -> dict[str, Any]:
    import cv2

    if not video_path.exists():
        return {"file": video_path.name, "status": "missing"}
    if dry_run:
        return {"file": video_path.name, "status": "planned"}

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    try:
        first_frame = read_exact_frame(cap, video_path, 0)
        height, width = first_frame.shape[:2]
        raw_tmp = video_path.with_name(f"{video_path.stem}.repair_tmp{video_path.suffix}")
        if raw_tmp.exists():
            raw_tmp.unlink()
        writer = open_video_writer(cv2, raw_tmp, fps, (width, height))
        written = 0
        try:
            writer.write(first_frame)
            written += 1
            left_frame = first_frame
            step_cursor = 1
            raw_frames = 0
            blended_frames = 0
            camera_name = video_camera_name(video_path)
            for interval_idx, segments in enumerate(interval_segments):
                right_frame = read_exact_frame(cap, video_path, interval_idx + 1)
                if right_frame.shape[:2] != (height, width):
                    raise ValueError(f"Video frame size changed in {video_path}")
                for step_idx in range(1, segments):
                    step = steps[step_cursor]
                    step_cursor += 1
                    raw_frame = raw_camera_frame(
                        cv2,
                        raw_cache,
                        camera_name,
                        step.timestamp_ns / 1_000_000_000.0,
                        raw_tolerance_sec,
                        (width, height),
                    )
                    if raw_frame is not None:
                        writer.write(raw_frame)
                        raw_frames += 1
                        written += 1
                        continue
                    alpha = step_idx / segments
                    blended = cv2.addWeighted(left_frame, 1.0 - alpha, right_frame, alpha, 0.0)
                    writer.write(blended)
                    blended_frames += 1
                    written += 1
                step_cursor += 1
                writer.write(right_frame)
                written += 1
                left_frame = right_frame
        finally:
            writer.release()
    finally:
        cap.release()

    transcode_video_to_h264(raw_tmp, video_path, gpu_encode, gpu_device, gpu_video_encoder)
    return {
        "file": video_path.name,
        "status": "repaired",
        "frame_count": written,
        "raw_frames": raw_frames,
        "interpolated_frames": blended_frames,
    }


def update_meta(
    episode_dir: Path,
    h5_info: dict[str, Any],
    video_results: list[dict[str, Any]],
    source_frame_count: int,
    max_gap_sec: float,
    raw_tolerance_sec: float,
    raw_cache_available: bool,
) -> None:
    meta_path = episode_dir / "meta" / "episode_meta.json"
    if not meta_path.exists():
        return
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["frame_count"] = h5_info["frame_count"]
    meta["duration"] = h5_info["duration"]
    meta["first_timestamp_ns"] = h5_info["first_timestamp_ns"]
    meta["last_timestamp_ns"] = h5_info["last_timestamp_ns"]
    meta["inferred_state_fps"] = h5_info["inferred_fps"]
    meta["timestamp_gap_repair"] = {
        "enabled": True,
        "target_max_gap_sec": max_gap_sec,
        "source_frame_count": source_frame_count,
        "repaired_frame_count": h5_info["frame_count"],
        "inserted_frames": h5_info["frame_count"] - source_frame_count,
        "raw_sensor_priority": raw_cache_available,
        "raw_time_tolerance_sec": raw_tolerance_sec,
    }

    video_counts = {
        item["file"]: item["frame_count"]
        for item in video_results
        if item.get("status") == "repaired" and "frame_count" in item
    }
    for item in meta.get("available_videos", []):
        if isinstance(item, dict) and item.get("file") in video_counts:
            item["frame_count"] = video_counts[item["file"]]

    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def interpolate_episode(
    episode_dir: Path,
    max_gap_sec: float,
    raw_tolerance_sec: float,
    dry_run: bool,
    gpu_encode: bool,
    gpu_device: str,
    gpu_video_encoder: str,
) -> dict[str, Any]:
    h5_path = episode_dir / "states" / "aligned_joints.h5"
    if not h5_path.exists():
        raise FileNotFoundError(h5_path)

    frame_indices, timestamps = hdf5_timestamps(h5_path)
    before_gap = max_timestamp_gap_sec(timestamps)
    steps, interval_segments = build_steps(timestamps, max_gap_sec)
    source_frame_count = len(frame_indices)
    inserted = len(steps) - source_frame_count
    raw_dir = aloha_episode_dir(episode_dir, episode_dir.name)
    raw_cache = RawEpisodeCache(raw_dir) if raw_dir.exists() else None

    print(
        f"Repair timestamps: {episode_dir.name} frames={source_frame_count} "
        f"insert={inserted} max_gap_before={before_gap:.6f}s target={max_gap_sec:.6f}s "
        f"raw_dir={'yes' if raw_cache is not None and raw_cache.available else 'no'}",
        flush=True,
    )

    if dry_run:
        return {
            "episode": episode_dir.name,
            "source_frame_count": source_frame_count,
            "repaired_frame_count": len(steps),
            "inserted_frames": inserted,
            "max_gap_before_sec": before_gap,
            "max_gap_after_sec": max_gap_sec if inserted else before_gap,
        }

    if inserted > 0:
        h5_info = write_repaired_hdf5(h5_path, steps, max_gap_sec, raw_cache, raw_tolerance_sec)
    else:
        h5_info = read_hdf5_info(h5_path)

    meta_path = episode_dir / "meta" / "episode_meta.json"
    fps = 30.0
    if meta_path.exists():
        try:
            fps = float(json.loads(meta_path.read_text(encoding="utf-8")).get("video_fps") or 30.0)
        except (TypeError, ValueError, json.JSONDecodeError):
            fps = 30.0

    video_results: list[dict[str, Any]] = []
    if inserted > 0:
        videos_dir = episode_dir / "videos"
        for video_path in sorted(videos_dir.glob("*.mp4")):
            video_results.append(
                repair_video(
                    video_path,
                    steps,
                    interval_segments,
                    fps,
                    raw_cache,
                    raw_tolerance_sec,
                    dry_run=False,
                    gpu_encode=gpu_encode,
                    gpu_device=gpu_device,
                    gpu_video_encoder=gpu_video_encoder,
                )
            )

    update_meta(
        episode_dir,
        h5_info,
        video_results,
        source_frame_count,
        max_gap_sec,
        raw_tolerance_sec,
        raw_cache is not None and raw_cache.available,
    )
    _, repaired_timestamps = hdf5_timestamps(h5_path)
    after_gap = max_timestamp_gap_sec(repaired_timestamps)
    return {
        "episode": episode_dir.name,
        "source_frame_count": source_frame_count,
        "repaired_frame_count": h5_info["frame_count"],
        "inserted_frames": h5_info["frame_count"] - source_frame_count,
        "max_gap_before_sec": before_gap,
        "max_gap_after_sec": after_gap,
        "videos": video_results,
    }


def main() -> int:
    args = parse_args()
    qc_summary = args.qcSummary.expanduser().resolve()
    if not qc_summary.is_file():
        raise FileNotFoundError(
            f"QC summary not found: {qc_summary}\n"
            "Check that DATASET_NAME is set inside the container and that QC has already "
            "generated data/qc_reports/<dataset_name>_<timestamp>/batch_summary.md."
        )
    info, episodes = parse_summary(qc_summary)
    inferred_output_root, inferred_mcap_root = infer_roots(qc_summary, info)
    output_root = args.outputRoot.expanduser().resolve() if args.outputRoot else inferred_output_root
    mcap_root = args.mcapRoot.expanduser().resolve() if args.mcapRoot else inferred_mcap_root

    if args.episodeNames:
        episode_names = [item.strip() for item in re.split(r"[,\s]+", args.episodeNames) if item.strip()]
    elif args.episodeIds:
        episode_names = [episode_name_from_id(item) for item in re.split(r"[,\s]+", args.episodeIds) if item.strip()]
    else:
        episode_names = repair_episode_names(info, episodes)

    print(f"QC summary : {qc_summary}")
    print(f"MCAP root  : {mcap_root}")
    print(f"Output root: {output_root}")
    print(f"Max gap    : {args.maxGap}")
    print(f"Raw tol    : {args.rawTimeTolerance}")
    print(f"Episodes   : {', '.join(episode_names) if episode_names else '(none)'}")

    if not episode_names:
        print("No need_repair episodes found.")
        return 0

    results = []
    for episode_name in episode_names:
        mcap_dir = mcap_root / episode_name
        if not mcap_dir.exists() and (mcap_root / f"{episode_name}.mcap").is_file():
            mcap_dir = mcap_root / f"{episode_name}.mcap"
        episode_dir = output_root / episode_name
        if not mcap_dir.exists():
            raise FileNotFoundError(f"Missing MCAP episode dir: {mcap_dir}")
        if not args.skipConvert:
            regenerate_episode(args, episode_name, mcap_dir, episode_dir)
        results.append(
            interpolate_episode(
                episode_dir,
                args.maxGap,
                args.rawTimeTolerance,
                args.dryRun,
                args.gpu_encode_videos,
                args.gpu_device,
                args.gpu_video_encoder,
            )
        )
        if args.cleanupIntermediate:
            cleanup_episode_work(episode_dir, args.dryRun)

    print(json.dumps({"ok": True, "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
