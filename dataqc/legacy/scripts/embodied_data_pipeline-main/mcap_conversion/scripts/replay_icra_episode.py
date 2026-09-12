#!/usr/bin/env python3
# -- coding: UTF-8
"""Replay ICRA-WBC-like episode directories with videos and aligned_joints.h5."""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
import mimetypes
import os
import re
import socket
import sys
import tempfile
import threading
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.trim_stationary_hdf5_episodes import (  # noqa: E402
    episode_lock,
    episode_video_paths as trim_episode_video_paths,
    frame_trim_transaction,
    hdf5_frame_count,
    install_trim_signal_handlers,
    recover_frame_trim_transaction,
    remap_segments,
    remap_step_index,
    trim_hdf5,
    trim_video,
    validate_no_partial_trim,
)

try:
    import h5py
except ImportError:  # pragma: no cover
    h5py = None  # type: ignore[assignment]


DEFAULT_EPISODE_DIR = Path("data/hdf5_episodes")
DEFAULT_FPS = 30.0
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
REASON_LABEL_TO_CODE = {
    **{value: key for key, value in REASON_CODE_LABELS_ZH.items()},
    **{value: key for key, value in REASON_CODE_LABELS_EN.items()},
}
VIDEO_ORDER = (
    ("head_color", "Head", "head_color.mp4"),
    ("hand_left_color", "Left Hand", "hand_left_color.mp4"),
    ("hand_right_color", "Right Hand", "hand_right_color.mp4"),
    ("head", "Head Auxiliary", "head.mp4"),
)
ALOHA_JOINT_LABELS = (
    [f"left_joint_{idx}" for idx in range(1, 7)]
    + ["left_gripper"]
    + [f"right_joint_{idx}" for idx in range(1, 7)]
    + ["right_gripper"]
)
ALOHA_ACTION_EXTRA_LABELS = (
    "action/waist/position",
    "action/robot/velocity.linear_x",
    "action/robot/velocity.linear_y",
    "action/robot/velocity.angular_z",
)
ALOHA_STATE_EXTRA_LABELS = (
    "state/waist/position",
    "state/robot/base_state.x",
    "state/robot/base_state.y",
    "state/robot/base_state.yaw",
    "state/robot/base_state.vx",
    "state/robot/base_state.vy",
    "state/robot/base_state.wz",
)
ALOHA_EXTRA_DISPLAY_ROWS = (
    ("waist_height", 14, 14),
    ("base_x", 15, None),
    ("base_y", 16, None),
    ("base_yaw", 17, None),
    ("base_vx", 18, 15),
    ("base_vy", 19, 16),
    ("base_wz", 20, 17),
)
G2_COMMON_LABELS = (
    [f"left_arm_{idx}" for idx in range(1, 8)]
    + ["left_gripper"]
    + [f"right_arm_{idx}" for idx in range(1, 8)]
    + ["right_gripper"]
    + [f"waist_{idx}" for idx in range(1, 6)]
)
G2_STATE_LABELS = list(G2_COMMON_LABELS) + [
    "base_x",
    "base_y",
    "base_z",
    "base_orientation_z",
    "base_orientation_w",
]
G2_ACTION_LABELS = list(G2_COMMON_LABELS) + [
    "base_vx",
    "base_vy",
    "base_wz",
]
ZERITH_LABELS = (
    [f"left_arm_{idx}" for idx in range(1, 8)]
    + ["left_gripper"]
    + [f"right_arm_{idx}" for idx in range(1, 8)]
    + ["right_gripper", "lift", "waist_pitch", "waist_yaw", "head_yaw", "head_pitch"]
    + ["base_linear_velocity", "base_angular_velocity"]
)


@dataclass
class EpisodeRecord:
    episode_dir: Path
    h5_path: Path
    meta: dict[str, Any]
    frame_count: int
    duration: float
    fps: float
    video_fps: float
    fps_override: float
    episode_type: str
    available_videos: list[dict[str, Any]]
    qc_status: str
    qc_remark: str
    quality_grade: str
    manual_review_reason: str


@dataclass
class EpisodeData:
    episode_dir: Path
    h5_path: Path
    meta: dict[str, Any]
    frame_count: int
    duration: float
    fps: float
    video_fps: float
    episode_type: str
    timestamps: list[float]
    state_joint_position: list[list[float]]
    action_joint_position: list[list[float]]
    state_joint_velocity: list[list[float]]
    state_joint_effort: list[list[float]]
    state_end_position: list[list[float]]
    action_robot_velocity: list[list[float]]
    display_rows: list[dict[str, Any]]
    available_videos: list[dict[str, Any]]
    qc_status: str
    qc_remark: str
    quality_grade: str
    manual_review_reason: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Web replay for one episode_xxxxx folder or a parent folder containing multiple episodes."
    )
    parser.add_argument(
        "--episode-dir",
        type=Path,
        default=DEFAULT_EPISODE_DIR,
        help=f"Episode directory, or a parent directory containing episodes. Default: {DEFAULT_EPISODE_DIR}",
    )
    parser.add_argument("--host", default="0.0.0.0", help="HTTP bind host. Default: 0.0.0.0")
    parser.add_argument("--port", type=int, default=8790, help="HTTP port. Default: 8790")
    parser.add_argument(
        "--fps",
        type=float,
        default=0.0,
        help="Override replay FPS. Default: infer HDF5 FPS from main_timestamp.",
    )
    parser.add_argument(
        "--type",
        choices=("auto", "aloha", "g2", "zerith"),
        default="auto",
        help="Episode layout to display. Use g2 to show 26D state and 24D action. Default: auto.",
    )
    parser.add_argument(
        "--qc-report-dir",
        type=Path,
        default=None,
        help=(
            "Directory containing batch_summary.md. If omitted, tries to find a sibling "
            "*_qc_report directory for the input episode directory."
        ),
    )
    parser.add_argument(
        "--manual-failure-json",
        type=Path,
        default=None,
        help="manual_failure_annotations.json used to mark collection-failed episodes.",
    )
    parser.add_argument("--open-browser", action="store_true", help="Open browser after starting.")
    return parser.parse_args()


def discover_episode_dirs(input_path: Path) -> list[Path]:
    input_path = input_path.expanduser().resolve()
    if episode_h5_path(input_path).is_file():
        return [input_path]

    episode_dirs = sorted(
        {
            (path.parent.parent if path.parent.name == "states" else path.parent).resolve()
            for pattern in (
                "*/states/aligned_joints.h5",
                "*/states/aligned_joints.hdf5",
                "*/episode.hdf5",
                "*/episode.h5",
            )
            for path in input_path.glob(pattern)
        },
        key=natural_sort_key,
    )
    if not episode_dirs:
        raise FileNotFoundError(
            f"No episode directories found under {input_path}. Expected aligned_joints.h5 or episode.hdf5."
        )
    return episode_dirs


def episode_h5_path(episode_dir: Path) -> Path:
    candidates = (
        episode_dir / "episode.hdf5",
        episode_dir / "episode.h5",
        episode_dir / "states" / "aligned_joints.h5",
        episode_dir / "states" / "aligned_joints.hdf5",
    )
    return next((path for path in candidates if path.is_file()), candidates[2])


def episode_meta_path(episode_dir: Path) -> Path:
    legacy = episode_dir / "meta" / "episode_meta.json"
    columnar = episode_dir / "episode_meta.json"
    if legacy.is_file() or (not columnar.is_file() and not (episode_dir / "episode.hdf5").is_file()):
        return legacy
    return columnar


def natural_sort_key(path: Path) -> list[Any]:
    parts = re.split(r"(\d+)", path.name)
    return [int(part) if part.isdigit() else part.lower() for part in parts]


def get_local_ip() -> str | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        return None
    finally:
        sock.close()


def read_meta(episode_dir: Path) -> dict[str, Any]:
    meta_path = episode_meta_path(episode_dir)
    if meta_path.is_file():
        with meta_path.open("r", encoding="utf-8") as file_obj:
            meta = json.load(file_obj)
    else:
        meta = {}
    h5_path = episode_h5_path(episode_dir)
    if h5py is not None and h5_path.is_file():
        try:
            with h5py.File(h5_path, "r") as file_obj:
                task = str(file_obj.attrs.get("task_name") or "").strip()
                if task:
                    meta.setdefault("task", task)
                    meta.setdefault("tasks", [task])
                for key in ("control_frequency", "total_frames", "task_id"):
                    value = file_obj.attrs.get(key)
                    if hasattr(value, "item"):
                        value = value.item()
                    if value is not None:
                        meta.setdefault(key, value)
        except OSError:
            pass
    return meta


def strict_integer(value: Any, field_name: str) -> int:
    """Parse an integer without silently accepting booleans or decimal values."""

    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer, not a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value.strip())
    raise ValueError(f"{field_name} must be an integer, got {value!r}")


def hdf5_frame_count_from_file(file_obj: Any) -> int:
    if "timestamp/t" in file_obj:
        timestamps = file_obj["timestamp/t"]
        if len(timestamps.shape) != 1:
            raise ValueError(
                f"{file_obj.filename}: timestamp/t must be one-dimensional, got {timestamps.shape}"
            )
        return int(timestamps.shape[0])
    frame_keys = [str(key) for key in file_obj.keys() if str(key).isdigit()]
    return len(frame_keys)


def validate_two_stage_transition_values(
    values: Any,
    frame_count: int,
    source: str,
) -> int:
    if hasattr(values, "tolist"):
        values = values.tolist()
    if not isinstance(values, (list, tuple)) or len(values) != 2:
        raise ValueError(
            f"{source}: expected exactly two cumulative subtask transitions [stage2_start, frame_count], "
            f"got {values!r}"
        )
    stage2_start = strict_integer(values[0], f"{source}[0]")
    final_boundary = strict_integer(values[1], f"{source}[1]")
    if frame_count < 2:
        raise ValueError(f"{source}: two stages require at least 2 frames, got {frame_count}")
    if not 1 <= stage2_start <= frame_count - 1:
        raise ValueError(
            f"{source}: stage 2 start must satisfy 1 <= boundary <= {frame_count - 1}, "
            f"got {stage2_start}"
        )
    if final_boundary != frame_count:
        raise ValueError(
            f"{source}: final transition must equal HDF5 frame count {frame_count}, "
            f"got {final_boundary}"
        )
    return stage2_start


def validate_step_index(
    meta: dict[str, Any],
    frame_count: int,
    source: str,
) -> dict[str, Any]:
    """Validate two real steps while accepting the collector's empty final sentinel."""

    for count_key in ("frame_count", "total_frames"):
        if count_key not in meta or meta[count_key] is None:
            continue
        stored_count = strict_integer(meta[count_key], f"{source}.{count_key}")
        if stored_count != frame_count:
            raise ValueError(
                f"{source}.{count_key}={stored_count} does not match HDF5 frame count {frame_count}"
            )

    raw_steps = meta.get("step_index")
    if not isinstance(raw_steps, list):
        raise ValueError(f"{source}.step_index must be a list")

    active: list[tuple[int, dict[str, Any], int, int]] = []
    sentinels: list[int] = []
    for index, item in enumerate(raw_steps):
        if not isinstance(item, dict):
            raise ValueError(f"{source}.step_index[{index}] must be an object")
        if "start_frame_id" not in item or "end_frame_id" not in item:
            raise ValueError(
                f"{source}.step_index[{index}] is missing start_frame_id or end_frame_id"
            )
        start = strict_integer(
            item["start_frame_id"], f"{source}.step_index[{index}].start_frame_id"
        )
        end = strict_integer(
            item["end_frame_id"], f"{source}.step_index[{index}].end_frame_id"
        )
        if end < start:
            if start != frame_count or end != frame_count - 1:
                raise ValueError(
                    f"{source}.step_index[{index}] has an invalid empty range [{start}, {end}]; "
                    f"only the collector sentinel [{frame_count}, {frame_count - 1}] is allowed"
                )
            sentinels.append(index)
            continue
        active.append((index, item, start, end))

    if len(active) != 2:
        raise ValueError(
            f"{source}.step_index must contain exactly two non-empty stages after filtering "
            f"empty sentinels, got {len(active)}"
        )
    (_, first, first_start, first_end), (_, second, second_start, second_end) = active
    if first_start != 0:
        raise ValueError(f"{source}.step_index stage 1 must start at frame 0, got {first_start}")
    if first_end + 1 != second_start:
        raise ValueError(
            f"{source}.step_index stages must be contiguous: stage 1 ends at {first_end}, "
            f"stage 2 starts at {second_start}"
        )
    if second_end != frame_count - 1:
        raise ValueError(
            f"{source}.step_index stage 2 must end at frame {frame_count - 1}, got {second_end}"
        )
    for expected_number, (_, item, _, _) in enumerate(active, start=1):
        if item.get("step_number") is None:
            continue
        step_number = strict_integer(
            item["step_number"], f"{source}.step_index stage {expected_number}.step_number"
        )
        if step_number != expected_number:
            raise ValueError(
                f"{source}.step_index expected step_number {expected_number}, got {step_number}"
            )
    stage2_start = second_start
    validate_two_stage_transition_values(
        [stage2_start, frame_count], frame_count, f"{source}.step_index"
    )
    return {
        "stage2_start": stage2_start,
        "active_indices": [item[0] for item in active],
        "sentinel_indices": sentinels,
        "steps": [first, second],
    }


def validate_hdf5_stage_layout(file_obj: Any, frame_count: int) -> int:
    actual_frame_count = hdf5_frame_count_from_file(file_obj)
    if actual_frame_count != frame_count:
        raise ValueError(
            f"{file_obj.filename}: live HDF5 frame count changed from {frame_count} "
            f"to {actual_frame_count}"
        )
    if frame_count < 2:
        raise ValueError(f"{file_obj.filename}: two stages require at least 2 frames")
    if "total_frames" in file_obj.attrs:
        attr_count = strict_integer(file_obj.attrs["total_frames"], "HDF5 attr total_frames")
        if attr_count != frame_count:
            raise ValueError(
                f"{file_obj.filename}: HDF5 attr total_frames={attr_count} does not match "
                f"timestamp frame count {frame_count}"
            )
    for attr_name in ("total_subtasks", "completed_subtasks"):
        if attr_name not in file_obj.attrs:
            continue
        attr_value = strict_integer(file_obj.attrs[attr_name], f"HDF5 attr {attr_name}")
        if attr_value != 2:
            raise ValueError(
                f"{file_obj.filename}: HDF5 attr {attr_name} must be 2, got {attr_value}"
            )
    if "subtask_transitions" not in file_obj:
        raise ValueError(f"{file_obj.filename}: missing root dataset /subtask_transitions")
    dataset = file_obj["subtask_transitions"]
    if tuple(dataset.shape) != (2,):
        raise ValueError(
            f"{file_obj.filename}: /subtask_transitions must have shape (2,), got {dataset.shape}"
        )
    if getattr(dataset.dtype, "kind", "") not in {"i", "u"}:
        raise ValueError(
            f"{file_obj.filename}: /subtask_transitions must use an integer dtype, got {dataset.dtype}"
        )
    return validate_two_stage_transition_values(
        dataset[...], frame_count, f"{file_obj.filename}:/subtask_transitions"
    )


def stage_ranges(stage2_start: int, frame_count: int) -> list[dict[str, Any]]:
    return [
        {
            "stage_number": 1,
            "label": "阶段 1",
            "start": 0,
            "end": stage2_start - 1,
        },
        {
            "stage_number": 2,
            "label": "阶段 2",
            "start": stage2_start,
            "end": frame_count - 1,
        },
    ]


