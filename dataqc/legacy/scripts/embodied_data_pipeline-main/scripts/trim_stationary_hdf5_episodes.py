#!/usr/bin/env python3
"""Trim long stationary runs from converted HDF5 episode folders."""

from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from contextlib import contextmanager
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
import fcntl
import json
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
import uuid

import h5py
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quality_pipeline.episode_io import read_raw_episode
from quality_pipeline.profiles import load_profile
from quality_pipeline.qc import _quality_config, _stationary_pair_metrics


TRIM_TRANSACTION_JOURNAL = ".frame_trim_transaction.json"
_TRIM_SIGNAL_PENDING = False


class FrameTrimInterrupted(KeyboardInterrupt):
    """Raised by SIGINT/SIGTERM so an active trim context can roll back."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"frame trim interrupted by signal {signum}")
        self.signum = int(signum)


def _trim_signal_handler(signum: int, _frame: Any) -> None:
    global _TRIM_SIGNAL_PENDING
    # Prevent a second termination signal from interrupting journal recovery.
    # The process exits after the first interrupt, so the mask need not be
    # restored in this process lifetime.
    if hasattr(signal, "pthread_sigmask"):
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
    if _TRIM_SIGNAL_PENDING:
        return
    _TRIM_SIGNAL_PENDING = True
    raise FrameTrimInterrupted(signum)


def install_trim_signal_handlers() -> None:
    """Install rollback-friendly handlers in the current process main thread."""
    global _TRIM_SIGNAL_PENDING
    if threading.current_thread() is not threading.main_thread():
        return
    _TRIM_SIGNAL_PENDING = False
    if hasattr(signal, "pthread_sigmask"):
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM})
    signal.signal(signal.SIGINT, _trim_signal_handler)
    signal.signal(signal.SIGTERM, _trim_signal_handler)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Trim converted HDF5 episodes by reducing each long stationary run "
            "to a fixed number of frames."
        ),
    )
    parser.add_argument("--input", required=True, help="HDF5 episode dir, HDF5 root, or aligned_joints.h5.")
    parser.add_argument("--profile", required=True, help="Path to robot_profiles/*.yaml.")
    parser.add_argument(
        "--episodeNames",
        default="",
        help="Comma-separated episode directory names to process. Default: all discovered episodes.",
    )
    parser.add_argument(
        "--keep-stationary-frames",
        type=int,
        default=15,
        help="Keep at most this many frames from each over-threshold stationary run.",
    )
    parser.add_argument(
        "--stationary-threshold",
        type=int,
        choices=(20, 40, 60),
        default=None,
        help=(
            "Override the profile threshold used to identify an overlong stationary run. "
            "Supported web-console levels: 20, 40, or 60 frames."
        ),
    )
    parser.add_argument(
        "--target-fps",
        type=float,
        default=30.0,
        help="Rewrite HDF5 main timestamps and output videos to this FPS.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned trims without modifying HDF5/videos/meta.",
    )
    parser.add_argument(
        "--num-workers",
        type=int,
        default=1,
        help="Number of episodes to process in parallel.",
    )
    parser.add_argument(
        "--video-workers",
        type=int,
        default=3,
        help="Number of videos to trim in parallel within each episode.",
    )
    return parser.parse_args()


def profile_with_stationary_threshold(profile: Any, threshold: int | None) -> Any:
    if threshold is None:
        return profile
    from copy import deepcopy
    from quality_pipeline.profiles import RobotProfile

    raw = deepcopy(profile.raw)
    raw.setdefault("processing", {}).setdefault("rtml", {}).setdefault(
        "global_constraints", {}
    )["max_stationary_action_frames"] = int(threshold)
    return RobotProfile(path=profile.path, raw=raw)


def discover_episode_dirs(path: Path) -> list[Path]:
    path = path.expanduser().resolve()
    if path.is_file() and path.suffix.lower() in {".h5", ".hdf5"}:
        return [episode_dir_from_h5(path)]
    if episode_h5_path(path).is_file():
        return [path]
    episodes = {
        episode_dir_from_h5(candidate)
        for pattern in (
            "*/episode.hdf5",
            "*/episode.h5",
            "**/states/aligned_joints.h5",
            "**/states/aligned_joints.hdf5",
        )
        for candidate in path.glob(pattern)
    }
    return sorted(episodes, key=lambda item: natural_key(str(item)))


def natural_key(text: str) -> list[tuple[int, Any]]:
    import re

    return [
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.split(r"(\d+)", text)
    ]


def episode_h5_path(episode_dir: Path) -> Path:
    candidates = (
        episode_dir / "episode.hdf5",
        episode_dir / "episode.h5",
        episode_dir / "states" / "aligned_joints.h5",
        episode_dir / "states" / "aligned_joints.hdf5",
        episode_dir / "aligned_joints.h5",
        episode_dir / "aligned_joints.hdf5",
    )
    return next((candidate for candidate in candidates if candidate.is_file()), candidates[2])


def episode_dir_from_h5(h5_path: Path) -> Path:
    if h5_path.name.startswith("aligned_joints") and h5_path.parent.name == "states":
        return h5_path.parent.parent
    return h5_path.parent


def episode_meta_path(episode_dir: Path) -> Path:
    columnar = episode_dir / "episode_meta.json"
    legacy = episode_dir / "meta" / "episode_meta.json"
    if columnar.is_file() or episode_h5_path(episode_dir).name.startswith("episode."):
        return columnar
    return legacy


def is_columnar_hdf5(h5_path: Path) -> bool:
    with h5py.File(h5_path, "r") as file_obj:
        return "timestamp/t" in file_obj


def episode_video_paths(episode_dir: Path) -> list[Path]:
    video_dir = episode_dir / "videos"
    return sorted(
        (
            path
            for path in video_dir.rglob("*.mp4")
            if path.is_file() and ".stationary_trim_" not in path.name
        ),
        key=lambda path: natural_key(str(path.relative_to(video_dir))),
    )


def selected_names(text: str) -> set[str]:
    return {item.strip() for item in text.split(",") if item.strip()}


def stationary_runs(episode_dir: Path, profile: Any) -> tuple[list[tuple[int, int]], dict[str, Any]]:
    episode = read_raw_episode(episode_dir, profile)
    config = _quality_config(profile)
    epsilon = float(config["action_stationary_epsilon"])
    state_epsilon = float(config["stationary_state_epsilon"])
    base_velocity_epsilon = float(config["stationary_base_velocity_epsilon"])
    requires_action_and_state = bool(config.get("stationary_requires_action_and_state", False))
    max_allowed = int(config["max_stationary_action_frames"])
    action_rows = stationary_action_rows(episode)
    state_rows = stationary_state_rows(episode)
    using_hdf5_stationary_vectors = bool(episode.stationary_action_vectors and episode.stationary_state_vectors)
    pair_count = min(len(action_rows), len(state_rows)) - 1

    runs: list[tuple[int, int]] = []
    current_start: int | None = None
    current_len = 0
    max_run = 0
    stationary_pairs = 0
    stationary_frames = 0

    for pair_idx in range(max(0, pair_count)):
        prev = action_rows[pair_idx]
        cur = action_rows[pair_idx + 1]
        if len(prev) != len(cur):
            is_stationary = False
            action_delta = 0.0
            state_delta = 0.0
            base_velocity_abs = 0.0
            stationary_source = "hdf5_full_motion_fields" if using_hdf5_stationary_vectors else "profile_state_action"
        else:
            metrics = _stationary_pair_metrics(
                profile,
                prev,
                cur,
                state_rows[pair_idx],
                state_rows[pair_idx + 1],
                using_hdf5_stationary_vectors,
            )
            action_delta = metrics["action_delta"]
            state_delta = metrics["state_delta"]
            base_velocity_abs = metrics["base_velocity_abs"]
            stationary_source = metrics["stationary_source"]
            action_stationary = action_delta <= epsilon
            state_stationary = state_delta <= state_epsilon
            motion_stationary = (
                action_stationary and state_stationary
                if requires_action_and_state
                else action_stationary or state_stationary
            )
            is_stationary = motion_stationary and base_velocity_abs <= base_velocity_epsilon

        if is_stationary:
            if current_start is None:
                current_start = pair_idx
            current_len += 1
            stationary_pairs += 1
            max_run = max(max_run, current_len)
            continue

        if current_start is not None:
            stationary_frames += current_len + 1
            if current_len + 1 > max_allowed:
                runs.append((current_start, current_start + current_len))
        current_start = None
        current_len = 0

    if current_start is not None:
        stationary_frames += current_len + 1
        if current_len + 1 > max_allowed:
            runs.append((current_start, current_start + current_len))

    return runs, {
        "frames": episode.n_frames,
        "actions": len(action_rows),
        "stationary_frames": stationary_frames,
        "stationary_pairs": stationary_pairs,
        "max_stationary_run": max_run,
        "max_stationary_run_frames": max_run + 1 if max_run else 0,
        "over_threshold_stationary_runs": len(runs),
        "max_allowed_stationary_run": max_allowed,
        "action_stationary_epsilon": epsilon,
        "state_stationary_epsilon": state_epsilon,
        "base_velocity_epsilon": base_velocity_epsilon,
        "requires_action_and_state": requires_action_and_state,
        "stationary_state_dim": len(state_rows[0]) if state_rows else 0,
        "stationary_action_dim": len(action_rows[0]) if action_rows else 0,
        "stationary_source": stationary_source if pair_count > 0 else (
            "hdf5_full_motion_fields" if using_hdf5_stationary_vectors else "profile_state_action"
        ),
    }


def stationary_state_rows(episode: Any) -> list[list[float]]:
    if episode.stationary_state_vectors and len(episode.stationary_state_vectors) >= len(episode.state_frames):
        return episode.stationary_state_vectors
    return [frame.state for frame in episode.state_frames]


def stationary_action_rows(episode: Any) -> list[list[float]]:
    if episode.stationary_action_vectors and len(episode.stationary_action_vectors) >= len(episode.actions):
        return episode.stationary_action_vectors
    return episode.actions


def uniform_sample_run_positions(start_frame: int, end_frame: int, keep_frames: int) -> set[int]:
    run_frame_count = end_frame - start_frame + 1
    keep_frames = max(1, min(int(keep_frames), run_frame_count))
    if run_frame_count <= keep_frames:
        return set(range(start_frame, end_frame + 1))
    if keep_frames == 1:
        return {start_frame}
    span = run_frame_count - 1
    sampled = {
        start_frame + round(index * span / (keep_frames - 1))
        for index in range(keep_frames)
    }
    # Defensive fill: rounded linspace should be unique here, but keep exactly
    # keep_frames positions even if Python rounding ever collapses a value.
    if len(sampled) < keep_frames:
        for pos in range(start_frame, end_frame + 1):
            sampled.add(pos)
            if len(sampled) >= keep_frames:
                break
    return sampled


def keep_positions_for_runs(frame_count: int, runs: list[tuple[int, int]], keep_frames: int) -> list[int]:
    remove: set[int] = set()
    keep_frames = max(1, int(keep_frames))
    for start, end in runs:
        start_frame = max(0, start)
        end_frame = min(frame_count - 1, end)
        run_frame_count = end_frame - start_frame + 1
        if run_frame_count <= keep_frames:
            continue
        keep_in_run = uniform_sample_run_positions(start_frame, end_frame, keep_frames)
        remove.update(pos for pos in range(start_frame, end_frame + 1) if pos not in keep_in_run)
    return [idx for idx in range(frame_count) if idx not in remove]


def numeric_frame_keys(h5_path: Path) -> list[str]:
    with h5py.File(h5_path, "r") as f:
        return sorted((str(key) for key in f.keys() if str(key).isdigit()), key=lambda key: int(key))


def hdf5_frame_count(h5_path: Path) -> int:
    with h5py.File(h5_path, "r") as file_obj:
        if "timestamp/t" in file_obj:
            return int(file_obj["timestamp/t"].shape[0])
        return sum(1 for key in file_obj.keys() if str(key).isdigit())


def read_first_timestamp(h5_path: Path, first_key: str) -> int:
    with h5py.File(h5_path, "r") as f:
        value = f[f"{first_key}/main_timestamp"][()]
        return int(np.asarray(value).reshape(-1)[0])


@contextmanager
def episode_lock(episode_dir: Path) -> Any:
    lock_path = episode_dir / (
        ".stationary_trim.lock"
        if episode_h5_path(episode_dir).name.startswith("episode.")
        else "states/.stationary_trim.lock"
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def h5_stationary_trim_attrs(h5_path: Path) -> dict[str, Any]:
    if not h5_path.exists():
        return {}
    with h5py.File(h5_path, "r") as f:
        if "timestamp/t" in f:
            current_frame_count = int(f["timestamp/t"].shape[0])
        else:
            current_frame_count = sum(1 for key in f.keys() if str(key).isdigit())

        raw_last_kind = f.attrs.get("last_frame_trim_kind", "")
        if isinstance(raw_last_kind, bytes):
            raw_last_kind = raw_last_kind.decode("utf-8", errors="replace")
        last_kind = str(raw_last_kind)
        prefixes = ("stationary_trim", "manual_frame_trim")
        valid = [
            prefix
            for prefix in prefixes
            if bool(f.attrs.get(f"{prefix}med", False))
            and int(f.attrs.get(f"{prefix}_kept_frame_count", -1) or -1) == current_frame_count
        ]
        if last_kind in valid:
            prefix = last_kind
        elif valid:
            # Backward-compatible fallback for files created before
            # last_frame_trim_kind existed. A stale trim marker whose kept
            # count does not match the current dataset must never win.
            prefix = max(
                valid,
                key=lambda item: int(f.attrs.get(f"{item}_generation", 0) or 0),
            )
        else:
            prefix = ""
        if not prefix:
            return {}
        return {
            "prefix": prefix,
            "generation": int(f.attrs.get(f"{prefix}_generation", 0) or 0),
            "target_fps": float(f.attrs.get(f"{prefix}_target_fps", 0.0) or 0.0),
            "original_frame_count": int(f.attrs.get(f"{prefix}_original_frame_count", 0) or 0),
            "kept_frame_count": int(f.attrs.get(f"{prefix}_kept_frame_count", 0) or 0),
            "removed_frame_count": int(f.attrs.get(f"{prefix}_removed_frame_count", 0) or 0),
        }


def video_frame_count(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        import cv2
    except ImportError:
        return 0
    cap = cv2.VideoCapture(str(path))
    try:
        return int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    finally:
        cap.release()


def validate_no_partial_trim(episode_dir: Path, h5_path: Path) -> None:
    attrs = h5_stationary_trim_attrs(h5_path)
    if not attrs:
        return
    kept = int(attrs.get("kept_frame_count") or 0)
    meta_path = episode_meta_path(episode_dir)
    meta_has_trim = not meta_path.is_file()
    meta_frame_count: int | None = None
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta_key = "manual_frame_trim" if attrs.get("prefix") == "manual_frame_trim" else "stationary_trim"
            meta_has_trim = isinstance(meta.get(meta_key), dict)
            raw_frame_count = meta.get("frame_count")
            meta_frame_count = int(raw_frame_count) if raw_frame_count is not None else None
        except (OSError, ValueError, json.JSONDecodeError):
            meta_has_trim = False
    bad_videos = []
    for video_path in episode_video_paths(episode_dir):
        count = video_frame_count(video_path)
        if count and count != kept:
            bad_videos.append(f"{video_path.relative_to(episode_dir / 'videos')}:{count}")
    temp_videos = sorted(
        str(path.relative_to(episode_dir / "videos"))
        for path in (episode_dir / "videos").rglob("*.stationary_trim_*")
        if ".stationary_trim_backup_" not in path.name
    )
    if (not meta_has_trim) or (meta_frame_count not in (None, kept)) or bad_videos or temp_videos:
        raise RuntimeError(
            "Partial stationary trim detected; regenerate this episode from MCAP before trimming again. "
            f"episode={episode_dir.name}, hdf5_kept={kept}, meta_has_trim={meta_has_trim}, "
            f"meta_frame_count={meta_frame_count}, bad_videos={bad_videos}, temp_videos={temp_videos}"
        )


def set_timestamp_dataset(group: Any, dataset_path: str, timestamp_ns: int) -> None:
    if dataset_path not in group:
        return
    dataset = group[dataset_path]
    dtype = dataset.dtype
    shape = dataset.shape
    del group[dataset_path]
    value = np.asarray(timestamp_ns, dtype=dtype)
    if shape:
        value = np.full(shape, timestamp_ns, dtype=dtype)
    group.create_dataset(dataset_path, data=value)


def rewrite_group_timestamps(group: Any, timestamp_ns: int) -> None:
    set_timestamp_dataset(group, "main_timestamp", timestamp_ns)
    if "timestamp" not in group:
        return
    timestamp_group = group["timestamp"]
    for name in list(timestamp_group.keys()):
        item = timestamp_group[name]
        if isinstance(item, h5py.Dataset):
            set_timestamp_dataset(group, f"timestamp/{name}", timestamp_ns)
        elif isinstance(item, h5py.Group):
            for child in list(item.keys()):
                if isinstance(item[child], h5py.Dataset):
                    set_timestamp_dataset(group, f"timestamp/{name}/{child}", timestamp_ns)


ZERITH_REQUIRED_FRAME_DATASETS = (
    "timestamp/t",
    "action/arm/position",
    "action/effector/position",
    "action/waist/position",
    "action/head/position",
    "action/base/velocity",
    "observation/state/arm/position",
    "observation/state/effector/position",
    "observation/state/waist/position",
    "observation/state/head/position",
    "observation/state/base/velocity",
)


def validate_columnar_schema(file_obj: Any) -> tuple[int, list[str]]:
    for path in ZERITH_REQUIRED_FRAME_DATASETS:
        if path not in file_obj or not isinstance(file_obj[path], h5py.Dataset):
            raise ValueError(f"{file_obj.filename}: missing required frame dataset {path}")
    timestamp = file_obj["timestamp/t"]
    if len(timestamp.shape) != 1 or int(timestamp.shape[0]) <= 0:
        raise ValueError(f"{file_obj.filename}: expected timestamp/t shape (N,), got {timestamp.shape}")
    frame_count = int(timestamp.shape[0])
    frame_datasets: list[str] = []

    def visitor(name: str, item: Any) -> None:
        if not isinstance(item, h5py.Dataset) or not name.startswith(("action/", "observation/", "timestamp/")):
            return
        if not item.shape:
            raise ValueError(f"{file_obj.filename}: frame dataset {name} must not be scalar")
        if int(item.shape[0]) != frame_count:
            raise ValueError(
                f"{file_obj.filename}: frame dataset {name} has {item.shape[0]} rows, "
                f"expected {frame_count}"
            )
        frame_datasets.append(name)

    file_obj.visititems(visitor)
    return frame_count, sorted(frame_datasets)


def dtype_contains_hdf5_references(dtype: Any) -> bool:
    """Return whether an HDF5 dtype contains object or region references.

    References cannot be copied verbatim between files: their stored object
    addresses belong to the source file.  Silently assigning such a value to
    the temporary file can therefore create a dangling (or, worse, wrongly
    retargeted) reference.
    """
    dtype = np.dtype(dtype)
    if h5py.check_dtype(ref=dtype) is not None:
        return True
    if dtype.fields:
        return any(dtype_contains_hdf5_references(field[0]) for field in dtype.fields.values())
    if dtype.subdtype is not None:
        return dtype_contains_hdf5_references(dtype.subdtype[0])
    return False


def value_contains_hdf5_references(value: Any) -> bool:
    """Detect references in an attribute value, including scalar references."""
    if isinstance(value, h5py.Reference):
        return True
    array = np.asarray(value)
    if dtype_contains_hdf5_references(array.dtype):
        return True
    # A scalar reference read from an attribute loses its dtype metadata, and
    # object arrays can contain references without advertising a ref dtype.
    if array.dtype.kind == "O":
        return any(isinstance(item, h5py.Reference) for item in array.reshape(-1))
    return False


def copy_hdf5_attrs(source: Any, target: Any) -> None:
    for key, value in source.attrs.items():
        if value_contains_hdf5_references(value):
            source_name = getattr(source, "name", "/") or "/"
            raise TypeError(
                f"cannot safely copy HDF5 reference attribute {source_name}:{key} between files"
            )
        target.attrs[key] = value


def dataset_create_kwargs(
    source: Any,
    shape: tuple[int, ...] | None,
    *,
    frame_dataset: bool,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"dtype": source.dtype}
    if shape is None:
        return kwargs
    if source.chunks is not None:
        chunks = list(source.chunks)
        for idx, size in enumerate(shape):
            if size > 0:
                chunks[idx] = min(int(chunks[idx]), int(size))
        kwargs["chunks"] = tuple(max(1, int(value)) for value in chunks)
    if source.compression is not None:
        kwargs["compression"] = source.compression
        kwargs["compression_opts"] = source.compression_opts
    if source.shuffle:
        kwargs["shuffle"] = True
    if source.fletcher32:
        kwargs["fletcher32"] = True
    if source.scaleoffset is not None:
        kwargs["scaleoffset"] = source.scaleoffset
    # Supplying maxshape for a contiguous source silently changes the copied
    # dataset to chunked storage.  A chunked source already carries maxshape,
    # including any unlimited dimensions, so preserve it only in that case.
    if source.chunks is not None and source.maxshape is not None:
        maxshape = list(source.maxshape)
        if frame_dataset:
            maxshape[0] = (
                None
                if source.maxshape[0] is None
                else max(int(maxshape[0]), int(shape[0]))
            )
        kwargs["maxshape"] = tuple(maxshape)
    return kwargs


def copy_object_dataset_values(
    source: Any,
    target: Any,
    keep_positions: list[int],
    frame_dataset: bool,
) -> None:
    """Copy VLEN/object-backed values without h5py's slice broadcasting trap.

    In particular, assigning a one-row VLEN uint8 fancy selection can make
    h5py interpret a JPEG payload of length M as an array shaped ``(1, M)``
    instead of one scalar VLEN value shaped ``(1,)``.  Scalar assignments keep
    the VLEN boundary unambiguous.  This also covers VLEN strings and compound
    dtypes containing VLEN fields.
    """
    if source.shape is None:
        return
    trailing_shape = tuple(source.shape[1:]) if frame_dataset else tuple(source.shape)
    if frame_dataset:
        # Batch the reads so large compressed image datasets remain efficient,
        # but keep writes scalar so h5py cannot flatten a VLEN payload into the
        # destination's frame dimension.
        for start in range(0, len(keep_positions), 8):
            positions = keep_positions[start : start + 8]
            values = source[positions]
            for offset in range(len(positions)):
                output_position = start + offset
                if not trailing_shape:
                    target[output_position] = values[offset]
                    continue
                for trailing_index in np.ndindex(trailing_shape):
                    target[(output_position, *trailing_index)] = values[
                        (offset, *trailing_index)
                    ]
        return
    if not source.shape:
        target[()] = source[()]
        return
    for index in np.ndindex(trailing_shape):
        target[index] = source[index]


def copy_columnar_dataset(
    source: Any,
    target_file: Any,
    name: str,
    keep_positions: list[int],
    frame_dataset: bool,
) -> None:
    if dtype_contains_hdf5_references(source.dtype):
        raise TypeError(
            f"cannot safely copy HDF5 reference dataset {name!r} between files"
        )
    output_shape = (
        ((len(keep_positions),) + tuple(source.shape[1:]))
        if frame_dataset
        else (tuple(source.shape) if source.shape is not None else None)
    )
    target = target_file.create_dataset(
        name,
        shape=output_shape,
        **dataset_create_kwargs(source, output_shape, frame_dataset=frame_dataset),
    )
    copy_hdf5_attrs(source, target)
    if source.dtype.hasobject:
        copy_object_dataset_values(source, target, keep_positions, frame_dataset)
    elif frame_dataset:
        # Numeric arrays use bounded fancy-index batches to balance HDF5 I/O
        # throughput with peak memory use.
        batch_size = 256
        for start in range(0, len(keep_positions), batch_size):
            positions = keep_positions[start : start + batch_size]
            target[start : start + len(positions)] = source[positions]
    elif source.shape is None:
        return
    elif source.shape:
        target[...] = source[...]
    else:
        target[()] = source[()]


def remap_columnar_boundaries(values: Any, keep_positions: list[int]) -> Any:
    array = np.asarray(values)
    mapped = [bisect_left(keep_positions, int(value)) for value in array.reshape(-1).tolist()]
    return np.asarray(mapped, dtype=array.dtype).reshape(array.shape)


def trim_columnar_hdf5(
    h5_path: Path,
    keep_positions: list[int],
    target_fps: float,
    dry_run: bool,
    attr_prefix: str,
) -> dict[str, Any]:
    with h5py.File(h5_path, "r") as source:
        frame_count, frame_datasets = validate_columnar_schema(source)
        timestamps = source["timestamp/t"]
        first_ms = float(timestamps[0])
    if any(position < 0 or position >= frame_count for position in keep_positions):
        raise IndexError(f"keep positions are outside 0..{frame_count - 1}")
    if keep_positions != sorted(set(keep_positions)):
        raise ValueError("keep positions must be unique and sorted")
    if not keep_positions:
        raise ValueError("cannot trim all frames")
    interval_ms = 1000.0 / target_fps
    last_ms = first_ms + (len(keep_positions) - 1) * interval_ms
    info = {
        "original_frame_count": frame_count,
        "kept_frame_count": len(keep_positions),
        "removed_frame_count": frame_count - len(keep_positions),
        "first_timestamp_ns": int(round(first_ms * 1_000_000.0)),
        "last_timestamp_ns": int(round(last_ms * 1_000_000.0)),
        "duration": (len(keep_positions) - 1) / target_fps if len(keep_positions) > 1 else 0.0,
        "inferred_fps": target_fps if len(keep_positions) > 1 else 0.0,
        "timestamp_unit": "milliseconds",
        "frame_dataset_count": len(frame_datasets),
    }
    if dry_run or len(keep_positions) == frame_count:
        return info

    tmp_path = h5_path.with_name(
        f"{h5_path.stem}.{attr_prefix}_tmp.{os.getpid()}.{time.time_ns()}{h5_path.suffix}"
    )
    trimmed_attr = f"{attr_prefix}med" if attr_prefix.endswith("trim") else f"{attr_prefix}_trimmed"
    try:
        with h5py.File(h5_path, "r") as source, h5py.File(tmp_path, "w") as target:
            copy_hdf5_attrs(source, target)
            generation = int(source.attrs.get("last_frame_trim_generation", 0) or 0) + 1
            target.attrs[trimmed_attr] = True
            target.attrs[f"{attr_prefix}_target_fps"] = target_fps
            target.attrs[f"{attr_prefix}_original_frame_count"] = frame_count
            target.attrs[f"{attr_prefix}_removed_frame_count"] = frame_count - len(keep_positions)
            target.attrs[f"{attr_prefix}_kept_frame_count"] = len(keep_positions)
            target.attrs[f"{attr_prefix}_generation"] = generation
            target.attrs["last_frame_trim_kind"] = attr_prefix
            target.attrs["last_frame_trim_generation"] = generation
            target.attrs["total_frames"] = len(keep_positions)

            def create_group(name: str, item: Any) -> None:
                if isinstance(item, h5py.Group):
                    group = target.require_group(name)
                    copy_hdf5_attrs(item, group)

            source.visititems(create_group)

            def create_dataset(name: str, item: Any) -> None:
                if not isinstance(item, h5py.Dataset):
                    return
                copy_columnar_dataset(item, target, name, keep_positions, name in frame_datasets)

            source.visititems(create_dataset)
            target["timestamp/t"][...] = first_ms + np.arange(len(keep_positions)) * interval_ms
            if "subtask_transitions" in target:
                target["subtask_transitions"][...] = remap_columnar_boundaries(
                    source["subtask_transitions"][...], keep_positions
                )
            target.flush()

        with h5py.File(tmp_path, "r") as candidate:
            candidate_count, candidate_datasets = validate_columnar_schema(candidate)
            if candidate_count != len(keep_positions) or candidate_datasets != frame_datasets:
                raise RuntimeError(
                    f"trimmed HDF5 validation failed: frames={candidate_count}, "
                    f"datasets={len(candidate_datasets)}"
                )
            candidate_timestamps = np.asarray(candidate["timestamp/t"][...], dtype=float)
            if len(candidate_timestamps) > 1 and not np.all(np.diff(candidate_timestamps) > 0):
                raise RuntimeError("trimmed HDF5 timestamps are not strictly increasing")
            if int(candidate.attrs.get("total_frames", -1)) != len(keep_positions):
                raise RuntimeError("trimmed HDF5 total_frames attribute is inconsistent")
        tmp_path.replace(h5_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return info


def trim_hdf5(
    h5_path: Path,
    keep_positions: list[int],
    target_fps: float,
    dry_run: bool,
    attr_prefix: str = "stationary_trim",
) -> dict[str, Any]:
    if is_columnar_hdf5(h5_path):
        return trim_columnar_hdf5(
            h5_path,
            keep_positions,
            target_fps,
            dry_run,
            attr_prefix,
        )
    frame_keys = numeric_frame_keys(h5_path)
    if not frame_keys:
        raise ValueError(f"No numeric frame groups in {h5_path}")
    if len(keep_positions) == len(frame_keys):
        first_ts = read_first_timestamp(h5_path, frame_keys[0])
        last_ts = read_first_timestamp(h5_path, frame_keys[-1])
        duration = (last_ts - first_ts) / 1_000_000_000.0 if len(frame_keys) > 1 else 0.0
        inferred_fps = (len(frame_keys) - 1) / duration if duration > 1e-9 else 0.0
        return {
            "original_frame_count": len(frame_keys),
            "kept_frame_count": len(frame_keys),
            "removed_frame_count": 0,
            "first_timestamp_ns": first_ts,
            "last_timestamp_ns": last_ts,
            "duration": duration,
            "inferred_fps": inferred_fps,
        }
    if dry_run:
        first_ts = read_first_timestamp(h5_path, frame_keys[0])
        last_ts = first_ts + round((len(keep_positions) - 1) * 1_000_000_000.0 / target_fps)
        return {
            "original_frame_count": len(frame_keys),
            "kept_frame_count": len(keep_positions),
            "removed_frame_count": len(frame_keys) - len(keep_positions),
            "first_timestamp_ns": first_ts,
            "last_timestamp_ns": last_ts,
            "duration": (len(keep_positions) - 1) / target_fps if len(keep_positions) > 1 else 0.0,
            "inferred_fps": target_fps if len(keep_positions) > 1 else 0.0,
        }

    tmp_path = h5_path.with_name(
        f"{h5_path.stem}.{attr_prefix}_tmp.{os.getpid()}.{time.time_ns()}{h5_path.suffix}"
    )
    interval_ns = int(round(1_000_000_000.0 / target_fps))
    trimmed_attr = f"{attr_prefix}med" if attr_prefix.endswith("trim") else f"{attr_prefix}_trimmed"

    try:
        with h5py.File(h5_path, "r") as src, h5py.File(tmp_path, "w") as dst:
            first_ts = int(np.asarray(src[f"{frame_keys[0]}/main_timestamp"][()]).reshape(-1)[0])
            generation = int(src.attrs.get("last_frame_trim_generation", 0) or 0) + 1
            numeric_keys = set(frame_keys)
            for key in src.keys():
                if key not in numeric_keys:
                    src.copy(key, dst, name=key)
            for attr_key, attr_value in src.attrs.items():
                dst.attrs[attr_key] = attr_value
            dst.attrs[trimmed_attr] = True
            dst.attrs[f"{attr_prefix}_target_fps"] = target_fps
            dst.attrs[f"{attr_prefix}_original_frame_count"] = len(frame_keys)
            dst.attrs[f"{attr_prefix}_removed_frame_count"] = len(frame_keys) - len(keep_positions)
            dst.attrs[f"{attr_prefix}_kept_frame_count"] = len(keep_positions)
            dst.attrs[f"{attr_prefix}_generation"] = generation
            dst.attrs["last_frame_trim_kind"] = attr_prefix
            dst.attrs["last_frame_trim_generation"] = generation
            dst.attrs["total_frames"] = len(keep_positions)

            for out_idx, source_pos in enumerate(keep_positions):
                timestamp_ns = first_ts + out_idx * interval_ns
                src.copy(frame_keys[source_pos], dst, name=str(out_idx))
                rewrite_group_timestamps(dst[str(out_idx)], timestamp_ns)
        tmp_path.replace(h5_path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise

    last_ts = first_ts + (len(keep_positions) - 1) * interval_ns
    return {
        "original_frame_count": len(frame_keys),
        "kept_frame_count": len(keep_positions),
        "removed_frame_count": len(frame_keys) - len(keep_positions),
        "first_timestamp_ns": first_ts,
        "last_timestamp_ns": last_ts,
        "duration": (len(keep_positions) - 1) / target_fps if len(keep_positions) > 1 else 0.0,
        "inferred_fps": target_fps if len(keep_positions) > 1 else 0.0,
    }


def open_writer(cv2: Any, output: Path, fps: float, size: tuple[int, int]) -> Any:
    writer = cv2.VideoWriter(str(output), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    if writer.isOpened():
        return writer
    writer.release()
    raise RuntimeError(f"Could not open video writer: {output}")


def transcode_to_h264(raw_tmp: Path, final_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raw_tmp.replace(final_path)
        return
    h264_tmp = final_path.with_name(
        f"{final_path.stem}.stationary_trim_h264_tmp.{os.getpid()}.{time.time_ns()}{final_path.suffix}"
    )
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(raw_tmp),
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-tag:v",
        "avc1",
        str(h264_tmp),
    ]
    try:
        subprocess.run(cmd, check=True)
    except Exception as exc:
        print(f"[WARN] ffmpeg H264 transcode failed for {final_path}: {exc}. Keeping mp4v output.")
        if h264_tmp.exists():
            h264_tmp.unlink()
        raw_tmp.replace(final_path)
        return
    raw_tmp.unlink(missing_ok=True)
    h264_tmp.replace(final_path)


def trim_video(video_path: Path, keep_positions: list[int], target_fps: float, dry_run: bool) -> dict[str, Any]:
    if not video_path.is_file():
        return {"file": video_path.name, "status": "missing"}
    if dry_run:
        return {"file": video_path.name, "status": "planned", "frame_count": len(keep_positions)}

    import cv2

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")
    raw_tmp = video_path.with_name(
        f"{video_path.stem}.stationary_trim_opencv_tmp.{os.getpid()}.{time.time_ns()}{video_path.suffix}"
    )

    keep_set = set(keep_positions)
    max_keep = max(keep_positions) if keep_positions else -1
    written = 0
    read_count = 0
    writer = None
    try:
        while read_count <= max_keep:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            if read_count in keep_set:
                if writer is None:
                    height, width = frame.shape[:2]
                    writer = open_writer(cv2, raw_tmp, target_fps, (width, height))
                writer.write(frame)
                written += 1
            read_count += 1
    finally:
        cap.release()
        if writer is not None:
            writer.release()

    if writer is None:
        if raw_tmp.exists():
            raw_tmp.unlink()
        raise RuntimeError(f"No frames were written while trimming {video_path}")
    if written != len(keep_positions):
        raw_tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"Video {video_path} produced {written} frames, expected {len(keep_positions)}"
        )
    transcode_to_h264(raw_tmp, video_path)
    verified_count = video_frame_count(video_path)
    if verified_count != len(keep_positions):
        raise RuntimeError(
            f"Trimmed video {video_path} verifies as {verified_count} frames, "
            f"expected {len(keep_positions)}"
        )
    return {
        "file": video_path.name,
        "status": "trimmed",
        "frame_count": written,
        "expected_frame_count": len(keep_positions),
        "source_frames_read": read_count,
    }


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json_durable(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp_path, path)
        _fsync_directory(path.parent)
    finally:
        tmp_path.unlink(missing_ok=True)


def _unlink_durable(path: Path) -> None:
    path.unlink(missing_ok=True)
    _fsync_directory(path.parent)


def _transaction_relative_path(episode_dir: Path, path: Path) -> str:
    root = episode_dir.resolve()
    candidate = path.resolve()
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"transaction path is outside episode directory: {path}") from exc


def _transaction_path(episode_dir: Path, relative: str) -> Path:
    root = episode_dir.resolve()
    raw_path = Path(relative)
    if not relative or raw_path.is_absolute() or relative in {".", ".."} or ".." in raw_path.parts:
        raise RuntimeError(f"unsafe path in frame-trim journal: {relative!r}")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"unsafe path in frame-trim journal: {relative}") from exc
    return candidate


def _journal_entry_paths(episode_dir: Path, item: Any) -> tuple[Path, Path]:
    if not isinstance(item, dict):
        raise RuntimeError("invalid file entry in frame-trim journal")
    original = _transaction_path(episode_dir, str(item.get("original") or ""))
    backup = _transaction_path(episode_dir, str(item.get("backup") or ""))
    expected_prefix = f".{original.name}.frame_trim_backup_"
    if backup == original or backup.parent != original.parent or not backup.name.startswith(expected_prefix):
        raise RuntimeError(
            f"unsafe backup mapping in frame-trim journal: original={original}, backup={backup}"
        )
    return original, backup


def _cleanup_transaction_temps(episode_dir: Path) -> None:
    patterns = (
        "*stationary_trim_tmp.*",
        "*manual_frame_trim_tmp.*",
        "*stationary_trim_opencv_tmp.*",
        "*stationary_trim_h264_tmp.*",
        "*stationary_trim_meta_tmp.*",
        "*manual_frame_trim_meta_tmp.*",
    )
    for pattern in patterns:
        for path in episode_dir.rglob(pattern):
            if path.is_file():
                path.unlink(missing_ok=True)


def recover_frame_trim_transaction(episode_dir: Path) -> str:
    """Recover an interrupted frame trim, or finish cleanup after commit.

    An ``active``/``preparing`` journal restores every available backup. A
    ``committed`` journal never restores old data; it only removes residual
    backups and temporary files.
    """
    episode_dir = episode_dir.resolve()
    journal_path = episode_dir / TRIM_TRANSACTION_JOURNAL
    if not journal_path.is_file():
        return "none"
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid frame-trim transaction journal: {journal_path}") from exc
    state = str(journal.get("state") or "")
    if state not in {"preparing", "active", "committed"}:
        raise RuntimeError(f"unknown frame-trim transaction state {state!r}: {journal_path}")
    entries = journal.get("files")
    if not isinstance(entries, list):
        raise RuntimeError(f"frame-trim journal has no file list: {journal_path}")

    parsed_entries = [_journal_entry_paths(episode_dir, item) for item in entries]
    originals = [original for original, _backup in parsed_entries]
    backups = [backup for _original, backup in parsed_entries]
    if len(set(originals)) != len(originals) or len(set(backups)) != len(backups):
        raise RuntimeError(f"duplicate file mapping in frame-trim journal: {journal_path}")

    touched_dirs: set[Path] = set()
    if state in {"preparing", "active"}:
        for original, backup in parsed_entries:
            touched_dirs.add(original.parent)
            if backup.exists():
                os.replace(backup, original)
            elif not original.exists():
                raise RuntimeError(
                    f"cannot recover {original}: both original and transaction backup are missing"
                )

    # For a committed journal, or after restoration, backups are garbage.
    for _original, backup in parsed_entries:
        touched_dirs.add(backup.parent)
        backup.unlink(missing_ok=True)
    _cleanup_transaction_temps(episode_dir)
    for directory in touched_dirs:
        _fsync_directory(directory)
    _unlink_durable(journal_path)
    return "cleaned_committed" if state == "committed" else "restored_active"


class FrameTrimTransaction:
    """Durable, explicit-commit transaction for an episode's mutable files."""

    def __init__(self, episode_dir: Path, paths: list[Path], kind: str) -> None:
        self.episode_dir = episode_dir.resolve()
        self.paths = list(dict.fromkeys(path.resolve() for path in paths))
        self.kind = str(kind)
        self.journal_path = self.episode_dir / TRIM_TRANSACTION_JOURNAL
        self.journal: dict[str, Any] = {}
        self.finished = False

    def __enter__(self) -> "FrameTrimTransaction":
        recover_frame_trim_transaction(self.episode_dir)
        self.paths = [path for path in self.paths if path.is_file()]
        token = uuid.uuid4().hex
        entries = []
        for original in self.paths:
            backup = original.with_name(f".{original.name}.frame_trim_backup_{token}")
            entries.append(
                {
                    "original": _transaction_relative_path(self.episode_dir, original),
                    "backup": _transaction_relative_path(self.episode_dir, backup),
                }
            )
        self.journal = {
            "version": 1,
            "state": "preparing",
            "kind": self.kind,
            "token": token,
            "pid": os.getpid(),
            "started_at_ns": time.time_ns(),
            "files": entries,
        }
        _write_json_durable(self.journal_path, self.journal)
        try:
            for item in entries:
                original = _transaction_path(self.episode_dir, item["original"])
                backup = _transaction_path(self.episode_dir, item["backup"])
                os.link(original, backup)
                _fsync_directory(backup.parent)
            self.journal["state"] = "active"
            _write_json_durable(self.journal_path, self.journal)
        except BaseException:
            recover_frame_trim_transaction(self.episode_dir)
            raise
        return self

    def commit(self) -> None:
        if self.finished:
            return
        self.journal["state"] = "committed"
        self.journal["committed_at_ns"] = time.time_ns()
        _write_json_durable(self.journal_path, self.journal)
        # From this point recovery must only clean up, never restore.
        self.finished = True
        recover_frame_trim_transaction(self.episode_dir)

    def rollback(self) -> None:
        if self.finished:
            return
        recover_frame_trim_transaction(self.episode_dir)
        self.finished = True

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        if not self.finished:
            self.rollback()
        return False


