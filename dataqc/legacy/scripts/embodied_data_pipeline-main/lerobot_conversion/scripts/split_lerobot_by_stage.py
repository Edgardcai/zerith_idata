#!/usr/bin/env python3
"""Split a Zerith LeRobot v2 dataset into its two recorded subtasks.

The source LeRobot episode is associated with its current HDF5 episode through
``meta/episode_name_mapping.json``.  ``/subtask_transitions`` is treated as the
authoritative boundary: ``[stage_1_end_exclusive, total_frames]``.

Both outputs are built in sibling temporary directories and validated before a
transactional rename.  Existing outputs are never touched unless ``--overwrite``
is supplied, and are restored if either rename fails.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Iterable

import cv2
import h5py
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from lerobot.datasets.lerobot_dataset import LeRobotDatasetMetadata
from lerobot.datasets.compute_stats import sample_indices


MAPPING_NAME = "episode_name_mapping.json"
REQUIRED_META_FILES = (
    "info.json",
    "tasks.jsonl",
    "episodes.jsonl",
    "episodes_stats.jsonl",
    MAPPING_NAME,
)
EXPECTED_VIDEO_KEYS = {
    "observation.images.cam_high",
    "observation.images.cam_left_wrist",
    "observation.images.cam_right_wrist",
}
REQUIRED_FRAME_COLUMNS = {
    "observation.state",
    "action",
    "timestamp",
    "frame_index",
    "episode_index",
    "index",
    "task_index",
}
ZERITH_HDF5_PARTS = {
    "action/arm/position": 14,
    "action/effector/position": 2,
    "action/waist/position": 3,
    "action/head/position": 2,
    "action/base/velocity": 2,
    "observation/state/arm/position": 14,
    "observation/state/effector/position": 2,
    "observation/state/waist/position": 3,
    "observation/state/head/position": 2,
    "observation/state/base/velocity": 2,
}
STAGE_SEPARATOR = re.compile(r"\s*(?:\band\s+then\b|\bthen\b|然后|接着)\s*", re.IGNORECASE)


class SplitError(RuntimeError):
    """Raised for an unsafe or structurally invalid split request."""


@dataclass(frozen=True)
class VideoProbe:
    frames: int
    width: int
    height: int
    fps: float


@dataclass(frozen=True)
class EpisodePlan:
    source_index: int
    output_index: int
    source_episode_row: dict[str, Any]
    source_mapping_row: dict[str, Any]
    source_parquet: Path
    source_videos: dict[str, Path]
    hdf5_path: Path
    total_frames: int
    transition: int
    source_task: str
    left_task: str
    right_task: str

    @property
    def left_length(self) -> int:
        return self.transition

    @property
    def right_length(self) -> int:
        return self.total_frames - self.transition


@dataclass(frozen=True)
class SourceDataset:
    root: Path
    hdf5_root: Path
    info: dict[str, Any]
    mapping: dict[str, Any]
    video_keys: tuple[str, ...]
    plans: tuple[EpisodePlan, ...]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Split a mapped Zerith LeRobot v2 dataset into left(stage 1) and right(stage 2)."
    )
    parser.add_argument("--hdf5-root", type=Path, required=True, help="Current Zerith HDF5 dataset root.")
    parser.add_argument(
        "--source-dataset",
        type=Path,
        required=True,
        help="One existing LeRobot grade directory, for example .../lerobot/twohands/dataset/A.",
    )
    parser.add_argument("--left-output", type=Path, required=True, help="Output LeRobot dataset for stage 1.")
    parser.add_argument("--right-output", type=Path, required=True, help="Output LeRobot dataset for stage 2.")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Atomically replace existing left/right outputs after both new datasets validate.",
    )
    return parser.parse_args(argv)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SplitError(f"required file is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SplitError(f"invalid JSON in {path}: {exc}") from exc


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise SplitError(f"required file is missing: {path}") from exc
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SplitError(f"invalid JSONL in {path}:{line_number}: {exc}") from exc
        if not isinstance(row, dict):
            raise SplitError(f"JSONL row must be an object: {path}:{line_number}")
        rows.append(row)
    return rows


def jsonable(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=jsonable)
        handle.write("\n")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=jsonable))
            handle.write("\n")


def require_int(value: Any, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise SplitError(f"{label} must be an integer, got {value!r}")
    result = int(value)
    if minimum is not None and result < minimum:
        raise SplitError(f"{label} must be >= {minimum}, got {result}")
    return result


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def safe_relative_path(value: str, label: str) -> Path:
    path = Path(value)
    if not value.strip() or path.is_absolute() or ".." in path.parts:
        raise SplitError(f"{label} must be a non-escaping relative path, got {value!r}")
    return path


def rendered_path(info: dict[str, Any], episode_index: int, *, video_key: str | None = None) -> Path:
    chunk_size = require_int(info.get("chunks_size"), "info.chunks_size", minimum=1)
    episode_chunk = episode_index // chunk_size
    if video_key is None:
        pattern = info.get("data_path")
        label = "info.data_path"
    else:
        pattern = info.get("video_path")
        label = "info.video_path"
    if not isinstance(pattern, str) or not pattern:
        raise SplitError(f"{label} is missing or invalid")
    try:
        value = pattern.format(
            episode_chunk=episode_chunk,
            episode_index=episode_index,
            video_key=video_key,
        )
    except (KeyError, IndexError, ValueError) as exc:
        raise SplitError(f"could not render {label}: {pattern!r}: {exc}") from exc
    return safe_relative_path(value, label)


def validate_path_layout(hdf5_root: Path, source: Path, left: Path, right: Path, overwrite: bool) -> None:
    for label, path in (("HDF5 root", hdf5_root), ("source dataset", source)):
        if not path.is_dir():
            raise SplitError(f"{label} is not a directory: {path}")

    named = (("HDF5 root", hdf5_root), ("source dataset", source), ("left output", left), ("right output", right))
    for i, (left_label, left_path) in enumerate(named):
        for right_label, right_path in named[i + 1 :]:
            if left_path == right_path:
                raise SplitError(f"{left_label} and {right_label} resolve to the same path: {left_path}")

    for output_label, output in (("left output", left), ("right output", right)):
        if is_relative_to(source, output) or is_relative_to(output, source):
            raise SplitError(f"{output_label} must not overlap source dataset: {output}")
        if is_relative_to(hdf5_root, output) or output == hdf5_root:
            raise SplitError(f"{output_label} must not contain or equal the HDF5 root: {output}")
        if output.exists():
            if not output.is_dir():
                raise SplitError(f"{output_label} exists and is not a directory: {output}")
            if not overwrite:
                raise SplitError(f"{output_label} already exists; pass --overwrite to replace it: {output}")

    if is_relative_to(left, right) or is_relative_to(right, left):
        raise SplitError("left and right output directories must not contain one another")


def indexed_rows(rows: list[dict[str, Any]], key: str, label: str) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for position, row in enumerate(rows):
        index = require_int(row.get(key), f"{label}[{position}].{key}", minimum=0)
        if index in result:
            raise SplitError(f"duplicate {key}={index} in {label}")
        result[index] = row
    return result


def require_contiguous(indices: set[int], expected_count: int, label: str) -> None:
    expected = set(range(expected_count))
    if indices != expected:
        missing = sorted(expected - indices)
        extra = sorted(indices - expected)
        raise SplitError(f"{label} is not contiguous 0..{expected_count - 1}: missing={missing}, extra={extra}")


def resolve_hdf5_path(hdf5_root: Path, mapping_row: dict[str, Any], source_index: int) -> Path:
    candidates: list[Path] = []
    for key in ("hdf5_file", "source_h5"):
        value = mapping_row.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(safe_relative_path(value, f"mapping episode {source_index}.{key}"))
    for key in ("hdf5_episode_dir", "source_episode_dir", "source_episode_name", "source_file_name"):
        value = mapping_row.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(safe_relative_path(value, f"mapping episode {source_index}.{key}") / "episode.hdf5")

    found: list[Path] = []
    root = hdf5_root.resolve(strict=True)
    for relative in candidates:
        candidate = (root / relative).resolve(strict=False)
        if not is_relative_to(candidate, root):
            raise SplitError(f"mapping episode {source_index} escapes HDF5 root: {relative}")
        if candidate.is_file() and candidate not in found:
            found.append(candidate)
    if not found:
        raise SplitError(f"mapping episode {source_index} does not resolve to an HDF5 file under {root}")
    if len(found) > 1:
        raise SplitError(f"mapping episode {source_index} resolves ambiguously to multiple HDF5 files: {found}")
    return found[0]


def validate_hdf5_episode(path: Path) -> tuple[int, int, str]:
    try:
        with h5py.File(path, "r") as file_obj:
            total_frames = require_int(file_obj.attrs.get("total_frames"), f"{path}@total_frames", minimum=2)
            total_subtasks = require_int(file_obj.attrs.get("total_subtasks"), f"{path}@total_subtasks")
            completed_subtasks = require_int(
                file_obj.attrs.get("completed_subtasks"), f"{path}@completed_subtasks"
            )
            if total_subtasks != 2 or completed_subtasks != 2:
                raise SplitError(
                    f"{path}: expected exactly two completed subtasks, got "
                    f"total={total_subtasks}, completed={completed_subtasks}"
                )
            if "subtask_transitions" not in file_obj:
                raise SplitError(f"{path}: missing /subtask_transitions")
            transitions = np.asarray(file_obj["subtask_transitions"])
            if transitions.shape != (2,) or transitions.dtype.kind not in "iu":
                raise SplitError(
                    f"{path}: /subtask_transitions must be an integer vector of shape (2,), "
                    f"got shape={transitions.shape}, dtype={transitions.dtype}"
                )
            transition, end = (int(value) for value in transitions.tolist())
            if not 0 < transition < end or end != total_frames:
                raise SplitError(
                    f"{path}: illegal two-stage transitions {transitions.tolist()} for {total_frames} frames"
                )

            if "timestamp/t" not in file_obj:
                raise SplitError(f"{path}: missing Zerith dataset /timestamp/t")
            timestamp = file_obj["timestamp/t"]
            if timestamp.shape != (total_frames,):
                raise SplitError(
                    f"{path}: /timestamp/t must have shape ({total_frames},), got {timestamp.shape}"
                )

            for dataset_name, expected_width in ZERITH_HDF5_PARTS.items():
                if dataset_name not in file_obj:
                    raise SplitError(f"{path}: missing Zerith dataset /{dataset_name}")
                dataset = file_obj[dataset_name]
                if dataset.shape != (total_frames, expected_width):
                    raise SplitError(
                        f"{path}: /{dataset_name} must have shape ({total_frames}, {expected_width}), "
                        f"got {dataset.shape}"
                    )
            task_name = file_obj.attrs.get("task_name", "")
            if isinstance(task_name, bytes):
                task_name = task_name.decode("utf-8", errors="replace")
            return total_frames, transition, str(task_name).strip()
    except OSError as exc:
        raise SplitError(f"cannot read HDF5 file {path}: {exc}") from exc


def parse_fraction(value: Any) -> float:
    try:
        result = float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError) as exc:
        raise SplitError(f"invalid video frame rate: {value!r}") from exc
    if not math.isfinite(result) or result <= 0:
        raise SplitError(f"invalid video frame rate: {value!r}")
    return result


def find_binary(name: str) -> str:
    binary = shutil.which(name)
    if not binary:
        raise SplitError(f"required executable was not found in PATH: {name}")
    return binary


def probe_video(path: Path, ffprobe: str) -> VideoProbe:
    command = [
        ffprobe,
        "-v",
        "error",
        "-count_frames",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=nb_read_frames,nb_frames,width,height,avg_frame_rate",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SplitError(f"could not probe video {path}: {detail}")
    try:
        streams = json.loads(result.stdout).get("streams", [])
    except json.JSONDecodeError as exc:
        raise SplitError(f"ffprobe returned invalid JSON for {path}: {exc}") from exc
    if len(streams) != 1:
        raise SplitError(f"expected one readable video stream in {path}, got {len(streams)}")
    stream = streams[0]
    frame_text = stream.get("nb_read_frames")
    if frame_text in (None, "N/A"):
        frame_text = stream.get("nb_frames")
    try:
        frames = int(frame_text)
        width = int(stream["width"])
        height = int(stream["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SplitError(f"video metadata is incomplete for {path}: {stream}") from exc
    if frames <= 0 or width <= 0 or height <= 0:
        raise SplitError(f"video metadata is invalid for {path}: {stream}")
    return VideoProbe(frames=frames, width=width, height=height, fps=parse_fraction(stream["avg_frame_rate"]))


def validate_vector_column(table: pa.Table, key: str, expected_width: int, label: str) -> None:
    if key not in table.column_names:
        raise SplitError(f"{label}: missing parquet column {key!r}")
    column_type = table.schema.field(key).type
    if pa.types.is_fixed_size_list(column_type) and column_type.list_size != expected_width:
        raise SplitError(f"{label}: {key} width is {column_type.list_size}, expected {expected_width}")
    values = table[key].to_pylist()
    bad = next((i for i, value in enumerate(values) if value is None or len(value) != expected_width), None)
    if bad is not None:
        raise SplitError(f"{label}: {key} row {bad} is not {expected_width}D")
    try:
        array = np.asarray(values, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise SplitError(f"{label}: {key} cannot be interpreted as numeric {expected_width}D data") from exc
    if array.shape != (table.num_rows, expected_width) or not np.all(np.isfinite(array)):
        raise SplitError(f"{label}: {key} contains non-finite or malformed values")


def column_numpy(table: pa.Table, key: str) -> np.ndarray:
    values = table[key].to_pylist()
    array = np.asarray(values)
    if len(array) != table.num_rows:
        raise SplitError(f"could not materialize parquet column {key!r}")
    return array


def require_array_equal(actual: np.ndarray, expected: np.ndarray, label: str) -> None:
    if actual.shape != expected.shape or not np.array_equal(actual, expected):
        raise SplitError(f"{label} is not the expected contiguous sequence")


def source_task_for_episode(
    row: dict[str, Any], task_by_index: dict[int, str], table: pa.Table, label: str
) -> str:
    indices = column_numpy(table, "task_index")
    unique_indices = sorted({require_int(value, f"{label}.task_index", minimum=0) for value in indices.tolist()})
    if len(unique_indices) != 1:
        raise SplitError(f"{label}: expected exactly one task_index, got {unique_indices}")
    missing = [index for index in unique_indices if index not in task_by_index]
    if missing:
        raise SplitError(f"{label}: parquet references missing task indices {missing}")

    listed_tasks = row.get("tasks")
    if not isinstance(listed_tasks, list) or not listed_tasks or not all(
        isinstance(value, str) and value.strip() for value in listed_tasks
    ):
        raise SplitError(f"{label}: episodes.jsonl must contain a non-empty string list in 'tasks'")
    if len(listed_tasks) != 1:
        raise SplitError(f"{label}: expected exactly one episode task, got {listed_tasks!r}")
    indexed_tasks = [task_by_index[index] for index in unique_indices]
    if set(listed_tasks) != set(indexed_tasks):
        raise SplitError(
            f"{label}: episodes.jsonl tasks {listed_tasks!r} do not match parquet task indices {indexed_tasks!r}"
        )
    task = row.get("task")
    if isinstance(task, str) and task.strip():
        source_task = task.strip()
    elif len(listed_tasks) == 1:
        source_task = listed_tasks[0].strip()
    else:
        raise SplitError(f"{label}: cannot choose one source task from {listed_tasks!r}")
    if source_task not in listed_tasks:
        raise SplitError(f"{label}: 'task' is not present in 'tasks': {source_task!r}")
    return source_task


def split_task_text(task: str) -> tuple[str, str]:
    def standalone(text: str) -> str:
        text = text.strip()
        if text and text[0].islower():
            return text[0].upper() + text[1:]
        return text

    parts = STAGE_SEPARATOR.split(task.strip(), maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return standalone(parts[0]), standalone(parts[1])
    return f"Left-hand stage of: {task.strip()}", f"Right-hand stage of: {task.strip()}"


def validate_mapping_file_reference(
    mapping_row: dict[str, Any], key: str, expected: Path, source_root: Path, label: str
) -> None:
    value = mapping_row.get(key)
    if not isinstance(value, str) or not value.strip():
        return
    mapped = (source_root / safe_relative_path(value, label)).resolve(strict=False)
    if mapped != expected.resolve(strict=False):
        raise SplitError(f"{label} points to {mapped}, expected {expected}")


def load_and_validate_source(source_root: Path, hdf5_root: Path, ffprobe: str) -> SourceDataset:
    meta_root = source_root / "meta"
    for name in REQUIRED_META_FILES:
        if not (meta_root / name).is_file():
            raise SplitError(f"source LeRobot metadata is incomplete: missing {meta_root / name}")

    info = read_json(meta_root / "info.json")
    if not isinstance(info, dict):
        raise SplitError("meta/info.json must contain an object")
    version = str(info.get("codebase_version", ""))
    if not version.startswith("v2."):
        raise SplitError(f"only LeRobot v2 datasets are supported, got codebase_version={version!r}")
    if str(info.get("robot_type", "")).strip().lower() != "zerith":
        raise SplitError(f"only robot_type='zerith' is supported, got {info.get('robot_type')!r}")
    total_episodes = require_int(info.get("total_episodes"), "info.total_episodes", minimum=1)
    total_frames = require_int(info.get("total_frames"), "info.total_frames", minimum=1)
    fps = float(info.get("fps", 0))
    if not math.isfinite(fps) or fps <= 0:
        raise SplitError(f"info.fps must be positive, got {info.get('fps')!r}")
    features = info.get("features")
    if not isinstance(features, dict):
        raise SplitError("info.features must be an object")
    for key in ("observation.state", "action"):
        feature = features.get(key)
        if not isinstance(feature, dict) or feature.get("dtype") not in ("float32", "float64"):
            raise SplitError(f"info.features.{key} is missing or not floating-point")
        if list(feature.get("shape", [])) != [23]:
            raise SplitError(f"only Zerith 23D {key} is supported, got shape={feature.get('shape')!r}")
    video_keys = tuple(sorted(key for key, value in features.items() if value.get("dtype") == "video"))
    if set(video_keys) != EXPECTED_VIDEO_KEYS:
        raise SplitError(
            f"expected the three Zerith video keys {sorted(EXPECTED_VIDEO_KEYS)}, got {list(video_keys)}"
        )
    if require_int(info.get("total_videos"), "info.total_videos", minimum=0) != total_episodes * len(video_keys):
        raise SplitError("info.total_videos does not match total_episodes * number of video keys")

    task_rows = read_jsonl(meta_root / "tasks.jsonl")
    task_indexed = indexed_rows(task_rows, "task_index", "tasks.jsonl")
    require_contiguous(set(task_indexed), len(task_rows), "tasks.jsonl task indices")
    task_by_index: dict[int, str] = {}
    for index, row in task_indexed.items():
        task = row.get("task")
        if not isinstance(task, str) or not task.strip():
            raise SplitError(f"tasks.jsonl task_index={index} has an empty task")
        task_by_index[index] = task
    if require_int(info.get("total_tasks"), "info.total_tasks", minimum=1) != len(task_by_index):
        raise SplitError("info.total_tasks does not match meta/tasks.jsonl")

    episode_rows = read_jsonl(meta_root / "episodes.jsonl")
    episodes = indexed_rows(episode_rows, "episode_index", "episodes.jsonl")
    require_contiguous(set(episodes), total_episodes, "episodes.jsonl episode indices")
    source_stats = indexed_rows(
        read_jsonl(meta_root / "episodes_stats.jsonl"), "episode_index", "episodes_stats.jsonl"
    )
    require_contiguous(set(source_stats), total_episodes, "episodes_stats.jsonl episode indices")

    mapping = read_json(meta_root / MAPPING_NAME)
    if not isinstance(mapping, dict) or not isinstance(mapping.get("episodes"), list):
        raise SplitError(f"{meta_root / MAPPING_NAME} must contain an 'episodes' list")
    mapping_rows = indexed_rows(mapping["episodes"], "lerobot_episode_index", MAPPING_NAME)
    if set(mapping_rows) != set(episodes):
        missing = sorted(set(episodes) - set(mapping_rows))
        extra = sorted(set(mapping_rows) - set(episodes))
        raise SplitError(f"episode mapping does not exactly cover source episodes: missing={missing}, extra={extra}")

    plans: list[EpisodePlan] = []
    seen_hdf5_paths: set[Path] = set()
    expected_source_global_index = 0
    summed_frames = 0
    for output_index, source_index in enumerate(sorted(episodes)):
        label = f"source episode {source_index}"
        row = episodes[source_index]
        mapping_row = mapping_rows[source_index]
        parquet_path = source_root / rendered_path(info, source_index)
        if not parquet_path.is_file():
            raise SplitError(f"{label}: missing parquet file {parquet_path}")
        if not isinstance(mapping_row.get("lerobot_data_file"), str):
            raise SplitError(f"{label}: mapping row is missing lerobot_data_file")
        validate_mapping_file_reference(
            mapping_row, "lerobot_data_file", parquet_path, source_root, f"{label}.mapping.lerobot_data_file"
        )
        try:
            table = pq.read_table(parquet_path)
        except Exception as exc:
            raise SplitError(f"{label}: could not read parquet {parquet_path}: {exc}") from exc
        missing_columns = sorted(REQUIRED_FRAME_COLUMNS - set(table.column_names))
        if missing_columns:
            raise SplitError(f"{label}: parquet is missing required columns {missing_columns}")
        source_length = require_int(row.get("length"), f"{label}.episodes.length", minimum=1)
        if table.num_rows != source_length:
            raise SplitError(f"{label}: parquet rows {table.num_rows} != episodes length {source_length}")
        if "num_frames" not in mapping_row:
            raise SplitError(f"{label}: mapping row is missing num_frames")
        if require_int(mapping_row["num_frames"], f"{label}.mapping.num_frames", minimum=1) != source_length:
            raise SplitError(f"{label}: mapping num_frames does not match source parquet")
        for dim_key in ("state_dim", "action_dim"):
            if dim_key not in mapping_row or require_int(mapping_row[dim_key], f"{label}.mapping.{dim_key}") != 23:
                raise SplitError(f"{label}: mapping {dim_key} must explicitly be 23")
        validate_vector_column(table, "observation.state", 23, label)
        validate_vector_column(table, "action", 23, label)
        require_array_equal(column_numpy(table, "frame_index"), np.arange(source_length), f"{label}.frame_index")
        require_array_equal(
            column_numpy(table, "episode_index"), np.full(source_length, source_index), f"{label}.episode_index"
        )
        require_array_equal(
            column_numpy(table, "index"),
            np.arange(expected_source_global_index, expected_source_global_index + source_length),
            f"{label}.index",
        )
        timestamps = column_numpy(table, "timestamp").astype(np.float64)
        if timestamps.shape != (source_length,) or not np.all(np.isfinite(timestamps)):
            raise SplitError(f"{label}: timestamps are missing or non-finite")
        if source_length > 1 and np.any(np.diff(timestamps) <= 0):
            raise SplitError(f"{label}: timestamps are not strictly increasing")

        source_task = source_task_for_episode(row, task_by_index, table, label)
        mapped_task = mapping_row.get("task")
        if not isinstance(mapped_task, str) or mapped_task.strip() != source_task:
            raise SplitError(
                f"{label}: mapping task {mapped_task!r} does not match episodes/parquet task {source_task!r}"
            )
        left_task, right_task = split_task_text(source_task)
        hdf5_path = resolve_hdf5_path(hdf5_root, mapping_row, source_index)
        if hdf5_path in seen_hdf5_paths:
            raise SplitError(f"{label}: multiple LeRobot episodes map to the same HDF5 file {hdf5_path}")
        seen_hdf5_paths.add(hdf5_path)
        hdf5_frames, transition, _ = validate_hdf5_episode(hdf5_path)
        if hdf5_frames != source_length:
            raise SplitError(
                f"{label}: current HDF5 frames {hdf5_frames} != source LeRobot frames {source_length}; "
                "reconvert the LeRobot dataset after HDF5 edits"
            )

        source_videos: dict[str, Path] = {}
        mapped_videos = mapping_row.get("lerobot_video_files")
        if not isinstance(mapped_videos, dict) or set(mapped_videos) != set(video_keys):
            raise SplitError(
                f"{label}: mapping lerobot_video_files must exactly cover {list(video_keys)}"
            )
        for video_key in video_keys:
            video_path = source_root / rendered_path(info, source_index, video_key=video_key)
            if not video_path.is_file():
                raise SplitError(f"{label}: missing video {video_path}")
            validate_mapping_file_reference(
                {"video": mapped_videos[video_key]},
                "video",
                video_path,
                source_root,
                f"{label}.mapping.lerobot_video_files[{video_key!r}]",
            )
            probe = probe_video(video_path, ffprobe)
            expected_shape = list(features[video_key].get("shape", []))
            if len(expected_shape) != 3 or expected_shape[2] != 3:
                raise SplitError(f"{label}: invalid video feature shape for {video_key}: {expected_shape}")
            if (probe.height, probe.width) != (int(expected_shape[0]), int(expected_shape[1])):
                raise SplitError(
                    f"{label}: {video_key} dimensions {probe.width}x{probe.height} do not match "
                    f"info shape {expected_shape}"
                )
            if probe.frames != source_length:
                raise SplitError(
                    f"{label}: {video_key} frames {probe.frames} != source LeRobot frames {source_length}"
                )
            if abs(probe.fps - fps) > 0.1:
                raise SplitError(f"{label}: {video_key} fps {probe.fps:.6f} != info fps {fps:.6f}")
            source_videos[video_key] = video_path

        plans.append(
            EpisodePlan(
                source_index=source_index,
                output_index=output_index,
                source_episode_row=copy.deepcopy(row),
                source_mapping_row=copy.deepcopy(mapping_row),
                source_parquet=parquet_path,
                source_videos=source_videos,
                hdf5_path=hdf5_path,
                total_frames=source_length,
                transition=transition,
                source_task=source_task,
                left_task=left_task,
                right_task=right_task,
            )
        )
        expected_source_global_index += source_length
        summed_frames += source_length

    if summed_frames != total_frames:
        raise SplitError(f"info.total_frames={total_frames} but episode lengths sum to {summed_frames}")
    expected_chunks = math.ceil(total_episodes / require_int(info["chunks_size"], "info.chunks_size", minimum=1))
    if require_int(info.get("total_chunks"), "info.total_chunks", minimum=1) != expected_chunks:
        raise SplitError("info.total_chunks does not match the episode count and chunks_size")

    # Reuse LeRobot's v2 metadata loader as an additional compatibility check.
    try:
        metadata = LeRobotDatasetMetadata(repo_id="local/stage-split-source", root=source_root)
    except Exception as exc:
        raise SplitError(f"LeRobot could not load source metadata: {exc}") from exc
    if metadata.total_episodes != total_episodes or metadata.total_frames != total_frames:
        raise SplitError("LeRobot metadata loader disagrees with source info totals")

    return SourceDataset(
        root=source_root,
        hdf5_root=hdf5_root,
        info=info,
        mapping=mapping,
        video_keys=video_keys,
        plans=tuple(plans),
    )


def replace_column(table: pa.Table, name: str, values: np.ndarray) -> pa.Table:
    index = table.schema.get_field_index(name)
    if index < 0:
        raise SplitError(f"cannot rebuild missing parquet column {name!r}")
    target_type = table.schema.field(index).type
    try:
        column = pa.array(values.tolist(), type=target_type)
    except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError, ValueError) as exc:
        raise SplitError(f"could not rebuild parquet column {name!r} as {target_type}: {exc}") from exc
    return table.set_column(index, name, column)


def split_parquet_table(
    source_table: pa.Table,
    start: int,
    end: int,
    output_index: int,
    global_start: int,
    task_index: int,
    fps: float,
) -> pa.Table:
    length = end - start
    if length <= 0:
        raise SplitError(f"cannot write an empty stage: start={start}, end={end}")
    table = source_table.slice(start, length)
    table = replace_column(table, "timestamp", np.arange(length, dtype=np.float64) / fps)
    table = replace_column(table, "frame_index", np.arange(length, dtype=np.int64))
    table = replace_column(table, "episode_index", np.full(length, output_index, dtype=np.int64))
    table = replace_column(
        table, "index", np.arange(global_start, global_start + length, dtype=np.int64)
    )
    table = replace_column(table, "task_index", np.full(length, task_index, dtype=np.int64))
    return table


def get_feature_stats(array: np.ndarray, axis: int | tuple[int, ...], keepdims: bool) -> dict[str, np.ndarray]:
    return {
        "min": np.min(array, axis=axis, keepdims=keepdims),
        "max": np.max(array, axis=axis, keepdims=keepdims),
        "mean": np.mean(array, axis=axis, keepdims=keepdims),
        "std": np.std(array, axis=axis, keepdims=keepdims),
        "count": np.asarray([len(array)], dtype=np.int64),
    }


def numeric_episode_stats(table: pa.Table, features: dict[str, Any]) -> dict[str, dict[str, np.ndarray]]:
    stats: dict[str, dict[str, np.ndarray]] = {}
    for key, feature in features.items():
        dtype = feature.get("dtype")
        if dtype in ("video", "image", "string"):
            continue
        if key not in table.column_names:
            raise SplitError(f"output parquet is missing numeric feature {key!r}")
        array = column_numpy(table, key)
        if array.ndim == 0 or len(array) == 0:
            raise SplitError(f"output feature {key!r} is empty")
        stats[key] = get_feature_stats(array, axis=0, keepdims=array.ndim == 1)
    return stats


def split_video(
    source: Path,
    output: Path,
    start: int,
    end: int,
    fps: float,
    ffmpeg: str,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fps_text = f"{fps:.12g}"
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vf",
        f"trim=start_frame={start}:end_frame={end},setpts=N/({fps_text}*TB)",
        "-an",
        "-r",
        fps_text,
        "-fps_mode",
        "cfr",
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
        str(output),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not output.is_file() or output.stat().st_size <= 0:
        detail = (result.stderr or result.stdout).strip()
        raise SplitError(f"ffmpeg failed while splitting {source} frames [{start}, {end}): {detail}")


def video_episode_stats(path: Path, expected_frames: int) -> dict[str, np.ndarray]:
    wanted = sample_indices(expected_frames)
    wanted_set = set(wanted)
    sampled: list[np.ndarray] = []
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise SplitError(f"could not decode output video: {path}")
    count = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            if count in wanted_set:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                sampled.append(np.transpose(rgb, (2, 0, 1)))
            count += 1
    finally:
        capture.release()
    if count != expected_frames:
        raise SplitError(f"decoded output video frames {count} != expected {expected_frames}: {path}")
    if len(sampled) != len(wanted):
        raise SplitError(f"could not collect all sampled frames from {path}")
    array = np.stack(sampled).astype(np.uint8, copy=False)
    raw = get_feature_stats(array, axis=(0, 2, 3), keepdims=True)
    return {
        key: value if key == "count" else np.squeeze(value.astype(np.float64) / 255.0, axis=0)
        for key, value in raw.items()
    }


def unique_tasks(source: SourceDataset, stage: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    tasks: list[dict[str, Any]] = []
    task_to_index: dict[str, int] = {}
    for plan in source.plans:
        task = plan.left_task if stage == "left" else plan.right_task
        if task not in task_to_index:
            index = len(task_to_index)
            task_to_index[task] = index
            tasks.append({"task_index": index, "task": task})
    return tasks, task_to_index


def output_episode_row(plan: EpisodePlan, stage: str, length: int, task: str) -> dict[str, Any]:
    row = copy.deepcopy(plan.source_episode_row)
    row.update(
        {
            "episode_index": plan.output_index,
            "tasks": [task],
            "length": length,
            "task": task,
            "full_instructions_en": [task],
            "subtask_segments": [
                {
                    "start": 0,
                    "end": length - 1,
                    "subtask": task,
                    "id": "stage_1" if stage == "left" else "stage_2",
                    "stage_number": 1 if stage == "left" else 2,
                    "source": "hdf5:/subtask_transitions",
                }
            ],
            "stage_count": 1,
            "subtask_transitions": [length],
            "split_stage": 1 if stage == "left" else 2,
            "split_stage_name": stage,
            "source_lerobot_episode_index": plan.source_index,
            "source_task": plan.source_task,
            "source_subtask_transitions": [plan.transition, plan.total_frames],
            "source_stage_start_frame": 0 if stage == "left" else plan.transition,
            "source_stage_end_frame_exclusive": plan.transition if stage == "left" else plan.total_frames,
        }
    )
    return row


def output_mapping_row(
    plan: EpisodePlan,
    source: SourceDataset,
    stage: str,
    output_root: Path,
    length: int,
    task: str,
) -> dict[str, Any]:
    row = copy.deepcopy(plan.source_mapping_row)
    start = 0 if stage == "left" else plan.transition
    end = plan.transition if stage == "left" else plan.total_frames
    data_file = rendered_path(source.info, plan.output_index)
    video_files = {
        key: str(rendered_path(source.info, plan.output_index, video_key=key)) for key in source.video_keys
    }
    row.update(
        {
            "source_lerobot_episode_index": plan.source_index,
            "source_lerobot_episode_name": f"episode_{plan.source_index:06d}",
            "lerobot_dataset_dir": str(output_root),
            "lerobot_episode_index": plan.output_index,
            "lerobot_episode_name": f"episode_{plan.output_index:06d}",
            "converted_file_name": f"episode_{plan.output_index:06d}",
            "num_frames": length,
            "state_dim": 23,
            "action_dim": 23,
            "task": task,
            "lerobot_data_file": str(data_file),
            "lerobot_video_files": video_files,
            "grade_episode_index": plan.output_index,
            "split_stage": 1 if stage == "left" else 2,
            "split_stage_name": stage,
            "source_num_frames": plan.total_frames,
            "source_task": plan.source_task,
            "source_stage_start_frame": start,
            "source_stage_end_frame_exclusive": end,
            "source_subtask_transitions": [plan.transition, plan.total_frames],
            "hdf5_file": str(plan.hdf5_path.relative_to(source.hdf5_root)),
        }
    )
    return row


def output_mapping(
    source: SourceDataset, stage: str, final_output: Path, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    mapping = copy.deepcopy(source.mapping)
    source_repo_id = str(mapping.get("repo_id") or source.root.name).rstrip("/")
    repo_parts = PurePosixPath(source_repo_id).parts
    if not repo_parts or source_repo_id.startswith("/") or ".." in repo_parts:
        raise SplitError(f"source mapping repo_id is invalid: {source_repo_id!r}")
    hand_component = "left_hand" if stage == "left" else "righthand"
    output_repo_id = "/".join((hand_component, *repo_parts))
    mapping.update(
        {
            "repo_id": output_repo_id,
            "data_dir": str(source.hdf5_root),
            "output_dataset": str(final_output),
            "mapping_file": f"meta/{MAPPING_NAME}",
            "split_from_lerobot_dataset": str(source.root),
            "split_stage": 1 if stage == "left" else 2,
            "split_stage_name": stage,
            "episode_count": len(rows),
            "episodes": rows,
        }
    )
    return mapping


def build_output_dataset(
    source: SourceDataset,
    stage: str,
    temp_root: Path,
    final_root: Path,
    ffmpeg: str,
    ffprobe: str,
) -> dict[str, int]:
    if stage not in ("left", "right"):
        raise ValueError(stage)
    task_rows, task_to_index = unique_tasks(source, stage)
    episode_rows: list[dict[str, Any]] = []
    stats_rows: list[dict[str, Any]] = []
    mapping_rows: list[dict[str, Any]] = []
    global_index = 0

    for plan in source.plans:
        start = 0 if stage == "left" else plan.transition
        end = plan.transition if stage == "left" else plan.total_frames
        length = end - start
        task = plan.left_task if stage == "left" else plan.right_task
        task_index = task_to_index[task]
        try:
            source_table = pq.read_table(plan.source_parquet)
        except Exception as exc:
            raise SplitError(f"could not reread source parquet {plan.source_parquet}: {exc}") from exc
        table = split_parquet_table(
            source_table,
            start,
            end,
            plan.output_index,
            global_index,
            task_index,
            float(source.info["fps"]),
        )
        data_path = temp_root / rendered_path(source.info, plan.output_index)
        data_path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, data_path, compression="zstd")

        stats = numeric_episode_stats(table, source.info["features"])
        for video_key in source.video_keys:
            video_path = temp_root / rendered_path(source.info, plan.output_index, video_key=video_key)
            split_video(
                plan.source_videos[video_key],
                video_path,
                start,
                end,
                float(source.info["fps"]),
                ffmpeg,
            )
            probe = probe_video(video_path, ffprobe)
            expected_shape = source.info["features"][video_key]["shape"]
            if probe.frames != length:
                raise SplitError(f"split video {video_path} has {probe.frames} frames, expected {length}")
            if (probe.height, probe.width) != (int(expected_shape[0]), int(expected_shape[1])):
                raise SplitError(f"split video {video_path} has unexpected dimensions")
            if abs(probe.fps - float(source.info["fps"])) > 0.1:
                raise SplitError(
                    f"split video {video_path} fps {probe.fps:.6f} != info fps {source.info['fps']}"
                )
            stats[video_key] = video_episode_stats(video_path, length)

        episode_rows.append(output_episode_row(plan, stage, length, task))
        stats_rows.append({"episode_index": plan.output_index, "stats": stats})
        mapping_rows.append(output_mapping_row(plan, source, stage, final_root, length, task))
        global_index += length

    info = copy.deepcopy(source.info)
    total_episodes = len(source.plans)
    chunk_size = require_int(info["chunks_size"], "info.chunks_size", minimum=1)
    info.update(
        {
            "robot_type": "zerith",
            "total_episodes": total_episodes,
            "total_frames": global_index,
            "total_tasks": len(task_rows),
            "total_videos": total_episodes * len(source.video_keys),
            "total_chunks": math.ceil(total_episodes / chunk_size),
            "splits": {"train": f"0:{total_episodes}"},
        }
    )
    write_json(temp_root / "meta/info.json", info)
    write_jsonl(temp_root / "meta/tasks.jsonl", task_rows)
    write_jsonl(temp_root / "meta/episodes.jsonl", episode_rows)
    write_jsonl(temp_root / "meta/episodes_stats.jsonl", stats_rows)
    write_json(
        temp_root / f"meta/{MAPPING_NAME}", output_mapping(source, stage, final_root, mapping_rows)
    )
    validate_output_dataset(temp_root, info, source, stage, ffprobe)
    return {"episodes": total_episodes, "frames": global_index, "tasks": len(task_rows)}


def validate_stats_row(row: dict[str, Any], features: dict[str, Any], label: str) -> None:
    stats = row.get("stats")
    if not isinstance(stats, dict) or set(stats) != set(features):
        raise SplitError(
            f"{label}: stats keys do not match features: missing={sorted(set(features) - set(stats or {}))}, "
            f"extra={sorted(set(stats or {}) - set(features))}"
        )
    for feature_key, feature_stats in stats.items():
        if not isinstance(feature_stats, dict) or set(feature_stats) != {"min", "max", "mean", "std", "count"}:
            raise SplitError(f"{label}: malformed stats for {feature_key}")
        count = feature_stats["count"]
        if not isinstance(count, list) or len(count) != 1 or not isinstance(count[0], int) or count[0] <= 0:
            raise SplitError(f"{label}: invalid stats count for {feature_key}: {count!r}")


def validate_output_dataset(
    root: Path, info: dict[str, Any], source: SourceDataset, stage: str, ffprobe: str
) -> None:
    episodes = indexed_rows(read_jsonl(root / "meta/episodes.jsonl"), "episode_index", "output episodes")
    stats = indexed_rows(read_jsonl(root / "meta/episodes_stats.jsonl"), "episode_index", "output stats")
    tasks = indexed_rows(read_jsonl(root / "meta/tasks.jsonl"), "task_index", "output tasks")
    mapping = read_json(root / f"meta/{MAPPING_NAME}")
    mapping_rows = indexed_rows(mapping.get("episodes", []), "lerobot_episode_index", "output mapping")
    expected_indices = set(range(len(source.plans)))
    for label, actual in (
        ("episodes", set(episodes)),
        ("stats", set(stats)),
        ("mapping", set(mapping_rows)),
    ):
        if actual != expected_indices:
            raise SplitError(f"output {label} does not cover every episode")
    require_contiguous(set(tasks), len(tasks), "output task indices")

    global_index = 0
    total_frames = 0
    for plan in source.plans:
        output_index = plan.output_index
        expected_length = plan.left_length if stage == "left" else plan.right_length
        row = episodes[output_index]
        if require_int(row.get("length"), f"output episode {output_index}.length") != expected_length:
            raise SplitError(f"output episode {output_index} length is incorrect")
        if row.get("stage_count") != 1 or row.get("subtask_transitions") != [expected_length]:
            raise SplitError(f"output episode {output_index} does not contain valid local single-stage metadata")
        segments = row.get("subtask_segments")
        if not isinstance(segments, list) or len(segments) != 1:
            raise SplitError(f"output episode {output_index} must contain exactly one local subtask segment")
        segment = segments[0]
        if segment.get("start") != 0 or segment.get("end") != expected_length - 1:
            raise SplitError(f"output episode {output_index} local subtask segment is out of bounds")
        expected_source_start = 0 if stage == "left" else plan.transition
        expected_source_end = plan.transition if stage == "left" else plan.total_frames
        if row.get("source_subtask_transitions") != [plan.transition, plan.total_frames]:
            raise SplitError(f"output episode {output_index} lost source subtask transitions")
        if (
            row.get("source_stage_start_frame") != expected_source_start
            or row.get("source_stage_end_frame_exclusive") != expected_source_end
        ):
            raise SplitError(f"output episode {output_index} source stage provenance is incorrect")
        data_path = root / rendered_path(info, output_index)
        table = pq.read_table(data_path)
        if table.num_rows != expected_length:
            raise SplitError(f"output parquet {data_path} has the wrong row count")
        validate_vector_column(table, "observation.state", 23, str(data_path))
        validate_vector_column(table, "action", 23, str(data_path))
        require_array_equal(column_numpy(table, "frame_index"), np.arange(expected_length), f"{data_path}.frame_index")
        require_array_equal(
            column_numpy(table, "episode_index"), np.full(expected_length, output_index), f"{data_path}.episode_index"
        )
        require_array_equal(
            column_numpy(table, "index"),
            np.arange(global_index, global_index + expected_length),
            f"{data_path}.index",
        )
        expected_timestamps = np.arange(expected_length, dtype=np.float64) / float(info["fps"])
        timestamps = column_numpy(table, "timestamp").astype(np.float64)
        if not np.allclose(timestamps, expected_timestamps, rtol=0, atol=1e-5):
            raise SplitError(f"{data_path}.timestamp was not reset to frame_index/fps")
        task_indices = column_numpy(table, "task_index")
        if any(int(value) not in tasks for value in task_indices.tolist()):
            raise SplitError(f"{data_path} references an undefined task index")
        for video_key in source.video_keys:
            video_path = root / rendered_path(info, output_index, video_key=video_key)
            video_probe = probe_video(video_path, ffprobe)
            if video_probe.frames != expected_length:
                raise SplitError(f"output video {video_path} has the wrong frame count")
            if abs(video_probe.fps - float(info["fps"])) > 0.1:
                raise SplitError(f"output video {video_path} has the wrong frame rate")
        validate_stats_row(stats[output_index], info["features"], f"output stats episode {output_index}")
        mapping_row = mapping_rows[output_index]
        if require_int(mapping_row.get("num_frames"), f"output mapping episode {output_index}.num_frames") != expected_length:
            raise SplitError(f"output mapping episode {output_index} has the wrong frame count")
        global_index += expected_length
        total_frames += expected_length

    expected_files = len(source.plans)
    parquet_files = list(root.glob("data/chunk-*/episode_*.parquet"))
    video_files = list(root.glob("videos/chunk-*/*/episode_*.mp4"))
    if len(parquet_files) != expected_files:
        raise SplitError(f"output has {len(parquet_files)} parquet files, expected {expected_files}")
    if len(video_files) != expected_files * len(source.video_keys):
        raise SplitError(
            f"output has {len(video_files)} videos, expected {expected_files * len(source.video_keys)}"
        )
    if total_frames != require_int(info.get("total_frames"), "output info.total_frames"):
        raise SplitError("output info.total_frames is incorrect")
    if require_int(info.get("total_episodes"), "output info.total_episodes") != len(source.plans):
        raise SplitError("output info.total_episodes is incorrect")
    if require_int(info.get("total_tasks"), "output info.total_tasks") != len(tasks):
        raise SplitError("output info.total_tasks is incorrect")

    try:
        metadata = LeRobotDatasetMetadata(repo_id=f"local/stage-split-{stage}", root=root)
    except Exception as exc:
        raise SplitError(f"LeRobot rejected the {stage} output metadata: {exc}") from exc
    if metadata.total_episodes != len(source.plans) or metadata.total_frames != total_frames:
        raise SplitError(f"LeRobot metadata totals are incorrect for {stage} output")


def temporary_output(final: Path) -> Path:
    final.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f".{final.name}.stage-split-", dir=final.parent))


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def commit_outputs(pairs: list[tuple[Path, Path]], overwrite: bool) -> None:
    backups: list[tuple[Path, Path]] = []
    installed: list[Path] = []
    try:
        for _, final in pairs:
            if final.exists():
                if not overwrite:
                    raise SplitError(f"output appeared during build and --overwrite was not set: {final}")
                backup = final.parent / f".{final.name}.stage-split-backup-{uuid.uuid4().hex}"
                os.replace(final, backup)
                backups.append((final, backup))
        for temporary, final in pairs:
            os.replace(temporary, final)
            installed.append(final)
    except Exception as commit_error:
        rollback_errors: list[str] = []
        for final in reversed(installed):
            try:
                remove_path(final)
            except Exception as exc:  # pragma: no cover - filesystem failure
                rollback_errors.append(f"could not remove new {final}: {exc}")
        for final, backup in reversed(backups):
            try:
                if final.exists():
                    remove_path(final)
                os.replace(backup, final)
            except Exception as exc:  # pragma: no cover - filesystem failure
                rollback_errors.append(f"could not restore {final} from {backup}: {exc}")
        detail = f"; rollback errors: {'; '.join(rollback_errors)}" if rollback_errors else ""
        raise SplitError(f"atomic output commit failed: {commit_error}{detail}") from commit_error

    for _, backup in backups:
        try:
            remove_path(backup)
        except Exception as exc:  # Valid outputs are committed; retain backup and report a warning.
            print(f"WARNING: committed outputs, but could not remove old backup {backup}: {exc}", file=sys.stderr)


def run(args: argparse.Namespace) -> dict[str, Any]:
    hdf5_root = args.hdf5_root.expanduser().resolve(strict=False)
    source_root = args.source_dataset.expanduser().resolve(strict=False)
    left_output = args.left_output.expanduser().resolve(strict=False)
    right_output = args.right_output.expanduser().resolve(strict=False)
    validate_path_layout(hdf5_root, source_root, left_output, right_output, bool(args.overwrite))
    ffmpeg = find_binary("ffmpeg")
    ffprobe = find_binary("ffprobe")

    source = load_and_validate_source(source_root, hdf5_root, ffprobe)
    left_temp: Path | None = None
    right_temp: Path | None = None
    try:
        left_temp = temporary_output(left_output)
        right_temp = temporary_output(right_output)
        left_summary = build_output_dataset(
            source, "left", left_temp, left_output, ffmpeg, ffprobe
        )
        right_summary = build_output_dataset(
            source, "right", right_temp, right_output, ffmpeg, ffprobe
        )
        commit_outputs([(left_temp, left_output), (right_temp, right_output)], bool(args.overwrite))
        left_temp = None
        right_temp = None
    finally:
        for temporary in (left_temp, right_temp):
            if temporary is not None and temporary.exists():
                remove_path(temporary)

    return {
        "source_dataset": str(source_root),
        "hdf5_root": str(hdf5_root),
        "left_output": str(left_output),
        "right_output": str(right_output),
        "left": left_summary,
        "right": right_summary,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = run(args)
    except SplitError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