def collector_stage_info(
    episode_dir: Path,
    h5_path: Path,
    frame_count: int,
) -> dict[str, Any]:
    """Read collector stage boundaries without hiding malformed or mismatched sources."""

    episode_dir = episode_dir.expanduser().resolve()
    h5_path = h5_path.expanduser().resolve()
    meta_path = episode_meta_path(episode_dir)
    errors: list[str] = []
    hdf5_boundary: int | None = None
    sidecar_boundary: int | None = None
    hdf5_present = False
    sidecar_present = False

    if h5py is None:
        errors.append("h5py is unavailable; cannot read /subtask_transitions")
    else:
        try:
            with h5py.File(h5_path, "r") as file_obj:
                hdf5_present = "subtask_transitions" in file_obj
                if hdf5_present:
                    hdf5_boundary = validate_hdf5_stage_layout(file_obj, frame_count)
        except Exception as exc:
            errors.append(f"HDF5 stage metadata invalid: {exc}")

    if meta_path.is_file():
        try:
            sidecar = json.loads(meta_path.read_text(encoding="utf-8"))
            if not isinstance(sidecar, dict):
                raise ValueError("top-level JSON value must be an object")
            sidecar_present = isinstance(sidecar.get("step_index"), list)
            if sidecar_present:
                sidecar_boundary = int(
                    validate_step_index(sidecar, frame_count, str(meta_path))["stage2_start"]
                )
        except Exception as exc:
            errors.append(f"episode_meta.json stage metadata invalid: {exc}")

    if hdf5_present != sidecar_present:
        errors.append(
            "Collector stage metadata is incomplete: /subtask_transitions and "
            "episode_meta.json.step_index must either both exist or both be absent"
        )
    if (
        hdf5_boundary is not None
        and sidecar_boundary is not None
        and hdf5_boundary != sidecar_boundary
    ):
        errors.append(
            f"Collector stage boundary mismatch: HDF5={hdf5_boundary}, "
            f"episode_meta.json={sidecar_boundary}"
        )

    boundary = hdf5_boundary if hdf5_boundary is not None else sidecar_boundary
    source = ""
    if hdf5_boundary is not None and sidecar_boundary is not None:
        source = "hdf5+episode_meta"
    elif hdf5_boundary is not None:
        source = "hdf5"
    elif sidecar_boundary is not None:
        source = "episode_meta"
    editable = bool(
        hdf5_boundary is not None
        and sidecar_boundary is not None
        and meta_path.is_file()
        and frame_count >= 2
    )
    return {
        "available": boundary is not None,
        "editable": editable,
        "consistent": bool(editable and hdf5_boundary == sidecar_boundary and not errors),
        "source": source,
        "frame_count": frame_count,
        "stage2_start_frame": boundary,
        "hdf5_stage2_start_frame": hdf5_boundary,
        "sidecar_stage2_start_frame": sidecar_boundary,
        "transitions": [boundary, frame_count] if boundary is not None else [],
        "stages": stage_ranges(boundary, frame_count) if boundary is not None else [],
        "meta_path": str(meta_path),
        "errors": errors,
    }


def ensure_same_resolved_path(provided: Any, expected: Path, field_name: str) -> None:
    raw = str(provided or "").strip()
    if not raw:
        raise ValueError(f"Missing required path field {field_name}")
    provided_path = Path(raw).expanduser().resolve()
    expected_path = expected.expanduser().resolve()
    if provided_path != expected_path:
        raise ValueError(
            f"{field_name} does not match the selected episode: expected {expected_path}, "
            f"got {provided_path}"
        )


def ensure_path_inside(path: Path, root: Path, field_name: str) -> None:
    resolved_path = path.expanduser().resolve()
    resolved_root = root.expanduser().resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must stay inside episode directory {resolved_root}, got {resolved_path}"
        ) from exc