def frame_trim_transaction(
    episode_dir: Path,
    paths: list[Path],
    kind: str,
) -> FrameTrimTransaction:
    return FrameTrimTransaction(episode_dir, paths, kind)


# Kept as compatibility helpers for out-of-tree callers. New mutations should
# use frame_trim_transaction(), which owns durable recovery semantics.
def create_file_backups(paths: list[Path]) -> dict[Path, Path]:
    token = uuid.uuid4().hex
    backups: dict[Path, Path] = {}
    try:
        for path in paths:
            backup = path.with_name(f".{path.name}.stationary_trim_backup_{token}")
            os.link(path, backup)
            backups[path] = backup
    except Exception:
        for backup in backups.values():
            backup.unlink(missing_ok=True)
        raise
    return backups


def restore_file_backups(backups: dict[Path, Path]) -> None:
    for original, backup in backups.items():
        if backup.exists():
            backup.replace(original)


def remove_file_backups(backups: dict[Path, Path]) -> None:
    for backup in backups.values():
        backup.unlink(missing_ok=True)


def remap_frame_value(value: Any, source_to_new: dict[int, int]) -> Any:
    try:
        old = int(value)
    except (TypeError, ValueError):
        return value
    if old in source_to_new:
        return source_to_new[old]
    lower = [source for source in source_to_new if source <= old]
    if lower:
        return source_to_new[max(lower)]
    return 0


def remap_segments(meta: dict[str, Any], source_to_new: dict[int, int]) -> None:
    for key in ("subtask_segments", "segment_instructions"):
        segments = meta.get(key)
        if not isinstance(segments, list):
            continue
        new_segments = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            item = dict(segment)
            if "start" in item:
                item["start"] = remap_frame_value(item["start"], source_to_new)
            if "end" in item:
                item["end"] = remap_frame_value(item["end"], source_to_new)
            if "start_frame" in item:
                item["start_frame"] = remap_frame_value(item["start_frame"], source_to_new)
            if "end_frame" in item:
                item["end_frame"] = remap_frame_value(item["end_frame"], source_to_new)
            new_segments.append(item)
        meta[key] = new_segments


def remap_step_index(
    meta: dict[str, Any],
    keep_positions: list[int],
    first_timestamp_ns: int,
    target_fps: float,
) -> None:
    steps = meta.get("step_index")
    if not isinstance(steps, list):
        return
    frame_count = len(keep_positions)
    first_timestamp_ms = first_timestamp_ns / 1_000_000.0
    for step in steps:
        if not isinstance(step, dict):
            continue
        try:
            old_start = int(step.get("start_frame_id"))
            old_end = int(step.get("end_frame_id"))
        except (TypeError, ValueError):
            continue
        if old_end < old_start:
            new_start = frame_count
            new_end = frame_count - 1
        else:
            new_start = min(frame_count, bisect_left(keep_positions, old_start))
            new_end = bisect_right(keep_positions, old_end) - 1
            new_end = max(-1, min(frame_count - 1, new_end))
            if new_start > new_end:
                new_start = min(new_start, max(0, frame_count - 1))
                new_end = new_start
        step["start_frame_id"] = new_start
        step["end_frame_id"] = new_end
        if "start_ts" in step:
            step["start_ts"] = int(round(first_timestamp_ms + new_start * 1000.0 / target_fps))
        if "end_ts" in step:
            end_boundary = new_end + 1 if new_end >= 0 else 0
            step["end_ts"] = int(round(first_timestamp_ms + end_boundary * 1000.0 / target_fps))