def write_temporary_sidecar(meta_path: Path, payload: dict[str, Any]) -> Path:
    mode = meta_path.stat().st_mode & 0o777
    fd, raw_path = tempfile.mkstemp(
        prefix=f".{meta_path.name}.stage_boundary_tmp.",
        dir=str(meta_path.parent),
        text=True,
    )
    tmp_path = Path(raw_path)
    try:
        os.chmod(tmp_path, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


def write_temporary_bytes(meta_path: Path, payload: bytes) -> Path:
    mode = meta_path.stat().st_mode & 0o777
    fd, raw_path = tempfile.mkstemp(
        prefix=f".{meta_path.name}.stage_boundary_rollback_tmp.",
        dir=str(meta_path.parent),
    )
    tmp_path = Path(raw_path)
    try:
        os.chmod(tmp_path, mode)
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def timestamp_for_meta(raw_value: Any, template: Any) -> int | float:
    if hasattr(raw_value, "item"):
        raw_value = raw_value.item()
    numeric = float(raw_value)
    if isinstance(template, int) and not isinstance(template, bool):
        return int(round(numeric))
    return numeric


def updated_step_index_meta(
    meta: dict[str, Any],
    frame_count: int,
    stage2_start: int,
    transition_timestamp: Any | None,
    source: str,
) -> dict[str, Any]:
    validated = validate_step_index(meta, frame_count, source)
    updated = copy.deepcopy(meta)
    first_index, second_index = validated["active_indices"]
    first = updated["step_index"][first_index]
    second = updated["step_index"][second_index]
    first.update(
        {
            "step_number": 1,
            "start_frame_id": 0,
            "end_frame_id": stage2_start - 1,
        }
    )
    second.update(
        {
            "step_number": 2,
            "start_frame_id": stage2_start,
            "end_frame_id": frame_count - 1,
        }
    )
    if transition_timestamp is not None:
        first["end_ts"] = timestamp_for_meta(
            transition_timestamp, first.get("end_ts")
        )
        second["start_ts"] = timestamp_for_meta(
            transition_timestamp, second.get("start_ts")
        )
    for sentinel_index in validated["sentinel_indices"]:
        sentinel = updated["step_index"][sentinel_index]
        sentinel["start_frame_id"] = frame_count
        sentinel["end_frame_id"] = frame_count - 1

    new_validation = validate_step_index(updated, frame_count, source)
    if int(new_validation["stage2_start"]) != stage2_start:
        raise ValueError(
            f"{source}: failed to prepare requested stage 2 boundary {stage2_start}"
        )
    return updated


def update_collector_stage_boundary(
    episode_dir: Path,
    h5_path: Path,
    stage2_start_frame: Any,
    expected_frame_count: Any,
    expected_episode_dir: Any,
    expected_h5_path: Any,
    expected_old_stage2_start_frame: Any,
    expected_old_sidecar_stage2_start_frame: Any,
) -> dict[str, Any]:
    """Update both collector stage sources in place, without creating a backup."""

    if h5py is None:
        raise RuntimeError("Missing dependency h5py; cannot update HDF5 stage metadata")
    episode_dir = episode_dir.expanduser().resolve()
    h5_path = h5_path.expanduser().resolve()
    ensure_same_resolved_path(expected_episode_dir, episode_dir, "episode_dir")
    ensure_same_resolved_path(expected_h5_path, h5_path, "h5_path")
    ensure_path_inside(h5_path, episode_dir, "h5_path")
    if not episode_dir.is_dir():
        raise FileNotFoundError(f"Episode directory does not exist: {episode_dir}")
    if not h5_path.is_file():
        raise FileNotFoundError(f"HDF5 file does not exist: {h5_path}")
    selected_h5_path = episode_h5_path(episode_dir).expanduser().resolve()
    if selected_h5_path != h5_path:
        raise ValueError(
            f"HDF5 path is inconsistent with selected episode: resolver chose {selected_h5_path}, "
            f"request selected {h5_path}"
        )

    requested_frame_count = strict_integer(expected_frame_count, "frame_count")
    stage2_start = strict_integer(stage2_start_frame, "stage2_start_frame")
    expected_old_hdf5_boundary = strict_integer(
        expected_old_stage2_start_frame, "expected_stage2_start_frame"
    )
    expected_old_sidecar_boundary = strict_integer(
        expected_old_sidecar_stage2_start_frame,
        "expected_sidecar_stage2_start_frame",
    )
    validate_two_stage_transition_values(
        [stage2_start, requested_frame_count],
        requested_frame_count,
        "request.stage2_start_frame",
    )

    meta_path = episode_meta_path(episode_dir).expanduser().resolve()
    ensure_path_inside(meta_path, episode_dir, "episode_meta.json path")
    if not meta_path.is_file():
        raise FileNotFoundError(
            f"Cannot update both stage sources because episode_meta.json is missing: {meta_path}"
        )

    with episode_lock(episode_dir):
        # Re-resolve under the episode lock so a stale request cannot target files
        # that were swapped while the replay page was open.
        live_h5_path = episode_h5_path(episode_dir).expanduser().resolve()
        live_meta_path = episode_meta_path(episode_dir).expanduser().resolve()
        if live_h5_path != h5_path:
            raise ValueError(
                f"Selected episode HDF5 changed before update: expected {h5_path}, got {live_h5_path}"
            )
        if live_meta_path != meta_path:
            raise ValueError(
                f"Selected episode sidecar changed before update: expected {meta_path}, got {live_meta_path}"
            )
        ensure_path_inside(live_h5_path, episode_dir, "live HDF5 path")
        ensure_path_inside(live_meta_path, episode_dir, "live episode_meta.json path")

        original_meta_bytes = meta_path.read_bytes()
        try:
            original_meta = json.loads(original_meta_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid episode_meta.json at {meta_path}: {exc}") from exc
        if not isinstance(original_meta, dict):
            raise ValueError(f"episode_meta.json must contain a JSON object: {meta_path}")
        sidecar_validation = validate_step_index(
            original_meta, requested_frame_count, str(meta_path)
        )
        if int(sidecar_validation["stage2_start"]) != expected_old_sidecar_boundary:
            raise ValueError(
                "Stale stage metadata: browser expected episode_meta.json Stage 2 start "
                f"{expected_old_sidecar_boundary}, live sidecar contains "
                f"{sidecar_validation['stage2_start']}; reload the episode before applying"
            )

        tmp_path: Path | None = None
        rollback_tmp_path: Path | None = None
        hdf5_written = False
        sidecar_replaced = False
        old_transition_values: Any = None
        old_hdf5_boundary: int | None = None
        try:
            with h5py.File(h5_path, "r+") as file_obj:
                live_frame_count = hdf5_frame_count_from_file(file_obj)
                if live_frame_count != requested_frame_count:
                    raise ValueError(
                        f"Stale frame_count: browser requested {requested_frame_count}, "
                        f"live HDF5 contains {live_frame_count} frames"
                    )
                old_hdf5_boundary = validate_hdf5_stage_layout(
                    file_obj, live_frame_count
                )
                if old_hdf5_boundary != expected_old_hdf5_boundary:
                    raise ValueError(
                        "Stale stage metadata: browser expected HDF5 Stage 2 start "
                        f"{expected_old_hdf5_boundary}, live /subtask_transitions contains "
                        f"{old_hdf5_boundary}; reload the episode before applying"
                    )
                transition_timestamp = None
                if "timestamp/t" in file_obj:
                    transition_timestamp = file_obj["timestamp/t"][stage2_start]
                updated_meta = updated_step_index_meta(
                    original_meta,
                    live_frame_count,
                    stage2_start,
                    transition_timestamp,
                    str(meta_path),
                )
                tmp_path = write_temporary_sidecar(meta_path, updated_meta)

                transitions = file_obj["subtask_transitions"]
                old_transition_values = transitions[...]
                transitions[...] = [stage2_start, live_frame_count]
                file_obj.flush()
                hdf5_written = True
                persisted_boundary = validate_hdf5_stage_layout(
                    file_obj, live_frame_count
                )
                if persisted_boundary != stage2_start:
                    raise RuntimeError(
                        f"HDF5 verification failed: expected boundary {stage2_start}, "
                        f"read back {persisted_boundary}"
                    )

                os.replace(tmp_path, meta_path)
                tmp_path = None
                sidecar_replaced = True
                fsync_directory(meta_path.parent)
                persisted_meta = load_json_file(meta_path)
                persisted_sidecar = validate_step_index(
                    persisted_meta, live_frame_count, str(meta_path)
                )
                if int(persisted_sidecar["stage2_start"]) != stage2_start:
                    raise RuntimeError(
                        f"episode_meta.json verification failed: expected boundary {stage2_start}, "
                        f"read back {persisted_sidecar['stage2_start']}"
                    )
        except Exception as exc:
            rollback_errors: list[str] = []
            if hdf5_written and old_transition_values is not None:
                try:
                    with h5py.File(h5_path, "r+") as rollback_h5:
                        rollback_h5["subtask_transitions"][...] = old_transition_values
                        rollback_h5.flush()
                except Exception as rollback_exc:
                    rollback_errors.append(f"HDF5 rollback failed: {rollback_exc}")
            if sidecar_replaced:
                try:
                    rollback_tmp_path = write_temporary_bytes(
                        meta_path, original_meta_bytes
                    )
                    os.replace(rollback_tmp_path, meta_path)
                    rollback_tmp_path = None
                    fsync_directory(meta_path.parent)
                except Exception as rollback_exc:
                    rollback_errors.append(
                        f"episode_meta.json rollback failed: {rollback_exc}"
                    )
            suffix = (
                " Rollback errors: " + "; ".join(rollback_errors)
                if rollback_errors
                else " Original stage metadata was restored."
            )
            raise RuntimeError(f"Stage boundary update failed: {exc}.{suffix}") from exc
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
            if rollback_tmp_path is not None:
                rollback_tmp_path.unlink(missing_ok=True)
        info = collector_stage_info(episode_dir, h5_path, requested_frame_count)
        if not info["consistent"] or info["stage2_start_frame"] != stage2_start:
            raise RuntimeError(
                "Stage boundary update completed but final HDF5/sidecar consistency validation failed: "
                + "; ".join(info.get("errors") or ["unknown consistency error"])
            )
    return {
        "ok": True,
        "episode_dir": str(episode_dir),
        "h5_path": str(h5_path),
        "episode_meta": str(meta_path),
        "frame_count": requested_frame_count,
        "old_hdf5_stage2_start_frame": old_hdf5_boundary,
        "old_sidecar_stage2_start_frame": int(sidecar_validation["stage2_start"]),
        "stage2_start_frame": stage2_start,
        "transitions": [stage2_start, requested_frame_count],
        "stage_info": info,
    }


def resolve_qc_report_dir(input_path: Path, explicit_path: Path | None) -> Path | None:
    if explicit_path is not None:
        path = explicit_path.expanduser().resolve()
        return path if path.is_dir() else None

    input_path = input_path.expanduser().resolve()
    candidates: list[Path] = []
    if input_path.name.endswith("_h5"):
        candidates.append(input_path.with_name(input_path.name[:-3] + "_qc_report"))
    candidates.append(input_path.with_name(input_path.name + "_qc_report"))

    if input_path.parent.name.endswith("_h5"):
        candidates.append(
            input_path.parent.with_name(input_path.parent.name[:-3] + "_qc_report")
        )
    candidates.append(input_path.parent / "qc_report")

    for candidate in candidates:
        if (candidate / "batch_summary.md").is_file():
            return candidate
    return None


def load_qc_summary(qc_report_dir: Path | None) -> dict[str, dict[str, str]]:
    if qc_report_dir is None:
        return {}
    summary_path = qc_report_dir / "batch_summary.md"
    if not summary_path.is_file():
        return {}

    rows: dict[str, dict[str, str]] = {}
    lines = summary_path.read_text(encoding="utf-8").splitlines()
    header: list[str] | None = None
    for line in lines:
        if not line.startswith("|"):
            if header is not None:
                break
            continue
        cells = split_markdown_row(line)
        if not cells:
            continue
        if "episode_id" in cells and "error" in cells:
            header = cells
            continue
        if header is None or all(set(cell) <= {"-", ":"} for cell in cells):
            continue
        row = dict(zip(header, cells))
        episode_id = row.get("episode_id", "").strip()
        if not episode_id:
            continue
        rows[episode_id] = {
            "status": row.get("是否删除", "").strip(),
            "remark": row.get("error", "").strip(),
        }
    return rows


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


def qc_info_for_episode(
    episode_dir: Path,
    meta: dict[str, Any],
    qc_summary: dict[str, dict[str, str]],
    manual_failures: dict[str, dict[str, Any]] | None = None,
) -> dict[str, str]:
    manual = manual_failure_for_episode(episode_dir, meta, manual_failures or {})
    if manual:
        grade = normalise_quality_grade(manual.get("quality_grade")) or ("F" if manual.get("is_failure") else "A")
        reason = str(manual.get("reason_label") or "").strip()
        if grade == "F" or manual.get("is_failure"):
            return {
                "status": "采集失败",
                "remark": reason or "人工标注采集失败",
                "quality_grade": "F",
                "manual_review_reason": reason or "人工标注采集失败",
            }
        return {
            "status": "采集成功",
            "remark": reason if grade == "B" else "",
            "quality_grade": grade,
            "manual_review_reason": reason if grade in {"B", "F"} else "",
        }
    collection_quality = meta.get("collection_quality") if isinstance(meta.get("collection_quality"), dict) else {}
    meta_grade = normalise_quality_grade(
        meta.get("manual_quality_grade")
        or meta.get("quality_grade")
        or collection_quality.get("grade")
    )
    if meta_grade:
        reason_fields = manual_reason_fields(
            meta.get("manual_review_reason")
            or meta.get("quality_description")
            or collection_quality.get("reason_note")
            or "",
            meta.get("reason_codes") or collection_quality.get("reason_codes"),
        )
        reason = str(reason_fields.get("reason_label") or "")
        return {
            "status": "采集失败" if meta_grade == "F" else "采集成功",
            "remark": reason,
            "quality_grade": meta_grade,
            "manual_review_reason": reason,
        }
    keys = [
        episode_dir.name,
        str(meta.get("episode_name") or ""),
        str(meta.get("source_episode_name") or ""),
        str(meta.get("source_mcap_episode_name") or ""),
    ]
    for key in keys:
        if key and key in qc_summary:
            info = dict(qc_summary[key])
            raw_status = str(info.get("status") or "")
            remark = str(info.get("remark") or "")
            explicit_grade = normalise_quality_grade(info.get("quality_grade"))
            if raw_status == "采集失败":
                status = "采集失败"
                grade = "F"
            else:
                status = "采集成功"
                if explicit_grade:
                    grade = explicit_grade
                elif remark and raw_status not in {"保留", "通过", "成功", "采集成功", ""}:
                    grade = "B"
                else:
                    grade = "A"
            info["status"] = status
            info["quality_grade"] = grade
            info["manual_review_reason"] = remark if grade in {"B", "F"} else ""
            return info
    return {"status": "", "remark": "", "quality_grade": "A", "manual_review_reason": ""}


def manual_failure_path_for_input(input_path: Path, explicit_path: Path | None) -> Path:
    if explicit_path is not None:
        return explicit_path.expanduser().resolve()
    input_path = input_path.expanduser().resolve()
    if episode_h5_path(input_path).is_file():
        return input_path.parent / "manual_failure_annotations.json"
    if input_path.is_file() and input_path.parent.name == "states":
        return input_path.parent.parent.parent / "manual_failure_annotations.json"
    return input_path / "manual_failure_annotations.json"


def manual_failure_truthy(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "fail", "failed", "failure", "失败", "采集失败"}


def normalise_quality_grade(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text if text in {"A", "B", "C", "F"} else ""


def split_reason_text(value: Any) -> list[str]:
    raw_items = value if isinstance(value, list) else [value]
    out: list[str] = []
    for raw_item in raw_items:
        for item in re.split(r"[；;,，]", str(raw_item or "")):
            text = item.strip()
            if text:
                out.append(text)
    return out


def normalise_reason_codes(value: Any) -> list[str]:
    codes: list[str] = []
    for item in split_reason_text(value):
        code = item if item in REASON_CODE_LABELS_ZH else REASON_LABEL_TO_CODE.get(item, item)
        if code and code not in codes:
            codes.append(code)
    return codes


def reason_labels_from_codes(codes: list[str], language: str) -> list[str]:
    mapping = REASON_CODE_LABELS_ZH if language == "zh" else REASON_CODE_LABELS_EN
    return [mapping.get(code, code) for code in codes if code]


def manual_reason_fields(reason_label: Any = "", reason_codes: Any = None) -> dict[str, Any]:
    codes = normalise_reason_codes(reason_codes)
    extra_labels = split_reason_text(reason_label)
    for label in extra_labels:
        code = label if label in REASON_CODE_LABELS_ZH else REASON_LABEL_TO_CODE.get(label)
        if code and code not in codes:
            codes.append(code)
    labels_zh = reason_labels_from_codes(codes, "zh")
    labels_en = reason_labels_from_codes(codes, "en")
    for label in extra_labels:
        if label in REASON_CODE_LABELS_ZH:
            continue
        if label not in labels_zh and label not in labels_en:
            labels_zh.append(label)
    return {
        "reason_codes": codes,
        "reason_labels": labels_zh,
        "reason_labels_zh": labels_zh,
        "reason_labels_en": labels_en,
        "reason_label": "；".join(labels_zh),
    }


def normalise_manual_failure_entries(payload: Any) -> dict[str, dict[str, Any]]:
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
        reason_codes_input: Any = None
        if isinstance(item, str):
            name = item.strip()
            reason = ""
            is_failure = True
            quality_grade = "F"
        elif isinstance(item, dict):
            name = str(item.get("episode_name") or item.get("episode_id") or item.get("name") or "").strip()
            reason = str(
                item.get("reason_label")
                or item.get("quality_description")
                or item.get("manual_review_reason")
                or item.get("reason")
                or item.get("remark")
                or ""
            ).strip()
            reason_codes_input = item.get("reason_codes")
            reason_fields = manual_reason_fields(reason, reason_codes_input)
            if not reason:
                reason = str(reason_fields.get("reason_label") or "")
            quality_grade = normalise_quality_grade(item.get("quality_grade") or item.get("grade"))
            failure_value = (
                item.get("is_failure")
                if "is_failure" in item
                else item.get("failure", item.get("failed", item.get("manual_failure")))
            )
            if quality_grade:
                is_failure = quality_grade == "F"
            else:
                is_failure = manual_failure_truthy(failure_value, default=bool(reason))
                quality_grade = "F" if is_failure else ""
        else:
            continue
        if name:
            reason_fields = manual_reason_fields(reason, reason_codes_input)
            entries[name] = {
                "episode_name": name,
                "is_failure": bool(is_failure),
                "reason_label": str(reason_fields.get("reason_label") or reason),
                "reason_codes": reason_fields.get("reason_codes", []),
                "reason_labels": reason_fields.get("reason_labels", []),
                "reason_labels_zh": reason_fields.get("reason_labels_zh", []),
                "reason_labels_en": reason_fields.get("reason_labels_en", []),
                "quality_grade": quality_grade,
            }
    return entries


def load_manual_failure_file(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return normalise_manual_failure_entries(payload)


def write_manual_failure_file(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "episodes": [
            {
                "episode_name": str(item.get("episode_name") or name),
                "is_failure": bool(item.get("is_failure")),
                "reason_label": str(item.get("reason_label") or ""),
                "reason_codes": item.get("reason_codes") or [],
                "reason_labels_zh": item.get("reason_labels_zh") or item.get("reason_labels") or [],
                "reason_labels_en": item.get("reason_labels_en") or [],
                "quality_grade": normalise_quality_grade(item.get("quality_grade")) or ("F" if item.get("is_failure") else "A"),
            }
            for name, item in sorted(entries.items(), key=lambda pair: natural_sort_key(Path(pair[0])))
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def manual_failure_for_episode(
    episode_dir: Path,
    meta: dict[str, Any],
    manual_failures: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    keys = [
        episode_dir.name,
        str(meta.get("episode_name") or ""),
        str(meta.get("source_episode_name") or ""),
        str(meta.get("source_mcap_episode_name") or ""),
    ]
    source_files = meta.get("source_mcap_files")
    if isinstance(source_files, list):
        for item in source_files:
            path = Path(str(item))
            keys.extend([path.stem, path.name])
    for key in keys:
        if key and key in manual_failures:
            return manual_failures[key]
    return None


def manual_failure_aliases_for_episode(episode_dir: Path, meta: dict[str, Any], preferred: str = "") -> list[str]:
    keys = [
        preferred,
        episode_dir.name,
        str(meta.get("episode_name") or ""),
        str(meta.get("source_episode_name") or ""),
        str(meta.get("source_mcap_episode_name") or ""),
    ]
    source_files = meta.get("source_mcap_files")
    if isinstance(source_files, list):
        for item in source_files:
            path = Path(str(item))
            keys.extend([path.stem, path.name])
    unique: list[str] = []
    seen: set[str] = set()
    for key in keys:
        key = str(key or "").strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def update_episode_meta_manual_review(
    episode_dir: Path,
    quality_grade: str,
    reason_label: str,
    reason_codes: list[str],
) -> dict[str, Any]:
    meta_path = episode_meta_path(episode_dir)
    meta = load_json_file(meta_path)
    if not isinstance(meta, dict):
        meta = {}

    labels_zh = reason_labels_from_codes(reason_codes, "zh")
    labels_en = reason_labels_from_codes(reason_codes, "en")
    for label in split_reason_text(reason_label):
        if label not in labels_zh and label not in labels_en:
            labels_zh.append(label)
    reason_text = "；".join(labels_zh)

    collection_quality = meta.get("collection_quality")
    if not isinstance(collection_quality, dict):
        collection_quality = {}
    collection_quality.update(
        {
            "schema_version": collection_quality.get("schema_version", 1),
            "grade": quality_grade,
            "status": "rejected" if quality_grade == "F" else "accepted",
            "reason_codes": reason_codes,
            "reason_labels": labels_zh,
            "reason_labels_zh": labels_zh,
            "reason_labels_en": labels_en,
            "reason_note": reason_text,
            "source": "hdf5_replay_manual_review",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    meta["collection_quality"] = collection_quality
    meta["quality_grade"] = quality_grade
    meta["manual_quality_grade"] = quality_grade
    meta["manual_failure"] = quality_grade == "F"
    meta["reason_codes"] = reason_codes
    meta["reason_labels"] = labels_zh
    meta["reason_labels_zh"] = labels_zh
    meta["reason_labels_en"] = labels_en
    meta["quality_description"] = reason_text
    meta["manual_review_reason"] = reason_text if quality_grade in {"B", "F"} else reason_text
    meta["manual_failure_reason"] = reason_text if quality_grade == "F" else ""

    meta_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = meta_path.with_name(
        f".{meta_path.name}.manual_review_meta_tmp."
        f"{threading.get_ident()}.{datetime.now().timestamp():.0f}"
    )
    with tmp_path.open("w", encoding="utf-8") as output:
        output.write(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
        output.flush()
        os.fsync(output.fileno())
    tmp_path.replace(meta_path)
    return meta


def update_episode_meta_manual_frame_trim(
    episode_dir: Path,
    h5_info: dict[str, Any],
    trim_info: dict[str, Any],
    video_results: list[dict[str, Any]],
) -> dict[str, Any]:
    meta_path = episode_meta_path(episode_dir)
    meta = load_json_file(meta_path)
    if not isinstance(meta, dict):
        meta = {}

    meta["frame_count"] = h5_info["kept_frame_count"]
    meta["duration"] = h5_info["duration"]
    meta["first_timestamp_ns"] = h5_info["first_timestamp_ns"]
    meta["last_timestamp_ns"] = h5_info["last_timestamp_ns"]
    meta["inferred_state_fps"] = h5_info["inferred_fps"]
    meta["video_fps"] = h5_info["inferred_fps"]

    stored_trim_info = dict(trim_info)
    stored_trim_info.pop("source_to_new_frame", None)
    stored_trim_info.pop("keep_positions", None)
    meta["manual_frame_trim"] = stored_trim_info
    history = meta.get("manual_frame_trim_history")
    if not isinstance(history, list):
        history = []
    history.append(stored_trim_info)
    meta["manual_frame_trim_history"] = history
    remap_segments(meta, trim_info.get("source_to_new_frame", {}))
    remap_step_index(
        meta,
        list(trim_info.get("keep_positions") or []),
        int(h5_info["first_timestamp_ns"]),
        float(trim_info.get("target_fps") or h5_info.get("inferred_fps") or DEFAULT_FPS),
    )

    video_counts = {
        item["file"]: item["frame_count"]
        for item in video_results
        if item.get("status") == "trimmed" and "frame_count" in item
    }
    for item in meta.get("available_videos", []):
        if isinstance(item, dict) and item.get("file") in video_counts:
            item["frame_count"] = video_counts[item["file"]]

    meta_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = meta_path.with_name(
        f".{meta_path.name}.manual_frame_trim_meta_tmp."
        f"{threading.get_ident()}.{datetime.now().timestamp():.0f}"
    )
    with tmp_path.open("w", encoding="utf-8") as output:
        output.write(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
        output.flush()
        os.fsync(output.fileno())
    tmp_path.replace(meta_path)
    return meta


def dataset_to_list(group: Any, name: str, frame_idx: int, default: list[float] | None = None) -> list[float]:
    if name not in group:
        return list(default or [])
    value = group[name][()]
    if hasattr(value, "reshape"):
        value = value.reshape(-1)
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, list):
        return [float(item) for item in value]
    return [float(value)]


def numeric_meta_value(meta: dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = meta.get(key)
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return 0.0


def is_sim_episode_meta(meta: dict[str, Any]) -> bool:
    timestamp_policy = str(meta.get("timestamp_policy") or "").lower()
    if timestamp_policy.startswith("synthetic_frame_index"):
        return True
    source_hdf5 = str(meta.get("source_hdf5") or "").lower()
    return "robotwin" in source_hdf5 or "sim_data" in source_hdf5


def timestamp_delta_seconds(raw_value: Any, first_value: Any) -> float:
    value = float(raw_value)
    first = float(first_value)
    delta = value - first
    magnitude = max(abs(value), abs(first))
    if magnitude > 1e17:
        return delta / 1_000_000_000.0
    if magnitude > 1e14:
        return delta / 1_000_000.0
    if magnitude > 1e11:
        return delta / 1_000.0
    return delta


def fit_dim(values: list[float], dim: int) -> list[float]:
    if len(values) >= dim:
        return [float(value) for value in values[:dim]]
    return [float(value) for value in values] + [0.0] * (dim - len(values))


def g2_base_velocity(raw: list[float]) -> list[float]:
    if len(raw) >= 6:
        return [float(raw[0]), float(raw[1]), float(raw[5])]
    return fit_dim(raw, 3)


def g2_state_from_frame(group: Any) -> list[float]:
    joint = fit_dim(dataset_to_list(group, "state/joint/position", 0), 14)
    left_gripper = fit_dim(dataset_to_list(group, "state/left_effector/position", 0), 1)
    right_gripper = fit_dim(dataset_to_list(group, "state/right_effector/position", 0), 1)
    waist = fit_dim(dataset_to_list(group, "state/waist/position", 0), 5)
    base_position = fit_dim(dataset_to_list(group, "state/robot/position", 0), 3)
    orientation = fit_dim(dataset_to_list(group, "state/robot/orientation", 0), 4)
    return fit_dim(
        joint[:7]
        + left_gripper[:1]
        + joint[7:14]
        + right_gripper[:1]
        + waist
        + base_position
        + orientation[2:4],
        26,
    )


def g2_action_from_frame(group: Any) -> list[float]:
    joint = fit_dim(dataset_to_list(group, "action/joint/position", 0), 14)
    left_gripper = fit_dim(dataset_to_list(group, "action/left_effector/position", 0), 1)
    right_gripper = fit_dim(dataset_to_list(group, "action/right_effector/position", 0), 1)
    waist = fit_dim(dataset_to_list(group, "action/waist/position", 0), 5)
    base_velocity = g2_base_velocity(dataset_to_list(group, "action/robot/velocity", 0))
    return fit_dim(
        joint[:7]
        + left_gripper[:1]
        + joint[7:14]
        + right_gripper[:1]
        + waist
        + base_velocity,
        24,
    )


def g2_state_side_channel_from_frame(group: Any, joint_path: str, waist_path: str) -> list[float]:
    joint = fit_dim(dataset_to_list(group, joint_path, 0), 14)
    waist = fit_dim(dataset_to_list(group, waist_path, 0), 5)
    return fit_dim(
        joint[:7]
        + [0.0]
        + joint[7:14]
        + [0.0]
        + waist
        + [0.0, 0.0, 0.0, 0.0, 0.0],
        26,
    )


def aloha_action_from_frame(group: Any) -> list[float]:
    joint = dataset_to_list(group, "action/joint/position", 0)
    lift = fit_dim(dataset_to_list(group, "action/waist/position", 0), 1)
    base_velocity = fit_dim(dataset_to_list(group, "action/robot/velocity", 0), 3)
    return joint + lift[:1] + base_velocity[:3]


def aloha_state_from_frame(group: Any) -> list[float]:
    joint = dataset_to_list(group, "state/joint/position", 0)
    lift = fit_dim(dataset_to_list(group, "state/waist/position", 0), 1)
    base_state = fit_dim(dataset_to_list(group, "state/robot/base_state", 0), 6)
    if not any(base_state):
        pose2d = fit_dim(dataset_to_list(group, "state/robot/pose2d", 0), 3)
        velocity = fit_dim(dataset_to_list(group, "state/robot/velocity", 0), 3)
        base_state = pose2d[:3] + velocity[:3]
    return joint + lift[:1] + base_state[:6]


def infer_episode_type(h5_path: Path, requested_type: str) -> str:
    if requested_type != "auto":
        return requested_type
    if h5py is None:
        return "aloha"
    try:
        with h5py.File(h5_path, "r") as file_obj:
            if "timestamp/t" in file_obj and "observation/state/arm/position" in file_obj:
                return "zerith"
            source_format = str(file_obj.attrs.get("source_format", "")).lower()
    except Exception:
        return "aloha"
    return "g2" if "g2" in source_format else "aloha"


def display_rows_for_episode(episode_type: str, state_dim: int, action_dim: int) -> list[dict[str, Any]]:
    if episode_type == "zerith":
        return [
            {
                "label": label,
                "state_index": idx if idx < state_dim else None,
                "action_index": idx if idx < action_dim else None,
                "diff": idx < state_dim and idx < action_dim,
            }
            for idx, label in enumerate(ZERITH_LABELS)
        ]
    if episode_type == "g2":
        rows: list[dict[str, Any]] = []
        for idx, label in enumerate(G2_COMMON_LABELS):
            rows.append({"label": label, "state_index": idx, "action_index": idx, "diff": True})
        for idx in range(len(G2_COMMON_LABELS), len(G2_STATE_LABELS)):
            rows.append({"label": G2_STATE_LABELS[idx], "state_index": idx, "action_index": None, "diff": False})
        for idx in range(len(G2_COMMON_LABELS), len(G2_ACTION_LABELS)):
            rows.append({"label": G2_ACTION_LABELS[idx], "state_index": None, "action_index": idx, "diff": False})
        return rows
    if episode_type == "aloha":
        rows = []
        joint_count = min(len(ALOHA_JOINT_LABELS), max(state_dim, action_dim))
        for idx, label in enumerate(ALOHA_JOINT_LABELS[:joint_count]):
            rows.append(
                {
                    "label": label,
                    "state_index": idx if idx < state_dim else None,
                    "action_index": idx if idx < action_dim else None,
                    "diff": idx < state_dim and idx < action_dim,
                }
            )
        for label, state_idx, action_idx in ALOHA_EXTRA_DISPLAY_ROWS:
            has_state = state_idx is not None and state_idx < state_dim
            has_action = action_idx is not None and action_idx < action_dim
            if has_state or has_action:
                rows.append(
                    {
                        "label": label,
                        "state_index": state_idx if has_state else None,
                        "action_index": action_idx if has_action else None,
                        "diff": has_state and has_action,
                    }
                )
        return rows
    return [
        {
            "label": f"q{idx:02d}",
            "state_index": idx if idx < state_dim else None,
            "action_index": idx if idx < action_dim else None,
            "diff": idx < state_dim and idx < action_dim,
        }
        for idx in range(max(state_dim, action_dim))
    ]


def zerith_hdf5_rows(
    file_obj: Any,
    key: str,
    width: int,
    frame_count: int,
) -> list[list[float]]:
    if key not in file_obj:
        raise ValueError(f"{file_obj.filename}: missing {key}")
    dataset = file_obj[key]
    if tuple(dataset.shape) != (frame_count, width):
        raise ValueError(
            f"{file_obj.filename}: expected {key} shape ({frame_count}, {width}), got {dataset.shape}"
        )
    return [[float(value) for value in row] for row in dataset[...].tolist()]


def zerith_compose_rows(
    arm: list[list[float]],
    effector: list[list[float]],
    waist: list[list[float]],
    head: list[list[float]],
    base: list[list[float]],
) -> list[list[float]]:
    return [
        arm_row[:7]
        + effector_row[:1]
        + arm_row[7:14]
        + effector_row[1:2]
        + waist_row[:3]
        + head_row[:2]
        + base_row[:2]
        for arm_row, effector_row, waist_row, head_row, base_row in zip(
            arm, effector, waist, head, base
        )
    ]


def zerith_arm_side_channel(rows: list[list[float]]) -> list[list[float]]:
    return [row[:7] + [0.0] + row[7:14] + [0.0] + [0.0] * 7 for row in rows]


def load_zerith_h5(file_obj: Any) -> dict[str, Any]:
    if "timestamp/t" not in file_obj:
        raise ValueError(f"{file_obj.filename}: missing timestamp/t")
    raw_timestamps = [float(value) for value in file_obj["timestamp/t"][...].tolist()]
    frame_count = len(raw_timestamps)
    if frame_count <= 0:
        raise ValueError(f"{file_obj.filename}: empty timestamp/t")
    first_ts = raw_timestamps[0]
    timestamps = [timestamp_delta_seconds(value, first_ts) for value in raw_timestamps]

    state_arm = zerith_hdf5_rows(file_obj, "observation/state/arm/position", 14, frame_count)
    state_effector = zerith_hdf5_rows(file_obj, "observation/state/effector/position", 2, frame_count)
    state_waist = zerith_hdf5_rows(file_obj, "observation/state/waist/position", 3, frame_count)
    state_head = zerith_hdf5_rows(file_obj, "observation/state/head/position", 2, frame_count)
    state_base = zerith_hdf5_rows(file_obj, "observation/state/base/velocity", 2, frame_count)
    action_arm = zerith_hdf5_rows(file_obj, "action/arm/position", 14, frame_count)
    action_effector = zerith_hdf5_rows(file_obj, "action/effector/position", 2, frame_count)
    action_waist = zerith_hdf5_rows(file_obj, "action/waist/position", 3, frame_count)
    action_head = zerith_hdf5_rows(file_obj, "action/head/position", 2, frame_count)
    action_base = zerith_hdf5_rows(file_obj, "action/base/velocity", 2, frame_count)

    states = zerith_compose_rows(
        state_arm, state_effector, state_waist, state_head, state_base
    )
    actions = zerith_compose_rows(
        action_arm, action_effector, action_waist, action_head, action_base
    )
    velocity_rows = (
        zerith_hdf5_rows(file_obj, "observation/state/arm/velocity", 14, frame_count)
        if "observation/state/arm/velocity" in file_obj
        else [[0.0] * 14 for _ in range(frame_count)]
    )
    effort_rows = (
        zerith_hdf5_rows(file_obj, "observation/state/arm/torque", 14, frame_count)
        if "observation/state/arm/torque" in file_obj
        else [[0.0] * 14 for _ in range(frame_count)]
    )
    end_rows = (
        zerith_hdf5_rows(file_obj, "observation/state/end/position", 14, frame_count)
        if "observation/state/end/position" in file_obj
        else [[] for _ in range(frame_count)]
    )
    duration = timestamps[-1] if timestamps else 0.0
    inferred_fps = (frame_count - 1) / duration if frame_count > 1 and duration > 0 else 0.0
    return {
        "frame_count": frame_count,
        "duration": duration,
        "inferred_fps": inferred_fps,
        "timestamps": timestamps,
        "state_joint_position": states,
        "action_joint_position": actions,
        "state_joint_velocity": zerith_arm_side_channel(velocity_rows),
        "state_joint_effort": zerith_arm_side_channel(effort_rows),
        "state_end_position": end_rows,
        "action_robot_velocity": action_base,
        "display_rows": display_rows_for_episode("zerith", 23, 23),
    }


def load_h5(h5_path: Path, episode_type: str) -> dict[str, Any]:
    if h5py is None:
        raise RuntimeError("Missing dependency h5py. Install it with: pip install h5py")

    timestamps: list[float] = []
    state_joint_position: list[list[float]] = []
    action_joint_position: list[list[float]] = []
    state_joint_velocity: list[list[float]] = []
    state_joint_effort: list[list[float]] = []
    state_end_position: list[list[float]] = []
    action_robot_velocity: list[list[float]] = []

    with h5py.File(h5_path, "r") as file_obj:
        if episode_type == "zerith":
            return load_zerith_h5(file_obj)
        frame_indices = sorted(int(key) for key in file_obj.keys() if key.isdigit())
        if not frame_indices:
            raise ValueError(f"No frame groups found in {h5_path}")

        first_ts = file_obj[str(frame_indices[0])]["main_timestamp"][()]
        for frame_idx in frame_indices:
            group = file_obj[str(frame_idx)]
            timestamp_value = group["main_timestamp"][()]
            timestamps.append(timestamp_delta_seconds(timestamp_value, first_ts))
            if episode_type == "g2":
                state_joint_position.append(g2_state_from_frame(group))
                action_joint_position.append(g2_action_from_frame(group))
                state_joint_velocity.append(
                    g2_state_side_channel_from_frame(group, "state/joint/velocity", "state/waist/velocity")
                )
                state_joint_effort.append(
                    g2_state_side_channel_from_frame(group, "state/joint/effort", "state/waist/effort")
                )
            else:
                state_joint_position.append(aloha_state_from_frame(group))
                action_joint_position.append(aloha_action_from_frame(group))
                state_joint_velocity.append(dataset_to_list(group, "state/joint/velocity", frame_idx))
                state_joint_effort.append(dataset_to_list(group, "state/joint/effort", frame_idx))
            state_end_position.append(dataset_to_list(group, "state/end/position", frame_idx))
            action_robot_velocity.append(dataset_to_list(group, "action/robot/velocity", frame_idx))

    state_dim = len(state_joint_position[0]) if state_joint_position else 0
    action_dim = len(action_joint_position[0]) if action_joint_position else 0
    duration = timestamps[-1] if timestamps else 0.0
    inferred_fps = (len(timestamps) - 1) / duration if len(timestamps) > 1 and duration > 0 else 0.0
    return {
        "frame_count": len(timestamps),
        "duration": duration,
        "inferred_fps": inferred_fps,
        "timestamps": timestamps,
        "state_joint_position": state_joint_position,
        "action_joint_position": action_joint_position,
        "state_joint_velocity": state_joint_velocity,
        "state_joint_effort": state_joint_effort,
        "state_end_position": state_end_position,
        "action_robot_velocity": action_robot_velocity,
        "display_rows": display_rows_for_episode(episode_type, state_dim, action_dim),
    }


def read_h5_summary(h5_path: Path) -> dict[str, Any]:
    if h5py is None:
        raise RuntimeError("Missing dependency h5py. Install it with: pip install h5py")
    with h5py.File(h5_path, "r") as file_obj:
        if "timestamp/t" in file_obj:
            timestamps = file_obj["timestamp/t"]
            frame_count = int(timestamps.shape[0])
            if frame_count <= 0:
                raise ValueError(f"No timestamps found in {h5_path}")
            first_ts = timestamps[0]
            last_ts = timestamps[-1]
            duration = max(0.0, timestamp_delta_seconds(last_ts, first_ts))
            return {
                "frame_count": frame_count,
                "duration": duration,
                "inferred_fps": (
                    (frame_count - 1) / duration
                    if frame_count > 1 and duration > 0
                    else 0.0
                ),
            }
        frame_indices = sorted(int(key) for key in file_obj.keys() if key.isdigit())
        if not frame_indices:
            raise ValueError(f"No frame groups found in {h5_path}")
        first_ts = file_obj[str(frame_indices[0])]["main_timestamp"][()]
        last_ts = file_obj[str(frame_indices[-1])]["main_timestamp"][()]
        duration = max(0.0, timestamp_delta_seconds(last_ts, first_ts))
    return {
        "frame_count": len(frame_indices),
        "duration": duration,
        "inferred_fps": (
            (len(frame_indices) - 1) / duration
            if len(frame_indices) > 1 and duration > 0
            else 0.0
        ),
    }


def resolve_replay_fps(meta: dict[str, Any], h5_summary: dict[str, Any], fps_override: float) -> tuple[float, float]:
    meta_fps = numeric_meta_value(meta, "state_fps", "fps", "inferred_state_fps")
    inferred_fps = float(h5_summary.get("inferred_fps") or 0.0)
    state_fps = meta_fps if is_sim_episode_meta(meta) and meta_fps > 0 else (inferred_fps or meta_fps)
    video_fps = numeric_meta_value(meta, "video_fps") or state_fps or DEFAULT_FPS
    if fps_override and fps_override > 0:
        state_fps = fps_override
        video_fps = fps_override
    if state_fps <= 0:
        state_fps = video_fps if video_fps > 0 else DEFAULT_FPS
    if video_fps <= 0:
        video_fps = DEFAULT_FPS
    return state_fps, video_fps


def available_video_infos(episode_dir: Path) -> list[dict[str, Any]]:
    available_videos = []
    videos_dir = episode_dir / "videos"
    candidates = [
        (key, label, filename, filename)
        for key, label, filename in VIDEO_ORDER
    ] + [
        ("cam_high", "Head", "cam_high.mp4", "rs/cam_high.mp4"),
        ("cam_left_wrist", "Left Hand", "cam_left_wrist.mp4", "rs/cam_left_wrist.mp4"),
        ("cam_right_wrist", "Right Hand", "cam_right_wrist.mp4", "rs/cam_right_wrist.mp4"),
    ]
    for key, label, filename, relative_path in candidates:
        path = videos_dir / relative_path
        if path.is_file():
            available_videos.append(
                {
                    "key": key,
                    "label": label,
                    "file": filename,
                    "relative_path": relative_path,
                }
            )
    if not available_videos:
        raise FileNotFoundError(f"No supported videos found under {videos_dir}")
    return available_videos


def episode_video_path(episode_dir: Path, video_info: dict[str, Any]) -> Path:
    relative_path = str(video_info.get("relative_path") or video_info["file"])
    return episode_dir / "videos" / relative_path


def load_episode_record(
    episode_dir: Path,
    fps_override: float,
    requested_type: str,
    qc_summary: dict[str, dict[str, str]],
    manual_failures: dict[str, dict[str, Any]],
) -> EpisodeRecord:
    episode_dir = episode_dir.expanduser().resolve()
    h5_path = episode_h5_path(episode_dir)
    if not h5_path.is_file():
        raise FileNotFoundError(f"Missing HDF5 file: {h5_path}")

    meta = read_meta(episode_dir)
    episode_type = infer_episode_type(h5_path, requested_type)
    qc_info = qc_info_for_episode(episode_dir, meta, qc_summary, manual_failures)
    h5_summary = read_h5_summary(h5_path)
    fps, video_fps = resolve_replay_fps(meta, h5_summary, fps_override)

    return EpisodeRecord(
        episode_dir=episode_dir,
        h5_path=h5_path,
        meta=meta,
        frame_count=int(h5_summary["frame_count"]),
        duration=float(h5_summary["duration"]),
        fps=fps,
        video_fps=video_fps,
        fps_override=fps_override,
        episode_type=episode_type,
        available_videos=available_video_infos(episode_dir),
        qc_status=qc_info.get("status", ""),
        qc_remark=qc_info.get("remark", ""),
        quality_grade=normalise_quality_grade(qc_info.get("quality_grade")) or "A",
        manual_review_reason=str(qc_info.get("manual_review_reason") or ""),
    )


def load_episode(
    episode_dir: Path,
    fps_override: float,
    episode_type: str,
    qc_status: str = "",
    qc_remark: str = "",
    quality_grade: str = "A",
    manual_review_reason: str = "",
) -> EpisodeData:
    episode_dir = episode_dir.expanduser().resolve()
    h5_path = episode_h5_path(episode_dir)
    if not h5_path.is_file():
        raise FileNotFoundError(f"Missing HDF5 file: {h5_path}")

    meta = read_meta(episode_dir)
    resolved_type = infer_episode_type(h5_path, episode_type)
    h5_data = load_h5(h5_path, resolved_type)
    fps, video_fps = resolve_replay_fps(meta, h5_data, fps_override)

    return EpisodeData(
        episode_dir=episode_dir,
        h5_path=h5_path,
        meta=meta,
        frame_count=int(h5_data["frame_count"]),
        duration=float(h5_data["duration"]),
        fps=fps,
        video_fps=video_fps,
        episode_type=resolved_type,
        timestamps=h5_data["timestamps"],
        state_joint_position=h5_data["state_joint_position"],
        action_joint_position=h5_data["action_joint_position"],
        state_joint_velocity=h5_data["state_joint_velocity"],
        state_joint_effort=h5_data["state_joint_effort"],
        state_end_position=h5_data["state_end_position"],
        action_robot_velocity=h5_data["action_robot_velocity"],
        display_rows=h5_data["display_rows"],
        available_videos=available_video_infos(episode_dir),
        qc_status=qc_status,
        qc_remark=qc_remark,
        quality_grade=normalise_quality_grade(quality_grade) or "A",
        manual_review_reason=manual_review_reason,
    )


def load_episodes(
    input_path: Path,
    fps_override: float,
    requested_type: str,
    qc_summary: dict[str, dict[str, str]],
    manual_failures: dict[str, dict[str, Any]],
) -> list[EpisodeRecord]:
    episodes = []
    for episode_dir in discover_episode_dirs(input_path):
        try:
            episodes.append(
                load_episode_record(
                    episode_dir,
                    fps_override,
                    requested_type,
                    qc_summary,
                    manual_failures,
                )
            )
        except Exception as exc:
            print(f"[WARN] skip {episode_dir}: {exc}", flush=True)
    if not episodes:
        raise RuntimeError(f"No playable episodes found under {input_path}")
    return episodes


def episode_name(data: EpisodeRecord | EpisodeData) -> str:
    return str(data.meta.get("episode_name") or data.episode_dir.name)


def episode_summary(data: EpisodeRecord | EpisodeData, index: int) -> dict[str, Any]:
    return {
        "index": index,
        "name": episode_name(data),
        "episode_dir": str(data.episode_dir),
        "frame_count": data.frame_count,
        "duration": data.duration,
        "fps": data.fps,
        "video_fps": data.video_fps,
        "episode_type": data.episode_type,
        "qc_status": data.qc_status,
        "qc_remark": data.qc_remark,
        "quality_grade": data.quality_grade,
        "manual_review_reason": data.manual_review_reason,
        "manual_failure": data.quality_grade == "F" or data.qc_status == "采集失败",
        "manual_failure_reason": data.manual_review_reason if data.quality_grade == "F" else "",
        "videos": [item["file"] for item in data.available_videos],
    }


def normalised_subtask_segments(meta: dict[str, Any]) -> list[dict[str, Any]]:
    raw_segments = meta.get("subtask_segments")
    if isinstance(raw_segments, list) and raw_segments:
        return [item for item in raw_segments if isinstance(item, dict)]

    raw_segments = meta.get("segment_instructions")
    if not isinstance(raw_segments, list):
        return []
    segments: list[dict[str, Any]] = []
    for item in raw_segments:
        if not isinstance(item, dict):
            continue
        try:
            start = int(item.get("start") if item.get("start") is not None else item.get("start_time"))
            end = int(item.get("end") if item.get("end") is not None else item.get("end_time"))
        except (TypeError, ValueError):
            continue
        description_en = item.get("description_en")
        description_zh = item.get("description_zh")
        if not isinstance(description_en, list):
            description_en = []
        if not isinstance(description_zh, list):
            description_zh = []
        segment = {
            "start": start,
            "end": end,
            "subtask": " ".join(str(value).strip() for value in description_en if str(value).strip())
            or " ".join(str(value).strip() for value in description_zh if str(value).strip()),
            "description_en": description_en,
            "description_zh": description_zh,
        }
        if item.get("id"):
            segment["id"] = str(item["id"])
        segments.append(segment)
    return segments


def episode_payload(data: EpisodeData, index: int) -> dict[str, Any]:
    state_dim = len(data.state_joint_position[0]) if data.state_joint_position else 0
    action_dim = len(data.action_joint_position[0]) if data.action_joint_position else 0
    tasks = data.meta.get("tasks")
    if not isinstance(tasks, list):
        tasks = [data.meta.get("task")] if data.meta.get("task") else []
    subtask_segments = normalised_subtask_segments(data.meta)
    stage_info = collector_stage_info(
        data.episode_dir, data.h5_path, data.frame_count
    )
    videos = []
    for item in data.available_videos:
        video_item = dict(item)
        video_path = episode_video_path(data.episode_dir, item)
        version = video_path.stat().st_mtime_ns if video_path.is_file() else 0
        video_item["url"] = f"/video/{index}/{item['file']}?v={version}"
        videos.append(video_item)
    return {
        "index": index,
        "name": episode_name(data),
        "episode_dir": str(data.episode_dir),
        "h5_path": str(data.h5_path),
        "meta": data.meta,
        "task": str(data.meta.get("task") or (tasks[0] if tasks else "")),
        "tasks": [str(item) for item in tasks if str(item).strip()],
        "subtask_segments": subtask_segments,
        "stage_info": stage_info,
        "frame_count": data.frame_count,
        "duration": data.duration,
        "fps": data.fps,
        "video_fps": data.video_fps,
        "episode_type": data.episode_type,
        "qc_status": data.qc_status,
        "qc_remark": data.qc_remark,
        "quality_grade": data.quality_grade,
        "manual_review_reason": data.manual_review_reason,
        "manual_failure": data.quality_grade == "F" or data.qc_status == "采集失败",
        "manual_failure_reason": data.manual_review_reason if data.quality_grade == "F" else "",
        "timestamps": data.timestamps,
        "state_dim": state_dim,
        "action_dim": action_dim,
        "videos": videos,
        "rows": data.display_rows,
        "state_joint_position": data.state_joint_position,
        "action_joint_position": data.action_joint_position,
        "state_joint_velocity": data.state_joint_velocity,
        "state_joint_effort": data.state_joint_effort,
        "state_end_position": data.state_end_position,
        "action_robot_velocity": data.action_robot_velocity,
    }


def parse_range(header_value: str, file_size: int) -> tuple[int, int] | None:
    if not header_value.startswith("bytes="):
        return None
    raw = header_value.split("=", 1)[1].split(",", 1)[0].strip()
    if "-" not in raw:
        return None
    start_text, end_text = raw.split("-", 1)
    try:
        if start_text:
            start = int(start_text)
            end = int(end_text) if end_text else file_size - 1
        else:
            suffix = int(end_text)
            start = max(file_size - suffix, 0)
            end = file_size - 1
    except ValueError:
        return None
    if start < 0 or end < start or start >= file_size:
        return None
    return start, min(end, file_size - 1)


def make_handler(
    episodes: list[EpisodeRecord],
    manual_failure_path: Path,
) -> type[BaseHTTPRequestHandler]:
    index_html = build_index_html().encode("utf-8")
    payload_jsons: dict[int, bytes] = {}
    payload_lock = threading.Lock()
    manual_lock = threading.Lock()
    video_paths = {
        (idx, item["file"]): episode_video_path(data.episode_dir, item)
        for idx, data in enumerate(episodes)
        for item in data.available_videos
    }

    def episodes_payload(initial_index: int = 0) -> bytes:
        return json.dumps(
            {
                "initial_index": initial_index,
                "episodes": [episode_summary(data, idx) for idx, data in enumerate(episodes)],
            },
            ensure_ascii=False,
        ).encode("utf-8")

    def payload_for_index(idx: int) -> bytes:
        with payload_lock:
            payload = payload_jsons.get(idx)
            if payload is not None:
                return payload
        record = episodes[idx]
        data = load_episode(
            record.episode_dir,
            record.fps_override,
            record.episode_type,
            qc_status=record.qc_status,
            qc_remark=record.qc_remark,
            quality_grade=record.quality_grade,
            manual_review_reason=record.manual_review_reason,
        )
        payload = json.dumps(episode_payload(data, idx), ensure_ascii=False).encode("utf-8")
        with payload_lock:
            payload_jsons[idx] = payload
        return payload

    def save_manual_failure(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            idx = int(payload.get("episode_index", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("Bad episode index") from exc
        if idx < 0 or idx >= len(episodes):
            raise IndexError("Episode Not Found")
        record = episodes[idx]
        name = str(payload.get("episode_name") or episode_name(record) or record.episode_dir.name).strip()
        if not name:
            raise ValueError("Missing episode_name")
        quality_grade = normalise_quality_grade(payload.get("quality_grade"))
        if not quality_grade:
            is_failure_payload = manual_failure_truthy(payload.get("is_failure"), default=False)
            quality_grade = "F" if is_failure_payload else "A"
        is_failure = quality_grade == "F"
        reason = str(payload.get("reason_label") or "").strip()
        if quality_grade not in {"B", "F"}:
            reason = ""
        if quality_grade == "F" and not reason:
            reason = "人工标注采集失败"
        reason_fields = manual_reason_fields(reason, payload.get("reason_codes"))
        reason = str(reason_fields.get("reason_label") or reason)
        reason_codes = list(reason_fields.get("reason_codes") or [])
        aliases = manual_failure_aliases_for_episode(record.episode_dir, record.meta, name)
        with manual_lock:
            entries = load_manual_failure_file(manual_failure_path)
            for alias in aliases:
                entries[alias] = {
                    "episode_name": alias,
                    "is_failure": is_failure,
                    "reason_label": reason,
                    "reason_codes": reason_codes,
                    "reason_labels": reason_fields.get("reason_labels", []),
                    "reason_labels_zh": reason_fields.get("reason_labels_zh", []),
                    "reason_labels_en": reason_fields.get("reason_labels_en", []),
                    "quality_grade": quality_grade,
                }
            write_manual_failure_file(manual_failure_path, entries)
            record.meta = update_episode_meta_manual_review(record.episode_dir, quality_grade, reason, reason_codes)
        if is_failure:
            record.qc_status = "采集失败"
            record.qc_remark = reason
        else:
            record.qc_status = "采集成功"
            record.qc_remark = reason if quality_grade == "B" else ""
        record.quality_grade = quality_grade
        record.manual_review_reason = reason if quality_grade in {"B", "F"} else ""
        with payload_lock:
            payload_jsons.pop(idx, None)
        return {
            "ok": True,
            "episode_index": idx,
            "episode_name": name,
            "aliases": aliases,
            "is_failure": is_failure,
            "reason_label": reason,
            "quality_grade": quality_grade,
            "manual_review_reason": record.manual_review_reason,
            "reason_codes": reason_codes,
            "reason_labels_zh": reason_fields.get("reason_labels_zh", []),
            "reason_labels_en": reason_fields.get("reason_labels_en", []),
            "manual_failure_json": str(manual_failure_path),
            "episode_meta": str(episode_meta_path(record.episode_dir)),
            "qc_status": record.qc_status,
            "qc_remark": record.qc_remark,
        }

    def save_stage_boundary(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            idx = strict_integer(payload.get("episode_index"), "episode_index")
        except ValueError as exc:
            raise ValueError(f"Bad episode index: {exc}") from exc
        if idx < 0 or idx >= len(episodes):
            raise IndexError("Episode Not Found")
        record = episodes[idx]
        result = update_collector_stage_boundary(
            record.episode_dir,
            record.h5_path,
            payload.get("stage2_start_frame"),
            payload.get("frame_count"),
            payload.get("episode_dir"),
            payload.get("h5_path"),
            payload.get("expected_stage2_start_frame"),
            payload.get("expected_sidecar_stage2_start_frame"),
        )
        record.meta = read_meta(record.episode_dir)
        with payload_lock:
            payload_jsons.pop(idx, None)
        result.update(
            {
                "episode_index": idx,
                "episode_name": episode_name(record),
            }
        )
        return result

    def delete_frame_range(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            idx = int(payload.get("episode_index", 0))
            start_frame = int(payload.get("start_frame"))
            end_frame = int(payload.get("end_frame"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Bad frame range") from exc
        if idx < 0 or idx >= len(episodes):
            raise IndexError("Episode Not Found")
        record = episodes[idx]
        with episode_lock(record.episode_dir):
            recover_frame_trim_transaction(record.episode_dir)
            frame_count = hdf5_frame_count(record.h5_path)
            if frame_count <= 0:
                raise ValueError(f"No HDF5 frames in {record.h5_path}")
            if start_frame < 0 or end_frame < 0 or start_frame > end_frame or end_frame >= frame_count:
                raise ValueError(f"Frame range must satisfy 0 <= start <= end < {frame_count}")
            remove_count = end_frame - start_frame + 1
            if remove_count >= frame_count:
                raise ValueError("Cannot delete all frames")

            keep_positions = [pos for pos in range(frame_count) if pos < start_frame or pos > end_frame]
            source_to_new = {source: new for new, source in enumerate(keep_positions)}
            target_fps = float(record.video_fps or record.fps or DEFAULT_FPS)
            if target_fps <= 0:
                target_fps = DEFAULT_FPS
            trim_info = {
                "enabled": True,
                "status": "trimmed",
                "source": "hdf5_replay_manual_frame_delete",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "target_fps": target_fps,
                "delete_ranges": [
                    {
                        "start_frame": start_frame,
                        "end_frame": end_frame,
                        "source_frame_count": remove_count,
                    }
                ],
                "source_to_new_frame": source_to_new,
                "keep_positions": keep_positions,
            }

            video_results: list[dict[str, Any]] = []
            video_paths_to_trim = trim_episode_video_paths(record.episode_dir)
            meta_path = episode_meta_path(record.episode_dir)
            backup_paths = [record.h5_path, *video_paths_to_trim]
            if meta_path.is_file():
                backup_paths.append(meta_path)
            with frame_trim_transaction(
                record.episode_dir,
                backup_paths,
                kind="manual_frame_trim",
            ) as transaction:
                h5_info = trim_hdf5(
                    record.h5_path,
                    keep_positions,
                    target_fps,
                    dry_run=False,
                    attr_prefix="manual_frame_trim",
                )
                trim_info.update(
                    {
                        "original_frame_count": h5_info["original_frame_count"],
                        "kept_frame_count": h5_info["kept_frame_count"],
                        "removed_frame_count": h5_info["removed_frame_count"],
                    }
                )
                for video_path in video_paths_to_trim:
                    video_results.append(trim_video(video_path, keep_positions, target_fps, dry_run=False))
                record.meta = update_episode_meta_manual_frame_trim(
                    record.episode_dir, h5_info, trim_info, video_results
                )
                validate_no_partial_trim(record.episode_dir, record.h5_path)
                transaction.commit()
            h5_summary = read_h5_summary(record.h5_path)
            record.frame_count = int(h5_summary["frame_count"])
            record.duration = float(h5_summary["duration"])
            record.fps, record.video_fps = resolve_replay_fps(record.meta, h5_summary, record.fps_override)
            record.available_videos = available_video_infos(record.episode_dir)
            for key in [key for key in list(video_paths) if key[0] == idx]:
                video_paths.pop(key, None)
            for item in record.available_videos:
                video_paths[(idx, item["file"])] = episode_video_path(record.episode_dir, item)
            with payload_lock:
                payload_jsons.pop(idx, None)

        return {
            "ok": True,
            "episode_index": idx,
            "episode_name": episode_name(record),
            "episode_dir": str(record.episode_dir),
            "hdf5": str(record.h5_path),
            "start_frame": start_frame,
            "end_frame": end_frame,
            "original_frame_count": h5_info["original_frame_count"],
            "kept_frame_count": h5_info["kept_frame_count"],
            "removed_frame_count": h5_info["removed_frame_count"],
            "fps": record.fps,
            "video_fps": record.video_fps,
            "videos": video_results,
        }

    class ReplayHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self._dispatch(send_body=True)

        def do_HEAD(self) -> None:  # noqa: N802
            self._dispatch(send_body=False)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path not in {
                "/api/manual-failure",
                "/api/frame-delete",
                "/api/stage-boundary",
            }:
                self.send_error(404, "Not Found")
                return
            try:
                payload = self._read_json()
                if parsed.path == "/api/manual-failure":
                    result = save_manual_failure(payload)
                elif parsed.path == "/api/stage-boundary":
                    result = save_stage_boundary(payload)
                else:
                    result = delete_frame_range(payload)
            except IndexError as exc:
                self._write_json({"error": str(exc)}, status=404)
                return
            except Exception as exc:
                self._write_json({"error": str(exc)}, status=400)
                return
            self._write_json(result)

        def _dispatch(self, send_body: bool) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._write_bytes(index_html, "text/html; charset=utf-8", send_body)
                return
            if parsed.path == "/api/episodes":
                self._write_bytes(episodes_payload(), "application/json; charset=utf-8", send_body)
                return
            if parsed.path == "/api/episode":
                params = parse_qs(parsed.query)
                try:
                    idx = int(params.get("index", ["0"])[0])
                except ValueError:
                    self.send_error(400, "Bad episode index")
                    return
                if idx < 0 or idx >= len(episodes):
                    self.send_error(404, "Episode Not Found")
                    return
                try:
                    payload = payload_for_index(idx)
                except Exception as exc:
                    self.send_error(500, f"Load Episode Failed: {exc}")
                    return
                self._write_bytes(payload, "application/json; charset=utf-8", send_body)
                return
            if parsed.path.startswith("/video/"):
                parts = parsed.path.split("/")
                try:
                    if len(parts) >= 4:
                        idx = int(parts[2])
                        filename = unquote(parts[3])
                    else:
                        idx = 0
                        filename = unquote(parts[-1])
                except ValueError:
                    self.send_error(400, "Bad episode index")
                    return
                video_path = video_paths.get((idx, filename))
                if video_path is None:
                    self.send_error(404, "Video Not Found")
                    return
                self._serve_file(video_path, send_body)
                return
            self.send_error(404, "Not Found")

        def _serve_file(self, path: Path, send_body: bool) -> None:
            file_size = path.stat().st_size
            content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            range_header = self.headers.get("Range", "")
            byte_range = parse_range(range_header, file_size) if range_header else None
            if byte_range is None:
                start, end = 0, file_size - 1
                self.send_response(200)
            else:
                start, end = byte_range
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            content_length = end - start + 1
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(content_length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if not send_body:
                return
            with path.open("rb") as file_obj:
                file_obj.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk = file_obj.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    remaining -= len(chunk)

        def _write_bytes(self, body: bytes, content_type: str, send_body: bool = True) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw) if raw.strip() else {}

        def _write_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    return ReplayHandler


def build_index_html() -> str:
    return r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>ICRA Episode Replay</title>
  <style>
    :root {
      --bg: #101214;
      --panel: #191d21;
      --panel-2: #20262b;
      --border: #343b42;
      --text: #eef2f5;
      --muted: #9aa6b2;
      --state: #42c2ff;
      --action: #f3b34c;
      --green: #67d391;
      --red: #ff6f61;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    }
    .app {
      height: 100vh;
      display: grid;
      grid-template-columns: minmax(460px, 0.95fr) minmax(640px, 1.05fr);
      grid-template-rows: minmax(0, 1fr);
      gap: 10px;
      padding: 10px;
      overflow: hidden;
    }
    .video-grid {
      min-height: 0;
      display: grid;
      grid-template-rows: minmax(0, 1fr) minmax(0, 1fr);
      gap: 10px;
    }
    .stereo-row {
      min-height: 0;
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }
    .card, .panel {
      min-width: 0;
      min-height: 0;
      border: 1px solid var(--border);
      background: var(--panel);
      overflow: hidden;
    }
    .card {
      display: flex;
      flex-direction: column;
      border-radius: 8px;
    }
    .card-head, .panel-head {
      height: 34px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 0 10px;
      border-bottom: 1px solid var(--border);
      background: var(--panel-2);
      color: var(--muted);
      font-size: 13px;
    }
    .episode-select {
      flex: 1;
      max-width: none;
      min-width: 120px;
    }
    video {
      display: block;
      width: 100%;
      height: calc(100% - 34px);
      object-fit: contain;
      background: #050607;
    }
    .panel {
      display: grid;
      grid-template-rows: auto auto auto auto auto minmax(0, 1fr) minmax(0, 1fr);
      border-radius: 8px;
    }
    .controls {
      display: grid;
      grid-template-columns: auto auto minmax(160px, 1fr) auto auto;
      gap: 8px;
      align-items: center;
      padding: 10px;
      border-bottom: 1px solid var(--border);
      background: #15191d;
    }
    button, select {
      height: 32px;
      border: 1px solid var(--border);
      background: var(--panel-2);
      color: var(--text);
      border-radius: 6px;
      padding: 0 10px;
      font-size: 13px;
    }
    button { cursor: pointer; min-width: 40px; }
    button:hover, select:hover { border-color: #53606b; }
    button:disabled { opacity: .45; cursor: not-allowed; }
    #title {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .episode-nav {
      display: flex;
      align-items: center;
      gap: 6px;
      flex: 1;
      max-width: 460px;
      min-width: 260px;
    }
    .episode-nav button {
      height: 28px;
      min-width: 58px;
      padding: 0 8px;
      font-size: 12px;
    }
    input[type=range] { width: 100%; }
    .metrics {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 1px;
      background: var(--border);
      border-bottom: 1px solid var(--border);
    }
    .qc-strip {
      display: grid;
      gap: 6px;
      padding: 8px 10px;
      border-bottom: 1px solid var(--border);
      background: #15191d;
      font-size: 13px;
    }
    .qc-line {
      display: grid;
      grid-template-columns: 64px minmax(0, 1fr);
      gap: 8px;
      align-items: center;
      min-width: 0;
    }
    .qc-label {
      color: var(--muted);
      white-space: nowrap;
    }
    .qc-remark {
      color: #d3d9df;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .failure-controls {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      min-width: 0;
    }
    .failure-controls > select, .failure-controls > button {
      height: 30px;
    }
    .cut-controls input[type="number"], .stage-controls input[type="number"] {
      width: 96px;
      height: 30px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: #111417;
      color: var(--text);
      padding: 0 8px;
    }
    .stage-controls {
      align-items: center;
    }
    .stage-range-text {
      color: #d3d9df;
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      font-size: 12px;
      white-space: normal;
    }
    .stage-warning { color: var(--action); }
    .qc-line.hidden { display: none; }
    .review-options {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 6px;
      min-width: 0;
      flex: 1;
    }
    .review-option {
      display: inline-flex;
      align-items: center;
      gap: 3px;
      min-width: 0;
      color: #d3d9df;
      font-size: 12px;
      white-space: nowrap;
    }
    .review-option input {
      width: 14px;
      height: 14px;
      min-height: 0;
      padding: 0;
    }
    .review-save-state {
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }
    .failure-controls input[type="text"] {
      flex: 1;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: #111417;
      color: var(--text);
      padding: 0 8px;
    }
    .failure-controls .hidden { display: none; }
    #taskText, #subtaskText {
      white-space: normal;
      overflow: visible;
      overflow-wrap: anywhere;
      text-overflow: clip;
      line-height: 1.35;
    }
    .status-badge {
      justify-self: start;
      min-width: 48px;
      padding: 2px 8px;
      border: 1px solid var(--border);
      border-radius: 999px;
      font-weight: 600;
      text-align: center;
    }
    .status-keep { color: var(--green); border-color: rgba(103, 211, 145, 0.55); }
    .status-fix { color: var(--action); border-color: rgba(243, 179, 76, 0.6); }
    .status-delete { color: var(--red); border-color: rgba(255, 111, 97, 0.6); }
    .status-empty { color: var(--muted); }
    .metric {
      background: var(--panel);
      padding: 8px 10px;
      min-width: 0;
    }
    .metric .label { color: var(--muted); font-size: 12px; }
    .metric .value {
      margin-top: 3px;
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      font-size: 13px;
    }
    .chart-wrap {
      position: relative;
      min-height: 0;
      overflow-x: hidden;
      overflow-y: auto;
      background: #111417;
    }
    canvas { display: block; width: 100%; min-height: 100%; }
    .legend {
      position: absolute;
      top: 8px;
      right: 10px;
      display: flex;
      gap: 12px;
      align-items: center;
      color: var(--muted);
      font-size: 12px;
      pointer-events: none;
      background: rgba(17, 20, 23, 0.82);
      padding: 4px 6px;
      border: 1px solid var(--border);
      border-radius: 6px;
    }
    .dot { width: 10px; height: 3px; display: inline-block; vertical-align: middle; margin-right: 5px; }
    .state-dot { background: var(--state); }
    .action-dot { background: var(--action); }
    .table-wrap {
      min-height: 0;
      overflow-x: auto;
      overflow-y: auto;
      border-top: 1px solid var(--border);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    }
    th, td {
      padding: 5px 8px;
      text-align: right;
      border-bottom: 1px solid #252b31;
      white-space: nowrap;
    }
    th {
      position: sticky;
      top: 0;
      background: var(--panel-2);
      color: var(--muted);
      z-index: 1;
    }
    th:first-child, td:first-child { text-align: left; }
    @media (max-width: 980px) {
      .app { grid-template-columns: 1fr; grid-template-rows: 52vh minmax(760px, 1fr); }
      .panel { grid-template-rows: auto auto auto auto auto minmax(0, 1fr) minmax(0, 1fr); }
      .chart-wrap { min-height: 0; }
      .controls { grid-template-columns: auto auto minmax(120px, 1fr); }
      .controls select, .controls .time-readout { display: none; }
      .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .qc-remark { white-space: normal; }
      .episode-nav { max-width: none; min-width: 0; }
      .episode-nav button { min-width: 50px; }
    }
  </style>
</head>
<body class="noda-dark">
  <main class="app">
    <section class="video-grid" id="videoGrid"></section>
    <aside class="panel">
      <div id="raw-review-filter"></div>
      <div class="panel-head">
        <span id="title">ICRA Episode Replay</span>
        <div class="episode-nav">
          <button id="prevEpisodeBtn" title="上一条 episode">上一条</button>
          <select class="episode-select" id="episodeSelect" title="Episode"></select>
          <button id="nextEpisodeBtn" title="下一条 episode">下一条</button>
        </div>
        <span id="stateLabel"></span>
      </div>
      <div class="controls">
        <button id="playBtn" title="Play/Pause">▶</button>
        <button id="prevBtn" title="Previous frame">‹</button>
        <input id="frameSlider" type="range" min="0" max="0" value="0" />
        <button id="nextBtn" title="Next frame">›</button>
        <select id="speedSelect" title="Playback speed">
          <option value="0.25">0.25x</option>
          <option value="0.5">0.5x</option>
          <option value="1" selected>1x</option>
          <option value="2">2x</option>
          <option value="4">4x</option>
        </select>
      </div>
      <div class="metrics">
        <div class="metric"><div class="label">Frame</div><div class="value" id="frameMetric">0 / 0</div></div>
        <div class="metric"><div class="label">Time</div><div class="value" id="timeMetric">0.000s</div></div>
        <div class="metric"><div class="label">FPS</div><div class="value" id="fpsMetric">0</div></div>
        <div class="metric"><div class="label">Data</div><div class="value" id="dataMetric"></div></div>
      </div>
      <div class="qc-strip">
        <div class="qc-line"><span class="qc-label">任务</span><span class="qc-remark" id="taskText"></span></div>
        <div class="qc-line"><span class="qc-label">当前阶段</span><span class="qc-remark" id="subtaskText"></span></div>
        <div class="qc-line" id="stageBoundaryLine">
          <span class="qc-label">阶段分界</span>
          <span class="failure-controls stage-controls">
            <span class="stage-range-text" id="stageRangeText"></span>
            <input id="stage2StartFrame" type="number" min="1" step="1" placeholder="阶段 2 起始帧" title="阶段 2 起始帧（0-based）" />
            <button id="useCurrentStageFrameBtn" type="button">使用当前帧</button>
            <button id="applyStageBoundaryBtn" type="button">应用阶段订正</button>
            <span id="stageBoundaryState" class="review-save-state"></span>
          </span>
        </div>
        <div class="qc-line" hidden><span class="qc-label">状态</span><span class="status-badge status-empty" id="qcStatus">未提供</span></div>
        <div class="qc-line" hidden><span class="qc-label">备注</span><span class="qc-remark" id="qcRemark"></span></div>
        <div id="raw-grade-bar"></div>
        <div class="qc-line" style="display:none">
          <span class="qc-label">人工审查</span>
          <span class="failure-controls">
            <select id="qualityGrade" title="质量等级">
              <option value="A">A</option>
              <option value="B">B</option>
              <option value="C">C</option>
              <option value="F">F</option>
            </select>
            <span id="reviewReasonOptions" class="review-options hidden"></span>
            <span id="reviewSaveState" class="review-save-state"></span>
            <button id="saveFailureBtn" type="button">保存</button>
          </span>
        </div>
        <div class="qc-line">
          <span class="qc-label">是否截取</span>
          <span class="failure-controls cut-controls">
            <select id="frameCutEnabled" title="是否截取">
              <option value="no" selected>否</option>
              <option value="yes">是</option>
            </select>
            <span id="frameCutControls" class="failure-controls hidden">
              <input id="cutStartFrame" type="number" min="0" step="1" placeholder="起始帧(0)" />
              <input id="cutEndFrame" type="number" min="0" step="1" placeholder="结束帧(含)" />
              <button id="executeFrameCutBtn" type="button">执行</button>
            </span>
            <span id="frameCutState" class="review-save-state"></span>
          </span>
        </div>
      </div>
      <div class="chart-wrap">
        <canvas id="chart"></canvas>
        <div class="legend">
          <span><i class="dot state-dot"></i>state</span>
          <span><i class="dot action-dot"></i>action</span>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>field</th><th>state</th><th>action</th><th>diff</th><th>vel</th><th>effort</th></tr></thead>
          <tbody id="jointRows"></tbody>
        </table>
      </div>
    </aside>
  </main>

  <link rel="stylesheet" href="/auto/assets/raw-grade.css?v=4">
  <link rel="stylesheet" href="/auto/assets/raw-compact.css?v=1">
  <script src="/auto/assets/raw-grade.js?v=4"></script>
  <script src="/auto/assets/raw-review-filter.js?v=1"></script>
  <script>
    const $ = (id) => document.getElementById(id);
    let episodes = [];
    let data = null;
    let episodeLoadRevision=0;
    const reviewFilter=window.NodaReview.mount($('raw-review-filter'),$('episodeSelect'),value=>loadEpisode(Number(value)),updateEpisodeButtons);
    const liveGrade=window.NodaGrade.mount($('raw-grade-bar'),s=>{
      reviewFilter.update(s);
      if(!data||data.episode_dir!==s.root)return;
      data.quality_grade=s.current_grade;data.collection_grade=s.collection_grade;data.qc_status=s.collection_grade?'已记录':'未提供';
      data.qc_remark=s.reason||'';updateQcBlock();updateCurrentEpisodeOption();
    });
    let videos = [];
    let playing = false;
    let currentFrame = 0;
    let animationHandle = 0;

    function formatNumber(value, digits = 4) {
      if (!Number.isFinite(value)) return "";
      return value.toFixed(digits);
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      }[ch]));
    }

    function collectionStatus(status, grade = "") {
      if (grade === "F" || status === "采集失败") return "采集失败";
      if (!status || status === "未提供") return "未提供";
      return "采集成功";
    }

    function statusClass(status) {
      if (status === "保留" || status === "成功" || status === "采集成功") return "status-keep";
      if (status === "修复") return "status-fix";
      if (status === "删除" || status === "采集失败") return "status-delete";
      return "status-empty";
    }

    const REVIEW_REASONS = {
      B: ["轻微碰撞", "轨迹不平滑", "重试后成功", "轻微视觉异常", "其他"],
      F: ["抓取失败", "物体掉落", "放置错误", "目标错误", "物体碰倒", "任务中止", "其他"],
    };
    let updatingReviewControls = false;
    let reviewSaveTimer = 0;

    function splitReviewReasons(text) {
      return String(text || "")
        .split(/[；;,，]/)
        .map(item => item.trim())
        .filter(Boolean);
    }

    function selectedReviewReasons() {
      return Array.from(document.querySelectorAll(".review-reason-check:checked"))
        .map(input => input.value);
    }

    function reviewReasonText() {
      return selectedReviewReasons().join("；");
    }

    function renderReviewReasons(grade, reasonText = "") {
      const root = $("reviewReasonOptions");
      const options = REVIEW_REASONS[grade] || [];
      const selected = new Set(splitReviewReasons(reasonText));
      const knownSelected = options.some(option => selected.has(option));
      if (selected.size && !knownSelected && options.includes("其他")) {
        selected.add("其他");
      }
      root.classList.toggle("hidden", !options.length);
      root.innerHTML = options.map(option => `
        <label class="review-option">
          <input class="review-reason-check" type="checkbox" value="${escapeHtml(option)}" ${selected.has(option) ? "checked" : ""} />
          <span>${escapeHtml(option)}</span>
        </label>
      `).join("");
      root.querySelectorAll(".review-reason-check").forEach(input => {
        input.addEventListener("change", () => scheduleReviewSave());
      });
    }

    function setReviewSaveState(text) {
      $("reviewSaveState").textContent = text || "";
    }

    function scheduleReviewSave() {
      if (updatingReviewControls) return;
      clearTimeout(reviewSaveTimer);
      setReviewSaveState("保存中...");
      reviewSaveTimer = setTimeout(() => {
        saveManualFailure().catch(err => {
          setReviewSaveState("保存失败");
          alert(String(err));
        });
      }, 250);
    }

    function createVideoCards(videoInfos) {
      const root = $("videoGrid");
      videos = [];
      root.innerHTML = "";
      const top = videoInfos.find(v => v.key === "head_color") || videoInfos[0];
      const rest = videoInfos.filter(v => v !== top);

      const topCard = makeVideoCard(top);
      root.appendChild(topCard.card);

      const row = document.createElement("div");
      row.className = "stereo-row";
      for (const info of rest) row.appendChild(makeVideoCard(info).card);
      root.appendChild(row);
    }

    function makeVideoCard(info) {
      const card = document.createElement("div");
      card.className = "card";
      const head = document.createElement("div");
      head.className = "card-head";
      head.innerHTML = `<span>${info.label}</span><span>${info.file}</span>`;
      const video = document.createElement("video");
      video.src = info.url;
      video.muted = true;
      video.playsInline = true;
      video.preload = "auto";
      video.addEventListener("loadedmetadata", () => {
        video.currentTime = Math.min(videoTimeForFrame(currentFrame), Math.max(video.duration - 0.001, 0));
      });
      card.appendChild(head);
      card.appendChild(video);
      videos.push(video);
      return { card, video };
    }

    async function loadEpisode(index) {
      const revision=++episodeLoadRevision;liveGrade.setRoot('');
      pause();
      currentFrame = 0;
      const res = await fetch(`/api/episode?index=${index}`);
      if (!res.ok) throw new Error(`load episode failed: ${res.status}`);
      const loaded = await res.json();
      if(revision!==episodeLoadRevision)return;
      data=loaded;reviewFilter.setCurrent(data.index);liveGrade.setRoot(data.episode_dir);
      $("episodeSelect").value = String(data.index);
      updateEpisodeButtons();
      $("title").textContent = data.name || data.meta?.episode_name || "ICRA Episode Replay";
      $("dataMetric").textContent = data.episode_dir.split("/").slice(-1)[0];
      updateQcBlock();
      $("frameSlider").max = String(Math.max(0, data.frame_count - 1));
      $("cutStartFrame").max = String(Math.max(0, data.frame_count - 1));
      $("cutEndFrame").max = String(Math.max(0, data.frame_count - 1));
      resetFrameCutControls();
      updateStageBoundaryControls();
      createVideoCards(data.videos);
      seekToFrame(0);
    }

    function updateEpisodeButtons() {
      const select = $("episodeSelect");
      const prev = $("prevEpisodeBtn");
      const next = $("nextEpisodeBtn");
      if (!select || !prev || !next) return;
      prev.disabled = select.selectedIndex <= 0;
      next.disabled = !select.options.length || select.selectedIndex >= select.options.length - 1;
    }

    function loadAdjacentEpisode(delta) {
      const select = $("episodeSelect");
      if (!select || !select.options.length) return;
      const nextIndex = Math.max(0, Math.min(select.options.length - 1, select.selectedIndex + delta));
      if (nextIndex === select.selectedIndex) return;
      loadEpisode(Number(select.options[nextIndex].value));
    }

    function updateCurrentEpisodeOption() {
      if (!data) return;
      const option = Array.from($("episodeSelect").options).find(item => Number(item.value) === Number(data.index));
      if (!option) return;
      const statusText = collectionStatus(data.qc_status || "", data.quality_grade || "");
      const status = statusText && statusText !== "未提供" ? ` · ${statusText}` : "";
      // Labels and pending status come from the shared live review queue.
    }

    function updateQcBlock() {
      const status = collectionStatus(data?.qc_status || "", data?.collection_grade ?? data?.quality_grade ?? "");
      const remark = data?.qc_remark || "";
      const statusEl = $("qcStatus");
      statusEl.textContent = status;
      statusEl.className = `status-badge ${statusClass(status)}`;
      $("qcRemark").textContent = remark || (status === "未提供" ? "未找到 batch_summary.md 中对应的 error 列" : "");
      $("qcRemark").title = $("qcRemark").textContent;
      updateFailureControls();
    }

    function setFrameCutState(text) {
      $("frameCutState").textContent = text || "";
    }

    function setStageBoundaryState(text, warning = false) {
      const root = $("stageBoundaryState");
      root.textContent = text || "";
      root.classList.toggle("stage-warning", Boolean(warning));
    }

    function collectorStages() {
      const stages = data?.stage_info?.stages;
      if (!Array.isArray(stages) || stages.length !== 2) return [];
      return stages.filter(stage => Number(stage.stage_number) === 1 || Number(stage.stage_number) === 2);
    }

    function updateStageBoundaryControls() {
      const info = data?.stage_info || {};
      const stages = collectorStages();
      const hasCollectorMetadata = Boolean(info.available || (Array.isArray(info.errors) && info.errors.length));
      $("stageBoundaryLine").classList.toggle("hidden", !hasCollectorMetadata);
      if (!hasCollectorMetadata) {
        $("stageRangeText").textContent = "";
        $("stage2StartFrame").value = "";
        setStageBoundaryState("");
        return;
      }
      $("stageRangeText").textContent = stages.map(stage =>
        `${stage.label || `阶段 ${stage.stage_number}`}: ${stage.start}-${stage.end}`
      ).join(" ｜ ");
      const boundary = Number(info.stage2_start_frame);
      $("stage2StartFrame").min = "1";
      $("stage2StartFrame").max = String(Math.max(1, Number(data?.frame_count || 2) - 1));
      $("stage2StartFrame").value = Number.isInteger(boundary) ? String(boundary) : "";
      const editable = Boolean(info.editable);
      $("stage2StartFrame").disabled = !editable;
      $("useCurrentStageFrameBtn").disabled = !editable;
      $("applyStageBoundaryBtn").disabled = !editable;
      const errors = Array.isArray(info.errors) ? info.errors.filter(Boolean) : [];
      setStageBoundaryState(errors.join("；"), errors.length > 0);
    }

    function useCurrentFrameForStage2() {
      if (!data?.stage_info?.editable) return;
      if (currentFrame < 1 || currentFrame > data.frame_count - 1) {
        alert(`阶段 2 起始帧必须在 1-${Math.max(1, data.frame_count - 1)} 之间，当前帧 ${currentFrame} 不可用。`);
        return;
      }
      $("stage2StartFrame").value = String(currentFrame);
      setStageBoundaryState(`已选当前帧 ${currentFrame}，点击“应用”后写入`, false);
    }

    async function applyStageBoundary() {
      if (!data?.stage_info?.editable) {
        alert("当前 episode 没有可同时订正的 /subtask_transitions 和 episode_meta.json.step_index。");
        return;
      }
      const boundary = Number($("stage2StartFrame").value);
      const maxBoundary = Number(data.frame_count) - 1;
      if (!Number.isInteger(boundary) || boundary < 1 || boundary > maxBoundary) {
        alert(`阶段 2 起始帧必须是 1-${maxBoundary} 之间的整数（0-based）。`);
        return;
      }
      if (!confirm(
        `确定把阶段 2 起始帧改为 ${boundary}？\n` +
        `阶段 1: 0-${boundary - 1}\n阶段 2: ${boundary}-${data.frame_count - 1}\n` +
        "该操作会原地修改 HDF5 和 episode_meta.json，不创建持久备份。"
      )) return;

      pause();
      const resumeFrame = currentFrame;
      $("applyStageBoundaryBtn").disabled = true;
      setStageBoundaryState("写入并校验中...", false);
      try {
        const res = await fetch("api/stage-boundary", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            episode_index: data.index,
            episode_dir: data.episode_dir,
            h5_path: data.h5_path,
            frame_count: data.frame_count,
            stage2_start_frame: boundary,
            expected_stage2_start_frame: data.stage_info.hdf5_stage2_start_frame,
            expected_sidecar_stage2_start_frame: data.stage_info.sidecar_stage2_start_frame,
          }),
        });
        const payload = await res.json();
        if (!res.ok || payload.error) throw new Error(payload.error || res.statusText);
        await loadEpisode(data.index);
        seekToFrame(Math.min(resumeFrame, data.frame_count - 1));
        setStageBoundaryState(`已应用：阶段 2 从帧 ${boundary} 开始`, false);
        window.parent?.postMessage({type: "pipeline-hdf5-stage-boundary-updated", payload}, "*");
      } catch (err) {
        setStageBoundaryState("应用失败", true);
        alert(String(err));
      } finally {
        $("applyStageBoundaryBtn").disabled = !Boolean(data?.stage_info?.editable);
      }
    }

    function updateFrameCutControls() {
      const enabled = $("frameCutEnabled").value === "yes";
      $("frameCutControls").classList.toggle("hidden", !enabled);
      if (enabled) {
        const maxFrame = Math.max(0, Number(data?.frame_count || 1) - 1);
        $("cutStartFrame").max = String(maxFrame);
        $("cutEndFrame").max = String(maxFrame);
        if (!$("cutStartFrame").value) $("cutStartFrame").value = String(currentFrame);
        if (!$("cutEndFrame").value) $("cutEndFrame").value = String(currentFrame);
      }
    }

    function resetFrameCutControls() {
      $("frameCutEnabled").value = "no";
      $("cutStartFrame").value = "";
      $("cutEndFrame").value = "";
      setFrameCutState("");
      updateFrameCutControls();
    }

    function updateFailureControls() {
      const grade = data?.quality_grade || (data?.manual_failure || data?.qc_status === "采集失败" ? "F" : "A");
      const reason = data?.manual_review_reason || data?.manual_failure_reason || ((grade === "B" || grade === "F") ? data?.qc_remark : "") || "";
      updatingReviewControls = true;
      $("qualityGrade").value = ["A", "B", "C", "F"].includes(grade) ? grade : "A";
      renderReviewReasons($("qualityGrade").value, reason);
      setReviewSaveState("");
      updatingReviewControls = false;
    }

    function updateFailureReasonVisibility() {
      const grade = $("qualityGrade").value;
      renderReviewReasons(grade, reviewReasonText());
    }

    async function saveManualFailure() {
      if (!data) return;
      const grade = $("qualityGrade").value;
      const needsReason = grade === "B" || grade === "F";
      const reason = needsReason ? reviewReasonText() : "";
      const res = await fetch("api/manual-failure", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          episode_index: data.index,
          episode_name: data.name || data.meta?.episode_name,
          quality_grade: grade,
          is_failure: grade === "F",
          reason_label: reason,
        }),
      });
      const payload = await res.json();
      if (!res.ok || payload.error) throw new Error(payload.error || res.statusText);
      data.manual_failure = payload.is_failure;
      data.manual_failure_reason = payload.reason_label || "";
      data.quality_grade = payload.quality_grade || grade;
      data.manual_review_reason = payload.manual_review_reason || payload.reason_label || "";
      data.qc_status = payload.qc_status || "";
      data.qc_remark = payload.qc_remark || "";
      updateQcBlock();
      updateCurrentEpisodeOption();
      setReviewSaveState("已保存");
      window.parent?.postMessage({type: "pipeline-manual-failure-updated"}, "*");
    }

    async function loadEpisodeRecords(preferredIndex = null) {
      const recordsRes = await fetch("/api/episodes");
      if (!recordsRes.ok) throw new Error(`load episodes failed: ${recordsRes.status}`);
      const recordsPayload = await recordsRes.json();
      episodes = recordsPayload.episodes || [];
      const select = $("episodeSelect");
      select.innerHTML = episodes.map(ep => {
        const statusText = collectionStatus(ep.qc_status || "", ep.quality_grade || "");
        const status = statusText && statusText !== "未提供" ? ` · ${statusText}` : "";
        return `<option value="${ep.index}">${escapeHtml(ep.name)}${escapeHtml(status)} · ${ep.frame_count}f</option>`;
      }).join("");
      const fallback = Number(recordsPayload.initial_index || 0);
      const wanted = preferredIndex === null ? fallback : Number(preferredIndex);
      reviewFilter.setRecords(episodes.map(ep=>({root:ep.episode_dir,value:ep.index,label:`${ep.name} · ${ep.frame_count}f`})),wanted);
      if (episodes.some(ep => Number(ep.index) === wanted)) {
        select.value = String(wanted);
        return wanted;
      }
      const first = episodes.length ? Number(episodes[0].index) : 0;
      select.value = String(first);
      return first;
    }

    async function executeFrameCut() {
      if (!data) return;
      const start = Number($("cutStartFrame").value);
      const end = Number($("cutEndFrame").value);
      const maxFrame = Math.max(0, Number(data.frame_count || 1) - 1);
      if (!Number.isInteger(start) || !Number.isInteger(end)) {
        alert("请输入整数帧号。");
        return;
      }
      if (start < 0 || end < start || end > maxFrame) {
        alert(`帧范围需要满足 0 <= 起始帧 <= 结束帧 <= ${maxFrame}`);
        return;
      }
      if ((end - start + 1) >= data.frame_count) {
        alert("不能删除全部帧。");
        return;
      }
      if (!confirm(`确定删除当前 episode 的 ${start}-${end} 帧（含两端）？此操作会直接修改 HDF5 和视频。`)) {
        return;
      }
      pause();
      setFrameCutState("执行中...");
      $("executeFrameCutBtn").disabled = true;
      try {
        const res = await fetch("api/frame-delete", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            episode_index: data.index,
            episode_name: data.name || data.meta?.episode_name,
            start_frame: start,
            end_frame: end,
          }),
        });
        const payload = await res.json();
        if (!res.ok || payload.error) throw new Error(payload.error || res.statusText);
        const nextIndex = await loadEpisodeRecords(payload.episode_index);
        await loadEpisode(nextIndex);
        seekToFrame(Math.min(start, Math.max(0, data.frame_count - 1)));
        $("frameCutEnabled").value = "no";
        updateFrameCutControls();
        setFrameCutState(`已删除 ${payload.removed_frame_count} 帧`);
        window.parent?.postMessage({type: "pipeline-hdf5-frames-deleted", payload}, "*");
      } catch (err) {
        setFrameCutState("执行失败");
        alert(String(err));
      } finally {
        $("executeFrameCutBtn").disabled = false;
      }
    }

    function currentSubtask(frame) {
      const stages = collectorStages();
      for (const stage of stages) {
        const start = Number(stage.start);
        const end = Number(stage.end);
        if (Number.isFinite(start) && Number.isFinite(end) && frame >= start && frame <= end) {
          return `${stage.label || `阶段 ${stage.stage_number}`} · 帧 ${start}-${end}`;
        }
      }
      const segments = Array.isArray(data?.subtask_segments) ? data.subtask_segments : [];
      for (const segment of segments) {
        const start = Number(segment.start ?? segment.start_time);
        const end = Number(segment.end ?? segment.end_time);
        if (Number.isFinite(start) && Number.isFinite(end) && frame >= start && frame <= end) {
          return String(segment.subtask || "");
        }
      }
      return "";
    }

    function videoTimeForFrame(frame) {
      const videoFps = Number(data?.video_fps || data?.fps || 30);
      return frame / Math.max(videoFps, 0.001);
    }

    function frameForVideoTime(time) {
      const videoFps = Number(data?.video_fps || data?.fps || 30);
      return Math.round(time * Math.max(videoFps, 0.001));
    }

    function seekToFrame(frame) {
      if (!data) return;
      currentFrame = Math.max(0, Math.min(data.frame_count - 1, Math.round(frame)));
      const time = videoTimeForFrame(currentFrame);
      for (const video of videos) {
        if (Number.isFinite(video.duration)) {
          video.currentTime = Math.min(time, Math.max(video.duration - 0.001, 0));
        } else {
          video.currentTime = time;
        }
      }
      updateInfo();
      drawChart();
    }

    function syncFrameFromVideo() {
      if (!data || !videos.length) return;
      const frame = Math.max(0, Math.min(data.frame_count - 1, frameForVideoTime(videos[0].currentTime)));
      if (frame !== currentFrame) {
        currentFrame = frame;
        updateInfo();
        drawChart();
      }
      if (playing) animationHandle = requestAnimationFrame(syncFrameFromVideo);
    }

    function play() {
      if (!data) return;
      playing = true;
      $("playBtn").textContent = "Ⅱ";
      const rate = Number($("speedSelect").value);
      for (const video of videos) {
        video.playbackRate = rate;
        video.play().catch(() => {});
      }
      cancelAnimationFrame(animationHandle);
      animationHandle = requestAnimationFrame(syncFrameFromVideo);
    }

    function pause() {
      playing = false;
      $("playBtn").textContent = "▶";
      for (const video of videos) video.pause();
      cancelAnimationFrame(animationHandle);
      syncFrameFromVideo();
    }

    function updateInfo() {
      $("frameSlider").value = String(currentFrame);
      const ts = data.timestamps[currentFrame] ?? (currentFrame / data.fps);
      $("frameMetric").textContent = `${currentFrame + 1} / ${data.frame_count}`;
      $("timeMetric").textContent = `${formatNumber(ts, 3)}s`;
      $("fpsMetric").textContent = `${formatNumber(data.fps, 2)}`;
      const typeLabel = (data.episode_type || "aloha").toUpperCase();
      $("stateLabel").textContent = `${typeLabel} ${data.state_dim}D state / ${data.action_dim}D action`;
      const taskText = data.task || (Array.isArray(data.tasks) ? data.tasks.join(", ") : "");
      $("taskText").textContent = taskText || "未提供";
      $("taskText").title = $("taskText").textContent;
      $("subtaskText").textContent = currentSubtask(currentFrame) || "未命中";
      $("subtaskText").title = $("subtaskText").textContent;
      renderJointTable();
    }

    function rowDefinitions() {
      if (Array.isArray(data.rows) && data.rows.length) return data.rows;
      const dim = Math.max(data.state_dim || 0, data.action_dim || 0);
      return Array.from({ length: dim }, (_, i) => ({
        label: `q${String(i).padStart(2, "0")}`,
        state_index: i < data.state_dim ? i : null,
        action_index: i < data.action_dim ? i : null,
        diff: i < data.state_dim && i < data.action_dim,
      }));
    }

    function valueAt(values, idx) {
      if (idx === null || idx === undefined) return undefined;
      const value = values[idx];
      return Number.isFinite(value) ? value : undefined;
    }

    function renderJointTable() {
      const state = data.state_joint_position[currentFrame] || [];
      const action = data.action_joint_position[currentFrame] || [];
      const vel = data.state_joint_velocity[currentFrame] || [];
      const effort = data.state_joint_effort[currentFrame] || [];
      const rows = [];
      for (const row of rowDefinitions()) {
        const s = valueAt(state, row.state_index);
        const a = valueAt(action, row.action_index);
        const v = valueAt(vel, row.state_index);
        const e = valueAt(effort, row.state_index);
        const canDiff = row.diff && s !== undefined && a !== undefined;
        rows.push(`<tr>
          <td>${escapeHtml(row.label)}</td>
          <td>${s === undefined ? "" : formatNumber(s)}</td>
          <td>${a === undefined ? "" : formatNumber(a)}</td>
          <td>${canDiff ? formatNumber(a - s) : ""}</td>
          <td>${v === undefined ? "" : formatNumber(v)}</td>
          <td>${e === undefined ? "" : formatNumber(e)}</td>
        </tr>`);
      }
      $("jointRows").innerHTML = rows.join("");
    }

    function drawChart() {
      if (!data) return;
      const canvas = $("chart");
      const rows = rowDefinitions();
      const chartWrap = canvas.parentElement;
      const visibleHeight = Math.max(1, chartWrap ? chartWrap.clientHeight : canvas.clientHeight);
      const contentHeight = Math.max(visibleHeight, rows.length * 34);
      canvas.style.height = `${contentHeight}px`;
      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      const width = Math.max(1, Math.floor(rect.width * dpr));
      const height = Math.max(1, Math.floor(rect.height * dpr));
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      const ctx = canvas.getContext("2d");
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "#111417";
      ctx.fillRect(0, 0, width, height);

      const state = data.state_joint_position;
      const action = data.action_joint_position;
      if (!state.length || !action.length || !rows.length) return;

      ctx.font = `${11 * dpr}px ui-monospace, Consolas, monospace`;
      ctx.textBaseline = "middle";
      const maxLabelWidth = rows.reduce((acc, row) => Math.max(acc, ctx.measureText(row.label).width), 0);
      const left = Math.min(width * 0.36, Math.max(44 * dpr, maxLabelWidth + 14 * dpr));
      const right = 10 * dpr;
      const top = 16 * dpr;
      const bottom = 10 * dpr;
      const plotW = width - left - right;
      const rowH = (height - top - bottom) / rows.length;
      const xScale = plotW / Math.max(1, data.frame_count - 1);

      for (let j = 0; j < rows.length; j++) {
        const row = rows[j];
        const y0 = top + rowH * j;
        const yMid = y0 + rowH / 2;
        const yPad = Math.max(3 * dpr, rowH * 0.18);
        let minVal = Infinity;
        let maxVal = -Infinity;
        for (let i = 0; i < data.frame_count; i++) {
          const s = valueAt(state[i] || [], row.state_index);
          const a = valueAt(action[i] || [], row.action_index);
          if (s !== undefined) { minVal = Math.min(minVal, s); maxVal = Math.max(maxVal, s); }
          if (a !== undefined) { minVal = Math.min(minVal, a); maxVal = Math.max(maxVal, a); }
        }
        if (!Number.isFinite(minVal) || !Number.isFinite(maxVal) || Math.abs(maxVal - minVal) < 1e-9) {
          minVal -= 1;
          maxVal += 1;
        }
        const yFor = (v) => y0 + yPad + (maxVal - v) / (maxVal - minVal) * Math.max(1, rowH - 2 * yPad);

        ctx.strokeStyle = "#252b31";
        ctx.lineWidth = 1 * dpr;
        ctx.beginPath();
        ctx.moveTo(left, yMid);
        ctx.lineTo(width - right, yMid);
        ctx.stroke();

        ctx.fillStyle = "#aab4bf";
        ctx.fillText(row.label, 9 * dpr, yMid);

        drawLine(ctx, action, row.action_index, left, xScale, yFor, "#f3b34c", dpr);
        drawLine(ctx, state, row.state_index, left, xScale, yFor, "#42c2ff", dpr);
      }

      const stages = collectorStages();
      if (stages.length === 2) {
        const stage2Start = Number(stages[1].start);
        if (Number.isInteger(stage2Start) && stage2Start >= 1 && stage2Start < data.frame_count) {
          const stageX = left + stage2Start * xScale;
          ctx.save();
          ctx.strokeStyle = "#f3b34c";
          ctx.lineWidth = 1.2 * dpr;
          ctx.setLineDash([5 * dpr, 4 * dpr]);
          ctx.beginPath();
          ctx.moveTo(stageX, top);
          ctx.lineTo(stageX, height - bottom);
          ctx.stroke();
          ctx.setLineDash([]);
          ctx.fillStyle = "#f3b34c";
          ctx.textBaseline = "top";
          ctx.fillText("阶段 2", Math.min(stageX + 4 * dpr, width - 56 * dpr), 2 * dpr);
          ctx.restore();
        }
      }

      const playX = left + currentFrame * xScale;
      ctx.strokeStyle = "#67d391";
      ctx.lineWidth = 1.5 * dpr;
      ctx.beginPath();
      ctx.moveTo(playX, top);
      ctx.lineTo(playX, height - bottom);
      ctx.stroke();
    }

    function drawLine(ctx, arr, dim, left, xScale, yFor, color, dpr) {
      if (dim === null || dim === undefined) return;
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.2 * dpr;
      ctx.beginPath();
      let started = false;
      for (let i = 0; i < arr.length; i++) {
        const v = valueAt(arr[i] || [], dim);
        if (v === undefined) continue;
        const x = left + i * xScale;
        const y = yFor(v);
        if (!started) {
          ctx.moveTo(x, y);
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
      }
      ctx.stroke();
    }

    async function boot() {
      const select = $("episodeSelect");
      select.addEventListener("change", (e) => loadEpisode(Number(e.target.value)));
      const initialIndex = await loadEpisodeRecords();
      await loadEpisode(initialIndex);

      $("prevEpisodeBtn").addEventListener("click", () => loadAdjacentEpisode(-1));
      $("nextEpisodeBtn").addEventListener("click", () => loadAdjacentEpisode(1));
      $("playBtn").addEventListener("click", () => playing ? pause() : play());
      $("prevBtn").addEventListener("click", () => { pause(); seekToFrame(currentFrame - 1); });
      $("nextBtn").addEventListener("click", () => { pause(); seekToFrame(currentFrame + 1); });
      $("frameSlider").addEventListener("input", (e) => { pause(); seekToFrame(Number(e.target.value)); });
      $("qualityGrade").addEventListener("change", () => {
        renderReviewReasons($("qualityGrade").value, "");
        scheduleReviewSave();
      });
      $("saveFailureBtn").addEventListener("click", () => {
        saveManualFailure().catch(err => alert(String(err)));
      });
      $("frameCutEnabled").addEventListener("change", updateFrameCutControls);
      $("executeFrameCutBtn").addEventListener("click", () => {
        executeFrameCut().catch(err => alert(String(err)));
      });
      $("useCurrentStageFrameBtn").addEventListener("click", useCurrentFrameForStage2);
      $("applyStageBoundaryBtn").addEventListener("click", () => {
        applyStageBoundary().catch(err => alert(String(err)));
      });
      $("speedSelect").addEventListener("change", () => {
        const rate = Number($("speedSelect").value);
        for (const video of videos) video.playbackRate = rate;
      });
      window.addEventListener("resize", drawChart);
      document.addEventListener("keydown", (e) => {
        if (["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(String(e.target?.tagName || ""))) return;
        if (e.code === "Space") { e.preventDefault(); playing ? pause() : play(); }
        if (e.code === "ArrowLeft") { pause(); seekToFrame(currentFrame - 1); }
        if (e.code === "ArrowRight") { pause(); seekToFrame(currentFrame + 1); }
      });
    }

    boot().catch(err => {
      document.body.innerHTML = `<pre style="padding:20px;color:#ffb4a8">${err.stack || err}</pre>`;
    });
  </script>
</body>
</html>
"""


def main() -> int:
    install_trim_signal_handlers()
    args = parse_args()
    input_path = args.episode_dir.expanduser().resolve()
    qc_report_dir = resolve_qc_report_dir(input_path, args.qc_report_dir)
    qc_summary = load_qc_summary(qc_report_dir)
    manual_failure_path = manual_failure_path_for_input(input_path, args.manual_failure_json)
    manual_failures = load_manual_failure_file(manual_failure_path)
    episodes = load_episodes(args.episode_dir, args.fps, args.type, qc_summary, manual_failures)
    handler = make_handler(episodes, manual_failure_path)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    local_ip = get_local_ip()
    print(f"Input   : {input_path}", flush=True)
    print(f"QC      : {qc_report_dir if qc_report_dir else '(not found)'}", flush=True)
    print(f"Failures: {manual_failure_path if manual_failure_path.is_file() else '(not found)'}", flush=True)
    print(f"Episodes: {len(episodes)}", flush=True)
    for idx, data in enumerate(episodes):
        videos = ", ".join(item["file"] for item in data.available_videos)
        status = data.qc_status or "-"
        print(
            f"  [{idx}] {episode_name(data)} | status={status} | "
            f"type={data.episode_type} | frames={data.frame_count} | videos={videos}",
            flush=True,
        )
    print(f"Local  : http://127.0.0.1:{args.port}/", flush=True)
    if local_ip:
        print(f"LAN    : http://{local_ip}:{args.port}/", flush=True)
    if args.open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}/")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