def update_meta(episode_dir: Path, h5_info: dict[str, Any], trim_info: dict[str, Any], video_results: list[dict[str, Any]]) -> None:
    meta_path = episode_meta_path(episode_dir)
    if not meta_path.exists():
        return
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["frame_count"] = h5_info["kept_frame_count"]
    meta["duration"] = h5_info["duration"]
    meta["first_timestamp_ns"] = h5_info["first_timestamp_ns"]
    meta["last_timestamp_ns"] = h5_info["last_timestamp_ns"]
    meta["inferred_state_fps"] = h5_info["inferred_fps"]
    meta["video_fps"] = h5_info["inferred_fps"]
    stored_trim_info = dict(trim_info)
    stored_trim_info.pop("source_to_new_frame", None)
    stored_trim_info.pop("keep_positions", None)
    meta["stationary_trim"] = stored_trim_info
    remap_segments(meta, trim_info.get("source_to_new_frame", {}))
    remap_step_index(
        meta,
        list(trim_info.get("keep_positions") or []),
        int(h5_info["first_timestamp_ns"]),
        float(trim_info.get("target_fps") or h5_info.get("inferred_fps") or 30.0),
    )

    video_counts = {
        item["file"]: item["frame_count"]
        for item in video_results
        if item.get("status") == "trimmed" and "frame_count" in item
    }
    for item in meta.get("available_videos", []):
        if isinstance(item, dict) and item.get("file") in video_counts:
            item["frame_count"] = video_counts[item["file"]]

    tmp_path = meta_path.with_name(
        f".{meta_path.name}.stationary_trim_meta_tmp.{os.getpid()}.{time.time_ns()}"
    )
    with tmp_path.open("w", encoding="utf-8") as output:
        output.write(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
        output.flush()
        os.fsync(output.fileno())
    tmp_path.replace(meta_path)
    _fsync_directory(meta_path.parent)


def update_hdf5_aggregate_trim_attrs(
    h5_path: Path,
    attr_prefix: str,
    original_frame_count: int,
    kept_frame_count: int,
    target_fps: float,
    pass_count: int,
) -> None:
    """Finalize multi-pass trim counters while the episode transaction is active."""
    with h5py.File(h5_path, "r+") as file_obj:
        file_obj.attrs[f"{attr_prefix}_original_frame_count"] = int(original_frame_count)
        file_obj.attrs[f"{attr_prefix}_kept_frame_count"] = int(kept_frame_count)
        file_obj.attrs[f"{attr_prefix}_removed_frame_count"] = int(original_frame_count) - int(kept_frame_count)
        file_obj.attrs[f"{attr_prefix}_target_fps"] = float(target_fps)
        file_obj.attrs[f"{attr_prefix}_pass_count"] = int(pass_count)
        file_obj.attrs["last_frame_trim_kind"] = attr_prefix
        file_obj.attrs["total_frames"] = int(kept_frame_count)
        file_obj.flush()
    with h5_path.open("rb") as input_file:
        os.fsync(input_file.fileno())


def process_episode(
    episode_dir: Path,
    profile: Any,
    keep_stationary_frames: int,
    target_fps: float,
    dry_run: bool,
    video_workers: int = 3,
) -> dict[str, Any]:
    with episode_lock(episode_dir):
        return _process_episode_locked(
            episode_dir,
            profile,
            keep_stationary_frames,
            target_fps,
            dry_run,
            video_workers,
        )


def _process_episode_locked(
    episode_dir: Path,
    profile: Any,
    keep_stationary_frames: int,
    target_fps: float,
    dry_run: bool,
    video_workers: int = 3,
) -> dict[str, Any]:
    h5_path = episode_h5_path(episode_dir)
    if not h5_path.exists():
        raise FileNotFoundError(h5_path)
    recover_frame_trim_transaction(episode_dir)
    validate_no_partial_trim(episode_dir, h5_path)

    max_passes = 5
    pass_results: list[dict[str, Any]] = []
    all_video_results: list[dict[str, Any]] = []
    initial_frame_count = hdf5_frame_count(h5_path)
    runs, initial_stationary_info = stationary_runs(episode_dir, profile)
    final_stationary_info = initial_stationary_info

    if not runs:
        h5_info = trim_hdf5(
            h5_path,
            list(range(initial_frame_count)),
            target_fps,
            dry_run,
        )
    elif dry_run:
        keep_positions = keep_positions_for_runs(initial_frame_count, runs, keep_stationary_frames)
        h5_info = trim_hdf5(h5_path, keep_positions, target_fps, dry_run=True)
        pass_results.append(
            {
                "pass": 1,
                "runs": [
                    {
                        "start_frame": start,
                        "end_frame": min(initial_frame_count - 1, end),
                        "source_frame_count": min(initial_frame_count - 1, end) - start + 1,
                        "kept_frame_count": min(
                            keep_stationary_frames,
                            min(initial_frame_count - 1, end) - start + 1,
                        ),
                        "keep_strategy": "uniform",
                    }
                    for start, end in runs
                ],
                "stationary_qc": initial_stationary_info,
                "original_frame_count": h5_info["original_frame_count"],
                "kept_frame_count": h5_info["kept_frame_count"],
                "removed_frame_count": h5_info["removed_frame_count"],
            }
        )
    else:
        video_paths = episode_video_paths(episode_dir)
        meta_path = episode_meta_path(episode_dir)
        transaction_paths = [h5_path, *video_paths]
        if meta_path.is_file():
            transaction_paths.append(meta_path)
        original_keep_positions = list(range(initial_frame_count))
        h5_info: dict[str, Any] | None = None

        with frame_trim_transaction(
            episode_dir,
            transaction_paths,
            kind="stationary_trim",
        ) as transaction:
            for pass_idx in range(1, max_passes + 1):
                frame_count = hdf5_frame_count(h5_path)
                keep_positions = keep_positions_for_runs(frame_count, runs, keep_stationary_frames)
                run_items = [
                    {
                        "start_frame": start,
                        "end_frame": min(frame_count - 1, end),
                        "source_frame_count": min(frame_count - 1, end) - start + 1,
                        "kept_frame_count": min(
                            keep_stationary_frames,
                            min(frame_count - 1, end) - start + 1,
                        ),
                        "keep_strategy": "uniform",
                    }
                    for start, end in runs
                ]
                h5_info = trim_hdf5(h5_path, keep_positions, target_fps, dry_run=False)
                if int(h5_info["removed_frame_count"]) <= 0:
                    raise RuntimeError("stationary trim pass did not remove any frames")

                video_results: list[dict[str, Any]] = []
                if video_workers > 1 and len(video_paths) > 1:
                    with ThreadPoolExecutor(max_workers=max(1, int(video_workers))) as executor:
                        futures = [
                            executor.submit(trim_video, video_path, keep_positions, target_fps, False)
                            for video_path in video_paths
                        ]
                        for future in as_completed(futures):
                            video_results.append(future.result())
                    video_results.sort(key=lambda item: str(item.get("file") or ""))
                else:
                    for video_path in video_paths:
                        video_results.append(trim_video(video_path, keep_positions, target_fps, False))

                original_keep_positions = [original_keep_positions[position] for position in keep_positions]
                pass_results.append(
                    {
                        "pass": pass_idx,
                        "runs": run_items,
                        "stationary_qc": final_stationary_info,
                        "original_frame_count": h5_info["original_frame_count"],
                        "kept_frame_count": h5_info["kept_frame_count"],
                        "removed_frame_count": h5_info["removed_frame_count"],
                    }
                )
                all_video_results.extend({**item, "pass": pass_idx} for item in video_results)

                runs, final_stationary_info = stationary_runs(episode_dir, profile)
                if not runs:
                    break
            else:
                raise RuntimeError(
                    f"stationary trimming still has over-threshold runs after {max_passes} passes"
                )

            if h5_info is None:
                raise RuntimeError("stationary trim produced no HDF5 result")
            final_frame_count = len(original_keep_positions)
            h5_info = {
                **h5_info,
                "original_frame_count": initial_frame_count,
                "kept_frame_count": final_frame_count,
                "removed_frame_count": initial_frame_count - final_frame_count,
                "duration": (final_frame_count - 1) / target_fps if final_frame_count > 1 else 0.0,
                "inferred_fps": target_fps if final_frame_count > 1 else 0.0,
            }
            source_to_new = {
                source_position: new_position
                for new_position, source_position in enumerate(original_keep_positions)
            }
            aggregate_trim_info = {
                "enabled": True,
                "target_fps": target_fps,
                "keep_stationary_frames": keep_stationary_frames,
                "keep_strategy": "uniform",
                "passes": pass_results,
                "runs": [
                    {**run, "pass": item["pass"]}
                    for item in pass_results
                    for run in item.get("runs", [])
                ],
                "stationary_qc": initial_stationary_info,
                "final_stationary_qc": final_stationary_info,
                "original_frame_count": initial_frame_count,
                "kept_frame_count": final_frame_count,
                "removed_frame_count": initial_frame_count - final_frame_count,
                "source_to_new_frame": source_to_new,
                "keep_positions": original_keep_positions,
            }
            update_hdf5_aggregate_trim_attrs(
                h5_path,
                "stationary_trim",
                initial_frame_count,
                final_frame_count,
                target_fps,
                len(pass_results),
            )
            update_meta(episode_dir, h5_info, aggregate_trim_info, all_video_results)
            validate_no_partial_trim(episode_dir, h5_path)
            transaction.commit()

    stationary_info = initial_stationary_info
    total_removed = initial_frame_count - int(h5_info["kept_frame_count"])
    if pass_results:
        h5_info = {
            **h5_info,
            "original_frame_count": initial_frame_count,
            "removed_frame_count": total_removed,
        }

    no_trim_reason = ""
    if h5_info["removed_frame_count"] == 0:
        no_trim_reason = (
            "no stationary run exceeded threshold "
            f"{stationary_info.get('max_allowed_stationary_run')} frames; "
            f"max run was {stationary_info.get('max_stationary_run_frames')} frames"
        )

    return {
        "episode": episode_dir.name,
        "status": "trimmed" if h5_info["removed_frame_count"] > 0 else "unchanged",
        "hdf5": str(h5_path),
        "original_frame_count": h5_info["original_frame_count"],
        "kept_frame_count": h5_info["kept_frame_count"],
        "removed_frame_count": h5_info["removed_frame_count"],
        "trim_passes": len(pass_results),
        "runs": [
            {**run, "pass": item["pass"]}
            for item in pass_results
            for run in item.get("runs", [])
        ],
        "stationary_qc": stationary_info,
        "final_stationary_qc": final_stationary_info or {},
        "no_trim_reason": no_trim_reason,
        "videos": all_video_results,
    }


def process_episode_worker(
    args: tuple[str, str, int, float, bool, int, int | None],
) -> dict[str, Any]:
    install_trim_signal_handlers()
    (
        episode_dir,
        profile_path,
        keep_stationary_frames,
        target_fps,
        dry_run,
        video_workers,
        stationary_threshold,
    ) = args
    profile = profile_with_stationary_threshold(
        load_profile(profile_path),
        stationary_threshold,
    )
    return process_episode(
        Path(episode_dir),
        profile,
        keep_stationary_frames,
        target_fps,
        dry_run,
        video_workers,
    )


def main() -> int:
    install_trim_signal_handlers()
    args = parse_args()
    profile = profile_with_stationary_threshold(
        load_profile(args.profile),
        args.stationary_threshold,
    )
    effective_threshold = int(_quality_config(profile)["max_stationary_action_frames"])
    if int(args.keep_stationary_frames) < 1:
        raise ValueError("--keep-stationary-frames must be at least 1")
    if int(args.keep_stationary_frames) > effective_threshold:
        raise ValueError(
            "--keep-stationary-frames cannot exceed the stationary threshold "
            f"({args.keep_stationary_frames} > {effective_threshold})"
        )
    input_path = Path(args.input).expanduser().resolve()
    names = selected_names(args.episodeNames)
    episode_dirs = discover_episode_dirs(input_path)
    if names:
        episode_dirs = [path for path in episode_dirs if path.name in names]
    if not episode_dirs:
        raise FileNotFoundError(f"No selected HDF5 episode directories found under {input_path}")

    results = []
    num_workers = max(1, int(args.num_workers or 1))
    video_workers = max(1, int(args.video_workers or 1))
    if num_workers > 1 and len(episode_dirs) > 1:
        worker_args = [
            (
                str(episode_dir),
                str(args.profile),
                args.keep_stationary_frames,
                args.target_fps,
                args.dry_run,
                video_workers,
                args.stationary_threshold,
            )
            for episode_dir in episode_dirs
        ]
        with ProcessPoolExecutor(max_workers=num_workers) as executor:
            future_to_episode = {
                executor.submit(process_episode_worker, item): Path(item[0]).name
                for item in worker_args
            }
            for future in as_completed(future_to_episode):
                result = future.result()
                results.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
    else:
        for episode_dir in episode_dirs:
            result = process_episode(
                episode_dir,
                profile,
                args.keep_stationary_frames,
                args.target_fps,
                args.dry_run,
                video_workers,
            )
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)

    results.sort(key=lambda item: natural_key(str(item.get("episode") or "")))

    summary = {
        "ok": True,
        "input": str(input_path),
        "episodes": len(results),
        "trimmed": sum(1 for item in results if item["status"] == "trimmed"),
        "removed_frames": sum(int(item["removed_frame_count"]) for item in results),
        "results": results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
