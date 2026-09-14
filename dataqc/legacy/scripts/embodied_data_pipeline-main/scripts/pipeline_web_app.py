#!/usr/bin/env python3
"""Local web console for MCAP -> HDF5 -> QC/repair/replay -> LeRobot."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import lerobot_cross_platform as cross_platform
from lerobot_cross_platform_web import CROSS_PLATFORM_HTML
from lerobot_visualization_web import LEROBOT_VISUALIZATION_HTML
import lerobot_manual_screening as manual_screening
from lerobot_manual_screening_web import MANUAL_SCREENING_HTML
from lerobot_manual_screening_records_web import MANUAL_SCREENING_RECORDS_HTML
import lerobot_manual_screening_yolo as manual_screening_yolo

DATA_ROOT = ROOT / "data"
DATA_SCAN_ROOT = Path(os.environ.get("PIPELINE_DATA_SCAN_ROOT") or "/home/agilex/data").expanduser()
H200_DATA_SCAN_ROOT = Path(
    os.environ.get("PIPELINE_H200_DATA_SCAN_ROOT") or "/srv/data/datasets/public"
).expanduser()
H200_MCAP_SCAN_ROOT = Path(
    os.environ.get("PIPELINE_H200_MCAP_SCAN_ROOT")
    or "/mnt/nas/agilex_raw_datasets_mcap/stage2_datasets"
).expanduser()
PROCESSED_CAMERA_NAMESPACES = (
    "four_camera",
    "three_camera_front",
    "three_camera_global",
)
H200_RECURSIVE_SCAN_MAX_DEPTH = 12
H200_RECURSIVE_SCAN_LIMIT = 500
H200_RECURSIVE_IGNORED_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "logs",
    "miniforge3",
    "node_modules",
    "qc_reports",
    "repair_backups",
}
DEFAULT_ALOHA_YAML = ROOT / "mcap_conversion" / "topic_configs" / "aloha_data_params.yaml"
ZERITH_STATIONARY_THRESHOLDS = (20, 40, 60)
LEROBOT_STAGE_SPLIT_SCRIPT = (
    ROOT / "lerobot_conversion" / "scripts" / "split_lerobot_by_stage.py"
)
LEROBOT_STAGE_SPLIT_SIDES = ("left_hand", "righthand")
try:
    from scripts.run_quality_pipeline import _quality_issue_text as format_quality_issue
except Exception:
    format_quality_issue = None
from quality_pipeline.task_names import (
    CANONICAL_BEVERAGE_NAMES,
    canonical_task,
    read_hdf5_task,
    require_hdf5_task,
    write_episode_sidecar_task,
    write_hdf5_task,
)
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
MANUAL_SCREENING_JOB_LOCK = threading.Lock()
MAX_JOB_LOG_LINES = 1000
STATUS_CACHE: dict[
    tuple[Any, ...],
    tuple[float, tuple[tuple[str, int, int], ...], dict[str, Any]],
] = {}
STATUS_CACHE_LOCK = threading.Lock()
STATUS_CACHE_TTL_SEC = 5.0
STATUS_CACHE_MAX_ENTRIES = 8
STATUS_CACHE_GENERATION = 0
MANUAL_FAILURE_FILE = "manual_failure_annotations.json"
QUALITY_GRADES = ("A", "B", "C", "F")
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
REPLAY_PROCESSES: dict[str, subprocess.Popen[str]] = {}
REPLAY_LOGS: dict[str, list[str]] = {}
REPLAY_PORTS: dict[str, int] = {}
ACTIVE_REPLAY_KEY: str | None = None


class JobConflictError(RuntimeError):
    """Raised when two jobs would mutate/read the same live pipeline dataset."""
LEROBOT_REPLAY_CONFIGS: dict[str, dict[str, Any]] = {}
JobStep = tuple[list[str], Path, dict[str, str]] | Callable[[dict[str, Any]], None]


def now_id() -> str:
    return time.strftime("%Y%m%d_%H%M%S") + f"_{int((time.time() % 1) * 1000):03d}"


def invalidate_status_cache() -> None:
    global STATUS_CACHE_GENERATION
    with STATUS_CACHE_LOCK:
        STATUS_CACHE.clear()
        STATUS_CACHE_GENERATION += 1


def json_response(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(data)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        handler.close_connection = True


def text_response(handler: BaseHTTPRequestHandler, text: str, content_type: str = "text/html") -> None:
    data = text.encode("utf-8")
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", f"{content_type}; charset=utf-8")
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(data)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        handler.close_connection = True


def read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    return json.loads(raw) if raw.strip() else {}


def path_from_user(text: str | None, default: Path | None = None) -> Path:
    if text and str(text).strip():
        return Path(str(text).strip()).expanduser().resolve()
    if default is None:
        raise ValueError("missing path")
    return default.expanduser().resolve()


def bool_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off", ""}:
        return False
    return default


def env_bool(name: str, default: bool = False) -> bool:
    return bool_value(os.environ.get(name), default)


def env_path(name: str) -> Path | None:
    value = str(os.environ.get(name) or "").strip()
    return Path(value).expanduser().resolve() if value else None


def normalise_output_namespace(value: Any) -> str:
    namespace = str(value or "").strip().strip("/")
    if namespace in {".", ".."} or any(part in {"", ".", ".."} for part in namespace.split("/") if namespace):
        raise ValueError(f"Invalid output namespace: {value}")
    return namespace


def default_output_base(data_root: Path, namespace: str) -> Path:
    return data_root / namespace if namespace else data_root


def relative_to(path: Path, root: Path) -> str:
    return str(path.resolve().relative_to(root.resolve()))


def count_dataset_episodes(dataset_path: Path) -> int:
    ignored_children = {"hdf5_episodes", "qc_reports", "lerobot", "logs"}
    episode_names: set[str] = set()
    try:
        children = [child for child in dataset_path.iterdir() if child.is_dir()]
    except OSError:
        return 0
    for child in children:
        if child.name.startswith("episode"):
            episode_names.add(child.name)
            continue
        if child.name in ignored_children:
            continue
        try:
            for episode_dir in child.iterdir():
                if episode_dir.is_dir() and episode_dir.name.startswith("episode"):
                    episode_names.add(episode_dir.name)
        except OSError:
            continue
    return len(episode_names)


def discover_processed_dataset_variants(
    dataset_path: Path,
) -> dict[str, list[dict[str, Any]]]:
    variants: dict[str, list[dict[str, Any]]] = {}
    for namespace in PROCESSED_CAMERA_NAMESPACES:
        output_root = dataset_path / namespace
        hdf5_parent = output_root / "hdf5_episodes"
        try:
            dataset_dirs = [
                child
                for child in hdf5_parent.iterdir()
                if child.is_dir()
            ]
        except OSError:
            continue
        entries: list[dict[str, Any]] = []
        for hdf5_root in sorted(dataset_dirs, key=lambda item: natural_key(item.name)):
            try:
                episode_count = sum(
                    1
                    for child in hdf5_root.iterdir()
                    if child.is_dir() and child.name.startswith("episode")
                )
            except OSError:
                continue
            if episode_count <= 0:
                continue
            entries.append(
                {
                    "dataset_name": hdf5_root.name,
                    "hdf5_root": str(hdf5_root.resolve()),
                    "qc_root": str((output_root / "qc_reports").resolve()),
                    "lerobot_root": str((output_root / "lerobot").resolve()),
                    "episode_count": episode_count,
                }
            )
        if entries:
            variants[namespace] = entries
    return variants


def has_valid_source_mcap(
    dataset_path: Path,
    processed_variants: dict[str, list[dict[str, Any]]],
) -> bool:
    mcap_files = bounded_mcap_files(dataset_path, max_depth=3, limit=2000)
    if not mcap_files:
        return False
    if not processed_variants:
        return True
    episode_name = re.compile(r"^(episode|recording|demo)[_-]?\d+", re.IGNORECASE)
    return any(
        episode_name.match(path.stem)
        or looks_like_mcap_episode_dir(path.parent)
        for path in mcap_files
    )


def discover_dataset_choices(scan_root: Path) -> list[dict[str, Any]]:
    root = scan_root.expanduser().resolve()
    if not root.is_dir():
        return []
    ignored_dirs = {"hdf5_episodes", "qc_reports", "lerobot", "logs", "repair_backups"}
    choices: list[dict[str, Any]] = []
    try:
        first_level = [child for child in root.iterdir() if child.is_dir() and child.name not in ignored_dirs]
    except OSError:
        return []
    for group_dir in first_level:
        try:
            dataset_dirs = [child for child in group_dir.iterdir() if child.is_dir() and child.name not in ignored_dirs]
        except OSError:
            continue
        for dataset_dir in dataset_dirs:
            choices.append(dataset_choice_entry(dataset_dir, root, group_dir.name))
    return sorted(choices, key=lambda item: natural_path_sort_key(Path(str(item["relative_path"]))))


def dataset_choice_entry(dataset_dir: Path, scan_root: Path, group: str = "") -> dict[str, Any]:
    """Describe one dataset directory without changing any path derivation rules."""

    dataset_path = dataset_dir.expanduser().resolve()
    root = scan_root.expanduser().resolve()
    is_lerobot = is_lerobot_dataset_dir(dataset_path)
    is_hdf5 = looks_like_hdf5_dataset_dir(dataset_path)
    inferred_robot_type = ""
    if is_hdf5:
        inferred_robot_type = (
            "zerith"
            if any(
                (child / "episode.hdf5").is_file() or (child / "episode.h5").is_file()
                for child in safe_iterdir(dataset_path)
                if child.is_dir()
            )
            else "aloha"
        )
    processed_variants = {} if is_lerobot else discover_processed_dataset_variants(dataset_path)
    if is_lerobot:
        raw_episode_count = lerobot_episode_count(dataset_path)
    elif is_hdf5:
        raw_episode_count = sum(
            1 for child in safe_iterdir(dataset_path) if looks_like_hdf5_episode_dir(child)
        )
    else:
        raw_episode_count = count_dataset_episodes(dataset_path)
    processed_episode_count = max(
        (
            int(entry.get("episode_count") or 0)
            for entries in processed_variants.values()
            for entry in entries
        ),
        default=0,
    )
    try:
        relative_path = str(dataset_path.relative_to(root))
    except ValueError:
        relative_path = str(dataset_path)
    return {
        "group": group or dataset_path.parent.name,
        "name": dataset_path.name,
        "relative_path": relative_path,
        "scan_root": str(root),
        "path": str(dataset_path),
        "parent_path": str(dataset_path.parent),
        "episode_count": max(raw_episode_count, processed_episode_count),
        "has_mcap": (
            False
            if is_lerobot or is_hdf5
            else has_valid_source_mcap(dataset_path, processed_variants)
        ),
        "dataset_type": (
            "lerobot"
            if is_lerobot
            else "hdf5"
            if is_hdf5
            else "mcap"
            if looks_like_mcap_dataset_dir(dataset_path)
            else "processed"
        ),
        "robot_type": inferred_robot_type,
        "processed_variants": processed_variants,
    }


def discover_direct_dataset_choices(scan_root: Path) -> list[dict[str, Any]]:
    """Discover immediate child datasets (the H200 public-dataset layout)."""

    root = scan_root.expanduser().resolve()
    if not root.is_dir():
        return []
    ignored_dirs = {"hdf5_episodes", "qc_reports", "lerobot", "logs", "repair_backups"}
    try:
        dataset_dirs = [
            child
            for child in root.iterdir()
            if child.is_dir() and child.name not in ignored_dirs
        ]
    except OSError:
        return []
    choices = [dataset_choice_entry(path, root, root.name) for path in dataset_dirs]
    return sorted(choices, key=lambda item: natural_path_sort_key(Path(str(item["relative_path"]))))


def looks_like_hdf5_episode_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    has_hdf5 = any(
        candidate.is_file()
        for candidate in (
            path / "episode.hdf5",
            path / "episode.h5",
            path / "states" / "aligned_joints.h5",
            path / "states" / "aligned_joints.hdf5",
            path / "aligned_joints.h5",
            path / "aligned_joints.hdf5",
        )
    )
    if not has_hdf5:
        return False
    return bool(
        (path / "episode.hdf5").is_file()
        or (path / "episode.h5").is_file()
        or re.match(r"^(episode|recording|demo)[_-]?\d+", path.name, re.IGNORECASE)
    )


def looks_like_hdf5_dataset_dir(path: Path) -> bool:
    return any(
        child.is_dir() and looks_like_hdf5_episode_dir(child)
        for child in safe_iterdir(path)
    )


def looks_like_mcap_dataset_dir(path: Path) -> bool:
    if direct_mcap_files(path):
        return True
    children = [child for child in safe_iterdir(path) if child.is_dir()]
    if any(looks_like_mcap_episode_dir(child) for child in children):
        return True
    return any(
        child.name.lower() in {"mcap", "raw_mcap"} and is_mcap_dataset_path(child)
        for child in children
    )


def discover_recursive_hdf5_dataset_choices(scan_root: Path) -> list[dict[str, Any]]:
    """Recursively discover HDF5 roots without listing LeRobot or MCAP datasets."""

    root = scan_root.expanduser().resolve()
    if not root.is_dir():
        return []
    dataset_dirs: list[Path] = []
    for current_text, child_names, _files in os.walk(root, topdown=True, followlinks=False):
        current = Path(current_text)
        try:
            depth = len(current.relative_to(root).parts)
        except ValueError:
            child_names[:] = []
            continue
        child_names[:] = sorted(
            name
            for name in child_names
            if name not in H200_RECURSIVE_IGNORED_DIRS and not name.startswith(".")
        )
        if depth >= H200_RECURSIVE_SCAN_MAX_DEPTH:
            child_names[:] = []
        is_lerobot_root = (
            (current / "meta" / "info.json").is_file()
            or (current / "meta" / "tasks.jsonl").is_file()
        )
        if current != root and is_lerobot_root:
            child_names[:] = []
            continue
        if current.name.lower() in {"mcap", "raw_mcap"}:
            child_names[:] = []
            continue
        is_hdf5_root = any(looks_like_hdf5_episode_dir(current / name) for name in child_names)
        if current != root and is_hdf5_root:
            dataset_dirs.append(current)
            child_names[:] = []
            if len(dataset_dirs) >= H200_RECURSIVE_SCAN_LIMIT:
                break

    choices = []
    for dataset_dir in dataset_dirs:
        try:
            relative = dataset_dir.relative_to(root)
            group = relative.parts[0] if len(relative.parts) > 1 else root.name
        except ValueError:
            group = dataset_dir.parent.name
        choices.append(dataset_choice_entry(dataset_dir, root, group))
    return sorted(choices, key=lambda item: natural_path_sort_key(Path(str(item["relative_path"]))))


def discover_recursive_mcap_dataset_choices(scan_root: Path) -> list[dict[str, Any]]:
    """Recursively discover MCAP dataset roots without listing episode directories."""

    root = scan_root.expanduser().resolve()
    if not root.is_dir():
        return []
    dataset_dirs: list[Path] = []
    episode_name = re.compile(r"^(episode|recording|demo)[_-]?\d+", re.IGNORECASE)
    for current_text, child_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current = Path(current_text)
        try:
            depth = len(current.relative_to(root).parts)
        except ValueError:
            child_names[:] = []
            continue
        child_names[:] = sorted(
            name
            for name in child_names
            if name not in H200_RECURSIVE_IGNORED_DIRS and not name.startswith(".")
        )
        if depth >= H200_RECURSIVE_SCAN_MAX_DEPTH:
            child_names[:] = []
        has_direct_mcap = any(name.lower().endswith(".mcap") for name in file_names)
        has_episode_mcap = any(
            episode_name.match(name)
            and any(child.suffix.lower() == ".mcap" for child in safe_iterdir(current / name))
            for name in child_names
        )
        if current != root and (has_direct_mcap or has_episode_mcap):
            dataset_dirs.append(current)
            child_names[:] = []
            if len(dataset_dirs) >= H200_RECURSIVE_SCAN_LIMIT:
                break

    choices = [dataset_choice_entry(path, root, root.name) for path in dataset_dirs]
    return sorted(choices, key=lambda item: natural_path_sort_key(Path(str(item["relative_path"]))))


def dataset_scan_source(machine: str) -> tuple[str, Path, int]:
    machine_key = str(machine or "agilex").strip().lower()
    if machine_key == "agilex":
        return machine_key, DATA_SCAN_ROOT, 2
    if machine_key == "h200":
        return machine_key, H200_DATA_SCAN_ROOT, H200_RECURSIVE_SCAN_MAX_DEPTH
    raise ValueError(f"Unsupported machine: {machine}; expected agilex or h200")


def discover_machine_datasets(machine: str) -> dict[str, Any]:
    machine_key, scan_root, scan_depth = dataset_scan_source(machine)
    resolved_root = scan_root.expanduser().resolve()
    if machine_key == "h200":
        resolved_mcap_root = H200_MCAP_SCAN_ROOT.expanduser().resolve()
        datasets = [
            *discover_recursive_hdf5_dataset_choices(resolved_root),
            *discover_recursive_mcap_dataset_choices(resolved_mcap_root),
        ]
        datasets = sorted(
            datasets,
            key=lambda item: (
                str(item.get("dataset_type") or ""),
                natural_path_sort_key(Path(str(item["relative_path"]))),
            ),
        )
        scan_roots = [
            {"type": "hdf5", "path": str(resolved_root), "exists": resolved_root.is_dir()},
            {"type": "mcap", "path": str(resolved_mcap_root), "exists": resolved_mcap_root.is_dir()},
        ]
    else:
        datasets = discover_dataset_choices(resolved_root)
        scan_roots = [
            {"type": "mixed", "path": str(resolved_root), "exists": resolved_root.is_dir()}
        ]
    return {
        "machine": machine_key,
        "scan_root": str(resolved_root),
        "scan_depth": scan_depth,
        "scan_recursive": machine_key == "h200",
        "scan_root_exists": resolved_root.is_dir(),
        "scan_roots": scan_roots,
        "datasets": datasets,
    }


def looks_like_mcap_file(path: Path) -> bool:
    return path.is_file() or path.suffix.lower() == ".mcap"


def mount_candidate(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if path.is_file() or path.suffix.lower() in {".mcap", ".h5", ".json", ".jsonl", ".md", ".parquet"}:
        return resolved.parent
    return resolved


def common_parent(paths: list[Path]) -> Path:
    candidates = [mount_candidate(path) for path in paths]
    return Path(os.path.commonpath([str(path) for path in candidates])).resolve()


def docker_data_root(paths: list[Path], preferred: Path) -> Path:
    preferred = preferred.resolve()
    try:
        for path in paths:
            path.resolve().relative_to(preferred)
        return preferred
    except ValueError:
        return common_parent(paths)


def nearest_existing_parent(path: Path) -> Path:
    current = path.expanduser().resolve()
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def ensure_creatable(path: Path, label: str) -> None:
    existing = nearest_existing_parent(path)
    if not os.access(existing, os.W_OK | os.X_OK):
        raise PermissionError(
            f"{label} cannot be created under {existing}: no write permission for user "
            f"{os.getuid()}:{os.getgid()}. Choose a writable output path or change the directory owner/permissions."
        )


def validate_lerobot_repo_id(output_root: Path, repo_id: str) -> None:
    root = Path(output_root).expanduser().resolve()
    repo_path = Path(str(repo_id).strip())
    if not str(repo_path) or repo_path.is_absolute():
        raise ValueError(f"LeRobot repo id must be a non-empty relative path: {repo_id!r}")
    candidate = (root / repo_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"LeRobot repo id escapes output root {root}: {repo_id!r}"
        ) from exc
    if candidate == root:
        raise ValueError(f"LeRobot repo id resolves to the output root itself: {repo_id!r}")


def profile_for_robot(robot_type: str) -> Path:
    robot = robot_type.lower()
    if robot == "g2":
        return ROOT / "robot_profiles" / "g2.yaml"
    if robot == "zerith":
        return ROOT / "robot_profiles" / "zerith.yaml"
    return ROOT / "robot_profiles" / "aloha.yaml"


def stationary_threshold_override(
    payload: dict[str, Any], robot_type: str
) -> int | None:
    """Return the validated Zerith stationary limit selected by the web UI."""

    if str(robot_type).lower() != "zerith":
        return None
    raw = payload.get("stationary_threshold")
    if raw is None or not str(raw).strip():
        from dataqc.config import settings
        return int(settings()["stationary_frames"])
    allowed_text = {str(value) for value in ZERITH_STATIONARY_THRESHOLDS}
    if isinstance(raw, bool) or str(raw).strip() not in allowed_text:
        choices = "/".join(str(value) for value in ZERITH_STATIONARY_THRESHOLDS)
        raise ValueError(f"Invalid stationary threshold: {raw!r}; expected {choices}")
    return int(str(raw).strip())


def hdf5_path_for_episode_dir(episode_dir: Path) -> Path:
    candidates = (
        episode_dir / "episode.hdf5",
        episode_dir / "episode.h5",
        episode_dir / "states" / "aligned_joints.h5",
        episode_dir / "states" / "aligned_joints.hdf5",
        episode_dir / "aligned_joints.h5",
        episode_dir / "aligned_joints.hdf5",
    )
    return next((path for path in candidates if path.is_file()), candidates[2])


def episode_meta_path_for_dir(episode_dir: Path) -> Path:
    legacy = episode_dir / "meta" / "episode_meta.json"
    columnar = episode_dir / "episode_meta.json"
    if legacy.is_file() or (not columnar.is_file() and not (episode_dir / "episode.hdf5").is_file()):
        return legacy
    return columnar


def resolve_camera_variant(payload: dict[str, Any], robot_type: str = "") -> dict[str, Any]:
    robot = str(robot_type or payload.get("robot_type") or "aloha").lower()
    selectable = env_bool("PIPELINE_CAMERA_VARIANT_SELECTABLE", False) and robot == "aloha"
    if selectable:
        raw_count = payload.get("camera_count")
        if raw_count is None or not str(raw_count).strip():
            raw_count = os.environ.get("PIPELINE_CAMERA_COUNT") or "3"
        count_text = str(raw_count).strip()
        if count_text not in {"3", "4"}:
            raise ValueError(f"Invalid camera count: {raw_count}; expected 3 or 4")
        camera_count = int(count_text)

        raw_head_source = payload.get("head_camera_source")
        if raw_head_source is None or not str(raw_head_source).strip():
            raw_head_source = os.environ.get("PIPELINE_HEAD_CAMERA_SOURCE") or "global"
        head_source = str(raw_head_source).strip().lower()
        if head_source not in {"front", "global"}:
            raise ValueError(
                f"Invalid head camera source: {raw_head_source}; expected front or global"
            )

        if camera_count == 4:
            camera_layout = "four_camera"
            namespace = "four_camera"
            feature_keys = [
                "observation.images.hand_head_color",
                "observation.images.hand_left_color",
                "observation.images.hand_right_color",
                "observation.images.global_color",
            ]
        else:
            camera_layout = f"three_camera_{head_source}"
            namespace = camera_layout
            feature_keys = [
                "observation.images.hand_head_color",
                "observation.images.hand_left_color",
                "observation.images.hand_right_color",
            ]
        return {
            "selectable": True,
            "camera_count": camera_count,
            "head_camera_source": head_source,
            "camera_layout": camera_layout,
            "output_namespace": namespace,
            "camera_feature_keys": feature_keys,
        }

    camera_layout = str(
        payload.get("camera_layout") or os.environ.get("PIPELINE_CAMERA_LAYOUT") or "three_camera"
    ).strip()
    allowed_layouts = {
        "three_camera",
        "three_camera_front",
        "three_camera_global",
        "four_camera",
    }
    if camera_layout not in allowed_layouts:
        raise ValueError(f"Invalid camera layout: {camera_layout}")
    head_source = "global" if camera_layout == "three_camera_global" else "front"
    return {
        "selectable": False,
        "camera_count": 4 if camera_layout == "four_camera" else 3,
        "head_camera_source": head_source,
        "camera_layout": camera_layout,
        "output_namespace": "",
        "camera_feature_keys": [],
    }


def write_camera_variant_profile(
    source_profile: Path,
    camera_count: int,
    head_camera_source: str,
) -> Path:
    source_profile = Path(source_profile).expanduser().resolve()
    if camera_count == 4:
        return source_profile
    if camera_count != 3:
        raise ValueError(f"Invalid camera count: {camera_count}; expected 3 or 4")
    if head_camera_source not in {"front", "global"}:
        raise ValueError(
            f"Invalid head camera source: {head_camera_source}; expected front or global"
        )

    from quality_pipeline.profiles import load_profile

    profile = load_profile(source_profile)
    raw = json.loads(json.dumps(profile.raw, ensure_ascii=False))
    cameras_by_raw_key = {
        str(camera.get("raw_key") or ""): camera
        for camera in raw.get("cameras", [])
        if isinstance(camera, dict)
    }
    head_raw_key = "head_color" if head_camera_source == "front" else "head"
    required_raw_keys = [head_raw_key, "hand_left_color", "hand_right_color"]
    missing = [key for key in required_raw_keys if key not in cameras_by_raw_key]
    if missing:
        raise ValueError(
            f"Camera profile {source_profile} is missing required cameras: {', '.join(missing)}"
        )

    selected_cameras = [
        json.loads(json.dumps(cameras_by_raw_key[key], ensure_ascii=False))
        for key in required_raw_keys
    ]
    selected_cameras[0]["lerobot_key"] = "observation.images.hand_head_color"
    selected_cameras[0]["role"] = "head"
    selected_cameras[0]["required"] = True
    raw["cameras"] = selected_cameras
    raw["profile_id"] = f"aloha_three_camera_{head_camera_source}"
    raw["display_name"] = (
        f"{raw.get('display_name') or 'ALOHA'} (Three Camera: {head_camera_source})"
    )
    raw.setdefault("quality_checks", {})["required_cameras"] = required_raw_keys

    annotations = raw.setdefault("processing", {}).setdefault("annotations", {})
    vision_pipeline = annotations.get("vision_pipeline")
    if isinstance(vision_pipeline, dict):
        vision_pipeline["camera"] = head_raw_key
    hand_target_assignment = annotations.get("hand_target_assignment")
    if isinstance(hand_target_assignment, dict):
        hand_target_assignment["cameras"] = [head_raw_key]

    log_root = path_from_user(
        os.environ.get("WEB_LOG_ROOT"),
        DATA_ROOT / "logs" / "pipeline_qc_web",
    )
    profile_dir = (log_root / "camera_profiles").resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    out_path = profile_dir / f"aloha_three_camera_{head_camera_source}.json"
    content = json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
    if out_path.is_file() and out_path.read_text(encoding="utf-8") == content:
        return out_path
    tmp_path = out_path.with_name(
        f"{out_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(out_path)
    return out_path


def write_aloha_joints_only_profile(source_profile: Path) -> Path:
    from quality_pipeline.profiles import load_profile

    profile = load_profile(source_profile)
    raw = json.loads(json.dumps(profile.raw, ensure_ascii=False))
    keep_names = {"left_arm", "left_gripper", "right_arm", "right_gripper"}
    raw["profile_id"] = "aloha"
    raw["display_name"] = f"{raw.get('display_name') or 'ALOHA'} (joints only)"
    raw["state"]["dim"] = 14
    raw["state"]["layout"] = [
        item for item in raw["state"].get("layout", []) if str(item.get("name") or "") in keep_names
    ]
    raw["action"]["dim"] = 14
    raw["action"]["layout"] = [
        item for item in raw["action"].get("layout", []) if str(item.get("name") or "") in keep_names
    ]
    quality_checks = raw.setdefault("quality_checks", {})
    quality_checks["state_dim"] = 14
    quality_checks["action_dim"] = 14

    profile_dir = DATA_ROOT / "_web_profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    out_path = profile_dir / f"{source_profile.stem}_joints_only.json"
    tmp_path = out_path.with_name(
        f"{out_path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    tmp_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(out_path)
    return out_path


def effective_profile_path(cfg: dict[str, Any]) -> Path:
    if str(cfg.get("robot_type") or "").lower() == "aloha" and not cfg.get("aloha_include_base_action", True):
        return write_aloha_joints_only_profile(Path(cfg["profile"]))
    return Path(cfg["profile"])


def infer_dataset_name_from_hdf5_path(path: Path) -> str:
    parts = list(path.parts)
    marker = next(
        (name for name in ("hdf5_episodes", "hdf5") if name in parts),
        None,
    )
    if marker is not None:
        idx = len(parts) - 1 - parts[::-1].index(marker)
        rel_parts = parts[idx + 1 :]
        if rel_parts:
            return rel_parts[0]
        if path.name == marker and path.parent.name:
            return path.parent.name
    if path.is_file() or path.suffix.lower() in {".h5", ".hdf5"}:
        if path.parent.name == "states":
            return path.parent.parent.name
        return path.stem
    return path.name


def data_root_from_hdf5_path(path: Path, fallback: Path) -> Path:
    parts = list(path.parts)
    marker = next(
        (name for name in ("hdf5_episodes", "hdf5") if name in parts),
        None,
    )
    if marker is not None:
        idx = len(parts) - 1 - parts[::-1].index(marker)
        if idx > 0:
            return Path(*parts[:idx]).resolve()
    if path.is_file() or path.suffix.lower() in {".h5", ".hdf5"}:
        if path.parent.name == "states":
            return path.parent.parent.parent.resolve()
        return path.parent.resolve()
    return path.parent.resolve() if path.name else fallback.resolve()


def discover_lerobot_dataset_children(path: Path) -> list[Path]:
    if not path.is_dir():
        return []
    candidates = list(path.iterdir())
    for child in list(candidates):
        if child.is_dir():
            try:
                candidates.extend(grandchild for grandchild in child.iterdir() if grandchild.is_dir())
            except OSError:
                continue
    return sorted(
        [child for child in candidates if child.is_dir() and is_lerobot_dataset_dir(child)],
        key=lambda child: natural_key(child.name),
    )


def safe_iterdir(path: Path) -> list[Path]:
    try:
        return list(path.iterdir())
    except OSError:
        return []


def direct_mcap_files(path: Path) -> list[Path]:
    if not path.is_dir():
        return []
    return sorted(
        [child for child in safe_iterdir(path) if child.is_file() and child.suffix.lower() == ".mcap"],
        key=lambda child: natural_key(child.name),
    )


def bounded_mcap_files(path: Path, max_depth: int = 3, limit: int = 2000) -> list[Path]:
    if path.is_file():
        return [path] if path.suffix.lower() == ".mcap" else []
    if not path.is_dir():
        return []
    found: list[Path] = []
    stack: list[tuple[Path, int]] = [(path, 0)]
    while stack and len(found) < limit:
        current, depth = stack.pop()
        for child in safe_iterdir(current):
            if child.is_file() and child.suffix.lower() == ".mcap":
                found.append(child)
                if len(found) >= limit:
                    break
            elif child.is_dir() and depth < max_depth:
                stack.append((child, depth + 1))
    return sorted(found, key=lambda child: natural_key(str(child)))


def looks_like_mcap_episode_dir(path: Path) -> bool:
    name = path.name.lower()
    return bool(
        direct_mcap_files(path)
        and (
            re.match(r"^(episode|recording|demo)[_-]?\d+", name)
            or (path / f"{path.name}_info.json").is_file()
        )
    )


def is_mcap_dataset_path(path: Path) -> bool:
    if path.is_file():
        return path.suffix.lower() == ".mcap"
    if not path.is_dir():
        return False
    if direct_mcap_files(path):
        return True
    if path.name == "raw_mcap":
        return False
    child_dirs = [child for child in safe_iterdir(path) if child.is_dir()]
    mcap_child_dirs = [child for child in child_dirs if bounded_mcap_files(child, max_depth=1, limit=1)]
    episode_dirs = [child for child in mcap_child_dirs if looks_like_mcap_episode_dir(child)]
    return bool(episode_dirs) and len(episode_dirs) == len(mcap_child_dirs)


def mcap_dataset_entry(path: Path) -> dict[str, Any]:
    mcap_count = len(bounded_mcap_files(path, max_depth=3, limit=2000))
    return {
        "name": path.stem if path.is_file() else path.name,
        "path": str(path),
        "kind": "file" if path.is_file() else "dir",
        "mcap_count": mcap_count,
    }


def discover_mcap_datasets(parent_text: str) -> dict[str, Any]:
    parent = Path(parent_text).expanduser().resolve()
    if not parent.exists():
        raise FileNotFoundError(parent)
    parent_is_dataset = is_mcap_dataset_path(parent)
    datasets: list[dict[str, Any]] = []
    if not parent_is_dataset and parent.is_dir():
        for child in sorted(safe_iterdir(parent), key=lambda item: natural_key(item.name)):
            if is_mcap_dataset_path(child):
                datasets.append(mcap_dataset_entry(child))
                continue
            if not child.is_dir():
                continue
            for grandchild in sorted(safe_iterdir(child), key=lambda item: natural_key(item.name)):
                if is_mcap_dataset_path(grandchild):
                    datasets.append(mcap_dataset_entry(grandchild))
    return {
        "parent": str(parent),
        "parent_is_dataset": parent_is_dataset,
        "selected_path": str(parent) if parent_is_dataset else "",
        "selected_parent": str(parent.parent) if parent_is_dataset else str(parent),
        "datasets": datasets,
    }


def derive_paths(payload: dict[str, Any]) -> dict[str, Any]:
    robot_type = str(payload.get("robot_type") or "aloha").lower()
    stationary_threshold = stationary_threshold_override(payload, robot_type)
    camera_variant = resolve_camera_variant(payload, robot_type)
    mcap_text = str(payload.get("mcap_path") or "").strip()
    hdf5_text = str(payload.get("hdf5_root") or "").strip()
    lerobot_text = str(payload.get("lerobot_root") or "").strip()
    repo_id_input = str(payload.get("repo_id") or "").strip()
    output_namespace = normalise_output_namespace(
        payload.get("output_namespace")
        if payload.get("output_namespace") is not None
        else os.environ.get("PIPELINE_OUTPUT_NAMESPACE")
    )
    if camera_variant["selectable"] and not output_namespace:
        output_namespace = camera_variant["output_namespace"]
    mcap_path = path_from_user(mcap_text, DATA_ROOT / "raw_mcap" / robot_type)
    dataset_name = str(payload.get("dataset_name") or "").strip()

    explicit_data_root = bool(str(payload.get("data_root") or "").strip())
    data_root = path_from_user(payload.get("data_root"), DATA_ROOT)
    mcap_is_file = looks_like_mcap_file(mcap_path)
    hdf5_path_from_user = Path(hdf5_text).expanduser().resolve() if hdf5_text else None
    lerobot_path_from_user = Path(lerobot_text).expanduser().resolve() if lerobot_text else None
    default_hdf5_root: Path | None = None
    default_qc_root: Path | None = None
    default_lerobot_root: Path | None = None
    lerobot_dataset_path: Path | None = None
    raw_parts = list(mcap_path.parts)
    if mcap_text and "raw_mcap" in raw_parts:
        raw_idx = raw_parts.index("raw_mcap")
        if not explicit_data_root:
            data_root = Path(*raw_parts[:raw_idx]).resolve()
        rel_parts = raw_parts[raw_idx + 1 :]
        if not dataset_name:
            if mcap_is_file:
                dataset_name = rel_parts[0] if len(rel_parts) > 1 else mcap_path.stem
            else:
                dataset_name = rel_parts[0] if rel_parts else mcap_path.name
    elif mcap_text and not explicit_data_root:
        data_root = mcap_path.parent.resolve()
        if not dataset_name:
            dataset_name = mcap_path.stem if mcap_is_file else mcap_path.name
        if mcap_is_file:
            external_output_base = default_output_base(mcap_path.parent.resolve(), output_namespace)
            default_hdf5_root = external_output_base / "hdf5_episodes"
            default_qc_root = external_output_base / "qc_reports"
            default_lerobot_root = external_output_base / "lerobot"
        else:
            output_base = default_output_base(data_root, output_namespace)
            default_hdf5_root = output_base / "hdf5_episodes" / dataset_name
            default_qc_root = output_base / "qc_reports"
            default_lerobot_root = output_base / "lerobot"
    elif hdf5_path_from_user is not None:
        if not dataset_name:
            dataset_name = infer_dataset_name_from_hdf5_path(hdf5_path_from_user)
        if not explicit_data_root:
            data_root = data_root_from_hdf5_path(hdf5_path_from_user, DATA_ROOT)
        hdf5_parts = list(hdf5_path_from_user.parts)
        if "hdf5" in hdf5_parts:
            hdf5_idx = len(hdf5_parts) - 1 - hdf5_parts[::-1].index("hdf5")
            if hdf5_idx > 0:
                scene_root = Path(*hdf5_parts[:hdf5_idx]).resolve()
                default_qc_root = scene_root / "qc_reports"
                default_lerobot_root = scene_root / "lerobot"
                if robot_type == "zerith":
                    default_lerobot_root /= "twohands"
    elif lerobot_path_from_user is not None:
        if is_lerobot_dataset_dir(lerobot_path_from_user):
            lerobot_dataset_path = lerobot_path_from_user
            default_lerobot_root = lerobot_path_from_user.parent
        elif repo_id_input and is_lerobot_dataset_dir(lerobot_path_from_user / repo_id_input):
            lerobot_dataset_path = (lerobot_path_from_user / repo_id_input).resolve()
            default_lerobot_root = lerobot_path_from_user
        else:
            children = discover_lerobot_dataset_children(lerobot_path_from_user)
            if len(children) == 1 and not repo_id_input:
                lerobot_dataset_path = children[0].resolve()
            default_lerobot_root = lerobot_path_from_user
        if lerobot_dataset_path is not None:
            if not dataset_name:
                dataset_name = lerobot_dataset_path.name
            if default_lerobot_root is None:
                default_lerobot_root = lerobot_dataset_path.parent
            mapping = load_json_file(lerobot_dataset_path / "meta" / "episode_name_mapping.json")
            data_dir_text = str(mapping.get("data_dir") or "").strip()
            if data_dir_text and not hdf5_text:
                default_hdf5_root = Path(data_dir_text).expanduser().resolve()
                if not explicit_data_root:
                    data_root = data_root_from_hdf5_path(default_hdf5_root, DATA_ROOT)
    if not dataset_name:
        dataset_name = mcap_path.stem if mcap_is_file else mcap_path.name

    output_base = default_output_base(data_root, output_namespace)
    hdf5_root = path_from_user(
        hdf5_text,
        default_hdf5_root or output_base / "hdf5_episodes" / dataset_name,
    )
    qc_root = path_from_user(payload.get("qc_root"), default_qc_root or output_base / "qc_reports")
    lerobot_root = path_from_user(payload.get("lerobot_root"), default_lerobot_root or output_base / "lerobot")
    if camera_variant["selectable"]:
        base_profile = env_path("PIPELINE_DEFAULT_PROFILE") or profile_for_robot(robot_type)
        profile = write_camera_variant_profile(
            base_profile,
            camera_variant["camera_count"],
            camera_variant["head_camera_source"],
        )
    else:
        default_profile = profile_for_robot(robot_type)
        if robot_type == "aloha":
            default_profile = env_path("PIPELINE_DEFAULT_PROFILE") or default_profile
        profile = path_from_user(
            payload.get("profile"),
            default_profile,
        )
    aloha_yaml = path_from_user(
        payload.get("aloha_yaml"),
        env_path("PIPELINE_ALOHA_YAML") or DEFAULT_ALOHA_YAML,
    )
    camera_layout = camera_variant["camera_layout"]
    repo_id = repo_id_input or (lerobot_dataset_path.name if lerobot_dataset_path is not None else dataset_name)
    validate_lerobot_repo_id(lerobot_root, repo_id)
    task_text = str(payload.get("task_text") or "").strip()
    default_jobs = str(os.environ.get("PIPELINE_DEFAULT_CONVERT_JOBS") or "6").strip()
    jobs_value = payload.get("convert_jobs")
    if jobs_value is None or not str(jobs_value).strip():
        jobs_value = default_jobs
    gpu_value = (
        payload.get("gpu_device")
        if "gpu_device" in payload
        else os.environ.get("PIPELINE_DEFAULT_GPU_DEVICE", "0")
    )

    return {
        "data_root": data_root,
        "dataset_name": dataset_name,
        "mcap_path": mcap_path,
        "hdf5_root": hdf5_root,
        "qc_root": qc_root,
        "lerobot_root": lerobot_root,
        "lerobot_dataset_dir": lerobot_dataset_path,
        "profile": profile,
        "aloha_yaml": aloha_yaml,
        "camera_layout": camera_layout,
        "camera_variant_selectable": camera_variant["selectable"],
        "camera_count": camera_variant["camera_count"],
        "head_camera_source": camera_variant["head_camera_source"],
        "camera_feature_keys": camera_variant["camera_feature_keys"],
        "output_namespace": output_namespace,
        "robot_type": robot_type,
        "repo_id": repo_id,
        "task_text": task_text,
        "mcap_path_provided": bool(mcap_text),
        "hdf5_root_provided": bool(hdf5_text),
        "lerobot_root_provided": bool(lerobot_text),
        "convert_jobs": max(1, int(jobs_value)),
        "use_docker": bool_value(payload.get("use_docker"), env_bool("PIPELINE_DEFAULT_USE_DOCKER", False)),
        "docker_image": str(payload.get("docker_image") or "data-tools-ros2:jazzy"),
        "overwrite_hdf5": bool_value(payload.get("overwrite_hdf5"), False),
        "lerobot_cuda": bool_value(payload.get("lerobot_cuda"), True),
        "aloha_include_base_action": bool_value(payload.get("aloha_include_base_action"), True),
        "gpu_device": str(gpu_value or "").strip(),
        "stationary_threshold": stationary_threshold,
    }


def latest_qc_report(qc_root: Path, dataset_name: str, explicit: str | None = None) -> Path | None:
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        return candidate if (candidate / "batch_summary.json").is_file() else None
    if not qc_root.exists():
        return None
    candidates = [
        path
        for path in qc_root.glob(f"{dataset_name}_*")
        if (path / "batch_summary.json").is_file()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path / "batch_summary.json").stat().st_mtime)


def load_qc_summary(report_dir: Path | None) -> dict[str, Any] | None:
    if report_dir is None:
        return None
    summary_path = report_dir / "batch_summary.json"
    if not summary_path.is_file():
        return None
    return json.loads(summary_path.read_text(encoding="utf-8"))


def qc_error_from_report(output_dir: Any) -> str:
    if format_quality_issue is None:
        return ""
    output_text = str(output_dir or "").strip()
    if not output_text:
        return ""
    report_path = Path(output_text).expanduser() / "qc_report.json"
    if not report_path.is_file():
        return ""
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if report.get("accepted") is not False:
        return ""
    issues = []
    for check in report.get("checks", []):
        if isinstance(check, dict) and check.get("status") == "fail":
            issues.append(format_quality_issue(check))
    issues = [issue for issue in issues if issue]
    return "未通过: " + "；".join(issues) if issues else "质检不通过"


def as_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def as_int(value: Any) -> int | None:
    number = as_number(value)
    return int(number) if number is not None else None


def qc_check_detail(qc_report: dict[str, Any], name: str) -> dict[str, Any]:
    for check in qc_report.get("checks", []):
        if not isinstance(check, dict) or check.get("name") != name:
            continue
        detail = check.get("detail")
        return detail if isinstance(detail, dict) else {}
    return {}


QC_WARNING_CHECK_ORDER = [
    "state_dim",
    "action_dim",
    "finite_values",
    "zerith_hdf5_schema",
    "zerith_external_videos",
    "timestamp_monotonic",
    "fps",
    "camera_completeness",
    "camera_repair_frames",
    "duration",
    "motion_stability",
    "action_stationary_frames",
    "gripper_activity",
]


def qc_check_by_name(qc_report: dict[str, Any], name: str) -> dict[str, Any] | None:
    for check in qc_report.get("checks", []):
        if isinstance(check, dict) and check.get("name") == name:
            return check
    return None


def qc_warning_text(check: dict[str, Any]) -> str:
    if check.get("detail", {}).get("shared_key"):
        return check["detail"].get("shared_text", "") if check.get("status") in ("warn", "fail") else ""
    name = str(check.get("name") or "")
    status = str(check.get("status") or "")
    detail = check.get("detail") if isinstance(check.get("detail"), dict) else {}
    if name == "state_dim":
        bad = as_int(detail.get("bad_frames"))
        frames = as_int(detail.get("frames"))
        if (bad is not None and bad > 0) or (frames is not None and frames <= 0) or status in {"fail", "warn"}:
            return f"state 维度: 异常帧 {bad if bad is not None else ''}，期望 {detail.get('expected', '')}维"
    elif name == "action_dim":
        bad = as_int(detail.get("bad_actions"))
        actions = as_int(detail.get("actions"))
        if actions == 0:
            return f"action 维度: 无 action 数据，期望 {detail.get('expected', '')}维"
        if detail.get("count_mismatch"):
            return f"action 数量: 实际 {actions}，期望与 state 一致为 {detail.get('expected_actions', '')}"
        if (bad is not None and bad > 0) or status in {"fail", "warn"}:
            return f"action 维度: 异常 action {bad if bad is not None else ''}，期望 {detail.get('expected', '')}维"
    elif name == "finite_values":
        bad_state = as_int(detail.get("bad_state_frames")) or 0
        bad_action = as_int(detail.get("bad_actions")) or 0
        if bad_state > 0 or bad_action > 0 or status in {"fail", "warn"}:
            return f"有限数值: 异常 state 帧 {bad_state}，异常 action {bad_action}"
    elif name == "timestamp_monotonic":
        frames = as_int(detail.get("frames"))
        max_gap = as_number(detail.get("max_gap_sec"))
        limit = as_number(detail.get("max_allowed_gap_sec"))
        if frames is not None and frames < 2:
            return f"时间戳连续性: 帧数 {frames}，无法计算连续性"
        if max_gap is not None and limit is not None and max_gap > limit:
            return (
                f"时间戳连续性: 最大间隔 {max_gap:.6g}s > 阈值 {limit:.6g}s，"
                f"发生帧 {detail.get('max_gap_start_frame')}-{detail.get('max_gap_end_frame')}"
            )
    elif name == "zerith_hdf5_schema":
        issues = detail.get("issues") if isinstance(detail.get("issues"), list) else []
        if issues or status in {"fail", "warn"}:
            return "零次方 HDF5 Schema: " + ", ".join(str(item) for item in issues)
    elif name == "zerith_external_videos":
        issues = detail.get("issues") if isinstance(detail.get("issues"), list) else []
        if issues or status in {"fail", "warn"}:
            parts = []
            for item in issues:
                if not isinstance(item, dict):
                    continue
                reasons = item.get("issues") if isinstance(item.get("issues"), list) else []
                parts.append(f"{item.get('camera')}: {','.join(str(reason) for reason in reasons)}")
            return "零次方外部视频: " + ("；".join(parts) or "缺失或不可解码")
    elif name == "fps":
        fps = as_number(detail.get("fps"))
        min_fps = as_number(detail.get("min_fps"))
        if fps is not None and min_fps is not None and fps < min_fps:
            return f"FPS: {fps:.2f}Hz < 阈值 {min_fps:.2f}Hz"
    elif name == "camera_completeness":
        summary = detail.get("_summary") if isinstance(detail.get("_summary"), dict) else {}
        expected = as_int(summary.get("expected_required_view_count"))
        present = as_int(summary.get("present_required_view_count"))
        if expected is None:
            expected = sum(
                1
                for info in detail.values()
                if isinstance(info, dict) and bool(info.get("required"))
            )
            present = sum(
                1
                for info in detail.values()
                if isinstance(info, dict) and bool(info.get("required")) and (as_int(info.get("count")) or 0) > 0
            )
        if expected is not None and present is not None and present != expected:
            return f"相机视角: 期望视角 {expected}，实际视角 {present}"
        mismatches = []
        for camera, info in detail.items():
            if not isinstance(info, dict) or camera == "_summary":
                continue
            missing = as_int(info.get("missing")) or 0
            extra = as_int(info.get("extra")) or 0
            if missing or extra:
                mismatches.append(f"{camera} 缺 {missing}/多 {extra} 帧")
        if mismatches:
            return "相机帧数: " + "；".join(mismatches)
    elif name == "camera_repair_frames":
        warnings = detail.get("warnings") if isinstance(detail.get("warnings"), list) else []
        parts = [camera_repair_warning_text(item, detail) for item in warnings if isinstance(item, dict)]
        if parts:
            return "\n".join(parts)
    elif name == "duration":
        duration = as_number(detail.get("duration_sec"))
        min_duration = as_number(detail.get("min_duration_sec"))
        max_duration = as_number(detail.get("max_duration_sec"))
        if duration is not None and min_duration is not None and duration < min_duration:
            return f"轨迹过短: {duration:.2f}s < 最短 {min_duration:.2f}s"
        if duration is not None and max_duration is not None and duration > max_duration:
            return f"轨迹过长: {duration:.2f}s > 最长 {max_duration:.2f}s"
    elif name == "motion_stability":
        max_state = as_number(detail.get("max_state_step"))
        max_state_limit = as_number(detail.get("max_allowed_state_step"))
        max_joint = as_number(detail.get("max_joint_step"))
        max_joint_limit = as_number(detail.get("max_allowed_joint_step"))
        mean_joint = as_number(detail.get("mean_joint_delta"))
        min_joint = as_number(detail.get("min_joint_motion_mean"))
        if max_state is not None and max_state_limit is not None and max_state > max_state_limit:
            return (
                f"运动稳定性: 最大 state 跳变 {max_state:.6g} > 阈值 {max_state_limit:.6g}，"
                f"发生帧 {detail.get('max_state_step_start_frame')}-{detail.get('max_state_step_end_frame')}"
            )
        if max_joint is not None and max_joint_limit is not None and max_joint > max_joint_limit:
            return (
                f"运动稳定性: 最大关节跳变 {max_joint:.6g} > 阈值 {max_joint_limit:.6g}，"
                f"发生帧 {detail.get('max_joint_step_start_frame')}-{detail.get('max_joint_step_end_frame')}"
            )
        if mean_joint is not None and min_joint is not None and mean_joint < min_joint:
            return f"运动稳定性: 平均关节变化 {mean_joint:.6g} < 阈值 {min_joint:.6g}"
    elif name == "action_stationary_frames":
        actions = as_int(detail.get("actions"))
        max_run = as_int(detail.get("max_stationary_run_frames", detail.get("max_stationary_run")))
        max_allowed = as_int(detail.get("max_allowed_stationary_run"))
        if actions is not None and actions < 2:
            return f"action 静止帧: action 数量 {actions}，无法比较"
        if max_run is not None and max_allowed is not None and max_run > max_allowed:
            return (
                f"action 静止帧: 最长连续静止 {max_run} 帧 > 阈值 {max_allowed} 帧，"
                f"发生帧 {detail.get('max_stationary_run_start_frame')}-{detail.get('max_stationary_run_end_frame')}"
            )
    elif name == "gripper_activity":
        transitions = 0
        for value in detail.values():
            if isinstance(value, dict):
                transitions += as_int(value.get("transitions")) or 0
        if (detail.get("note") and status in {"fail", "warn"}) or (detail and transitions == 0 and status in {"fail", "warn"}):
            return "夹爪活动: 未检测到开闭切换"
    elif status in {"fail", "warn"}:
        return str(name)
    return ""


def camera_repair_warning_text(item: dict[str, Any], parent_detail: dict[str, Any]) -> str:
    camera = str(item.get("camera") or item.get("camera_color_name") or "unknown")
    threshold = as_int(parent_detail.get("max_allowed_consecutive_repair_frames"))
    repair_run = as_int(item.get("max_consecutive_repair_or_reuse_frames"))
    repair_start = item.get("max_consecutive_repair_or_reuse_start")
    repair_end = item.get("max_consecutive_repair_or_reuse_end")
    return (
        f"相机修复帧: {camera} 连续修复/复用 {repair_run} 帧 > "
        f"阈值 {threshold} 帧，帧 {repair_start}-{repair_end}"
    )


def qc_warning_summary_from_report(qc_report: dict[str, Any]) -> dict[str, Any]:
    warnings: list[str] = []
    for name in (QC_WARNING_CHECK_ORDER if not qc_report.get("rules_version") else [c["name"] for c in qc_report["checks"]]):
        check = qc_check_by_name(qc_report, name)
        if not check:
            continue
        text = qc_warning_text(check)
        if text:
            warnings.append(text)
    stationary_detail = qc_check_detail(qc_report, "action_stationary_frames")
    return {
        "qc_report_loaded": True,
        "shared_quality_grade": qc_report.get("quality_grade"),
        "shared_rules_version": qc_report.get("rules_version"),
        "shared_review_required": qc_report.get("review_required", False),
        "shared_reason": qc_report.get("reason", ""),
        "warning_count": len(warnings),
        "warning_remarks": warnings,
        "max_stationary_run_frames": as_int(
            stationary_detail.get("max_stationary_run_frames", stationary_detail.get("max_stationary_run"))
        ),
    }


def load_qc_report(
    report_path: Path,
    report_cache: dict[Path, dict[str, Any] | None] | None = None,
) -> dict[str, Any] | None:
    path = report_path.expanduser().resolve()
    if report_cache is not None and path in report_cache:
        return report_cache[path]
    report: dict[str, Any] | None = None
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, dict):
            report = loaded
    if report_cache is not None:
        report_cache[path] = report
    return report


def qc_warning_summary_from_output(
    output_dir: Any,
    report_cache: dict[Path, dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    output_text = str(output_dir or "").strip()
    if not output_text:
        return {"qc_report_loaded": False, "warning_count": None, "warning_remarks": []}
    report_path = Path(output_text).expanduser() / "qc_report.json"
    report = load_qc_report(report_path, report_cache)
    if report is None:
        return {"qc_report_loaded": False, "warning_count": None, "warning_remarks": []}
    return qc_warning_summary_from_report(report)


def camera_completeness_from_detail(detail: dict[str, Any]) -> tuple[float | None, int | None, int | None, int | None, str]:
    completeness_values: list[float] = []
    missing_total = 0
    has_missing = False
    expected_views = None
    present_views = None
    required_views_from_detail = 0
    present_views_from_detail = 0
    parts: list[str] = []
    summary = detail.get("_summary") if isinstance(detail.get("_summary"), dict) else {}
    if summary:
        expected_views = as_int(summary.get("expected_required_view_count"))
        present_views = as_int(summary.get("present_required_view_count"))
    for camera, info in detail.items():
        if str(camera).startswith("_"):
            continue
        if not isinstance(info, dict):
            continue
        ratio = as_number(info.get("missing_ratio"))
        missing = as_int(info.get("missing"))
        count = as_int(info.get("count"))
        if ratio is not None:
            completeness_values.append(max(0.0, min(100.0, (1.0 - ratio) * 100.0)))
        if missing is not None:
            has_missing = True
            missing_total += missing
        if info.get("required") and not summary:
            required_views_from_detail += 1
            if count is not None and count > 0:
                present_views_from_detail += 1
        parts.append(f"{camera}: {count if count is not None else ''}帧, 缺失{missing if missing is not None else ''}")
    if not summary and required_views_from_detail:
        expected_views = required_views_from_detail
        present_views = present_views_from_detail
    completeness = min(completeness_values) if completeness_values else None
    return completeness, missing_total if has_missing else None, expected_views, present_views, "；".join(parts)


def gripper_close_events_from_detail(detail: dict[str, Any]) -> tuple[int | None, int | None]:
    def close_events(side: str) -> int | None:
        info = detail.get(side)
        if not isinstance(info, dict):
            return None
        value = as_int(info.get("close_events", info.get("grasp_events")))
        return value

    return close_events("left"), close_events("right")


def qc_overview_record_from_json(
    episode_id: str,
    report_path: Path,
    report_cache: dict[Path, dict[str, Any] | None] | None = None,
) -> dict[str, Any] | None:
    qc_report = load_qc_report(report_path, report_cache)
    if qc_report is None:
        return None
    summary = qc_report.get("summary") if isinstance(qc_report.get("summary"), dict) else {}
    state_detail = qc_check_detail(qc_report, "state_dim")
    action_detail = qc_check_detail(qc_report, "action_dim")
    fps_detail = qc_check_detail(qc_report, "fps")
    camera_detail = qc_check_detail(qc_report, "camera_completeness")
    duration_detail = qc_check_detail(qc_report, "duration")
    motion_detail = qc_check_detail(qc_report, "motion_stability")
    stationary_detail = qc_check_detail(qc_report, "action_stationary_frames")
    gripper_detail = qc_check_detail(qc_report, "gripper_activity")
    camera_pct, camera_missing, expected_views, present_views, camera_text = camera_completeness_from_detail(camera_detail)
    left_close_events, right_close_events = gripper_close_events_from_detail(gripper_detail)
    return {
        "episode_id": episode_id,
        "state_dim": as_int(state_detail.get("expected")),
        "state_bad_frames": as_int(state_detail.get("bad_frames")),
        "action_dim": as_int(action_detail.get("expected")),
        "action_bad_frames": as_int(action_detail.get("bad_actions")),
        "fps": as_number(fps_detail.get("fps") if fps_detail else summary.get("fps")),
        "camera_completeness_pct": camera_pct,
        "camera_missing": camera_missing,
        "camera_expected_view_count": expected_views,
        "camera_view_count": present_views,
        "camera_detail": camera_text,
        "duration_sec": as_number(duration_detail.get("duration_sec") if duration_detail else summary.get("duration_sec")),
        "max_state_step": as_number(motion_detail.get("max_state_step")),
        "max_state_step_start_frame": as_int(motion_detail.get("max_state_step_start_frame")),
        "max_state_step_end_frame": as_int(motion_detail.get("max_state_step_end_frame")),
        "stationary_frames": as_int(stationary_detail.get("stationary_frames")),
        "max_stationary_run_frames": as_int(
            stationary_detail.get("max_stationary_run_frames", stationary_detail.get("max_stationary_run"))
        ),
        "left_gripper_close_events": left_close_events,
        "right_gripper_close_events": right_close_events,
        "final_report_table": str(report_path.with_name("final_report_table.md")),
        "qc_report": str(report_path),
    }


def markdown_cells(line: str) -> list[str]:
    text = line.strip()
    if not text.startswith("|"):
        return []
    text = text.strip("|")
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


def first_number(pattern: str, text: str) -> float | None:
    match = re.search(pattern, text)
    return as_number(match.group(1)) if match else None


def first_int(pattern: str, text: str) -> int | None:
    number = first_number(pattern, text)
    return int(number) if number is not None else None


def qc_overview_record_from_final_table(episode_id: str, table_path: Path) -> dict[str, Any] | None:
    if not table_path.is_file():
        return None
    try:
        lines = table_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    details: dict[str, str] = {}
    trajectory: dict[str, str] = {}
    in_traj = False
    for line in lines:
        if line.startswith("## "):
            in_traj = line.strip() == "## 轨迹信息"
            continue
        cells = markdown_cells(line)
        if len(cells) < 2 or all(set(cell) <= {"-", ":"} for cell in cells):
            continue
        if in_traj and len(cells) >= 2 and cells[0] not in {"信息项", "---"}:
            trajectory[cells[0]] = cells[1]
        if len(cells) >= 3 and cells[0] not in {"检查项", "---"}:
            details[cells[0]] = cells[2]

    state_text = details.get("state 维度", "")
    action_text = details.get("action 维度", "")
    fps_text = details.get("FPS", "")
    camera_text = details.get("相机视角", "") or details.get("相机完整性", "")
    duration_text = details.get("轨迹时长", "") or details.get("时长", "")
    motion_text = details.get("运动稳定性", "")
    stationary_text = details.get("action 静止帧", "")
    gripper_text = details.get("夹爪活动", "")
    ratios = [as_number(item) for item in re.findall(r"缺失率\s*([0-9.eE+-]+)", camera_text)]
    ratios = [item for item in ratios if item is not None]
    missing_values = [as_int(item) for item in re.findall(r"缺失\s*([0-9]+)", camera_text)]
    missing_values = [item for item in missing_values if item is not None]
    camera_expected_view_count = first_int(r"期望视角\s*([0-9]+)", camera_text)
    camera_view_count = first_int(r"实际视角\s*([0-9]+)", camera_text)
    if camera_view_count is None:
        camera_names = re.findall(r"([A-Za-z0-9_]+):\s*(?:[0-9]+\s*帧，)?缺失", camera_text)
        camera_view_count = len(set(camera_names)) if camera_names else None
    return {
        "episode_id": episode_id,
        "state_dim": first_int(r"期望维度\s*([0-9]+)", state_text),
        "state_bad_frames": first_int(r"异常帧\s*([0-9]+)", state_text),
        "action_dim": first_int(r"期望维度\s*([0-9]+)", action_text),
        "action_bad_frames": first_int(r"异常 action\s*([0-9]+)", action_text),
        "fps": first_number(r"fps\s*([0-9.eE+-]+)Hz", fps_text) or as_number(trajectory.get("fps")),
        "camera_completeness_pct": min((1.0 - ratio) * 100.0 for ratio in ratios) if ratios else None,
        "camera_missing": sum(missing_values) if missing_values else None,
        "camera_expected_view_count": camera_expected_view_count,
        "camera_view_count": camera_view_count,
        "camera_detail": camera_text,
        "duration_sec": first_number(r"时长\s*([0-9.eE+-]+)s", duration_text) or as_number(trajectory.get("duration_sec")),
        "max_state_step": first_number(r"最大 state 跳变\s*([0-9.eE+-]+)", motion_text),
        "max_state_step_start_frame": None,
        "max_state_step_end_frame": None,
        "stationary_frames": first_int(r"静止帧\s*([0-9]+)", stationary_text),
        "max_stationary_run_frames": first_int(r"最长连续静止帧数\s*([0-9]+)", stationary_text),
        "left_gripper_close_events": first_int(r"left:[^；]*闭合次数\s*([0-9]+)", gripper_text),
        "right_gripper_close_events": first_int(r"right:[^；]*闭合次数\s*([0-9]+)", gripper_text),
        "final_report_table": str(table_path),
        "qc_report": "",
    }


def qc_overview_standards(cfg: dict[str, Any] | None) -> dict[str, int]:
    robot_type = str((cfg or {}).get("robot_type") or "aloha").lower()
    if robot_type == "g2":
        return {"state_dim": 26, "action_dim": 24, "camera_view_count": 3}
    if robot_type == "zerith":
        return {"state_dim": 23, "action_dim": 23, "camera_view_count": 3}
    if not (cfg or {}).get("aloha_include_base_action", True):
        return {"state_dim": 14, "action_dim": 14, "camera_view_count": 3}
    return {"state_dim": 21, "action_dim": 18, "camera_view_count": 3}


def quality_check_items(cfg: dict[str, Any]) -> list[dict[str, str]]:
    if cfg.get("robot_type") == "zerith":
        return [dict(name=name,criterion=criterion) for name,criterion in [
            ("结构与完整性", "State/Action 各 23 维；数值有限；HDF5、3 路图像与视频逐帧完整"),
            ("左右夹爪与阶段", "Action/State 左右各闭合一次，阶段 1 左手、阶段 2 右手"),
            ("双臂连续性", "时间间隔 > 0.1 s、重复/倒退仅预警 B，显示帧位置和间隔；关节跳变 > 0.8 rad 判 F"),
            ("腰部 / 头部", "pitch/yaw 均值、Q01/Q99 均为 ±0.02 rad，State / Action 超限预警 B"),
            ("升降柱", "真机按目录目标高度；仿真读取 episode_meta.json；允许 ±0.02 m"),
            ("静止与标注", "动作指标集中交给 Terra 审查；静止段及缺失阶段需人工确认"),
            ("视觉匹配", "类别识别默认关闭；开启后每手 YOLO ≥2/3 且 Terra 复核通过"),
            ("结果与导出", "预警默认 B；F 与待确认不进入导出；A/B 转换后再次校验"),
        ]]
    from quality_pipeline.profiles import load_profile

    profile = load_profile(effective_profile_path(cfg))
    rtml = profile.processing.get("rtml", {})
    constraints = dict(rtml.get("global_constraints") or {})

    max_gap = float(constraints.get("max_timestamp_gap_sec", 0.3))
    min_fps = float(constraints.get("min_fps", 29.0))
    min_duration = float(constraints.get("min_duration_sec", 2.0))
    max_duration_raw = constraints.get("max_duration_sec")
    max_missing = float(constraints.get("max_missing_camera_ratio", 0.02))
    max_repair = int(constraints.get("max_camera_repair_run_frames", 2))
    max_state_step = float(constraints.get("max_state_step", 0.8))
    max_joint_step = float(constraints.get("max_joint_step", 0.8))
    min_joint_motion = float(constraints.get("min_joint_motion_mean", 1e-4))
    max_stationary = stationary_threshold_for_cfg(cfg)
    gripper_delta = float(constraints.get("gripper_delta_threshold", 0.02))
    required_cameras = len(profile.required_camera_keys)
    if max_duration_raw is None:
        duration_criterion = f"不少于 {min_duration:.1f} 秒（不足时提示）"
    else:
        duration_criterion = (
            f"{min_duration:.1f}-{float(max_duration_raw):.1f} 秒（超出范围提示）"
        )

    items = [
        {"name": "State 维度", "criterion": f"每帧 {profile.state_dim} 维"},
        {"name": "Action 维度", "criterion": f"每条 action {profile.action_dim} 维"},
        {"name": "有限数值", "criterion": "state、action 和时间戳不含 NaN/Inf"},
        {"name": "时间戳连续性", "criterion": f"严格递增，最大间隔不超过 {max_gap:g} 秒"},
        {"name": "FPS", "criterion": f"不低于 {min_fps:g} Hz"},
        {
            "name": "相机完整性",
            "criterion": (
                f"{required_cameras} 路必需视角，单路缺帧率不超过 {max_missing * 100:.1f}%"
            ),
        },
        {"name": "相机修复帧", "criterion": f"最长连续修复/复用不超过 {max_repair} 帧"},
        {"name": "轨迹时长", "criterion": duration_criterion},
        {
            "name": "运动稳定性",
            "criterion": (
                f"state/关节单步跳变不超过 {max_state_step:g}/{max_joint_step:g}，"
                f"平均关节变化不低于 {min_joint_motion:g}"
            ),
        },
        {"name": "Action 静止帧", "criterion": f"最长连续静止不超过 {max_stationary} 帧"},
        {"name": "夹爪活动", "criterion": f"检测开闭变化，变化阈值 {gripper_delta:g}"},
    ]
    if str(profile.raw.get("adapter") or "").lower() == "zerith_columnar":
        items.insert(
            2,
            {
                "name": "零次方 HDF5 Schema",
                "criterion": "全部逐帧 dataset 长度一致，total_frames/frequency/action_mode 正确",
            },
        )
        items.insert(
            3,
            {
                "name": "零次方外部 MP4",
                "criterion": "3 路转换源视频均存在、可完整解码，且帧数与 state/action 完全一致",
            },
        )
    return items


def stationary_threshold_for_cfg(cfg: dict[str, Any]) -> int:
    from quality_pipeline.profiles import load_profile

    override = cfg.get("stationary_threshold")
    if override is not None:
        value = int(override)
        if value not in ZERITH_STATIONARY_THRESHOLDS:
            raise ValueError(f"Invalid configured stationary threshold: {value}")
        return value
    profile = load_profile(effective_profile_path(cfg))
    constraints = dict(profile.processing.get("rtml", {}).get("global_constraints") or {})
    return int(constraints.get("max_stationary_action_frames", 15))


def qc_overview_status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    neutral_statuses = {"", "未质检"}
    success = 0
    failure = 0
    manual_failure = 0
    untested = 0
    for row in rows:
        status = str(row.get("delete_status") or "")
        collection_status = str(row.get("collection_status") or "")
        is_collection_failure = (
            collection_status == "采集失败"
            or status == "采集失败"
            or row.get("manual_failure") is True
        )
        if is_collection_failure:
            manual_failure += 1
            failure += 1
        elif status in neutral_statuses and not collection_status:
            untested += 1
        else:
            success += 1
    return {
        "total": len(rows),
        "success": success,
        "failure": failure,
        "manual_failure": manual_failure,
        "untested": untested,
    }


def qc_overview_quality_grade_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"A": 0, "B": 0, "C": 0, "F": 0, "no_grade": 0}
    for row in rows:
        grade = normalise_quality_grade(row.get("quality_grade"))
        if grade:
            counts[grade] += 1
        else:
            counts["no_grade"] += 1
    return counts


def build_qc_overview(
    report_dir: Path | None,
    rows: list[dict[str, Any]],
    cfg: dict[str, Any] | None = None,
    report_cache: dict[Path, dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    if report_dir is None:
        return {
            "records": [],
            "source_report_dir": "",
            "standards": qc_overview_standards(cfg),
            "status_counts": qc_overview_status_counts(rows),
            "quality_grade_counts": qc_overview_quality_grade_counts(rows),
        }
    records: list[dict[str, Any]] = []
    for row in rows:
        episode_id = str(row.get("episode_id") or "")
        if not episode_id:
            continue
        output_text = str(row.get("qc_output") or "").strip()
        episode_report_dir = Path(output_text).expanduser() if output_text else report_dir / episode_id
        qc_path = episode_report_dir / "qc_report.json"
        final_table = episode_report_dir / "final_report_table.md"
        record = qc_overview_record_from_json(episode_id, qc_path, report_cache)
        if record is None:
            record = qc_overview_record_from_final_table(episode_id, final_table)
        if record is not None:
            record["status"] = str(row.get("collection_status") or row.get("delete_status") or "")
            record["quality_grade"] = str(row.get("quality_grade") or "")
            records.append(record)
    return {
        "records": sorted(records, key=lambda item: natural_key(str(item.get("episode_id") or ""))),
        "source_report_dir": str(report_dir),
        "standards": qc_overview_standards(cfg),
        "status_counts": qc_overview_status_counts(rows),
        "quality_grade_counts": qc_overview_quality_grade_counts(rows),
    }


def estimate_hdf5_frame_info(h5_path: Path) -> tuple[int | None, float | None]:
    try:
        import h5py  # type: ignore
    except ImportError:
        return None, None
    try:
        with h5py.File(h5_path, "r") as file_obj:
            if "timestamp/t" in file_obj:
                timestamps = file_obj["timestamp/t"]
                frame_count = int(timestamps.shape[0])
                if frame_count < 2:
                    return frame_count, None
                first = float(timestamps[0])
                last = float(timestamps[-1])
                delta = (last - first) / 1e3
                fps = (frame_count - 1) / delta if delta > 1e-9 else None
                return frame_count, round(fps, 3) if fps and fps > 0.0 else None
            frame_keys = sorted(
                [key for key in file_obj.keys() if str(key).isdigit()],
                key=lambda key: int(str(key)),
            )
            if not frame_keys:
                return None, None
            frame_count = len(frame_keys)
            if frame_count < 2:
                return frame_count, None
            first_ds = f"{frame_keys[0]}/main_timestamp"
            last_ds = f"{frame_keys[-1]}/main_timestamp"
            if first_ds not in file_obj or last_ds not in file_obj:
                return frame_count, None
            first = float(file_obj[first_ds][()])
            last = float(file_obj[last_ds][()])
    except Exception:
        return None, None
    delta = last - first
    magnitude = max(abs(first), abs(last))
    if magnitude > 1e17:
        delta /= 1e9
    elif magnitude > 1e14:
        delta /= 1e6
    elif magnitude > 1e11:
        delta /= 1e3
    fps = (frame_count - 1) / delta if delta > 1e-9 else None
    return frame_count, round(fps, 3) if fps and fps > 0.0 else None


def discover_hdf5_episodes(hdf5_root: Path) -> list[dict[str, Any]]:
    if not hdf5_root.exists():
        return []
    episodes: list[dict[str, Any]] = []
    if hdf5_root.is_file() and hdf5_root.suffix.lower() in {".h5", ".hdf5"}:
        h5_paths = [hdf5_root]
    elif looks_like_hdf5_episode_dir(hdf5_root):
        h5_paths = [hdf5_path_for_episode_dir(hdf5_root)]
    else:
        h5_paths = discover_lerobot_input_h5_paths(hdf5_root)
    for h5_path in h5_paths:
        episode_dir = hdf5_episode_dir_from_h5(h5_path)
        meta_path = episode_meta_path_for_dir(episode_dir)
        frame_count = None
        fps = None
        stationary_trim_note = ""
        stationary_trim_applied = False
        stationary_trim_kept_frame_count = None
        source_episode_name = ""
        source_mcap_episode_name = ""
        source_mcap_files: list[str] = []
        manual_quality_grade = ""
        manual_review_reason = ""
        manual_failure = False
        manual_failure_reason = ""
        reason_codes: list[str] = []
        reason_labels_zh: list[str] = []
        reason_labels_en: list[str] = []
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                frame_count = meta.get("frame_count")
                fps = meta.get("state_fps") or meta.get("fps") or meta.get("inferred_state_fps") or meta.get("video_fps")
                stationary_trim = meta.get("stationary_trim")
                stationary_trim_note = format_stationary_trim_note(stationary_trim)
                if isinstance(stationary_trim, dict):
                    removed = as_int(stationary_trim.get("removed_frame_count"))
                    stationary_trim_applied = (
                        str(stationary_trim.get("status") or "").lower() == "trimmed"
                        or (removed is not None and removed > 0)
                    )
                    stationary_trim_kept_frame_count = as_int(stationary_trim.get("kept_frame_count"))
                source_episode_name = str(meta.get("source_episode_name") or meta.get("episode_name") or "")
                source_mcap_episode_name = str(
                    meta.get("source_mcap_episode_name")
                    or meta.get("source_episode_name")
                    or meta.get("episode_name")
                    or ""
                )
                raw_mcap_files = meta.get("source_mcap_files")
                if isinstance(raw_mcap_files, list):
                    source_mcap_files = [str(item) for item in raw_mcap_files if str(item)]
                collection_quality = meta.get("collection_quality") if isinstance(meta.get("collection_quality"), dict) else {}
                manual_quality_grade = normalise_quality_grade(
                    meta.get("manual_quality_grade")
                    or meta.get("quality_grade")
                    or collection_quality.get("grade")
                )
                reason_codes = normalise_reason_codes(meta.get("reason_codes") or collection_quality.get("reason_codes"))
                raw_reason_labels_zh = (
                    meta.get("reason_labels_zh")
                    or meta.get("reason_labels")
                    or collection_quality.get("reason_labels_zh")
                    or collection_quality.get("reason_labels")
                    or []
                )
                raw_reason_labels_en = meta.get("reason_labels_en") or collection_quality.get("reason_labels_en") or []
                reason_labels_zh = split_reason_text(raw_reason_labels_zh)
                reason_labels_en = split_reason_text(raw_reason_labels_en)
                if not reason_labels_zh and reason_codes:
                    reason_labels_zh = reason_labels_from_codes(reason_codes, "zh")
                if not reason_labels_en and reason_codes:
                    reason_labels_en = reason_labels_from_codes(reason_codes, "en")
                manual_review_reason = str(
                    meta.get("manual_review_reason")
                    or meta.get("quality_description")
                    or "；".join(reason_labels_zh)
                    or ""
                ).strip()
                manual_failure = bool(meta.get("manual_failure")) or manual_quality_grade == "F"
                manual_failure_reason = str(meta.get("manual_failure_reason") or (manual_review_reason if manual_failure else "")).strip()
            except json.JSONDecodeError:
                frame_count = None
                fps = None
                stationary_trim_note = ""
                stationary_trim_applied = False
                stationary_trim_kept_frame_count = None
                manual_quality_grade = ""
                manual_review_reason = ""
                manual_failure = False
                manual_failure_reason = ""
                reason_codes = []
                reason_labels_zh = []
                reason_labels_en = []
        frame_count_number = as_number(frame_count)
        fps_number = as_number(fps)
        estimated_frame_count = None
        estimated_fps = None
        if (
            frame_count_number is None
            or frame_count_number <= 0.0
            or fps_number is None
            or fps_number <= 0.0
        ):
            estimated_frame_count, estimated_fps = estimate_hdf5_frame_info(h5_path)
        if frame_count_number is None or frame_count_number <= 0.0:
            frame_count = estimated_frame_count
        if fps_number is None or fps_number <= 0.0:
            fps = estimated_fps
        else:
            fps = round(fps_number, 3)
        episodes.append(
            {
                "episode_id": episode_dir.name,
                "episode_dir": str(episode_dir),
                "h5": str(h5_path),
                "frame_count": frame_count,
                "fps": fps,
                "stationary_trim_note": stationary_trim_note,
                "stationary_trim_applied": stationary_trim_applied,
                "stationary_trim_kept_frame_count": stationary_trim_kept_frame_count,
                "source_episode_name": source_episode_name,
                "source_mcap_episode_name": source_mcap_episode_name,
                "source_mcap_files": source_mcap_files,
                "manual_quality_grade": manual_quality_grade,
                "manual_review_reason": manual_review_reason,
                "manual_failure": manual_failure,
                "manual_failure_reason": manual_failure_reason,
                "reason_codes": reason_codes,
                "reason_labels_zh": reason_labels_zh,
                "reason_labels_en": reason_labels_en,
            }
        )
    return episodes


def manual_failure_hdf5_path(cfg: dict[str, Any]) -> Path:
    return cfg["hdf5_root"] / MANUAL_FAILURE_FILE


def manual_failure_lerobot_path(cfg: dict[str, Any]) -> Path:
    return lerobot_dataset_dir(cfg) / "meta" / MANUAL_FAILURE_FILE


def manual_failure_source_path(cfg: dict[str, Any]) -> Path:
    mcap_path = cfg["mcap_path"]
    base = mcap_path.parent if looks_like_mcap_file(mcap_path) else mcap_path
    return base / MANUAL_FAILURE_FILE


def manual_failure_candidate_paths(cfg: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    if cfg.get("mcap_path_provided"):
        paths.append(manual_failure_source_path(cfg))
    paths.append(manual_failure_lerobot_path(cfg))
    paths.append(manual_failure_hdf5_path(cfg))
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.expanduser().resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def _manual_failure_truthy(value: Any, default: bool = False) -> bool:
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
                is_failure = _manual_failure_truthy(failure_value, default=bool(reason))
                quality_grade = "F" if is_failure else ""
        else:
            continue
        if not name:
            continue
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
    entries = normalise_manual_failure_entries(payload)
    for item in entries.values():
        item["source"] = str(path)
    return entries


def load_manual_failures_for_cfg(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for path in manual_failure_candidate_paths(cfg):
        merged.update(load_manual_failure_file(path))
    return merged


def write_manual_failure_file(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
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
            for name, item in sorted(entries.items(), key=lambda pair: natural_key(pair[0]))
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def hdf5_quality_grade_entry_for_episode(
    cfg: dict[str, Any], episode_name: str, quality_grade: str
) -> dict[str, Any]:
    name = str(episode_name or "").strip()
    grade = normalise_quality_grade(quality_grade)
    if not name:
        raise ValueError("missing episode name")
    if not grade:
        raise ValueError(f"invalid quality grade: {quality_grade}")
    matches = [
        entry
        for entry in lerobot_source_episode_entries(cfg["hdf5_root"])
        if str(entry.get("episode_id") or "").strip() == name
    ]
    if not matches:
        raise ValueError(f"missing HDF5 episode for quality grade save: {name}")
    if len(matches) != 1:
        raise ValueError(f"duplicate HDF5 episodes for quality grade save: {name}")
    entry = dict(matches[0])
    entry["quality_grade"] = grade
    return entry


def save_qc_report_quality_grade(
    cfg: dict[str, Any],
    episode_name: str,
    quality_grade: str,
    reason_label: Any = "",
    reason_codes: Any = None,
) -> dict[str, Any]:
    name = str(episode_name or "").strip()
    if not name:
        raise ValueError("missing episode name")
    grade = normalise_quality_grade(quality_grade)
    if not grade:
        raise ValueError(f"invalid quality grade: {quality_grade}")

    from integrations.zerith_rules import prepare_manual_approval, finish_manual_approval
    prepared = prepare_manual_approval(sys.modules[__name__], cfg, name, grade, reason_label)
    hdf5_entry = hdf5_quality_grade_entry_for_episode(cfg, name, grade)
    sync_hdf5_quality_grade_entries([hdf5_entry])

    entries = load_manual_failures_for_cfg(cfg)
    existing = entries.get(name, {})
    reason_text = str(reason_label or existing.get("reason_label") or "").strip()
    codes_input = reason_codes if reason_codes is not None else existing.get("reason_codes")
    reason_fields = manual_reason_fields(reason_text, codes_input)
    saved = {
        "episode_name": name,
        "is_failure": grade == "F",
        "reason_label": str(reason_fields.get("reason_label") or reason_text),
        "reason_codes": reason_fields.get("reason_codes", []),
        "reason_labels": reason_fields.get("reason_labels", []),
        "reason_labels_zh": reason_fields.get("reason_labels_zh", []),
        "reason_labels_en": reason_fields.get("reason_labels_en", []),
        "quality_grade": grade,
    }
    entries[name] = saved
    write_manual_failure_file(manual_failure_hdf5_path(cfg), entries)
    finish_manual_approval(prepared)
    return saved


def sync_manual_failures_step(cfg: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
    def step(job: dict[str, Any]) -> None:
        dst = manual_failure_hdf5_path(cfg)
        merged: dict[str, dict[str, Any]] = {}
        used_paths: list[str] = []
        for path in manual_failure_candidate_paths(cfg):
            entries = load_manual_failure_file(path)
            if entries:
                used_paths.append(str(path))
                merged.update(entries)
        if not merged:
            append_job(job, "No manual failure annotations found.")
            return
        write_manual_failure_file(dst, merged)
        failed = sum(1 for item in merged.values() if item.get("is_failure"))
        append_job(job, f"Synced manual failure annotations: {failed} failed / {len(merged)} total -> {dst}")
        for path in used_paths:
            append_job(job, f"  source: {path}")

    step.__name__ = "同步采集失败标注"
    return step


def manual_failure_for_episode(row: dict[str, Any], manual_map: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    keys = [
        str(row.get("episode_id") or ""),
        str(row.get("source_episode_name") or ""),
        str(row.get("source_mcap_episode_name") or ""),
    ]
    for path_text in row.get("source_mcap_files") or []:
        path = Path(str(path_text))
        keys.extend([path.stem, path.name])
    for key in keys:
        if key and key in manual_map:
            return manual_map[key]
    return None


def apply_manual_failures_to_rows(
    cfg: dict[str, Any],
    rows: list[dict[str, Any]],
    summary: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    manual_map = load_manual_failures_for_cfg(cfg)
    for row in rows:
        manual = manual_failure_for_episode(row, manual_map)
        if not manual:
            continue
        grade = normalise_quality_grade(manual.get("quality_grade")) or ("F" if manual.get("is_failure") else "A")
        reason = str(manual.get("reason_label") or "")
        row["manual_quality_grade"] = grade
        row["manual_review_reason"] = reason
        row["manual_failure"] = grade == "F"
        row["manual_failure_reason"] = reason if grade == "F" else ""
        row["reason_codes"] = manual.get("reason_codes") or []
        row["reason_labels_zh"] = manual.get("reason_labels_zh") or manual.get("reason_labels") or []
        row["reason_labels_en"] = manual.get("reason_labels_en") or []
        if grade == "F":
            row["delete_status"] = "采集失败"
            row["accepted"] = False
            row["error"] = row["manual_failure_reason"] or "人工标注采集失败"
        elif row.get("delete_status") == "采集失败":
            row["delete_status"] = "成功"
            row["accepted"] = True
            row["error"] = ""

    failure_rows = [
        row
        for row in rows
        if row.get("delete_status") == "采集失败" or row.get("manual_failure") is True
    ]
    if summary is None and failure_rows:
        summary = {}
    if summary is not None:
        summary = dict(summary)
        summary["manual_failure"] = len(failure_rows)
        summary["manual_failure_ids"] = [str(row.get("episode_id") or "") for row in failure_rows]
    return rows, summary


def lerobot_episode_rows(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    dataset_dir = lerobot_dataset_dir(cfg)
    if not is_lerobot_dataset_dir(dataset_dir):
        return []
    try:
        summary = build_lerobot_replay_summary(cfg)
    except Exception:
        return []
    fps = summary.get("info", {}).get("fps") if isinstance(summary.get("info"), dict) else None
    rows: list[dict[str, Any]] = []
    for item in summary.get("episodes", []):
        if not isinstance(item, dict):
            continue
        episode_name = str(item.get("episode_name") or "")
        rows.append(
            {
                "episode_id": episode_name,
                "episode_dir": str(dataset_dir),
                "h5": str(item.get("source_hdf5_path") or ""),
                "frame_count": item.get("length"),
                "fps": fps,
                "delete_status": "未质检",
                "error": "来自 LeRobot 数据集，未找到对应 HDF5/QC 报告",
                "source_episode_name": str(item.get("source_episode_name") or ""),
                "source_mcap_episode_name": str(
                    item.get("source_mcap_episode_name") or item.get("source_episode_name") or ""
                ),
                "lerobot_episode_index": item.get("episode_index"),
                "lerobot_dataset_dir": str(dataset_dir),
            }
        )
    return rows


def format_stationary_trim_note(stationary_trim: Any) -> str:
    if not isinstance(stationary_trim, dict):
        return ""
    kept = stationary_trim.get("kept_frame_count")
    original = stationary_trim.get("original_frame_count")
    removed = stationary_trim.get("removed_frame_count")
    threshold = stationary_trim.get("keep_stationary_frames")
    parts = []
    if kept is not None:
        parts.append(f"静止帧剔除后 {kept} 帧")
    if original is not None and removed is not None:
        parts.append(f"原始 {original} 帧，删除 {removed} 帧")
    elif removed is not None:
        parts.append(f"删除 {removed} 帧")
    if threshold is not None:
        parts.append(f"每段最多保留 {threshold} 帧")
    return "；".join(parts)


def combine_remark(*parts: Any) -> str:
    texts = [str(part).strip() for part in parts if str(part or "").strip()]
    return "；".join(texts)


def unique_texts(parts: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = str(part or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def finalise_qc_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        collection_failed = (
            normalise_quality_grade(row.get("manual_quality_grade")) == "F"
            or row.get("manual_failure") is True
            or str(row.get("delete_status") or "") == "采集失败"
        )
        row["collection_status"] = "采集失败" if collection_failed else "采集成功"
        warning_remarks = row.get("warning_remarks")
        if not isinstance(warning_remarks, list):
            warning_remarks = []
        warning_remarks = unique_texts(warning_remarks)
        warning_count = as_int(row.get("warning_count"))
        if warning_count is None:
            warning_count = len(warning_remarks)
        row["warning_count"] = warning_count
        row["warning_remarks"] = warning_remarks
        manual_grade = normalise_quality_grade(row.get("manual_quality_grade"))
        if manual_grade == "F":
            row["quality_grade"] = "F"
        elif row.get("shared_rules_version"):
            row["quality_grade"] = row.get("shared_quality_grade") or ""
        elif manual_grade:
            row["quality_grade"] = manual_grade
        elif collection_failed:
            row["quality_grade"] = "F"
        else:
            row["quality_grade"] = "A"
        max_stationary = as_int(row.get("max_stationary_run_frames"))
        if max_stationary is not None:
            row["longest_stationary_display"] = str(max_stationary)
        elif row.get("stationary_trim_applied"):
            row["longest_stationary_display"] = "已剔除"
        else:
            row["longest_stationary_display"] = ""
        if row.get("shared_rules_version") and row.get("shared_quality_grade") == "F":
            row["quality_grade"] = "F"
        quality_description_parts: list[Any] = [row.get("shared_reason", "")]
        if row.get("manual_review_reason"):
            quality_description_parts.append(row.get("manual_review_reason"))
        if collection_failed:
            quality_description_parts.append(row.get("manual_failure_reason") or row.get("error") or "人工标注采集失败")
        row["quality_description"] = "；".join(unique_texts(quality_description_parts))
        row["warning_text"] = "\n".join(warning_remarks)
        row["display_remark"] = "；".join(
            unique_texts([row["quality_description"], "；".join(warning_remarks)])
        )
    return rows


def status_cache_key(
    cfg: dict[str, Any],
    report_dir: Path | None,
) -> tuple[Any, ...]:
    return (
        json.dumps(
            stringify_config(cfg),
            ensure_ascii=False,
            sort_keys=True,
        ),
        str(report_dir or ""),
    )


def _path_status_signature(path: Path) -> tuple[str, int, int]:
    try:
        stat = path.stat()
    except OSError:
        return str(path), -1, -1
    return str(path), stat.st_mtime_ns, stat.st_size


def status_source_fingerprint(
    cfg: dict[str, Any],
    report_dir: Path | None,
) -> tuple[tuple[str, int, int], ...]:
    paths = [
        Path(cfg["hdf5_root"]),
        Path(cfg["qc_root"]),
        Path(cfg["lerobot_root"]),
        Path(cfg["mcap_path"]),
        Path(cfg["profile"]),
        LEROBOT_STAGE_SPLIT_SCRIPT,
        manual_failure_hdf5_path(cfg),
        Path(cfg["hdf5_root"]) / "renumber_plan.json",
    ]
    if str(cfg.get("robot_type") or "").lower() == "zerith":
        try:
            split_base = lerobot_stage_split_base_dir(cfg)
        except (OSError, RuntimeError, ValueError):
            split_base = None
        if split_base is not None:
            for grade in QUALITY_GRADES:
                grade_dir = split_base / grade
                paths.extend(
                    (
                        grade_dir,
                        grade_dir / "meta" / "info.json",
                        grade_dir / "meta" / "episode_name_mapping.json",
                    )
                )
    if report_dir is not None:
        paths.append(report_dir / "batch_summary.json")
    return tuple(_path_status_signature(path) for path in paths)


def _build_dataset_status(
    cfg: dict[str, Any],
    payload: dict[str, Any],
    report_dir: Path | None,
) -> dict[str, Any]:
    report_cache: dict[Path, dict[str, Any] | None] = {}
    summary = load_qc_summary(report_dir)
    episodes = discover_hdf5_episodes(cfg["hdf5_root"])
    by_id = {item["episode_id"]: item for item in episodes}
    has_hdf5_episodes = bool(episodes)

    if summary:
        for item in summary.get("episodes", []):
            if not isinstance(item, dict):
                continue
            episode_id = str(item.get("episode_id") or "")
            if has_hdf5_episodes and episode_id not in by_id:
                continue
            row = by_id.setdefault(episode_id, {"episode_id": episode_id})
            warning_summary = qc_warning_summary_from_output(
                item.get("output"),
                report_cache,
            )
            item_fps = as_number(item.get("fps"))
            merged_fps = item_fps if item_fps is not None and item_fps > 0.0 else row.get("fps")
            row.update(
                {
                    "qc_ok": item.get("ok"),
                    "accepted": item.get("accepted"),
                    "raw_quality_score": item.get("quality_score"),
                    "fps": merged_fps,
                    "delete_status": item.get("delete_status"),
                    "raw_delete_status": item.get("delete_status"),
                    "qc_output": item.get("output"),
                    "manual_failure": item.get("manual_failure", row.get("manual_failure")),
                    "manual_failure_reason": item.get("manual_failure_reason", row.get("manual_failure_reason", "")),
                    "error": str(item.get("error") or ""),
                    **warning_summary,
                    "qc_report_loaded": warning_summary.get("qc_report_loaded"),
                    "warning_count": warning_summary.get("warning_count"),
                    "warning_remarks": warning_summary.get("warning_remarks", []),
                    "max_stationary_run_frames": warning_summary.get("max_stationary_run_frames"),
                }
            )
    for row in by_id.values():
        if cfg.get("robot_type") == "zerith" and row.get("qc_ok") is False:
            row.update(shared_rules_version="zerith_qc_4",shared_quality_grade="F",shared_reason="读取或质检失败："+str(row.get("error") or "请查看运行日志"))
        row.setdefault("delete_status", "成功")
    rows = sorted(by_id.values(), key=lambda item: natural_key(str(item.get("episode_id") or "")))
    lerobot_dir = lerobot_dataset_dir(cfg)
    legacy_lerobot_exists = is_lerobot_dataset_dir(lerobot_dir)
    grade_dataset_dirs = {} if cfg.get("lerobot_dataset_dir") else lerobot_grade_dataset_dirs(cfg)
    existing_grade_dataset_dirs = {
        grade: path for grade, path in grade_dataset_dirs.items() if is_lerobot_dataset_dir(path)
    }
    lerobot_exists = legacy_lerobot_exists or bool(existing_grade_dataset_dirs)
    if not rows and summary is None and legacy_lerobot_exists:
        rows = lerobot_episode_rows(cfg)
    rows, summary = apply_manual_failures_to_rows(cfg, rows, summary)
    rows = finalise_qc_rows(rows)
    renumber_plan_path = cfg["hdf5_root"] / "renumber_plan.json"
    renumber_exists = renumber_plan_path.is_file()
    renumber = load_renumber_plan(cfg["hdf5_root"]) if renumber_exists else []
    lerobot_mapping = lerobot_dir / "meta" / "episode_name_mapping.json"
    grade_mapping_paths = {
        grade: path / "meta" / "episode_name_mapping.json"
        for grade, path in existing_grade_dataset_dirs.items()
    }
    displayed_lerobot_paths = (
        "\n".join(f"{grade}: {path}" for grade, path in existing_grade_dataset_dirs.items())
        if existing_grade_dataset_dirs
        else str(lerobot_dir)
    )
    displayed_mapping_paths = (
        "\n".join(f"{grade}: {path}" for grade, path in grade_mapping_paths.items())
        if grade_mapping_paths
        else str(lerobot_mapping)
    )
    lerobot_tasks: list[str] = []
    if existing_grade_dataset_dirs:
        for path in existing_grade_dataset_dirs.values():
            lerobot_tasks.extend(read_lerobot_tasks(path))
        lerobot_tasks = unique_strings(lerobot_tasks)
    elif legacy_lerobot_exists:
        lerobot_tasks = read_lerobot_tasks(lerobot_dir)
    raw_mcap_tasks, raw_mcap_instruction_files = discover_mcap_info_tasks(cfg["mcap_path"])
    return {
        "config": stringify_config(cfg),
        "qc_report_dir": str(report_dir) if report_dir else "",
        "summary": summary,
        "episodes": rows,
        "qc_overview": build_qc_overview(
            report_dir,
            rows,
            cfg,
            report_cache,
        ),
        "quality_check_items": quality_check_items(cfg),
        "stationary_threshold": stationary_threshold_for_cfg(cfg),
        "renumber_plan": renumber,
        "renumber_count": len(renumber),
        "renumber_grade_groups": renumber_grade_groups(renumber),
        "renumber_plan_path": str(renumber_plan_path),
        "renumber_plan_exists": renumber_exists,
        "lerobot_dataset_path": displayed_lerobot_paths,
        "lerobot_dataset_exists": lerobot_exists,
        "lerobot_grade_dataset_paths": {grade: str(path) for grade, path in existing_grade_dataset_dirs.items()},
        "lerobot_mapping": displayed_mapping_paths,
        "lerobot_mapping_exists": lerobot_mapping.is_file() or any(path.is_file() for path in grade_mapping_paths.values()),
        "lerobot_grade_mappings": {grade: str(path) for grade, path in grade_mapping_paths.items()},
        "lerobot_stage_split": lerobot_stage_split_status(cfg),
        "raw_mcap_tasks": raw_mcap_tasks,
        "raw_mcap_instruction_files": raw_mcap_instruction_files,
        "hdf5_tasks": discover_hdf5_tasks(cfg["hdf5_root"]),
        "lerobot_tasks": lerobot_tasks,
    }


def dataset_status(payload: dict[str, Any]) -> dict[str, Any]:
    cfg = derive_paths(payload)
    report_dir = latest_qc_report(
        cfg["qc_root"],
        cfg["dataset_name"],
        payload.get("qc_report_dir"),
    )
    key = status_cache_key(cfg, report_dir)
    fingerprint = status_source_fingerprint(cfg, report_dir)
    now = time.monotonic()
    with STATUS_CACHE_LOCK:
        cache_generation = STATUS_CACHE_GENERATION
        cached = STATUS_CACHE.get(key)
        if cached is not None:
            expires_at, cached_fingerprint, cached_payload = cached
            if now < expires_at and cached_fingerprint == fingerprint:
                STATUS_CACHE.pop(key)
                STATUS_CACHE[key] = cached
                return cached_payload
            STATUS_CACHE.pop(key, None)

    status = _build_dataset_status(cfg, payload, report_dir)
    latest_fingerprint = status_source_fingerprint(cfg, report_dir)
    with STATUS_CACHE_LOCK:
        if (
            cache_generation == STATUS_CACHE_GENERATION
            and latest_fingerprint == fingerprint
        ):
            STATUS_CACHE[key] = (
                time.monotonic() + STATUS_CACHE_TTL_SEC,
                fingerprint,
                status,
            )
            while len(STATUS_CACHE) > STATUS_CACHE_MAX_ENTRIES:
                STATUS_CACHE.pop(next(iter(STATUS_CACHE)))
    return status


def stringify_config(cfg: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in cfg.items():
        out[key] = str(value) if isinstance(value, Path) else value
    return out


def natural_key(text: str) -> list[tuple[int, Any]]:
    return natural_sort_key_tagged(text)


def natural_sort_key_tagged(text: str) -> list[tuple[int, Any]]:
    parts: list[tuple[int, Any]] = []
    for part in re.split(r"(\d+)", text):
        if not part:
            continue
        parts.append((0, int(part)) if part.isdigit() else (1, part.lower()))
    return parts


def natural_path_sort_key(path: Path) -> list[tuple[int, Any]]:
    key: list[tuple[int, Any]] = []
    for part in path.parts:
        key.extend(natural_sort_key_tagged(part))
        key.append((2, "/"))
    return key


def discover_lerobot_input_h5_paths(hdf5_root: Path) -> list[Path]:
    root = hdf5_root.expanduser().resolve()
    if root.is_file():
        return [root]
    if looks_like_hdf5_episode_dir(root):
        return [hdf5_path_for_episode_dir(root).resolve()]
    if not root.exists():
        return []
    preferred = (
        sorted(root.glob("**/aligned_joints.h5"))
        + sorted(root.glob("**/aligned_joints.hdf5"))
        + sorted(root.glob("*/episode.hdf5"))
        + sorted(root.glob("*/episode.h5"))
    )
    return [path.resolve() for path in preferred if "record" not in path.parts]


def hdf5_episode_dir_from_h5(h5_path: Path) -> Path:
    return h5_path.parent.parent if h5_path.parent.name == "states" else h5_path.parent


def lerobot_source_episode_entries(hdf5_root: Path) -> list[dict[str, Any]]:
    data_root = hdf5_root.expanduser().resolve()
    h5_paths = discover_lerobot_input_h5_paths(data_root)

    def sort_key(h5_path: Path) -> tuple[list[tuple[int, Any]], str, str]:
        episode_dir = hdf5_episode_dir_from_h5(h5_path)
        try:
            relative_episode_dir = episode_dir.relative_to(data_root)
        except ValueError:
            relative_episode_dir = episode_dir
        return natural_path_sort_key(relative_episode_dir), str(episode_dir), h5_path.name

    entries: list[dict[str, Any]] = []
    for source_index, h5_path in enumerate(sorted(h5_paths, key=sort_key)):
        episode_dir = hdf5_episode_dir_from_h5(h5_path)
        entries.append(
            {
                "source_episode_index": source_index,
                "episode_id": episode_dir.name,
                "episode_dir": episode_dir,
                "hdf5_file": h5_path,
            }
        )
    return entries


def quality_grade_rows_by_episode(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    status = dataset_status(stringify_config(cfg))
    return {
        str(row.get("episode_id") or ""): row
        for row in status.get("episodes", [])
        if str(row.get("episode_id") or "")
    }


def authoritative_quality_grade_entries(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    status = dataset_status(stringify_config(cfg))
    status_rows = status.get("episodes")
    if not isinstance(status_rows, list):
        raise ValueError("Web QC status has no episode rows")
    rows_by_id: dict[str, list[dict[str, Any]]] = {}
    for row in status_rows:
        if not isinstance(row, dict):
            continue
        episode_id = str(row.get("episode_id") or "").strip()
        if episode_id:
            rows_by_id.setdefault(episode_id, []).append(row)

    entries: list[dict[str, Any]] = []
    for source in lerobot_source_episode_entries(cfg["hdf5_root"]):
        episode_id = str(source.get("episode_id") or "").strip()
        matches = rows_by_id.get(episode_id, [])
        if not matches:
            raise ValueError(
                f"HDF5 episode {episode_id}: missing finalized Web QC status row"
            )
        if len(matches) != 1:
            raise ValueError(
                f"duplicate finalized Web QC status rows for HDF5 episode {episode_id}"
            )
        row = matches[0]
        grade = normalise_quality_grade(row.get("quality_grade"))
        if str(cfg.get("robot_type")) == "zerith" and row.get("manual_quality_grade") in ("A", "B"):
            if row.get("shared_review_required") or row.get("shared_quality_grade") == "F":
                # Rebind approval to the newest report without replacing the
                # persisted human grade with its lower-priority model result.
                from integrations.zerith_rules import prepare_manual_approval
                try:
                    prepared = prepare_manual_approval(sys.modules[__name__], cfg, episode_id, grade, "采用已保存的人工复核等级")
                except ValueError as exc:
                    raise ValueError(f"{episode_id}: 人工等级保留为 {grade}，转换检查未通过：{exc}") from exc
                if prepared:
                    row = dict(row, grade_approval=prepared[1], shared_quality_grade=grade, shared_review_required=False, grade_export_block="")
            if row.get("grade_export_block"):
                raise ValueError(f"{episode_id}: 人工等级保留为 {grade}，转换检查未通过：{row['grade_export_block']}")
        if str(cfg.get("robot_type")) == "zerith" and row.get("shared_rules_version") and row.get("shared_review_required"):
            continue
        if not grade:
            raise ValueError(
                f"HDF5 episode {episode_id}: invalid finalized Web QC quality grade "
                f"{row.get('quality_grade')!r}"
            )
        if str(cfg.get("robot_type")) == "zerith":
            if not row.get("shared_rules_version"):
                raise ValueError(f"{episode_id}: 请先执行新版质检，再转换 LeRobot")
            if row.get("shared_quality_grade") == "F" or row.get("shared_review_required") or grade not in ("A", "B"):
                continue
        entry = dict(source)
        entry["quality_grade"] = grade
        entry["status_row"] = row
        entries.append(entry)
    return entries


def sync_hdf5_quality_grade_entries(entries: list[dict[str, Any]]) -> int:
    changed = 0
    for entry in entries:
        episode_id = str(entry.get("episode_id") or "").strip()
        grade = normalise_quality_grade(entry.get("quality_grade"))
        if not grade:
            raise ValueError(
                f"HDF5 episode {episode_id}: missing authoritative quality grade"
            )
        meta_path = episode_meta_path_for_dir(Path(entry["episode_dir"]))
        payload = load_json_object_strict(meta_path) if meta_path.is_file() else {}
        updated = dict(payload)
        updated["quality_grade"] = grade
        updated["manual_quality_grade"] = grade
        updated["manual_failure"] = grade == "F"
        if updated != payload:
            atomic_write_json(meta_path, updated)
            changed += 1
    return changed


def validate_hdf5_quality_grade_entries(
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    counts = {grade: 0 for grade in QUALITY_GRADES}
    errors: list[str] = []
    for entry in entries:
        episode_id = str(entry.get("episode_id") or "").strip()
        expected = normalise_quality_grade(entry.get("quality_grade"))
        if not expected:
            errors.append(
                f"HDF5 episode {episode_id}: expected quality_grade is missing or invalid"
            )
            continue
        counts[expected] += 1
        meta_path = episode_meta_path_for_dir(Path(entry["episode_dir"]))
        try:
            sidecar = load_json_object_strict(meta_path)
        except ValueError as exc:
            errors.append(f"HDF5 episode {episode_id}: {exc}")
            continue

        for field in ("quality_grade", "manual_quality_grade"):
            actual = normalise_quality_grade(sidecar.get(field))
            if actual != expected:
                errors.append(
                    f"HDF5 episode {episode_id}: {field} expected {expected}, "
                    f"actual {sidecar.get(field)!r}"
                )

        expected_failure = expected == "F"
        actual_failure = sidecar.get("manual_failure")
        if not isinstance(actual_failure, bool) or actual_failure != expected_failure:
            errors.append(
                f"HDF5 episode {episode_id}: manual_failure expected "
                f"{expected_failure}, actual {actual_failure!r}"
            )

    if errors:
        raise ValueError("HDF5 质量等级完整性校验失败：\n" + "\n".join(errors))
    return {
        "episode_count": len(entries),
        "quality_grade_counts": counts,
    }


def quality_grade_episode_groups(
    cfg: dict[str, Any],
    entries: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {grade: [] for grade in QUALITY_GRADES}
    authoritative_entries = entries if entries is not None else authoritative_quality_grade_entries(cfg)
    for entry in authoritative_entries:
        grade = normalise_quality_grade(entry.get("quality_grade"))
        if not grade:
            raise ValueError(
                f"HDF5 episode {entry.get('episode_id')}: missing authoritative quality grade"
            )
        groups[grade].append(dict(entry))
    return {grade: entries for grade, entries in groups.items() if entries}


def build_renumber_plan(
    cfg: dict[str, Any],
    entries: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    groups = quality_grade_episode_groups(cfg, entries)
    for grade in QUALITY_GRADES:
        for grade_idx, entry in enumerate(groups.get(grade, [])):
            episode_dir = Path(entry["episode_dir"])
            h5_path = Path(entry["hdf5_file"])
            source_index = int(entry["source_episode_index"])
            row = entry.get("status_row") if isinstance(entry.get("status_row"), dict) else {}
            meta = load_json_file(episode_meta_path_for_dir(episode_dir))
            source_mcap_files = meta.get("source_mcap_files")
            if not isinstance(source_mcap_files, list):
                source_mcap_files = []
            source_mcap_episode_name = str(meta.get("source_episode_name") or "").strip()
            if not source_mcap_episode_name:
                source_mcap_episode_name = episode_dir.name
            dataset_dir = lerobot_grade_dataset_dir(cfg, grade)
            plan.append(
                {
                    "quality_grade": grade,
                    "source_episode_index": source_index,
                    "source_episode_name": episode_dir.name,
                    "source_episode_dir": str(episode_dir),
                    "source_mcap_episode_name": source_mcap_episode_name,
                    "source_mcap_files": [str(path) for path in source_mcap_files],
                    "source_mcap_file_names": [Path(str(path)).name for path in source_mcap_files],
                    "hdf5_episode_name": episode_dir.name,
                    "hdf5_episode_dir": str(episode_dir),
                    "hdf5_file": str(h5_path),
                    "lerobot_output_root": str(lerobot_grade_output_root(cfg, grade)),
                    "lerobot_repo_id": lerobot_grade_repo_id(cfg, grade),
                    "lerobot_dataset_dir": str(dataset_dir),
                    "lerobot_episode_index": grade_idx,
                    "grade_episode_index": grade_idx,
                    "lerobot_episode_name": f"episode_{grade_idx:06d}",
                    "collection_status": str(row.get("collection_status") or ""),
                    "warning_count": row.get("warning_count"),
                }
            )
    return plan


def load_renumber_plan(hdf5_root: Path) -> list[dict[str, Any]]:
    path = hdf5_root / "renumber_plan.json"
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    episodes = payload.get("episodes", []) if isinstance(payload, dict) else []
    out: list[dict[str, Any]] = []
    for item in episodes:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        episode_dir_text = str(
            row.get("hdf5_episode_dir")
            or row.get("source_episode_dir")
            or ""
        ).strip()
        episode_dir = Path(episode_dir_text) if episode_dir_text else hdf5_root / str(row.get("source_episode_name") or "")
        meta = load_json_file(episode_meta_path_for_dir(episode_dir))
        source_mcap_files = meta.get("source_mcap_files")
        if not isinstance(source_mcap_files, list):
            source_mcap_files = []
        hdf5_episode_name = str(row.get("hdf5_episode_name") or episode_dir.name or row.get("source_episode_name") or "")
        row.setdefault("hdf5_episode_name", hdf5_episode_name)
        row.setdefault("hdf5_episode_dir", str(episode_dir))
        row.setdefault("hdf5_file", str(hdf5_path_for_episode_dir(episode_dir)))
        row.setdefault("quality_grade", "")
        row.setdefault("grade_episode_index", row.get("lerobot_episode_index"))
        row.setdefault("lerobot_dataset_dir", "")
        row.setdefault("source_mcap_episode_name", str(meta.get("source_episode_name") or hdf5_episode_name))
        row.setdefault("source_mcap_files", [str(path) for path in source_mcap_files])
        row.setdefault("source_mcap_file_names", [Path(str(path)).name for path in source_mcap_files])
        out.append(row)
    return out


def renumber_grade_groups(plan: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for item in plan:
        grade = normalise_quality_grade(item.get("quality_grade")) or "A"
        group = groups.setdefault(
            grade,
            {
                "quality_grade": grade,
                "count": 0,
                "lerobot_output_root": item.get("lerobot_output_root", ""),
                "lerobot_repo_id": item.get("lerobot_repo_id", ""),
                "lerobot_dataset_dir": item.get("lerobot_dataset_dir", ""),
                "episodes": [],
            },
        )
        group["count"] += 1
        group["episodes"].append(str(item.get("hdf5_episode_name") or item.get("source_episode_name") or ""))
    return {grade: groups[grade] for grade in QUALITY_GRADES if grade in groups}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            item = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_json_object_strict(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: could not read JSON object: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return payload


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)


def unique_strings(values: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def discover_hdf5_tasks(hdf5_root: Path) -> list[str]:
    values: list[str] = []
    if not hdf5_root.exists():
        return values
    meta_paths = sorted(hdf5_root.rglob("meta/episode_meta.json")) + sorted(
        hdf5_root.glob("*/episode_meta.json")
    )
    for meta_path in meta_paths:
        meta = load_json_file(meta_path)
        for key in ("task", "text", "prompt"):
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                values.append(value)
        tasks = meta.get("tasks")
        if isinstance(tasks, list):
            values.extend(tasks)
    for h5_path in discover_lerobot_input_h5_paths(hdf5_root):
        try:
            import h5py  # type: ignore

            with h5py.File(h5_path, "r") as file_obj:
                task_name = str(file_obj.attrs.get("task_name") or "").strip()
                if task_name:
                    values.append(task_name)
        except (ImportError, OSError):
            continue
    return unique_strings(values)


def discover_mcap_info_files(mcap_path: Path) -> list[Path]:
    if not mcap_path.exists():
        return []
    if mcap_path.is_file():
        mcap_files = [mcap_path] if mcap_path.suffix.lower() == ".mcap" else []
    else:
        mcap_files = sorted(mcap_path.rglob("*.mcap"), key=lambda path: natural_key(str(path)))

    candidates: list[Path] = []
    for mcap_file in mcap_files:
        candidates.append(mcap_file.with_name(f"{mcap_file.stem}_info.json"))
        candidates.append(mcap_file.parent / "instructions.json")

    out: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen or not candidate.is_file():
            continue
        seen.add(key)
        out.append(candidate)
    return out


def discover_mcap_info_tasks(mcap_path: Path) -> tuple[list[str], list[str]]:
    values: list[Any] = []
    files: list[str] = []
    for info_path in discover_mcap_info_files(mcap_path):
        payload = load_json_file(info_path)
        mark = payload.get("mark") if isinstance(payload.get("mark"), dict) else payload
        if not isinstance(mark, dict):
            continue
        files.append(str(info_path))
        for key in ("full-instructions-en", "full_instructions_en", "full-instructions", "full_instructions"):
            raw_value = mark.get(key)
            if isinstance(raw_value, list):
                values.extend(raw_value)
            elif isinstance(raw_value, str):
                values.append(raw_value)
    return unique_strings(values), files


def configured_lerobot_dataset_dir(cfg: dict[str, Any]) -> Path:
    dataset_dir = cfg.get("lerobot_dataset_dir")
    if isinstance(dataset_dir, Path):
        return dataset_dir.resolve()
    return (cfg["lerobot_root"] / cfg["repo_id"]).resolve()


def is_lerobot_dataset_dir(path: Path) -> bool:
    return (
        (path / "meta" / "info.json").is_file()
        or (path / "meta" / "tasks.jsonl").is_file()
        or any(path.glob("data/chunk-*/episode_*.parquet"))
    )


def lerobot_dataset_matches_hdf5(path: Path, cfg: dict[str, Any]) -> bool:
    mapping = load_json_file(path / "meta" / "episode_name_mapping.json")
    data_dir = str(mapping.get("data_dir") or "").strip()
    if not data_dir:
        return False
    try:
        return Path(data_dir).expanduser().resolve() == cfg["hdf5_root"].resolve()
    except OSError:
        return False


def lerobot_dataset_dir(cfg: dict[str, Any]) -> Path:
    return configured_lerobot_dataset_dir(cfg)


def lerobot_episode_count(dataset_dir: Path) -> int:
    info = load_json_file(dataset_dir / "meta" / "info.json")
    total = as_int(info.get("total_episodes"))
    if total is not None:
        return total
    return len(list(dataset_dir.glob("data/chunk-*/episode_*.parquet")))


def grade_dataset_candidates(base: Path) -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []
    for grade in QUALITY_GRADES:
        path = base / grade
        if is_lerobot_dataset_dir(path):
            candidates.append((grade, path.resolve()))
    return candidates


def available_lerobot_replay_grades(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[tuple[str, Path]] = []
    configured = configured_lerobot_dataset_dir(cfg)
    if is_lerobot_dataset_dir(configured):
        candidates.append(("", configured.resolve()))
    candidates.extend(grade_dataset_candidates(configured))

    root = Path(cfg["lerobot_root"]).resolve()
    if is_lerobot_dataset_dir(root):
        candidates.append(("", root))
    candidates.extend(grade_dataset_candidates(root))

    seen: set[Path] = set()
    ordered: list[tuple[str, Path]] = []
    for grade, path in candidates:
        if path in seen:
            continue
        seen.add(path)
        ordered.append((grade, path))
    grade_rank = {grade: idx for idx, grade in enumerate(QUALITY_GRADES)}
    return [
        {
            "grade": grade,
            "label": grade or "全部",
            "dataset_dir": str(path),
            "episode_count": lerobot_episode_count(path),
        }
        for grade, path in sorted(ordered, key=lambda item: (grade_rank.get(item[0], 99), natural_key(str(item[1]))))
    ]


def resolve_lerobot_replay_cfg(cfg: dict[str, Any], requested_grade: str | None = None) -> dict[str, Any]:
    configured = configured_lerobot_dataset_dir(cfg)
    grades = available_lerobot_replay_grades(cfg)
    if not grades:
        raise FileNotFoundError(f"LeRobot dataset not found: {configured}")
    requested = normalise_quality_grade(requested_grade)
    if requested:
        selected = next((item for item in grades if item["grade"] == requested), None)
        if selected is None:
            raise FileNotFoundError(f"LeRobot grade {requested} dataset not found under {configured}")
    else:
        selected = next((item for item in grades if as_int(item.get("episode_count")) and as_int(item.get("episode_count")) > 0), grades[0])
    selected_grade = str(selected.get("grade") or "")
    selected_path = Path(str(selected["dataset_dir"])).resolve()
    resolved = dict(cfg)
    resolved["lerobot_dataset_dir"] = selected_path
    resolved["lerobot_available_grades"] = grades
    if selected_grade:
        resolved["lerobot_replay_grade"] = selected_grade
        resolved["repo_id"] = f"{cfg['repo_id'].strip('/')}/{selected_grade}" if str(cfg.get("repo_id") or "").strip() else selected_grade
    return resolved


def lerobot_grade_output_root(cfg: dict[str, Any], grade: str) -> Path:
    _ = grade
    return cfg["lerobot_root"].resolve()


def lerobot_grade_repo_id(cfg: dict[str, Any], grade: str) -> str:
    base = str(cfg["repo_id"]).strip().strip("/")
    grade_text = normalise_quality_grade(grade)
    return f"{base}/{grade_text}" if base else grade_text


def lerobot_grade_dataset_dir(cfg: dict[str, Any], grade: str) -> Path:
    return (cfg["lerobot_root"] / lerobot_grade_repo_id(cfg, grade)).resolve()


def lerobot_grade_dataset_dirs(cfg: dict[str, Any]) -> dict[str, Path]:
    return {grade: lerobot_grade_dataset_dir(cfg, grade) for grade in QUALITY_GRADES}


def _strict_integer(value: Any) -> int | None:
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value.strip())
    return None


def lerobot_stage_split_base_dir(cfg: dict[str, Any]) -> Path:
    root = Path(cfg["lerobot_root"]).expanduser().resolve()
    repo_id = str(cfg.get("repo_id") or "").strip()
    validate_lerobot_repo_id(root, repo_id)
    reserved_parts = {
        part.lower()
        for part in Path(repo_id).parts
        if part not in {"", ".", ".."}
    }
    reserved_sides = {*LEROBOT_STAGE_SPLIT_SIDES, "lefthand", "twohand", "twohands"}
    overlap = sorted(reserved_parts & reserved_sides)
    if overlap:
        raise ValueError(
            "LeRobot repo id 指向阶段输出目录，不能再作为切分源："
            + ", ".join(overlap)
        )
    base = (root / repo_id).resolve()
    try:
        base.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"LeRobot 阶段切分目录越界: {base}") from exc
    return base


def lerobot_stage_split_output_base_dir(cfg: dict[str, Any]) -> Path:
    """Return the parent of the twohands/lefthand/righthand buckets (legacy twohand too)."""

    source_root = Path(cfg["lerobot_root"]).expanduser().resolve()
    return source_root.parent if source_root.name.lower() in {"twohand", "twohands"} else source_root


def _lerobot_stage_split_child(base: Path, *parts: str) -> Path:
    expected_parts = tuple(str(part) for part in parts)
    candidate = base.joinpath(*expected_parts).resolve()
    try:
        relative = candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"LeRobot 阶段切分路径越界: {candidate}") from exc
    if relative.parts != expected_parts:
        raise ValueError(
            f"LeRobot 阶段切分路径包含符号链接或别名: {base.joinpath(*expected_parts)}"
        )
    return candidate


def lerobot_stage_split_output_dir(
    cfg: dict[str, Any], side: str, grade: str
) -> Path:
    side_text = str(side).strip().lower()
    if side_text == "lefthand":
        side_text = "left_hand"  # Retain the old API alias; the output directory is lefthand.
    if side_text not in LEROBOT_STAGE_SPLIT_SIDES:
        raise ValueError(f"invalid LeRobot stage side: {side!r}")
    grade_text = normalise_quality_grade(grade)
    if not grade_text:
        raise ValueError(f"invalid LeRobot quality grade: {grade!r}")
    root = Path(cfg["lerobot_root"]).expanduser().resolve()
    repo_id = str(cfg.get("repo_id") or "").strip()
    validate_lerobot_repo_id(root, repo_id)
    return _lerobot_stage_split_child(
        lerobot_stage_split_output_base_dir(cfg),
        "lefthand" if side_text == "left_hand" else side_text,
        *Path(repo_id).parts,
        grade_text,
    )


def _mapped_hdf5_path_for_stage_split(
    cfg: dict[str, Any], record: dict[str, Any]
) -> Path:
    source_text = str(
        record.get("source_h5") or record.get("hdf5_file") or ""
    ).strip()
    if not source_text:
        raise ValueError("mapping record 缺少 source_h5/hdf5_file")
    hdf5_root = Path(cfg["hdf5_root"]).expanduser().resolve()
    source = Path(source_text).expanduser()
    source = source.resolve() if source.is_absolute() else (hdf5_root / source).resolve()
    try:
        source.relative_to(hdf5_root)
    except ValueError as exc:
        raise ValueError(f"mapping HDF5 路径越界: {source}") from exc
    return source


def validate_lerobot_stage_split_grade(
    cfg: dict[str, Any], grade: str, source_dataset: Path
) -> dict[str, Any]:
    """Cheap fail-closed phase preflight for a converted Zerith grade."""

    grade_text = normalise_quality_grade(grade)
    mapping_path = source_dataset / "meta" / "episode_name_mapping.json"
    errors: list[str] = []
    error_count = 0

    def add_error(message: str) -> None:
        nonlocal error_count
        error_count += 1
        if len(errors) < 8:
            errors.append(message)

    info_path = source_dataset / "meta" / "info.json"
    try:
        info = load_json_file(info_path)
    except (OSError, UnicodeError) as exc:
        info = {}
        add_error(f"无法读取 LeRobot info {info_path}: {exc}")
    if not info:
        if error_count == 0:
            add_error(f"缺少或无法解析 LeRobot info: {info_path}")
    else:
        codebase_version = str(info.get("codebase_version") or "").strip()
        if not re.fullmatch(r"v2(?:\..+)?", codebase_version, re.IGNORECASE):
            add_error(
                f"LeRobot codebase_version 必须为 v2，实际 {codebase_version!r}"
            )
        info_robot_type = str(info.get("robot_type") or "").strip().lower()
        if info_robot_type != "zerith":
            add_error(
                f"LeRobot robot_type 必须为 zerith，实际 {info_robot_type!r}"
            )
        features = info.get("features")
        if not isinstance(features, dict):
            add_error("LeRobot info.features 缺失或格式错误")
            features = {}

        for feature_key in ("observation.state", "action"):
            feature = features.get(feature_key)
            shape = feature.get("shape") if isinstance(feature, dict) else None
            parsed_shape = (
                [_strict_integer(value) for value in shape]
                if isinstance(shape, list)
                else []
            )
            if parsed_shape != [23]:
                add_error(
                    f"LeRobot {feature_key} shape 必须为 [23]，实际 {shape!r}"
                )

        required_video_keys = (
            "observation.images.cam_high",
            "observation.images.cam_left_wrist",
            "observation.images.cam_right_wrist",
        )
        declared_video_keys = {
            str(key)
            for key, feature in features.items()
            if isinstance(feature, dict)
            and str(feature.get("dtype") or "").strip().lower() == "video"
        }
        if declared_video_keys != set(required_video_keys):
            add_error(
                "LeRobot 视频特征必须且只能是 3 路 Zerith 相机，"
                f"实际 {sorted(declared_video_keys)}"
            )
        for feature_key in required_video_keys:
            feature = features.get(feature_key)
            if not isinstance(feature, dict):
                add_error(f"LeRobot 缺少 Zerith 视频特征 {feature_key}")
                continue
            if str(feature.get("dtype") or "").strip().lower() != "video":
                add_error(
                    f"LeRobot {feature_key} dtype 必须为 video，"
                    f"实际 {feature.get('dtype')!r}"
                )
            shape = feature.get("shape")
            parsed_shape = (
                [_strict_integer(value) for value in shape]
                if isinstance(shape, list)
                else []
            )
            if len(parsed_shape) != 3 or parsed_shape[-1:] != [3]:
                add_error(
                    f"LeRobot {feature_key} shape 必须为 [H,W,3]，实际 {shape!r}"
                )

    try:
        mapping = load_json_file(mapping_path)
    except (OSError, UnicodeError) as exc:
        mapping = {}
        add_error(f"无法读取 mapping {mapping_path}: {exc}")
    if not mapping:
        if error_count == 0:
            add_error(f"缺少或无法解析 mapping: {mapping_path}")
        return {
            "grade": grade_text,
            "compatible": False,
            "checked_episode_count": 0,
            "error_count": error_count,
            "errors": errors,
            "reason": errors[0],
        }

    hdf5_root = Path(cfg["hdf5_root"]).expanduser().resolve()
    mapping_data_dir = str(mapping.get("data_dir") or "").strip()
    if not mapping_data_dir:
        add_error("mapping 缺少 data_dir")
    else:
        try:
            mapped_root = Path(mapping_data_dir).expanduser().resolve()
        except OSError as exc:
            add_error(f"mapping data_dir 无法解析: {exc}")
        else:
            if mapped_root != hdf5_root:
                add_error(
                    f"mapping data_dir 与当前 HDF5 不一致: {mapped_root} != {hdf5_root}"
                )

    records = mapping.get("episodes")
    if not isinstance(records, list) or not records:
        add_error("mapping episodes 为空")
        records = []

    try:
        source_episode_count = lerobot_episode_count(source_dataset)
    except OSError as exc:
        add_error(f"无法读取 LeRobot episode 数量: {exc}")
        source_episode_count = 0
    if source_episode_count <= 0:
        add_error("LeRobot 等级目录没有 episode")
    if len(records) != source_episode_count:
        add_error(
            f"mapping episode 数与 LeRobot 不一致: mapping={len(records)}, "
            f"lerobot={source_episode_count}"
        )

    mapped_indices: list[int] = []
    for record in records:
        if not isinstance(record, dict):
            add_error("mapping episodes 含非对象记录")
            continue
        mapped_index = _strict_integer(record.get("lerobot_episode_index"))
        if mapped_index is None:
            add_error("mapping record 的 lerobot_episode_index 非法")
        else:
            mapped_indices.append(mapped_index)
    if sorted(mapped_indices) != list(range(source_episode_count)):
        add_error("mapping 的 LeRobot episode 编号不是连续的 0..N-1")

    try:
        import h5py  # type: ignore
    except ImportError as exc:
        add_error(f"无法导入 h5py，不能预检阶段边界: {exc}")
        return {
            "grade": grade_text,
            "compatible": False,
            "checked_episode_count": 0,
            "error_count": error_count,
            "errors": errors,
            "reason": errors[0],
        }

    checked = 0
    seen_hdf5: set[Path] = set()
    for record_number, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            continue
        label = str(
            record.get("source_episode_name")
            or record.get("hdf5_episode_name")
            or f"mapping#{record_number}"
        )
        try:
            hdf5_path = _mapped_hdf5_path_for_stage_split(cfg, record)
        except (OSError, ValueError) as exc:
            add_error(f"{label}: {exc}")
            continue
        if hdf5_path in seen_hdf5:
            add_error(f"{label}: mapping 重复引用 HDF5 {hdf5_path}")
            continue
        seen_hdf5.add(hdf5_path)
        if not hdf5_path.is_file():
            add_error(f"{label}: HDF5 不存在 {hdf5_path}")
            continue

        mapped_frames = _strict_integer(record.get("num_frames"))
        if mapped_frames is None or mapped_frames <= 0:
            add_error(f"{label}: mapping num_frames 非法")
            continue
        try:
            with h5py.File(hdf5_path, "r") as file_obj:
                transitions = file_obj.get("subtask_transitions")
                timestamp = file_obj.get("timestamp/t")
                if transitions is None:
                    add_error(f"{label}: 缺少 /subtask_transitions")
                    continue
                if tuple(transitions.shape) != (2,) or transitions.dtype.kind not in "iu":
                    add_error(
                        f"{label}: /subtask_transitions 必须是 shape=(2,) 的整数数组，"
                        f"实际 shape={tuple(transitions.shape)}, dtype={transitions.dtype}"
                    )
                    continue
                values = [_strict_integer(value) for value in transitions[...]]
                if any(value is None for value in values):
                    add_error(f"{label}: /subtask_transitions 含非整数值")
                    continue
                boundary, final_boundary = (int(values[0]), int(values[1]))
                if timestamp is None or len(timestamp.shape) != 1:
                    add_error(f"{label}: 缺少一维 /timestamp/t")
                    continue
                frame_count = int(timestamp.shape[0])
                declared_frames = _strict_integer(file_obj.attrs.get("total_frames"))
                total_subtasks = _strict_integer(file_obj.attrs.get("total_subtasks"))
                completed_subtasks = _strict_integer(
                    file_obj.attrs.get("completed_subtasks")
                )
                if declared_frames != frame_count:
                    add_error(
                        f"{label}: total_frames 与 timestamp 长度不一致: "
                        f"{declared_frames} != {frame_count}"
                    )
                    continue
                if mapped_frames != frame_count:
                    add_error(
                        f"{label}: mapping num_frames 与 HDF5 不一致: "
                        f"{mapped_frames} != {frame_count}"
                    )
                    continue
                if total_subtasks != 2 or completed_subtasks != 2:
                    add_error(
                        f"{label}: 子任务属性必须为 total=2/completed=2，"
                        f"实际 total={total_subtasks}, completed={completed_subtasks}"
                    )
                    continue
                if not (0 < boundary < frame_count and final_boundary == frame_count):
                    add_error(
                        f"{label}: 阶段边界必须为 [boundary, N] 且 0<boundary<N，"
                        f"实际 [{boundary}, {final_boundary}], N={frame_count}"
                    )
                    continue
        except (
            AttributeError,
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            add_error(f"{label}: HDF5 阶段预检失败: {exc}")
            continue
        checked += 1

    compatible = error_count == 0 and checked == source_episode_count > 0
    reason = (
        f"已验证 {checked} 条 episode 的严格两阶段边界"
        if compatible
        else (errors[0] if errors else "两阶段预检未通过")
    )
    return {
        "grade": grade_text,
        "compatible": compatible,
        "checked_episode_count": checked,
        "error_count": error_count,
        "errors": errors,
        "reason": reason,
    }


def lerobot_stage_split_status(cfg: dict[str, Any]) -> dict[str, Any]:
    supported = str(cfg.get("robot_type") or "").lower() == "zerith"
    result: dict[str, Any] = {
        "supported": supported,
        "available": False,
        "phase_compatible": False,
        "reason": "仅零次方机器人支持按左右手阶段切分 LeRobot。",
        "script": str(LEROBOT_STAGE_SPLIT_SCRIPT),
        "source_grades": [],
        "grades": [],
        "overwrite": True,
    }
    if not supported:
        return result

    try:
        base = lerobot_stage_split_base_dir(cfg)
    except (OSError, RuntimeError, ValueError) as exc:
        result["reason"] = str(exc)
        return result
    result["base_dir"] = str(base)
    hdf5_root = Path(cfg["hdf5_root"]).expanduser().resolve()
    result["hdf5_root_exists"] = hdf5_root.is_dir()
    result["script_exists"] = LEROBOT_STAGE_SPLIT_SCRIPT.is_file()

    grade_rows: list[dict[str, Any]] = []
    source_scan_errors: list[str] = []
    for grade in QUALITY_GRADES:
        try:
            source = _lerobot_stage_split_child(base, grade)
        except (OSError, RuntimeError, ValueError) as exc:
            source_scan_errors.append(f"{grade}: {exc}")
            continue
        # Only immediate <repo_id>/<grade> children are sources.  In particular,
        # Never recurse into generated left_hand/righthand stage outputs.
        try:
            if not is_lerobot_dataset_dir(source):
                continue
            episode_count = lerobot_episode_count(source)
        except OSError as exc:
            source_scan_errors.append(f"{grade}: 无法读取 LeRobot 数据集: {exc}")
            continue
        if episode_count <= 0:
            continue
        compatibility = validate_lerobot_stage_split_grade(cfg, grade, source)
        try:
            left_output = lerobot_stage_split_output_dir(cfg, "left_hand", grade)
            right_output = lerobot_stage_split_output_dir(cfg, "righthand", grade)
        except (OSError, RuntimeError, ValueError) as exc:
            compatibility = {
                "grade": grade,
                "compatible": False,
                "checked_episode_count": compatibility.get(
                    "checked_episode_count", 0
                ),
                "error_count": int(compatibility.get("error_count") or 0) + 1,
                "errors": [
                    *(compatibility.get("errors") or []),
                    f"输出路径无效: {exc}",
                ],
                "reason": f"输出路径无效: {exc}",
            }
            output_base = lerobot_stage_split_output_base_dir(cfg)
            repo_id = Path(str(cfg.get("repo_id") or "").strip())
            left_output = output_base / "lefthand" / repo_id / grade
            right_output = output_base / "righthand" / repo_id / grade
        row = {
            "grade": grade,
            "source_dataset": str(source),
            "episode_count": episode_count,
            "left_output": str(left_output),
            "right_output": str(right_output),
            **compatibility,
        }
        grade_rows.append(row)

    result["grades"] = grade_rows
    result["source_grades"] = [row["grade"] for row in grade_rows]
    phase_compatible = bool(grade_rows) and all(
        bool(row.get("compatible")) for row in grade_rows
    )
    result["phase_compatible"] = phase_compatible

    if not hdf5_root.is_dir():
        result["reason"] = f"HDF5 数据集目录不存在: {hdf5_root}"
    elif source_scan_errors:
        result["reason"] = "LeRobot 源等级检查失败；" + "；".join(
            source_scan_errors
        )
    elif not grade_rows:
        result["reason"] = (
            "未发现可切分的源等级；请先生成至少一个非空的 A/B/C/F LeRobot 等级目录。"
        )
    elif not phase_compatible:
        failed = [
            f"{row['grade']}: {row.get('reason') or '两阶段预检未通过'}"
            for row in grade_rows
            if not row.get("compatible")
        ]
        result["reason"] = "阶段预检未通过；" + "；".join(failed)
    elif not LEROBOT_STAGE_SPLIT_SCRIPT.is_file():
        result["reason"] = f"阶段切分脚本不存在: {LEROBOT_STAGE_SPLIT_SCRIPT}"
    else:
        result["available"] = True
        result["reason"] = (
            "可切分等级 "
            + "/".join(result["source_grades"])
            + "；输出将覆盖对应 lefthand/righthand 等级目录。"
        )
    return result


def lerobot_browser_grade_entry(grade: str, dataset_dir: Path, root: Path, repo_id: str) -> dict[str, Any]:
    info = load_json_file(dataset_dir / "meta" / "info.json")
    total_episodes = as_int(info.get("total_episodes"))
    if total_episodes is None:
        total_episodes = len(list(dataset_dir.glob("data/chunk-*/episode_*.parquet")))
    grade_text = normalise_quality_grade(grade) if grade else ""
    return {
        "grade": grade_text,
        "label": grade_text or "空",
        "path": str(dataset_dir),
        "lerobot_root": str(root),
        "repo_id": repo_id.strip("/"),
        "episode_count": total_episodes,
        "fps": as_number(info.get("fps")),
    }


def discover_lerobot_browser_datasets(parent: Path) -> list[dict[str, Any]]:
    parent = parent.expanduser().resolve()
    if not parent.is_dir():
        return []

    groups: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add_group(name: str, path: Path, grades: list[dict[str, Any]]) -> None:
        if not grades:
            return
        key = (name, str(path.resolve()))
        if key in seen:
            return
        seen.add(key)
        groups.append(
            {
                "name": name,
                "path": str(path.resolve()),
                "grades": grades,
                "episode_count": sum(as_int(item.get("episode_count")) or 0 for item in grades),
            }
        )

    if is_lerobot_dataset_dir(parent):
        add_group(
            parent.name,
            parent,
            [lerobot_browser_grade_entry("", parent, parent.parent, parent.name)],
        )

    parent_grades = [
        lerobot_browser_grade_entry(grade, parent / grade, parent, grade)
        for grade in QUALITY_GRADES
        if is_lerobot_dataset_dir(parent / grade)
    ]
    if parent_grades:
        add_group(parent.name, parent, parent_grades)

    for child in sorted(parent.iterdir(), key=lambda path: natural_key(path.name)):
        if not child.is_dir():
            continue
        if parent_grades and normalise_quality_grade(child.name):
            continue
        grades: list[dict[str, Any]] = []
        if is_lerobot_dataset_dir(child):
            grades.append(lerobot_browser_grade_entry("", child, parent, child.name))
        for grade in QUALITY_GRADES:
            grade_dir = child / grade
            if is_lerobot_dataset_dir(grade_dir):
                grades.append(lerobot_browser_grade_entry(grade, grade_dir, parent, f"{child.name}/{grade}"))
        add_group(child.name, child, grades)

    return sorted(groups, key=lambda item: natural_key(str(item.get("name") or "")))


def read_lerobot_tasks(dataset_dir: Path) -> list[str]:
    tasks: list[Any] = []
    for item in load_jsonl(dataset_dir / "meta" / "tasks.jsonl"):
        task = item.get("task")
        if task:
            tasks.append(task)
    return unique_strings(tasks)


def lerobot_task_map(dataset_dir: Path) -> dict[int, str]:
    """Read the authoritative task_index -> prompt mapping for replay."""
    path = dataset_dir / "meta" / "tasks.jsonl"
    rows = load_jsonl(path)
    if path.is_file() and not rows:
        raise ValueError(f"{path}: 没有可读取的 Prompt 映射")
    tasks: dict[int, str] = {}
    for row_number, row in enumerate(rows, start=1):
        task_index = as_int(row.get("task_index"))
        task = str(row.get("task") or "").strip()
        if task_index is None or not task:
            raise ValueError(f"{path}:{row_number}: task_index/task 无效")
        previous = tasks.get(task_index)
        if previous is not None and previous != task:
            raise ValueError(f"{path}: task_index {task_index} 对应多个 Prompt")
        tasks[task_index] = task
    return tasks


def lerobot_episode_files(dataset_dir: Path) -> dict[int, Path]:
    """Return one and only one parquet file for every Episode index."""
    files: dict[int, Path] = {}
    for path in sorted(dataset_dir.glob("data/chunk-*/episode_*.parquet")):
        match = re.fullmatch(r"episode_(\d+)\.parquet", path.name)
        if match is None:
            continue
        episode_index = int(match.group(1))
        previous = files.get(episode_index)
        if previous is not None and previous.resolve() != path.resolve():
            raise ValueError(
                f"Episode {episode_index} 对应多个 parquet：{previous}；{path}"
            )
        files[episode_index] = path.resolve()
    if not files:
        raise ValueError(f"{dataset_dir}: 未发现 data/chunk-*/episode_*.parquet")
    return files


def _indexed_lerobot_metadata(
    rows: list[dict[str, Any]], key: str, path: Path
) -> dict[int, dict[str, Any]]:
    indexed: dict[int, dict[str, Any]] = {}
    for row_number, row in enumerate(rows, start=1):
        index = as_int(row.get(key))
        if index is None:
            raise ValueError(f"{path}:{row_number}: {key} 无效")
        if index in indexed:
            raise ValueError(f"{path}: {key}={index} 重复")
        indexed[index] = row
    return indexed


def replay_cfg_for_dataset(dataset_dir: Path) -> dict[str, Any]:
    dataset_dir = dataset_dir.expanduser().resolve()
    return {
        "lerobot_dataset_dir": dataset_dir,
        "lerobot_root": dataset_dir,
        "repo_id": dataset_dir.name,
        "hdf5_root": dataset_dir,
    }


def validate_lerobot_visualization_dataset(dataset_dir: Path) -> dict[str, Any]:
    """Validate the cheap, file-level one-to-one replay mapping before opening."""
    dataset_dir = dataset_dir.expanduser().resolve()
    if not cross_platform.is_lerobot_dataset(dataset_dir):
        raise ValueError(f"不是 LeRobot 数据集：{dataset_dir}")
    data_files = lerobot_episode_files(dataset_dir)
    data_indices = set(data_files)
    info = load_json_file(dataset_dir / "meta" / "info.json")
    declared_total = as_int(info.get("total_episodes"))
    if declared_total is not None and declared_total != len(data_files):
        raise ValueError(
            f"info.json 声明 {declared_total} 个 Episode，实际 parquet 为 {len(data_files)} 个"
        )

    episodes_path = dataset_dir / "meta" / "episodes.jsonl"
    if episodes_path.is_file():
        episode_meta = _indexed_lerobot_metadata(
            load_jsonl(episodes_path), "episode_index", episodes_path
        )
        if set(episode_meta) != data_indices:
            raise ValueError(
                "episodes.jsonl 与 parquet 的 Episode 编号不一致："
                f"meta={sorted(episode_meta)}，parquet={sorted(data_indices)}"
            )

    mapping_path = dataset_dir / "meta" / "episode_name_mapping.json"
    mapping = load_json_file(mapping_path)
    mapping_rows = mapping.get("episodes") if isinstance(mapping.get("episodes"), list) else []
    if mapping_rows:
        mapping_index = _indexed_lerobot_metadata(
            [row for row in mapping_rows if isinstance(row, dict)],
            "lerobot_episode_index",
            mapping_path,
        )
        if set(mapping_index) != data_indices:
            raise ValueError(
                "episode_name_mapping.json 与 parquet 的 Episode 编号不一致："
                f"mapping={sorted(mapping_index)}，parquet={sorted(data_indices)}"
            )

    lerobot_task_map(dataset_dir)
    cfg = resolve_lerobot_replay_cfg(replay_cfg_for_dataset(dataset_dir))
    summary = build_lerobot_replay_summary(cfg)
    summary_by_index = {
        int(item["episode_index"]): item for item in summary.get("episodes", [])
    }
    if set(summary_by_index) != data_indices:
        raise ValueError(
            "回放列表与实际 parquet 的 Episode 编号不一致："
            f"replay={sorted(summary_by_index)}，parquet={sorted(data_indices)}"
        )

    expected_data_by_index = {
        index: str(path.relative_to(dataset_dir)) for index, path in data_files.items()
    }
    view_counts: set[int] = set()
    for episode_index, episode in summary_by_index.items():
        data_file = str(episode.get("data_file") or "")
        if data_file != expected_data_by_index[episode_index]:
            raise ValueError(
                f"Episode {episode_index}: 回放 parquet={data_file!r}，"
                f"实际={expected_data_by_index[episode_index]!r}"
            )
        video_files = episode.get("video_files")
        if not isinstance(video_files, dict) or not video_files:
            raise ValueError(f"Episode {episode_index}: 没有可视化视频")
        view_counts.add(len(video_files))
        for video_key, relative_path in video_files.items():
            video_path = (dataset_dir / str(relative_path)).resolve()
            try:
                video_path.relative_to(dataset_dir)
            except ValueError as exc:
                raise ValueError(
                    f"Episode {episode_index} / {video_key}: 视频路径越界 {relative_path}"
                ) from exc
            expected_name = f"episode_{episode_index:06d}.mp4"
            if video_path.name != expected_name:
                raise ValueError(
                    f"Episode {episode_index} / {video_key}: 视频文件名应为 {expected_name}，"
                    f"实际为 {video_path.name}"
                )
            if not video_path.is_file():
                raise ValueError(
                    f"Episode {episode_index} / {video_key}: 视频不存在 {video_path}"
                )
    if len(view_counts) > 1:
        raise ValueError(f"不同 Episode 的视频视角数量不一致：{sorted(view_counts)}")

    return {
        "dataset_dir": str(dataset_dir),
        "episode_count": len(data_files),
        "fps": as_number(info.get("fps")) or 0,
        "video_view_count": next(iter(view_counts), 0),
        "task_count": len(lerobot_task_map(dataset_dir)),
        "summary": summary,
    }


def discover_lerobot_visualization_datasets(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"目录不存在或不可读取：{root}")
    paths = cross_platform.discover_under(root, recursive=True)
    datasets: list[dict[str, Any]] = []
    for path in paths:
        try:
            relative = "." if path.resolve() == root else str(path.resolve().relative_to(root))
        except ValueError:
            relative = str(path.resolve())
        entry: dict[str, Any] = {
            "name": path.name if relative == "." else relative,
            "path": str(path.resolve()),
            "relative_path": relative,
            "episode_count": lerobot_episode_count(path),
            "fps": 0,
            "video_view_count": 0,
            "task_count": 0,
            "error": "",
        }
        try:
            validation = validate_lerobot_visualization_dataset(path)
            entry.update(
                {
                    "episode_count": validation["episode_count"],
                    "fps": validation["fps"],
                    "video_view_count": validation["video_view_count"],
                    "task_count": validation["task_count"],
                }
            )
        except Exception as exc:
            entry["error"] = str(exc)
        datasets.append(entry)
    return {
        "root": str(root),
        "recursive": True,
        "datasets": datasets,
        "dataset_count": len(datasets),
        "valid_dataset_count": sum(1 for item in datasets if not item["error"]),
    }


def lerobot_quality_description(meta: dict[str, Any], mapping_row: dict[str, Any]) -> str:
    preferred_values: list[Any] = []
    fallback_values: list[Any] = []
    for source in (meta, mapping_row):
        preferred_values.append(source.get("quality_description"))
        preferred_values.append(source.get("manual_review_reason"))
        preferred_values.append(source.get("reason_label"))
        preferred_values.append(source.get("reason_labels_zh"))
        preferred_values.append(source.get("reason_labels"))
        fallback_values.append(source.get("reason_labels_en"))
    out: list[str] = []
    for value in preferred_values:
        for text in split_reason_text(value):
            if text and text not in out:
                out.append(text)
    if not out:
        for value in fallback_values:
            for text in split_reason_text(value):
                if text and text not in out:
                    out.append(text)
    return "；".join(out)


def lerobot_episode_chunk(episode_index: int, info: dict[str, Any]) -> int:
    chunks_size = int(info.get("chunks_size") or 1000)
    return episode_index // max(1, chunks_size)


def format_lerobot_path(pattern: str, episode_index: int, info: dict[str, Any], video_key: str = "") -> str:
    return pattern.format(
        episode_chunk=lerobot_episode_chunk(episode_index, info),
        episode_index=episode_index,
        video_key=video_key,
    )


def lerobot_video_keys(info: dict[str, Any]) -> list[str]:
    features = info.get("features")
    if not isinstance(features, dict):
        return []
    keys = [
        str(key)
        for key, value in features.items()
        if isinstance(value, dict) and value.get("dtype") == "video"
    ]
    return sorted(keys)


def source_hdf5_path(cfg: dict[str, Any], mapping: dict[str, Any], episode: dict[str, Any]) -> str:
    source_h5 = str(episode.get("source_h5") or episode.get("source_hdf5") or "").strip()
    source_dir = str(episode.get("source_episode_dir") or episode.get("source_episode_name") or "").strip()
    base_text = str(mapping.get("data_dir") or cfg["hdf5_root"])
    base = Path(base_text).expanduser()
    if source_h5:
        path = Path(source_h5)
        return str(path if path.is_absolute() else (base / path).resolve())
    if source_dir:
        return str(hdf5_path_for_episode_dir((base / source_dir).resolve()).resolve())
    return ""


def build_lerobot_replay_summary(cfg: dict[str, Any]) -> dict[str, Any]:
    dataset_dir = lerobot_dataset_dir(cfg)
    if not dataset_dir.exists():
        raise FileNotFoundError(f"LeRobot dataset not found: {dataset_dir}")
    info = load_json_file(dataset_dir / "meta" / "info.json")
    episodes_meta = {
        int(item.get("episode_index")): item
        for item in load_jsonl(dataset_dir / "meta" / "episodes.jsonl")
        if str(item.get("episode_index", "")).isdigit()
    }
    mapping = load_json_file(dataset_dir / "meta" / "episode_name_mapping.json")
    mapping_rows = mapping.get("episodes", []) if isinstance(mapping.get("episodes"), list) else []
    mapping_by_index = {
        int(item.get("lerobot_episode_index")): item
        for item in mapping_rows
        if isinstance(item, dict) and str(item.get("lerobot_episode_index", "")).isdigit()
    }
    data_pattern = str(info.get("data_path") or "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet")
    video_pattern = str(info.get("video_path") or "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4")
    video_keys = lerobot_video_keys(info)
    task_by_index = lerobot_task_map(dataset_dir)
    indices = sorted(set(episodes_meta) | set(mapping_by_index))
    if not indices:
        for parquet in sorted(dataset_dir.glob("data/chunk-*/episode_*.parquet")):
            stem = parquet.stem.replace("episode_", "")
            if stem.isdigit():
                indices.append(int(stem))
    summaries: list[dict[str, Any]] = []
    for idx in sorted(set(indices)):
        meta = episodes_meta.get(idx, {})
        map_item = mapping_by_index.get(idx, {})
        episode_name = str(map_item.get("lerobot_episode_name") or f"episode_{idx:06d}")
        source_episode_name = str(map_item.get("source_episode_name") or "")
        source_mcap_episode_name = str(
            map_item.get("source_mcap_episode_name")
            or map_item.get("source_episode_name")
            or ""
        )
        video_files = map_item.get("lerobot_video_files") if isinstance(map_item.get("lerobot_video_files"), dict) else {}
        if not video_files:
            video_files = {key: format_lerobot_path(video_pattern, idx, info, key) for key in video_keys}
        full_instructions_en = meta.get("full_instructions_en")
        if isinstance(full_instructions_en, list):
            full_instruction_tasks = [str(item) for item in full_instructions_en if str(item).strip()]
        else:
            full_instruction_tasks = []
        tasks = meta.get("tasks")
        metadata_tasks = (
            [str(item).strip() for item in tasks if isinstance(item, str) and str(item).strip()]
            if isinstance(tasks, list)
            else []
        )
        explicit_task = str(meta.get("task") or map_item.get("task") or "").strip()
        metadata_task_index = as_int(meta.get("task_index"))
        indexed_task = task_by_index.get(metadata_task_index, "") if metadata_task_index is not None else ""
        episode_tasks = full_instruction_tasks or metadata_tasks
        if not episode_tasks and explicit_task:
            episode_tasks = [explicit_task]
        if not episode_tasks and indexed_task:
            episode_tasks = [indexed_task]
        if not episode_tasks and len(task_by_index) == 1:
            episode_tasks = [next(iter(task_by_index.values()))]
        subtask_segments = meta.get("subtask_segments")
        if not isinstance(subtask_segments, list):
            subtask_segments = []
        if not subtask_segments and isinstance(map_item.get("stages"), list):
            offset = int((map_item.get("source_range") or [0])[0])
            length = int(meta.get("length") or 0)
            subtask_segments = [dict(start=max(0,int(st["start"])-offset), end=min(length,int(st["end"])-offset)-1,
                subtask=f"阶段 {idx+1} · {'左手' if st['hand']=='left' else '右手'} · {st['item']}")
                for idx,st in enumerate(map_item["stages"]) if int(st["end"])>offset and int(st["start"])<offset+length]
        source_hdf5 = source_hdf5_path(cfg, mapping, map_item)
        quality_grade = normalise_quality_grade(meta.get("quality_grade") or map_item.get("quality_grade"))
        summaries.append(
            {
                "episode_index": idx,
                "episode_name": episode_name,
                "source_episode_name": source_episode_name,
                "source_mcap_episode_name": source_mcap_episode_name,
                "source_hdf5_path": source_hdf5,
                "task": str(explicit_task or (episode_tasks[0] if episode_tasks else "")),
                "tasks": episode_tasks,
                "subtask_segments": subtask_segments,
                "description_en": meta.get("description_en") if isinstance(meta.get("description_en"), list) else [],
                "quality_grade": quality_grade,
                "quality_description": lerobot_quality_description(meta, map_item),
                "length": int(meta.get("length") or map_item.get("num_frames") or 0),
                "data_file": str(map_item.get("lerobot_data_file") or format_lerobot_path(data_pattern, idx, info)),
                "video_files": {str(key): str(value) for key, value in video_files.items()},
            }
        )
    return {
        "dataset_dir": str(dataset_dir),
        "hdf5_root": str(cfg["hdf5_root"]),
        "repo_id": cfg["repo_id"],
        "selected_grade": str(cfg.get("lerobot_replay_grade") or ""),
        "available_grades": cfg.get("lerobot_available_grades") or available_lerobot_replay_grades(cfg),
        "tasks": read_lerobot_tasks(dataset_dir),
        "info": {
            "fps": info.get("fps"),
            "total_episodes": info.get("total_episodes"),
            "total_frames": info.get("total_frames"),
            "robot_type": info.get("robot_type"),
        },
        "episodes": summaries,
    }


def compact_vector(value: Any) -> list[float]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, tuple):
        value = list(value)
    if not isinstance(value, list):
        return []
    out: list[float] = []
    for item in value:
        try:
            out.append(round(float(item), 6))
        except (TypeError, ValueError):
            continue
    return out


def subtask_for_frame(frame_index: int, segments: list[Any]) -> str:
    for item in segments:
        if not isinstance(item, dict):
            continue
        try:
            start = int(item.get("start"))
            end = int(item.get("end"))
        except (TypeError, ValueError):
            continue
        if start <= frame_index <= end:
            return str(item.get("subtask") or "")
    return ""


def lerobot_vector_columns(dataset_dir: Path, df_columns: list[str]) -> tuple[str | None, str | None]:
    columns = set(df_columns)
    state_candidates = ["state", "observation.state"]
    action_candidates = ["actions", "action"]
    state_key = next((key for key in state_candidates if key in columns), None)
    action_key = next((key for key in action_candidates if key in columns), None)
    if state_key and action_key:
        return state_key, action_key

    info = load_json_file(dataset_dir / "meta" / "info.json")
    features = info.get("features")
    if isinstance(features, dict):
        vector_keys = [
            str(key)
            for key, value in features.items()
            if key in columns
            and isinstance(value, dict)
            and value.get("dtype") in {"float32", "float64"}
            and isinstance(value.get("shape"), list)
        ]
        if state_key is None:
            state_key = next((key for key in vector_keys if "state" in key), None)
        if action_key is None:
            action_key = next((key for key in vector_keys if "action" in key), None)
    return state_key, action_key


def load_lerobot_episode_payload(cfg: dict[str, Any], episode_index: int) -> dict[str, Any]:
    summary = build_lerobot_replay_summary(cfg)
    summary_episode = next(
        (item for item in summary["episodes"] if int(item["episode_index"]) == episode_index),
        None,
    )
    if summary_episode is None:
        raise FileNotFoundError(f"LeRobot episode not found: {episode_index}")
    dataset_dir = Path(summary["dataset_dir"])
    episode = dict(summary_episode)
    parquet_path = (dataset_dir / episode["data_file"]).resolve()
    parquet_path.relative_to(dataset_dir)
    if not parquet_path.is_file():
        raise FileNotFoundError(parquet_path)
    expected_parquet_name = f"episode_{episode_index:06d}.parquet"
    if parquet_path.name != expected_parquet_name:
        raise ValueError(
            f"Episode {episode_index}: parquet 文件名应为 {expected_parquet_name}，"
            f"实际为 {parquet_path.name}"
        )
    try:
        import pandas as pd  # type: ignore
    except ImportError as exc:
        raise ImportError("pandas/pyarrow is required to preview LeRobot parquet files") from exc
    df = pd.read_parquet(parquet_path)
    if df.empty:
        raise ValueError(f"Episode {episode_index}: parquet 没有数据帧")
    if "episode_index" not in df.columns:
        raise ValueError(f"Episode {episode_index}: parquet 缺少 episode_index")
    parquet_episode_indices = {
        int(value) for value in df["episode_index"].dropna().unique().tolist()
    }
    if parquet_episode_indices != {episode_index}:
        raise ValueError(
            f"Episode {episode_index}: parquet 内 episode_index={sorted(parquet_episode_indices)}"
        )
    if "frame_index" not in df.columns:
        raise ValueError(f"Episode {episode_index}: parquet 缺少 frame_index")
    frame_indices = [int(value) for value in df["frame_index"].tolist()]
    expected_frame_indices = list(range(len(df)))
    if frame_indices != expected_frame_indices:
        raise ValueError(
            f"Episode {episode_index}: frame_index 必须按 0..{len(df) - 1} 连续排列"
        )
    if "timestamp" not in df.columns:
        raise ValueError(f"Episode {episode_index}: parquet 缺少 timestamp")
    timestamps = [float(value) for value in df["timestamp"].tolist()]
    if any(not math.isfinite(value) for value in timestamps) or any(
        later < earlier for earlier, later in zip(timestamps, timestamps[1:])
    ):
        raise ValueError(f"Episode {episode_index}: timestamp 非有限值或顺序错误")

    task_by_index = lerobot_task_map(dataset_dir)
    actual_task = ""
    actual_task_index: int | None = None
    if "task_index" in df.columns:
        task_indices = {
            int(value) for value in df["task_index"].dropna().unique().tolist()
        }
        if len(task_indices) != 1:
            raise ValueError(
                f"Episode {episode_index}: 应且只能对应一个 task_index，实际为 {sorted(task_indices)}"
            )
        actual_task_index = next(iter(task_indices))
        actual_task = task_by_index.get(actual_task_index, "")
        if not actual_task:
            raise ValueError(
                f"Episode {episode_index}: task_index={actual_task_index} 在 tasks.jsonl 中无对应 Prompt"
            )
    else:
        declared = unique_strings(
            [
                value
                for value in [episode.get("task"), *(episode.get("tasks") or [])]
                if isinstance(value, str) and value.strip()
            ]
        )
        if len(declared) != 1:
            raise ValueError(
                f"Episode {episode_index}: parquet 缺少 task_index，元数据 Prompt 也不唯一"
            )
        actual_task = declared[0]

    declared_tasks = unique_strings(
        [
            value
            for value in [episode.get("task"), *(episode.get("tasks") or [])]
            if isinstance(value, str) and value.strip()
        ]
    )
    if declared_tasks and actual_task not in declared_tasks:
        raise ValueError(
            f"Episode {episode_index}: parquet Prompt={actual_task!r}，"
            f"Episode 元数据 Prompt={declared_tasks!r}"
        )
    episode["task"] = actual_task
    episode["tasks"] = [actual_task]
    episode["task_index"] = actual_task_index

    state_key, action_key = lerobot_vector_columns(dataset_dir, list(df.columns))
    if not state_key or not action_key:
        raise ValueError(
            f"Episode {episode_index}: 无法同时定位 State/Action 列，"
            f"state={state_key!r}，action={action_key!r}"
        )
    info = load_json_file(dataset_dir / "meta" / "info.json")
    features = info.get("features") if isinstance(info.get("features"), dict) else {}

    def expected_vector_dim(key: str) -> int | None:
        feature = features.get(key)
        shape = feature.get("shape") if isinstance(feature, dict) else None
        return as_int(shape[-1]) if isinstance(shape, list) and shape else None

    expected_state_dim = expected_vector_dim(state_key)
    expected_action_dim = expected_vector_dim(action_key)
    frames: list[dict[str, Any]] = []
    task = actual_task
    subtask_segments = episode.get("subtask_segments") if isinstance(episode.get("subtask_segments"), list) else []
    for _, row in df.iterrows():
        frame_index = int(row.get("frame_index", len(frames)))
        state = compact_vector(row.get(state_key, []))
        actions = compact_vector(row.get(action_key, []))
        if expected_state_dim is not None and len(state) != expected_state_dim:
            raise ValueError(
                f"Episode {episode_index} frame {frame_index}: State 维度 "
                f"{len(state)} != {expected_state_dim}"
            )
        if expected_action_dim is not None and len(actions) != expected_action_dim:
            raise ValueError(
                f"Episode {episode_index} frame {frame_index}: Action 维度 "
                f"{len(actions)} != {expected_action_dim}"
            )
        frames.append(
            {
                "frame_index": frame_index,
                "timestamp": float(row.get("timestamp", 0.0)),
                "state": state,
                "actions": actions,
                "task": task,
                "subtask": subtask_for_frame(frame_index, subtask_segments),
            }
        )

    video_files = episode.get("video_files")
    if not isinstance(video_files, dict) or not video_files:
        raise ValueError(f"Episode {episode_index}: 没有可视化视频")
    try:
        import cv2  # type: ignore
    except ImportError as exc:
        raise ImportError("opencv-python is required to validate LeRobot videos") from exc
    video_validation: list[dict[str, Any]] = []
    expected_fps = as_number(summary.get("info", {}).get("fps")) or 0.0
    for video_key, relative_path in video_files.items():
        video_path = (dataset_dir / str(relative_path)).resolve()
        video_path.relative_to(dataset_dir)
        expected_video_name = f"episode_{episode_index:06d}.mp4"
        if video_path.name != expected_video_name or not video_path.is_file():
            raise ValueError(
                f"Episode {episode_index} / {video_key}: 视频文件不对应或不存在：{video_path}"
            )
        capture = cv2.VideoCapture(str(video_path))
        try:
            if not capture.isOpened():
                raise ValueError(
                    f"Episode {episode_index} / {video_key}: 视频无法打开：{video_path}"
                )
            video_frame_count = int(round(capture.get(cv2.CAP_PROP_FRAME_COUNT)))
            video_fps = float(capture.get(cv2.CAP_PROP_FPS))
        finally:
            capture.release()
        if video_frame_count != len(frames):
            raise ValueError(
                f"Episode {episode_index} / {video_key}: 视频 {video_frame_count} 帧，"
                f"parquet {len(frames)} 帧"
            )
        if expected_fps and (not math.isfinite(video_fps) or abs(video_fps - expected_fps) > 0.2):
            raise ValueError(
                f"Episode {episode_index} / {video_key}: 视频 FPS={video_fps:g}，"
                f"数据集 FPS={expected_fps:g}"
            )
        video_validation.append(
            {
                "key": str(video_key),
                "frame_count": video_frame_count,
                "fps": video_fps,
            }
        )
    return {
        "dataset": {
            "dataset_dir": summary["dataset_dir"],
            "hdf5_root": summary["hdf5_root"],
            "repo_id": summary["repo_id"],
            "tasks": summary["tasks"],
            "info": summary["info"],
        },
        "episode": episode,
        "frames": frames,
        "state_dim": len(frames[0]["state"]) if frames else 0,
        "action_dim": len(frames[0]["actions"]) if frames else 0,
        "state_key": state_key or "",
        "action_key": action_key or "",
        "integrity": {
            "verified": True,
            "episode_index": episode_index,
            "task_index": actual_task_index,
            "task": actual_task,
            "frame_count": len(frames),
            "video_views": video_validation,
        },
    }


def env_value_with_prefix(value: str, prefix: str) -> str:
    parts = [part for part in value.split(":") if part and part != prefix]
    return ":".join([prefix, *parts])


def host_mcap_python() -> str:
    configured = str(os.environ.get("PIPELINE_MCAP_PYTHON") or "").strip()
    if configured:
        return configured
    return "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else sys.executable


def lerobot_python() -> str:
    configured = str(os.environ.get("LEROBOT_PYTHON") or "").strip()
    return configured or sys.executable


def command_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    python_bin = str(Path(sys.executable).resolve().parent)
    current_path = env.get("PATH", "")
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "TQDM_MININTERVAL": "1.0",
            "OPENCV_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "PATH": env_value_with_prefix(current_path, python_bin),
        }
    )
    if extra:
        env.update(extra)
    return env


def mcap_conversion_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = command_env(extra)
    env["PYTHONPATH"] = env_value_with_prefix(env.get("PYTHONPATH", ""), "/usr/lib/python3/dist-packages")
    return env


def gpu_device_index(gpu_device: str) -> str:
    text = str(gpu_device).strip()
    if text.startswith("cuda:"):
        return text.split(":", 1)[1].strip() or "0"
    if text == "cuda":
        return "0"
    return text or "0"


def gpu_enabled(cfg: dict[str, Any]) -> bool:
    return bool(str(cfg.get("gpu_device") or "").strip())


def gpu_process_env(cfg: dict[str, Any]) -> dict[str, str]:
    if not gpu_enabled(cfg):
        return {}
    device = gpu_device_index(cfg["gpu_device"])
    return {
        "CUDA_VISIBLE_DEVICES": device,
        "NVIDIA_VISIBLE_DEVICES": device,
        "NVIDIA_DRIVER_CAPABILITIES": "compute,video,utility",
    }


def docker_gpu_args(cfg: dict[str, Any]) -> list[str]:
    if not gpu_enabled(cfg):
        return []
    return ["--gpus", f"device={gpu_device_index(cfg['gpu_device'])}"]


def docker_gpu_env_args(cfg: dict[str, Any]) -> list[str]:
    if not gpu_enabled(cfg):
        return []
    device = gpu_device_index(cfg["gpu_device"])
    return [
        "-e",
        f"GPU_DEVICE={device}",
        "-e",
        f"CUDA_VISIBLE_DEVICES={device}",
        "-e",
        f"NVIDIA_VISIBLE_DEVICES={device}",
        "-e",
        "NVIDIA_DRIVER_CAPABILITIES=compute,video,utility",
    ]


def docker_profile_resource(profile: Path) -> tuple[str, list[str]]:
    profile = Path(profile).expanduser().resolve()
    stock_root = (ROOT / "robot_profiles").resolve()
    try:
        relative_profile = profile.relative_to(stock_root)
        return f"/workspace/robot_profiles/{relative_profile}", []
    except ValueError:
        container_path = f"/workspace/variant/{profile.name}"
        return container_path, ["-v", f"{profile}:{container_path}:ro"]


def convert_command(cfg: dict[str, Any]) -> tuple[list[str], Path, dict[str, str]]:
    if not cfg.get("mcap_path_provided"):
        raise ValueError("MCAP 转 HDF5 需要填写 MCAP 数据集路径。只有 HDF5 时请使用“仅批量质检”、回放或“生成 LeRobot”。")
    ensure_creatable(cfg["hdf5_root"], "HDF5 output path")
    if cfg["use_docker"]:
        data_root = docker_data_root([cfg["mcap_path"], cfg["hdf5_root"]], cfg["data_root"])
        input_rel = relative_to(cfg["mcap_path"], data_root)
        output_rel = relative_to(cfg["hdf5_root"], data_root)
        profile_container, profile_mount = docker_profile_resource(cfg["profile"])
        try:
            aloha_yaml_rel = cfg["aloha_yaml"].resolve().relative_to(
                (ROOT / "mcap_conversion").resolve()
            )
            aloha_yaml_container = f"/workspace/mcap_conversion/{aloha_yaml_rel}"
            aloha_yaml_mount: list[str] = []
        except ValueError:
            aloha_yaml_container = "/workspace/variant/aloha_data_params.yaml"
            aloha_yaml_mount = [
                "-v",
                f"{cfg['aloha_yaml']}:{aloha_yaml_container}:ro",
            ]
        gpu_convert_args = (
            '    --gpu-encode-videos \\\n'
            '    --gpu-device "$GPU_DEVICE" \\\n'
            '    --gpu-video-encoder h264_nvenc \\\n'
            if gpu_enabled(cfg)
            else ""
        )
        inner_script = f"""
set -e
INPUT_ROOT="$DATA_ROOT/$INPUT_REL"
OUTPUT_ROOT="$DATA_ROOT/$OUTPUT_REL"
CONVERT_JOBS="${{CONVERT_JOBS:-2}}"
case "$CONVERT_JOBS" in ""|*[!0-9]*) CONVERT_JOBS=2 ;; esac
if [ "$CONVERT_JOBS" -lt 1 ]; then CONVERT_JOBS=1; fi
mkdir -p "$OUTPUT_ROOT"
if [ -f "$INPUT_ROOT" ]; then
  MCAP_INPUTS=("$INPUT_ROOT")
else
  shopt -s nullglob
  MCAP_INPUTS=("$INPUT_ROOT"/*)
fi
if [ "${{#MCAP_INPUTS[@]}}" -eq 0 ]; then
  echo "No MCAP inputs found: $INPUT_ROOT"
  exit 1
fi
convert_one() {{
  local mcap_path="$1"
  [ -e "$mcap_path" ] || return 0
  if [ -f "$mcap_path" ] && [ "${{mcap_path##*.}}" != "mcap" ]; then
    echo "==== skip non-mcap file $(basename "$mcap_path") ===="
    return 0
  fi
  if [ -d "$mcap_path" ] && ! find "$mcap_path" -type f -name "*.mcap" -print -quit | grep -q .; then
    echo "==== skip directory without mcap $(basename "$mcap_path") ===="
    return 0
  fi
  local name
  name=$(basename "$mcap_path")
  name="${{name%.mcap}}"
  local out_dir="$OUTPUT_ROOT/$name"
  if [ "{int(cfg['overwrite_hdf5'])}" = "0" ] && \
     python3 camera_layouts.py --episode-dir "$out_dir" --camera-layout "$CAMERA_LAYOUT"; then
    if [ -n "$TASK_TEXT" ]; then
      python3 - "$out_dir" "$TASK_TEXT" <<'PY'
import json
import sys
from pathlib import Path

episode_dir = Path(sys.argv[1])
task_text = sys.argv[2].strip()
meta_path = episode_dir / "meta" / "episode_meta.json"
h5_path = episode_dir / "states" / "aligned_joints.h5"
if task_text and meta_path.is_file():
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        meta = {{}}
    if not isinstance(meta, dict):
        meta = {{}}
    meta["task"] = task_text
    meta["tasks"] = [task_text]
    meta["full_instructions_en"] = [task_text]
    tmp_path = meta_path.with_suffix(meta_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
    tmp_path.replace(meta_path)
if task_text and h5_path.is_file():
    import h5py

    with h5py.File(h5_path, "a") as file_obj:
        file_obj.attrs["task"] = task_text
PY
    fi
    echo "==== skip existing $name ===="
    return 0
  fi
  local episode_id="${{name//[!0-9]/}}"
  episode_id="${{episode_id:-0}}"
  local text_args=()
  if [ -n "$TASK_TEXT" ]; then text_args=(--text "$TASK_TEXT"); fi
  local overwrite_args=()
  if [ "{int(cfg['overwrite_hdf5'])}" = "1" ]; then
    overwrite_args=(--overwrite)
  elif [ -e "$out_dir/states/aligned_joints.h5" ] || [ -e "$out_dir/videos" ] || [ -e "$out_dir/meta" ] || [ -e "$out_dir/_work" ]; then
    echo "==== overwrite incomplete existing $name ===="
    overwrite_args=(--overwrite)
  fi
  echo "==== convert $name ===="
    python3 mcap_to_icra_episode.py \\
    --mcapPath "$mcap_path" \\
    --output "$out_dir" \\
    --episodeName "$name" \\
    --episodeId "$episode_id" \\
    --type "$ROBOT_TYPE" \\
    --profile "$PROFILE_CONTAINER" \\
    --alohaYaml "$ALOHA_YAML_CONTAINER" \\
    --cameraLayout "$CAMERA_LAYOUT" \\
{gpu_convert_args}    "${{text_args[@]}}" \\
    "${{overwrite_args[@]}}" \\
    --cleanupIntermediate
}}
fail=0
pids=()
wait_oldest() {{
  local pid="${{pids[0]}}"
  if ! wait "$pid"; then fail=1; fi
  pids=("${{pids[@]:1}}")
}}
for mcap_path in "${{MCAP_INPUTS[@]}}"; do
  [ -e "$mcap_path" ] || continue
  convert_one "$mcap_path" &
  pids+=("$!")
  if [ "${{#pids[@]}}" -ge "$CONVERT_JOBS" ]; then
    wait_oldest
  fi
done
while [ "${{#pids[@]}}" -gt 0 ]; do
  wait_oldest
done
exit "$fail"
"""
        cmd = [
            "docker",
            "run",
            "--rm",
            *docker_gpu_args(cfg),
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-e",
            "HOME=/tmp",
            "-e",
            "DATA_ROOT=/workspace/data",
            "-e",
            f"DATASET_NAME={cfg['dataset_name']}",
            "-e",
            f"ROBOT_TYPE={cfg['robot_type']}",
            "-e",
            f"PROFILE_CONTAINER={profile_container}",
            "-e",
            f"ALOHA_YAML_CONTAINER={aloha_yaml_container}",
            "-e",
            f"CAMERA_LAYOUT={cfg['camera_layout']}",
            "-e",
            f"TASK_TEXT={cfg['task_text']}",
            "-e",
            f"INPUT_REL={input_rel}",
            "-e",
            f"OUTPUT_REL={output_rel}",
            "-e",
            f"CONVERT_JOBS={cfg['convert_jobs']}",
            *docker_gpu_env_args(cfg),
            "-e",
            "OPENCV_NUM_THREADS=1",
            "-e",
            "OMP_NUM_THREADS=1",
            "-e",
            "OPENBLAS_NUM_THREADS=1",
            "-e",
            "MKL_NUM_THREADS=1",
            "-w",
            "/workspace/mcap_conversion/scripts",
            "-v",
            "/etc/passwd:/etc/passwd:ro",
            "-v",
            "/etc/group:/etc/group:ro",
            "-v",
            f"{ROOT / 'mcap_conversion'}:/workspace/mcap_conversion",
            "-v",
            f"{ROOT / 'robot_profiles'}:/workspace/robot_profiles:ro",
            *profile_mount,
            *aloha_yaml_mount,
            "-v",
            f"{data_root}:/workspace/data",
            cfg["docker_image"],
            "bash",
            "-lc",
            inner_script,
        ]
        return cmd, ROOT, command_env(gpu_process_env(cfg))

    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "convert_mcap_dataset.py"),
        "--dataset-name",
        cfg["dataset_name"],
        "--data-root",
        str(cfg["data_root"]),
        "--input-root",
        str(cfg["mcap_path"]),
        "--output-root",
        str(cfg["hdf5_root"]),
        "--type",
        cfg["robot_type"],
        "--profile",
        str(cfg["profile"]),
        "--aloha-yaml",
        str(cfg["aloha_yaml"]),
        "--camera-layout",
        cfg["camera_layout"],
        "--jobs",
        str(cfg["convert_jobs"]),
        "--cleanupIntermediate",
        "--python",
        host_mcap_python(),
    ]
    if gpu_enabled(cfg):
        cmd.extend(
            [
                "--gpu-encode-videos",
                "--gpu-device",
                gpu_device_index(cfg["gpu_device"]),
                "--gpu-video-encoder",
                "h264_nvenc",
            ]
        )
    if cfg["task_text"]:
        cmd.extend(["--text", cfg["task_text"]])
    if cfg["overwrite_hdf5"]:
        cmd.append("--overwrite")
    return cmd, ROOT, mcap_conversion_env(gpu_process_env(cfg))


def qc_command(cfg: dict[str, Any]) -> tuple[list[str], Path, dict[str, str]]:
    report_dir = cfg["qc_root"] / f"{cfg['dataset_name']}_{time.strftime('%Y%m%d_%H%M%S')}"
    ensure_creatable(report_dir, "QC report path")
    profile_path = effective_profile_path(cfg)
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "run_quality_pipeline.py"),
        "--profile",
        str(profile_path),
        "--input",
        str(cfg["hdf5_root"]),
        "--output",
        str(report_dir),
        "--compact",
        "--num-workers",
        str(1 if cfg.get("robot_type") == "zerith" else cfg["convert_jobs"]),
    ]
    if cfg.get("stationary_threshold") is not None:
        cmd.extend(["--stationary-threshold", str(stationary_threshold_for_cfg(cfg))])
    cmd.extend(["--manual-failure-json", str(manual_failure_hdf5_path(cfg))])
    return cmd, ROOT, command_env()


def convert_then_qc_step(cfg: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
    def step(job: dict[str, Any]) -> None:
        append_job(job, "==== convert 1/2: MCAP -> HDF5 ====")
        convert_cmd, convert_cwd, convert_env = convert_command(cfg)
        convert_returncode = run_process_for_job(job, convert_cmd, convert_cwd, convert_env)
        if convert_returncode != 0:
            append_job(
                job,
                (
                    f"Convert exited with code {convert_returncode}. "
                    "Will still run QC if any HDF5 episode was generated."
                ),
            )

        episodes = discover_hdf5_episodes(cfg["hdf5_root"])
        if not episodes:
            raise RuntimeError(
                f"No HDF5 episodes found for QC under {cfg['hdf5_root']}. "
                f"Convert exit code: {convert_returncode}"
            )
        append_job(job, f"HDF5 episodes found for QC: {len(episodes)}")
        sync_manual_failures_step(cfg)(job)

        append_job(job, "==== qc 2/2: HDF5 quality check ====")
        qc_cmd, qc_cwd, qc_env = qc_command(cfg)
        qc_returncode = run_process_for_job(job, qc_cmd, qc_cwd, qc_env)
        if qc_returncode != 0:
            raise RuntimeError(f"QC failed with exit code {qc_returncode}")
        if convert_returncode != 0:
            raise RuntimeError(
                f"Convert failed with exit code {convert_returncode}; "
                "QC report has been generated for existing HDF5 episodes."
            )

    step.__name__ = "MCAP 转 HDF5 后自动质检"
    return step


def selected_episode_names(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("episode_names") or payload.get("selected_episodes") or []
    if isinstance(raw, str):
        values = [part.strip() for part in raw.replace(",", " ").split()]
    elif isinstance(raw, list):
        values = [str(item).strip() for item in raw]
    else:
        values = []
    names = [name for name in values if name]
    if not names:
        raise ValueError("请先在质检报告表格中选择 episode")
    return names


def repair_commands(
    cfg: dict[str, Any],
    qc_report_dir: str | None,
    episode_names: list[str] | None = None,
) -> list[JobStep]:
    _ = qc_report_dir
    repair_names = episode_names or []
    if not repair_names:
        raise ValueError("No selected episodes found")
    profile_path = effective_profile_path(cfg)
    stationary_threshold = stationary_threshold_for_cfg(cfg)
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "trim_stationary_hdf5_episodes.py"),
        "--input",
        str(cfg["hdf5_root"]),
        "--profile",
        str(profile_path),
        "--episodeNames",
        ",".join(repair_names),
        "--keep-stationary-frames",
        str(stationary_threshold),
        "--target-fps",
        "30",
        "--num-workers",
        str(cfg["convert_jobs"]),
        "--video-workers",
        str(min(3, max(1, int(cfg["convert_jobs"])))),
    ]
    if cfg.get("stationary_threshold") is not None:
        cmd.extend(["--stationary-threshold", str(stationary_threshold)])
    return [(cmd, ROOT, command_env()), sync_manual_failures_step(cfg), qc_command(cfg)]


def optimize_hdf5_commands(cfg: dict[str, Any]) -> list[JobStep]:
    if str(cfg.get("robot_type") or "").lower() != "zerith":
        raise ValueError("HDF5 UUID 重编号仅用于零次方 episode.hdf5 数据")
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "optimize_hdf5_episode_names.py"),
        "--input",
        str(cfg["hdf5_root"]),
        "--start-index",
        "1",
    ]
    return [(cmd, ROOT, command_env()), sync_manual_failures_step(cfg), qc_command(cfg)]


def delete_selected_hdf5_step(cfg: dict[str, Any], episode_names: list[str]) -> Callable[[dict[str, Any]], None]:
    hdf5_root = cfg["hdf5_root"].resolve()

    def step(job: dict[str, Any]) -> None:
        append_job(job, f"Delete selected HDF5 episodes under: {hdf5_root}")
        hdf5_root.mkdir(parents=True, exist_ok=True)
        deleted = 0
        for name in episode_names:
            target = (hdf5_root / name).resolve()
            try:
                target.relative_to(hdf5_root)
            except ValueError as exc:
                raise ValueError(f"Episode path escapes HDF5 root: {name}") from exc
            if not target.exists():
                append_job(job, f"skip missing: {name}")
                continue
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            deleted += 1
            append_job(job, f"deleted HDF5 episode: {name}")
        append_job(job, f"Deleted {deleted}/{len(episode_names)} selected HDF5 episodes.")

    step.__name__ = "删除选中 HDF5 episode"
    return step


def quality_grade_episode_names(cfg: dict[str, Any], grade: str) -> list[str]:
    target_grade = normalise_quality_grade(grade)
    if not target_grade:
        return []
    existing_names = {
        str(row.get("episode_id") or "")
        for row in discover_hdf5_episodes(cfg["hdf5_root"])
        if str(row.get("episode_id") or "")
    }
    if not existing_names:
        return []
    status = dataset_status(stringify_config(cfg))
    names = {
        str(row.get("episode_id") or "")
        for row in status.get("episodes", [])
        if str(row.get("episode_id") or "") in existing_names
        and normalise_quality_grade(row.get("quality_grade")) == target_grade
    }
    return sorted(names, key=natural_key)


def delete_quality_grade_hdf5_step(cfg: dict[str, Any], grade: str) -> Callable[[dict[str, Any]], None]:
    def step(job: dict[str, Any]) -> None:
        target_grade = normalise_quality_grade(grade)
        names = quality_grade_episode_names(cfg, target_grade)
        if not names:
            append_job(job, f"No quality grade {target_grade or grade} HDF5 episodes found to delete.")
            return
        append_job(job, f"Delete quality grade {target_grade} HDF5 episodes: {', '.join(names)}")
        delete_selected_hdf5_step(cfg, names)(job)

    step.__name__ = f"删除等级 {normalise_quality_grade(grade) or grade} HDF5 episode"
    return step


def episode_filter_text(source_indices: list[int]) -> str:
    return ",".join(str(index) for index in sorted(set(source_indices)))


def lerobot_command(
    cfg: dict[str, Any],
    grade: str | None = None,
    source_indices: list[int] | None = None,
) -> tuple[list[str], Path, dict[str, str]]:
    output_root = lerobot_grade_output_root(cfg, grade) if grade else cfg["lerobot_root"]
    repo_id = lerobot_grade_repo_id(cfg, grade) if grade else cfg["repo_id"]
    ensure_creatable(output_root, "LeRobot output root")
    profile_path = effective_profile_path(cfg)
    cmd = [
        lerobot_python(),
        str(ROOT / "lerobot_conversion" / "scripts" / "convert_hdf5_to_lerobot_v2.py"),
        "--profile",
        str(profile_path),
        "--repo-id",
        repo_id,
        "--output-root",
        str(output_root),
        "--data-dir",
        str(cfg["hdf5_root"]),
        "--num-workers",
        str(cfg["convert_jobs"]),
        "--queue-size",
        str(max(4, int(cfg["convert_jobs"]) * 4)),
        "--image-writer-processes",
        "8",
        "--image-writer-threads",
        "16",
        "--cpu-video-encoder",
        "libx264",
        "--cpu-video-crf",
        "20",
        "--cpu-video-preset",
        "fast",
        "--overwrite",
    ]
    if source_indices is not None:
        cmd.extend(["--episode-filter", episode_filter_text(source_indices)])
    if grade:
        grade_text = normalise_quality_grade(grade)
        if not grade_text:
            raise ValueError(f"invalid LeRobot quality grade: {grade!r}")
        cmd.extend(["--quality-grade", grade_text])
    if cfg["robot_type"] == "g2":
        cmd.extend(["--key-style", "pi05"])
    elif cfg["robot_type"] == "aloha":
        cmd.extend(
            [
                "--aloha-action-mode",
                "joints_base" if cfg["aloha_include_base_action"] else "joints",
            ]
        )
    if cfg["lerobot_cuda"] and gpu_enabled(cfg):
        cmd.extend(
            [
                "--preprocess-device",
                "cuda",
                "--gpu-device",
                cfg["gpu_device"],
                "--gpu-resize-batch-size",
                "64",
            ]
        )
    env = command_env({"HF_LEROBOT_HOME": str(output_root), **gpu_process_env(cfg)})
    return cmd, ROOT, env


def sync_hdf5_quality_grades_step(
    entries: list[dict[str, Any]],
) -> Callable[[dict[str, Any]], None]:
    def step(job: dict[str, Any]) -> None:
        changed = sync_hdf5_quality_grade_entries(entries)
        append_job(
            job,
            f"Synchronized Web quality grades to HDF5 sidecars: "
            f"episodes={len(entries)}, changed={changed}.",
        )

    step.__name__ = "同步网页质量等级到 HDF5 元数据"
    return step


def validate_hdf5_quality_grades_step(
    entries: list[dict[str, Any]],
) -> Callable[[dict[str, Any]], None]:
    def step(job: dict[str, Any]) -> None:
        result = validate_hdf5_quality_grade_entries(entries)
        counts = result["quality_grade_counts"]
        append_job(
            job,
            "Validated all HDF5 quality grades: "
            f"episodes={result['episode_count']}, "
            + ", ".join(f"{grade}={counts[grade]}" for grade in QUALITY_GRADES)
            + ".",
        )

    step.__name__ = "验证全部 HDF5 质量等级"
    return step


def hdf5_quality_grade_sync_steps(cfg: dict[str, Any]) -> list[JobStep]:
    entries = authoritative_quality_grade_entries(cfg)
    return [
        sync_hdf5_quality_grades_step(entries),
        validate_hdf5_quality_grades_step(entries),
    ]


def write_renumber_plan_step(
    cfg: dict[str, Any],
    entries: list[dict[str, Any]] | None = None,
) -> Callable[[dict[str, Any]], None]:
    def step(job: dict[str, Any]) -> None:
        path = write_renumber_plan(cfg, entries)
        plan = load_renumber_plan(cfg["hdf5_root"])
        groups = renumber_grade_groups(plan)
        append_job(job, f"Wrote quality-grade renumber plan: {path}")
        for grade, info in groups.items():
            append_job(
                job,
                f"  grade {grade}: {info.get('count', 0)} episodes -> {info.get('lerobot_dataset_dir', '')}",
            )

    step.__name__ = "生成按质量等级编号计划"
    return step


def prepare_hdf5_tasks_step(cfg: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
    def step(job: dict[str, Any]) -> None:
        robot_type = str(cfg.get("robot_type") or "").lower()
        if robot_type not in {"aloha", "zerith"}:
            append_job(job, "This robot profile keeps its existing task metadata behavior.")
            return
        entries = lerobot_source_episode_entries(cfg["hdf5_root"])
        missing: list[dict[str, Any]] = []
        retained = 0
        for entry in entries:
            h5_path = Path(entry["hdf5_file"])
            if read_hdf5_task(h5_path):
                retained += 1
            else:
                missing.append(entry)
        if not missing:
            append_job(job, f"HDF5 task check passed: retained {retained}, missing 0.")
            return

        manual_task = str(cfg.get("task_text") or "").strip()
        missing_names = [str(entry.get("episode_id") or Path(entry["hdf5_file"]).parents[1].name) for entry in missing]
        if not manual_task:
            raise ValueError(
                "以下 HDF5 episode 缺少任务文本，请在页面“任务文本”中手动输入规范提示词后重试："
                + ", ".join(missing_names)
            )
        allowed_tasks = {canonical_task(product) for product in CANONICAL_BEVERAGE_NAMES}
        if robot_type == "aloha" and manual_task not in allowed_tasks:
            raise ValueError(
                "手动任务文本必须严格使用批准的 19 个饮料名和句式："
                "Grasp <规范饮料名> with the left hand."
            )
        for entry in missing:
            h5_path = Path(entry["hdf5_file"])
            write_hdf5_task(h5_path, manual_task)
            episode_dir = hdf5_episode_dir_from_h5(h5_path)
            write_episode_sidecar_task(
                episode_meta_path_for_dir(episode_dir),
                manual_task,
            )
        append_job(
            job,
            f"HDF5 task check: filled {len(missing)} missing episode(s), retained {retained} existing task(s).",
        )

    step.__name__ = "检查并补写 HDF5 任务文本"
    return step


def _lerobot_source_h5(cfg: dict[str, Any], record: dict[str, Any]) -> Path:
    text = str(record.get("source_h5") or record.get("hdf5_file") or "").strip()
    if not text:
        raise ValueError("LeRobot mapping record has no source_h5/hdf5_file")
    path = Path(text).expanduser()
    return path if path.is_absolute() else cfg["hdf5_root"] / path


def validate_lerobot_grade_tasks(cfg: dict[str, Any]) -> dict[str, Any]:
    import pyarrow.parquet as pq  # type: ignore

    grade_count = 0
    episode_count = 0
    all_expected_tasks: set[str] = set()
    declared_by_grade: dict[str, set[str]] = {}
    expected_by_grade: dict[str, set[str]] = {}
    errors: list[str] = []
    for grade in QUALITY_GRADES:
        dataset_dir = lerobot_grade_dataset_dir(cfg, grade)
        tasks_path = dataset_dir / "meta" / "tasks.jsonl"
        mapping_path = dataset_dir / "meta" / "episode_name_mapping.json"
        if not tasks_path.is_file() and not mapping_path.is_file():
            continue
        grade_count += 1
        task_rows = load_jsonl(tasks_path)
        task_by_index: dict[int, str] = {}
        for row in task_rows:
            try:
                task_by_index[int(row.get("task_index"))] = str(row.get("task") or "").strip()
            except (TypeError, ValueError):
                errors.append(f"grade {grade}: invalid tasks.jsonl row {row!r}")
        declared_by_grade[grade] = set(task_by_index.values())
        expected_by_grade[grade] = set()

        mapping = load_json_file(mapping_path)
        records = mapping.get("episodes")
        if not isinstance(records, list):
            errors.append(f"grade {grade}: missing episode mapping {mapping_path}")
            continue
        record_by_index: dict[int, dict[str, Any]] = {}
        for record in records:
            if not isinstance(record, dict):
                continue
            try:
                record_by_index[int(record.get("lerobot_episode_index"))] = record
            except (TypeError, ValueError):
                errors.append(f"grade {grade}: invalid mapping record {record!r}")

        episode_task_indices: dict[int, set[int]] = {}
        for parquet_path in sorted((dataset_dir / "data").rglob("*.parquet")):
            table = pq.read_table(parquet_path, columns=["episode_index", "task_index"])
            for episode_index, task_index in zip(
                table["episode_index"].to_pylist(),
                table["task_index"].to_pylist(),
            ):
                episode_task_indices.setdefault(int(episode_index), set()).add(int(task_index))

        for episode_index, task_indices in sorted(episode_task_indices.items()):
            episode_name = f"episode_{episode_index:06d}"
            episode_count += 1
            if len(task_indices) != 1:
                errors.append(
                    f"grade {grade} {episode_name}: multiple task_index values {sorted(task_indices)}"
                )
                continue
            task_index = next(iter(task_indices))
            actual_task = task_by_index.get(task_index, "")
            record = record_by_index.get(episode_index)
            if record is None:
                errors.append(f"grade {grade} {episode_name}: mapping record is missing")
                continue
            try:
                source_h5 = _lerobot_source_h5(cfg, record)
                expected_task = require_hdf5_task(source_h5)
            except (OSError, ValueError) as exc:
                errors.append(f"grade {grade} {episode_name}: {exc}")
                continue
            all_expected_tasks.add(expected_task)
            expected_by_grade[grade].add(expected_task)
            if actual_task != expected_task:
                errors.append(
                    f"grade {grade} {episode_name}: HDF5 task={expected_task!r}, "
                    f"LeRobot task={actual_task!r}"
                )
            mapping_task = str(record.get("task") or "").strip()
            if mapping_task != expected_task:
                errors.append(
                    f"grade {grade} {episode_name}: HDF5 task={expected_task!r}, "
                    f"mapping task={mapping_task!r}"
                )

        extra_mapping_indices = set(record_by_index) - set(episode_task_indices)
        if extra_mapping_indices:
            errors.append(
                f"grade {grade}: mapping contains episodes with no Parquet data "
                f"{sorted(extra_mapping_indices)}"
            )

    for grade, declared in declared_by_grade.items():
        expected = expected_by_grade.get(grade, set())
        if declared != expected:
            errors.append(
                f"grade {grade}: declared tasks {sorted(declared)!r} do not match "
                f"tasks used by source HDF5 episodes {sorted(expected)!r}"
            )
    if errors:
        raise ValueError("LeRobot 分级任务一致性校验失败：\n" + "\n".join(errors))
    return {
        "grade_count": grade_count,
        "episode_count": episode_count,
        "tasks": sorted(all_expected_tasks),
    }


def validate_lerobot_grade_tasks_step(cfg: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
    def step(job: dict[str, Any]) -> None:
        result = validate_lerobot_grade_tasks(cfg)
        append_job(
            job,
            "LeRobot grade task validation passed: "
            f"grades={result['grade_count']}, episodes={result['episode_count']}, "
            f"tasks={result['tasks']}",
        )

    step.__name__ = "验证分级 LeRobot 任务一致性"
    return step


def lerobot_commands(cfg: dict[str, Any]) -> list[JobStep]:
    if cfg.get("robot_type") == "zerith":
        from integrations.manual_export import convert_step
        return [convert_step(sys.modules[__name__], cfg)]
    authoritative_entries = authoritative_quality_grade_entries(cfg)
    groups = quality_grade_episode_groups(cfg, authoritative_entries)
    commands: list[JobStep] = [
        prepare_hdf5_tasks_step(cfg),
        sync_hdf5_quality_grades_step(authoritative_entries),
        write_renumber_plan_step(cfg, authoritative_entries),
    ]
    grade_command_count = 0
    for grade in QUALITY_GRADES:
        entries = groups.get(grade, [])
        if not entries:
            continue
        source_indices = [int(entry["source_episode_index"]) for entry in entries]
        commands.append(lerobot_command(cfg, grade, source_indices))
        grade_command_count += 1
    if grade_command_count == 0:
        def no_episodes_step(job: dict[str, Any]) -> None:
            append_job(job, "No HDF5 episodes found for LeRobot conversion.")

        no_episodes_step.__name__ = "无可转换 HDF5 episode"
        commands.append(no_episodes_step)
    else:
        commands.append(
            sync_lerobot_quality_grades_step(cfg, authoritative_entries)
        )
    commands.append(validate_lerobot_grade_tasks_step(cfg))
    commands.append(
        validate_quality_grade_consistency_step(cfg, authoritative_entries)
    )
    return commands


def validate_lerobot_stage_split_grade_step(
    cfg: dict[str, Any], grade: str, source_dataset: Path
) -> Callable[[dict[str, Any]], None]:
    def step(job: dict[str, Any]) -> None:
        if not is_lerobot_dataset_dir(source_dataset):
            raise ValueError(
                f"等级 {grade} 的 LeRobot 源目录已不存在或无效: {source_dataset}"
            )
        result = validate_lerobot_stage_split_grade(cfg, grade, source_dataset)
        if not result.get("compatible"):
            details = "；".join(str(item) for item in result.get("errors") or [])
            raise ValueError(
                f"等级 {grade} 阶段预检未通过: "
                + (details or str(result.get("reason") or "未知原因"))
            )
        append_job(
            job,
            f"等级 {grade} 两阶段预检通过: "
            f"episodes={result.get('checked_episode_count', 0)}, source={source_dataset}",
        )

    step.__name__ = f"复核等级 {grade} 两阶段边界"
    return step


def split_lerobot_stage_commands(cfg: dict[str, Any]) -> list[JobStep]:
    if cfg.get("robot_type") == "zerith":
        from integrations.manual_export import split_step
        return [split_step(sys.modules[__name__], cfg)]
    plan = lerobot_stage_split_status(cfg)
    if not plan.get("supported"):
        raise ValueError(str(plan.get("reason") or "仅零次方机器人支持阶段切分"))
    if not plan.get("available"):
        raise ValueError(str(plan.get("reason") or "当前 LeRobot 数据不可切分"))
    if not LEROBOT_STAGE_SPLIT_SCRIPT.is_file():
        raise ValueError(f"阶段切分脚本不存在: {LEROBOT_STAGE_SPLIT_SCRIPT}")

    commands: list[JobStep] = []
    for row in plan.get("grades") or []:
        if not isinstance(row, dict):
            continue
        grade = normalise_quality_grade(row.get("grade"))
        if not grade:
            continue
        source_dataset = Path(str(row["source_dataset"])).expanduser().resolve()
        left_output = Path(str(row["left_output"])).expanduser().resolve()
        right_output = Path(str(row["right_output"])).expanduser().resolve()
        expected_source = _lerobot_stage_split_child(
            lerobot_stage_split_base_dir(cfg), grade
        )
        if source_dataset != expected_source:
            raise ValueError(
                f"等级 {grade} 源目录不是 <repo_id>/{grade}: {source_dataset}"
            )
        expected_left = lerobot_stage_split_output_dir(cfg, "left_hand", grade)
        expected_right = lerobot_stage_split_output_dir(cfg, "righthand", grade)
        if left_output != expected_left or right_output != expected_right:
            raise ValueError(f"等级 {grade} 阶段输出规划不一致")
        ensure_creatable(left_output.parent, f"LeRobot left_hand/{grade} output")
        ensure_creatable(right_output.parent, f"LeRobot righthand/{grade} output")
        commands.append(
            validate_lerobot_stage_split_grade_step(cfg, grade, source_dataset)
        )
        commands.append(
            (
                [
                    lerobot_python(),
                    str(LEROBOT_STAGE_SPLIT_SCRIPT),
                    "--hdf5-root",
                    str(Path(cfg["hdf5_root"]).expanduser().resolve()),
                    "--source-dataset",
                    str(source_dataset),
                    "--left-output",
                    str(left_output),
                    "--right-output",
                    str(right_output),
                    "--overwrite",
                ],
                ROOT,
                command_env(),
            )
        )
    if not commands:
        raise ValueError("未发现可切分的 A/B/C/F LeRobot 等级目录")
    return commands


def load_jsonl_strict(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"{path}: JSONL file is missing")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{line_number}: expected a JSON object")
        rows.append(row)
    return rows


def atomic_write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _quality_grade_entry_indexes(
    entries: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    by_hdf5_path: dict[str, dict[str, Any]] = {}
    by_episode_name: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        grade = normalise_quality_grade(entry.get("quality_grade"))
        episode_id = str(entry.get("episode_id") or "").strip()
        hdf5_file = Path(entry["hdf5_file"]).expanduser().resolve()
        if not grade:
            raise ValueError(
                f"HDF5 episode {episode_id}: missing authoritative quality grade"
            )
        path_key = str(hdf5_file)
        if path_key in by_hdf5_path:
            raise ValueError(f"duplicate authoritative HDF5 path: {hdf5_file}")
        by_hdf5_path[path_key] = entry
        by_episode_name.setdefault(episode_id, []).append(entry)
    return by_hdf5_path, by_episode_name


def _rows_by_episode_index(
    rows: list[dict[str, Any]],
    *,
    index_key: str,
    label: str,
) -> dict[int, dict[str, Any]]:
    indexed: dict[int, dict[str, Any]] = {}
    for row in rows:
        try:
            episode_index = int(row.get(index_key))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}: invalid episode index {row.get(index_key)!r}") from exc
        if episode_index in indexed:
            raise ValueError(f"{label}: duplicate episode index {episode_index}")
        indexed[episode_index] = row
    return indexed


def _mapping_authoritative_entry(
    cfg: dict[str, Any],
    record: dict[str, Any],
    *,
    grade: str,
    episode_index: int,
    by_hdf5_path: dict[str, dict[str, Any]],
    by_episode_name: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    source_text = str(
        record.get("source_h5")
        or record.get("hdf5_file")
        or record.get("raw_path")
        or ""
    ).strip()
    entry: dict[str, Any] | None = None
    if source_text:
        source_path = Path(source_text).expanduser()
        if not source_path.is_absolute():
            source_path = cfg["hdf5_root"] / source_path
        entry = by_hdf5_path.get(str(source_path.resolve()))
    if entry is None:
        source_name = str(
            record.get("hdf5_episode_name")
            or record.get("source_episode_name")
            or record.get("source_file_name")
            or ""
        ).strip()
        name_matches = by_episode_name.get(source_name, [])
        if len(name_matches) == 1:
            entry = name_matches[0]
    if entry is None:
        raise ValueError(
            f"grade {grade} episode {episode_index}: unresolvable source "
            f"{source_text or record.get('hdf5_episode_name')!r}"
        )
    expected_grade = normalise_quality_grade(entry.get("quality_grade"))
    if expected_grade != grade:
        raise ValueError(
            f"grade {grade} episode {episode_index}: expected {expected_grade} "
            f"from Web QC for {entry.get('episode_id')}"
        )
    return entry


def _lerobot_grade_metadata(
    cfg: dict[str, Any], grade: str
) -> tuple[Path, Path, dict[str, Any], list[dict[str, Any]]] | None:
    dataset_dir = lerobot_grade_dataset_dir(cfg, grade)
    mapping_path = dataset_dir / "meta" / "episode_name_mapping.json"
    episodes_path = dataset_dir / "meta" / "episodes.jsonl"
    if not mapping_path.exists() and not episodes_path.exists():
        return None
    mapping = load_json_object_strict(mapping_path)
    mapping_rows = mapping.get("episodes")
    if not isinstance(mapping_rows, list) or not all(
        isinstance(row, dict) for row in mapping_rows
    ):
        raise ValueError(f"{mapping_path}: episodes must be a list of objects")
    episode_rows = load_jsonl_strict(episodes_path)
    return mapping_path, episodes_path, mapping, episode_rows


def sync_lerobot_quality_grade_metadata(
    cfg: dict[str, Any], entries: list[dict[str, Any]]
) -> dict[str, int]:
    by_hdf5_path, by_episode_name = _quality_grade_entry_indexes(entries)
    dataset_count = 0
    episode_count = 0
    changed_files = 0
    for grade in QUALITY_GRADES:
        loaded = _lerobot_grade_metadata(cfg, grade)
        if loaded is None:
            continue
        mapping_path, episodes_path, mapping, episode_rows = loaded
        dataset_dir = lerobot_grade_dataset_dir(cfg, grade)
        mapping_rows = mapping["episodes"]
        mapping_by_index = _rows_by_episode_index(
            mapping_rows,
            index_key="lerobot_episode_index",
            label=f"grade {grade} mapping",
        )
        episodes_by_index = _rows_by_episode_index(
            episode_rows,
            index_key="episode_index",
            label=f"grade {grade} episodes.jsonl",
        )
        if set(mapping_by_index) != set(episodes_by_index):
            raise ValueError(
                f"grade {grade}: episode index mismatch: "
                f"mapping={sorted(mapping_by_index)}, "
                f"episodes.jsonl={sorted(episodes_by_index)}"
            )

        original_mapping = json.dumps(mapping, ensure_ascii=False, sort_keys=True)
        original_episodes = json.dumps(episode_rows, ensure_ascii=False, sort_keys=True)
        mapping["quality_grade"] = grade
        mapping["lerobot_dataset_dir"] = str(dataset_dir)
        mapping["lerobot_output_root"] = str(lerobot_grade_output_root(cfg, grade))
        for episode_index, mapping_row in sorted(mapping_by_index.items()):
            entry = _mapping_authoritative_entry(
                cfg,
                mapping_row,
                grade=grade,
                episode_index=episode_index,
                by_hdf5_path=by_hdf5_path,
                by_episode_name=by_episode_name,
            )
            mapping_row["quality_grade"] = grade
            mapping_row["grade_episode_index"] = episode_index
            mapping_row["lerobot_dataset_dir"] = str(dataset_dir)
            mapping_row.setdefault("hdf5_episode_name", entry.get("episode_id"))
            episodes_by_index[episode_index]["quality_grade"] = grade
            episode_count += 1

        if json.dumps(mapping, ensure_ascii=False, sort_keys=True) != original_mapping:
            atomic_write_json(mapping_path, mapping)
            changed_files += 1
        if json.dumps(episode_rows, ensure_ascii=False, sort_keys=True) != original_episodes:
            atomic_write_jsonl(episodes_path, episode_rows)
            changed_files += 1
        dataset_count += 1
    return {
        "datasets": dataset_count,
        "episodes": episode_count,
        "changed_files": changed_files,
    }


def validate_quality_grade_consistency(
    cfg: dict[str, Any], entries: list[dict[str, Any]]
) -> dict[str, int]:
    errors: list[str] = []
    by_hdf5_path, by_episode_name = _quality_grade_entry_indexes(entries)
    for entry in entries:
        episode_id = str(entry.get("episode_id") or "")
        expected_grade = normalise_quality_grade(entry.get("quality_grade"))
        meta_path = episode_meta_path_for_dir(Path(entry["episode_dir"]))
        try:
            sidecar = load_json_object_strict(meta_path)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        for key in ("quality_grade", "manual_quality_grade"):
            actual = sidecar.get(key)
            if actual != expected_grade:
                errors.append(
                    f"HDF5 episode {episode_id} {meta_path}: {key} "
                    f"expected {expected_grade} actual {actual!r}"
                )
        expected_failure = expected_grade == "F"
        actual_failure = sidecar.get("manual_failure")
        if actual_failure is not expected_failure:
            errors.append(
                f"HDF5 episode {episode_id} {meta_path}: manual_failure "
                f"expected {expected_failure} actual {actual_failure!r}"
            )

    dataset_count = 0
    lerobot_episode_count = 0
    for grade in QUALITY_GRADES:
        try:
            loaded = _lerobot_grade_metadata(cfg, grade)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if loaded is None:
            continue
        _, _, mapping, episode_rows = loaded
        try:
            mapping_by_index = _rows_by_episode_index(
                mapping["episodes"],
                index_key="lerobot_episode_index",
                label=f"grade {grade} mapping",
            )
            episodes_by_index = _rows_by_episode_index(
                episode_rows,
                index_key="episode_index",
                label=f"grade {grade} episodes.jsonl",
            )
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if set(mapping_by_index) != set(episodes_by_index):
            errors.append(
                f"grade {grade}: episode index mismatch: "
                f"mapping={sorted(mapping_by_index)}, "
                f"episodes.jsonl={sorted(episodes_by_index)}"
            )
            continue
        mapping_grade = mapping.get("quality_grade")
        if mapping_grade != grade:
            errors.append(
                f"grade {grade} dataset mapping: expected {grade} "
                f"actual {mapping_grade!r}"
            )
        for episode_index, mapping_row in sorted(mapping_by_index.items()):
            try:
                entry = _mapping_authoritative_entry(
                    cfg,
                    mapping_row,
                    grade=grade,
                    episode_index=episode_index,
                    by_hdf5_path=by_hdf5_path,
                    by_episode_name=by_episode_name,
                )
            except ValueError as exc:
                errors.append(str(exc))
                continue
            source_name = str(entry.get("episode_id") or "")
            for label, actual in (
                ("mapping", mapping_row.get("quality_grade")),
                ("episodes.jsonl", episodes_by_index[episode_index].get("quality_grade")),
            ):
                if actual != grade:
                    errors.append(
                        f"grade {grade} episode {episode_index} source {source_name}: "
                        f"{label} expected {grade} actual {actual}"
                    )
            lerobot_episode_count += 1
        dataset_count += 1

    if errors:
        raise ValueError(
            "HDF5/LeRobot quality grade consistency validation failed:\n"
            + "\n".join(errors)
        )
    return {
        "hdf5_episodes": len(entries),
        "lerobot_datasets": dataset_count,
        "lerobot_episodes": lerobot_episode_count,
    }


def sync_lerobot_quality_grades_step(
    cfg: dict[str, Any], entries: list[dict[str, Any]]
) -> Callable[[dict[str, Any]], None]:
    def step(job: dict[str, Any]) -> None:
        result = sync_lerobot_quality_grade_metadata(cfg, entries)
        append_job(
            job,
            "Synchronized quality grades to LeRobot metadata: "
            f"datasets={result['datasets']}, episodes={result['episodes']}, "
            f"changed_files={result['changed_files']}.",
        )

    step.__name__ = "同步质量等级到 LeRobot 元数据"
    return step


def validate_quality_grade_consistency_step(
    cfg: dict[str, Any], entries: list[dict[str, Any]]
) -> Callable[[dict[str, Any]], None]:
    def step(job: dict[str, Any]) -> None:
        result = validate_quality_grade_consistency(cfg, entries)
        append_job(
            job,
            "HDF5/LeRobot quality grade validation passed: "
            f"hdf5_episodes={result['hdf5_episodes']}, "
            f"lerobot_datasets={result['lerobot_datasets']}, "
            f"lerobot_episodes={result['lerobot_episodes']}.",
        )

    step.__name__ = "验证 HDF5 与 LeRobot 质量等级一致性"
    return step


def append_job(job: dict[str, Any], line: str) -> None:
    clean_line = line.rstrip("\n")
    with JOBS_LOCK:
        log = job["log"]
        next_index = int(job.get("log_next", int(job.get("log_offset", 0)) + len(log)))
        log.append(clean_line)
        job["log_next"] = next_index + 1
        if len(log) > MAX_JOB_LOG_LINES:
            drop_count = len(log) - MAX_JOB_LOG_LINES
            del log[:drop_count]
            job["log_offset"] = int(job.get("log_offset", 0)) + drop_count
        job["updated_at"] = time.time()


def public_job_payload(job: dict[str, Any], *, include_log: bool = False) -> dict[str, Any]:
    hidden = {"_process"}
    if not include_log:
        hidden.add("log")
    return {key: value for key, value in job.items() if key not in hidden}


def stream_process_output(job: dict[str, Any], process: subprocess.Popen[str]) -> None:
    assert process.stdout is not None
    buffer: list[str] = []
    last_progress_emit = 0.0
    while True:
        char = process.stdout.read(1)
        if char == "":
            break
        if char == "\n":
            if buffer:
                append_job(job, "".join(buffer))
                buffer = []
            continue
        if char == "\r":
            now = time.time()
            if buffer and now - last_progress_emit >= 1.0:
                append_job(job, "".join(buffer))
                last_progress_emit = now
            buffer = []
            continue
        buffer.append(char)
    if buffer:
        append_job(job, "".join(buffer))


def run_process_for_job(job: dict[str, Any], cmd: list[str], cwd: Path, env: dict[str, str]) -> int:
    append_job(job, "+ " + " ".join(str(part) for part in cmd))
    process = subprocess.Popen(
        [str(part) for part in cmd],
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    with JOBS_LOCK:
        job["pid"] = process.pid
        job["_process"] = process
    try:
        stream_process_output(job, process)
        return process.wait()
    finally:
        with JOBS_LOCK:
            if job.get("_process") is process:
                job.pop("_process", None)


def create_job(
    label: str,
    commands: list[JobStep],
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    job_metadata = dict(metadata or {})
    job = {
        "id": now_id(),
        "label": label,
        "status": "queued",
        "returncode": None,
        "stop_requested": False,
        "log": [],
        "log_offset": 0,
        "log_next": 0,
        "created_at": time.time(),
        "updated_at": time.time(),
        **job_metadata,
    }
    with JOBS_LOCK:
        requested_resources = {
            str(value)
            for value in job_metadata.get("resource_keys", [])
            if str(value).strip()
        }
        if requested_resources:
            for existing in JOBS.values():
                if str(existing.get("status") or "") not in {"queued", "running"}:
                    continue
                existing_resources = {
                    str(value)
                    for value in existing.get("resource_keys", [])
                    if str(value).strip()
                }
                overlap = sorted(requested_resources & existing_resources)
                if not overlap:
                    continue
                raise JobConflictError(
                    "同一数据集已有任务运行中，请等待或先停止该任务："
                    f"{existing.get('label')} ({existing.get('id')})"
                )
        JOBS[job["id"]] = job

    thread = threading.Thread(target=run_job, args=(job, commands), daemon=True)
    thread.start()
    return job


def create_pipeline_job(
    label: str,
    commands: list[JobStep],
    cfg: dict[str, Any],
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    job_metadata = dict(metadata or {})
    hdf5_root = Path(cfg["hdf5_root"]).expanduser().resolve()
    raw_resource_keys = job_metadata.get("resource_keys", [])
    if isinstance(raw_resource_keys, (str, Path)):
        raw_resource_keys = [raw_resource_keys]
    elif not isinstance(raw_resource_keys, (list, tuple, set)):
        raw_resource_keys = []
    resource_keys = [
        str(value)
        for value in raw_resource_keys
        if str(value).strip()
    ]
    job_metadata["resource_keys"] = list(
        dict.fromkeys([f"hdf5:{hdf5_root}", *resource_keys])
    )
    return create_job(label, commands, metadata=job_metadata)


def mark_job_terminal(
    job: dict[str, Any],
    status: str,
    returncode: int | None,
) -> None:
    invalidate_status_cache()
    with JOBS_LOCK:
        job["status"] = status
        job["returncode"] = returncode
        job["updated_at"] = time.time()


def mark_job_stopped(job: dict[str, Any], returncode: int | None = None) -> None:
    mark_job_terminal(job, "stopped", returncode)


def latest_active_job() -> dict[str, Any] | None:
    with JOBS_LOCK:
        active = [
            job
            for job in JOBS.values()
            if str(job.get("status") or "") in {"queued", "running"}
        ]
    if not active:
        return None
    return max(active, key=lambda item: float(item.get("created_at") or 0.0))


def stop_job_by_id(job_id: str | None = None) -> dict[str, Any]:
    fallback_job = latest_active_job() if not job_id else None
    with JOBS_LOCK:
        job = JOBS.get(job_id or "") if job_id else fallback_job
        if job is None:
            return {"ok": False, "error": "No running job found"}
        status = str(job.get("status") or "")
        if status not in {"queued", "running"}:
            return {"ok": False, "error": f"Job is not running: {status}", "job": public_job_payload(job)}
        job["stop_requested"] = True
        process = job.get("_process")
        graceful_stop_only = bool(job.get("graceful_stop_only"))
        target_job_id = str(job.get("id") or "")
        job_payload = public_job_payload(job)
    append_job(job, "Stop requested by user.")
    if isinstance(process, subprocess.Popen) and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            append_job(job, f"Sent SIGTERM to process group {process.pid}.")
        except ProcessLookupError:
            pass
        except Exception as exc:
            append_job(job, f"Failed to terminate process group {process.pid}: {exc}")

        if graceful_stop_only:
            append_job(
                job,
                "Transactional data operation: waiting for graceful rollback; SIGKILL is disabled.",
            )
        else:
            def kill_later() -> None:
                time.sleep(5)
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                        append_job(job, f"Sent SIGKILL to process group {process.pid}.")
                    except ProcessLookupError:
                        pass
                    except Exception as exc:
                        append_job(job, f"Failed to kill process group {process.pid}: {exc}")

            threading.Thread(target=kill_later, daemon=True).start()
    return {"ok": True, "job_id": target_job_id, "job": job_payload}


def run_job(job: dict[str, Any], commands: list[JobStep]) -> None:
    try:
        _run_job(job, commands)
    except Exception as exc:
        append_job(job, f"Unexpected job failure: {exc}")
        mark_job_terminal(job, "failed", 1)


def _run_job(job: dict[str, Any], commands: list[JobStep]) -> None:
    with JOBS_LOCK:
        job["status"] = "running"
    for idx, step in enumerate(commands, start=1):
        if job.get("stop_requested"):
            append_job(job, "Job stopped before next step.")
            mark_job_stopped(job)
            return
        append_job(job, f"==== step {idx}/{len(commands)} ====")
        if callable(step):
            append_job(job, f"+ {getattr(step, '__name__', 'internal step')}")
            try:
                step(job)
            except Exception as exc:
                if job.get("stop_requested"):
                    append_job(job, "Job stopped by user.")
                    mark_job_stopped(job, 130)
                    return
                append_job(job, f"Internal step failed: {exc}")
                mark_job_terminal(job, "failed", 1)
                return
            if job.get("stop_requested"):
                append_job(job, "Job stopped by user.")
                mark_job_stopped(job)
                return
            continue
        cmd, cwd, env = step
        returncode = run_process_for_job(job, cmd, cwd, env)
        if job.get("stop_requested"):
            append_job(job, "Job stopped by user.")
            mark_job_stopped(job, returncode)
            return
        if returncode != 0:
            append_job(job, f"Command failed with exit code {returncode}")
            mark_job_terminal(job, "failed", returncode)
            return
    mark_job_terminal(job, "completed", 0)


def write_renumber_plan(
    cfg: dict[str, Any],
    entries: list[dict[str, Any]] | None = None,
) -> Path:
    plan = build_renumber_plan(cfg, entries)
    out_path = cfg["hdf5_root"] / "renumber_plan.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "mode": "quality_grade",
        "repo_id": cfg["repo_id"],
        "lerobot_root": str(cfg["lerobot_root"]),
        "grade_groups": renumber_grade_groups(plan),
        "episodes": plan,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out_path


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_port(port: int, process: subprocess.Popen[str], timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Replay process exited with code {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"Replay server did not listen on port {port} within {timeout:.1f}s")


def pump_replay_output(key: str, process: subprocess.Popen[str]) -> None:
    if process.stdout is None:
        return
    for line in process.stdout:
        with JOBS_LOCK:
            lines = REPLAY_LOGS.setdefault(key, [])
            lines.append(line.rstrip("\n"))
            del lines[:-200]


def replay_proxy_path(path: str, query: str) -> str | None:
    if path == "/replay":
        target = "/"
    elif path.startswith("/replay/"):
        target = "/" + path[len("/replay/") :]
    elif path in {"/api/episodes", "/api/episode"} or path.startswith("/video/"):
        target = path
    else:
        return None
    if query:
        target += "?" + query
    return target


def active_replay_port() -> int:
    with JOBS_LOCK:
        key = ACTIVE_REPLAY_KEY
        process = REPLAY_PROCESSES.get(key or "")
        port = REPLAY_PORTS.get(key or "")
    if not key or process is None or port is None:
        raise RuntimeError("No active replay server. Click 打开/刷新回放 first.")
    if process.poll() is not None:
        raise RuntimeError(f"Replay process exited with code {process.returncode}")
    return port


def proxy_replay(
    handler: BaseHTTPRequestHandler,
    target_path: str,
    send_body: bool,
    method: str | None = None,
) -> None:
    try:
        port = active_replay_port()
    except Exception as exc:
        json_response(handler, {"error": str(exc)}, 502)
        return

    method = method or ("HEAD" if not send_body else "GET")
    body: bytes | None = None
    if method.upper() == "POST":
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length > 0 else b""
    target_url = f"http://127.0.0.1:{port}{target_path}"
    headers = {
        key: value
        for key, value in handler.headers.items()
        if key.lower()
        in {"range", "accept", "user-agent", "if-modified-since", "if-none-match", "content-type"}
    }
    request = Request(target_url, data=body, headers=headers, method=method)
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=30) as response:
            handler.send_response(response.status)
            for key, value in response.headers.items():
                if key.lower() in {
                    "content-type",
                    "content-length",
                    "content-range",
                    "accept-ranges",
                    "cache-control",
                }:
                    handler.send_header(key, value)
            handler.end_headers()
            if send_body:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
    except HTTPError as exc:
        handler.send_response(exc.code)
        for key, value in exc.headers.items():
            if key.lower() in {"content-type", "content-length"}:
                handler.send_header(key, value)
        handler.end_headers()
        if send_body:
            handler.wfile.write(exc.read())
    except URLError as exc:
        json_response(handler, {"error": f"Replay proxy failed: {exc}"}, 502)


def start_replay(payload: dict[str, Any]) -> dict[str, Any]:
    global ACTIVE_REPLAY_KEY

    cfg = derive_paths(payload)
    manual_entries = load_manual_failures_for_cfg(cfg)
    if manual_entries:
        write_manual_failure_file(manual_failure_hdf5_path(cfg), manual_entries)
    report_dir = latest_qc_report(cfg["qc_root"], cfg["dataset_name"], payload.get("qc_report_dir"))
    key = str(cfg["hdf5_root"])
    old = REPLAY_PROCESSES.get(key)
    if old and old.poll() is None:
        old.terminate()
        try:
            old.wait(timeout=2)
        except subprocess.TimeoutExpired:
            old.kill()
    port = free_port()
    cmd = [
        sys.executable,
        str(ROOT / "mcap_conversion" / "scripts" / "replay_icra_episode.py"),
        "--episode-dir",
        str(cfg["hdf5_root"]),
        "--host",
        "0.0.0.0",
        "--port",
        str(port),
        "--type",
        cfg["robot_type"],
    ]
    if report_dir:
        cmd.extend(["--qc-report-dir", str(report_dir)])
    cmd.extend(["--manual-failure-json", str(manual_failure_hdf5_path(cfg))])
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    REPLAY_PROCESSES[key] = proc
    with JOBS_LOCK:
        REPLAY_LOGS[key] = []
        REPLAY_PORTS[key] = port
        ACTIVE_REPLAY_KEY = key
    threading.Thread(target=pump_replay_output, args=(key, proc), daemon=True).start()
    try:
        wait_for_port(port, proc)
    except Exception as exc:
        with JOBS_LOCK:
            log_tail = "\n".join(REPLAY_LOGS.get(key, [])[-30:])
        raise RuntimeError(f"{exc}\n{log_tail}".strip()) from exc
    return {
        "url": "/replay/",
        "port": port,
        "pid": proc.pid,
        "qc_report_dir": str(report_dir) if report_dir else "",
    }


def start_lerobot_replay(payload: dict[str, Any]) -> dict[str, Any]:
    base_cfg = derive_paths(payload)
    cfg = resolve_lerobot_replay_cfg(base_cfg)
    summary = build_lerobot_replay_summary(cfg)
    key = now_id()
    with JOBS_LOCK:
        LEROBOT_REPLAY_CONFIGS[key] = base_cfg
    return {
        "url": f"/lerobot-replay/?key={key}",
        "key": key,
        "summary": summary,
    }


def start_cross_platform_lerobot_replay(payload: dict[str, Any]) -> dict[str, Any]:
    dataset_text = str(payload.get("dataset_path") or "").strip()
    if not dataset_text:
        raise ValueError("missing LeRobot dataset_path")
    dataset_dir = Path(dataset_text).expanduser().resolve()
    if not cross_platform.is_lerobot_dataset(dataset_dir):
        raise ValueError(f"not a LeRobot dataset: {dataset_dir}")
    base_cfg = {
        "lerobot_dataset_dir": dataset_dir,
        "lerobot_root": dataset_dir,
        "repo_id": dataset_dir.name,
        "hdf5_root": dataset_dir,
    }
    cfg = resolve_lerobot_replay_cfg(base_cfg)
    summary = build_lerobot_replay_summary(cfg)
    key = now_id()
    with JOBS_LOCK:
        LEROBOT_REPLAY_CONFIGS[key] = base_cfg
    return {
        "url": f"/lerobot-replay/?key={key}",
        "key": key,
        "summary": summary,
    }


def start_lerobot_visualization_replay(payload: dict[str, Any]) -> dict[str, Any]:
    dataset_text = str(payload.get("dataset_path") or "").strip()
    if not dataset_text:
        raise ValueError("请选择需要可视化的 LeRobot 数据集")
    dataset_dir = Path(dataset_text).expanduser().resolve()
    validation = validate_lerobot_visualization_dataset(dataset_dir)
    base_cfg = replay_cfg_for_dataset(dataset_dir)
    key = now_id()
    with JOBS_LOCK:
        LEROBOT_REPLAY_CONFIGS[key] = base_cfg
    return {
        "url": f"/lerobot-replay/?key={key}",
        "key": key,
        "summary": validation["summary"],
        "validation": {
            "episode_count": validation["episode_count"],
            "video_view_count": validation["video_view_count"],
            "task_count": validation["task_count"],
        },
    }


def save_lerobot_visualization_screening_record(payload: dict[str, Any]) -> dict[str, Any]:
    dataset_text = str(payload.get("dataset_path") or "").strip()
    if not dataset_text:
        raise ValueError("请选择需要筛查的 LeRobot 数据集")
    dataset_dir = Path(dataset_text).expanduser().resolve()
    if not cross_platform.is_lerobot_dataset(dataset_dir):
        raise ValueError(f"不是 LeRobot 数据集：{dataset_dir}")
    try:
        episode_index = int(payload.get("episode_index"))
    except (TypeError, ValueError) as exc:
        raise ValueError("请选择需要筛查的 Episode") from exc
    summary = build_lerobot_replay_summary(replay_cfg_for_dataset(dataset_dir))
    episode = next(
        (
            item
            for item in summary.get("episodes", [])
            if int(item.get("episode_index", -1)) == episode_index
        ),
        None,
    )
    if episode is None:
        raise ValueError(f"数据集中没有 episode_{episode_index:06d}")
    return manual_screening.save_visualization_record(
        dataset_dir,
        episode_index,
        payload.get("error_types") if isinstance(payload.get("error_types"), list) else [],
        payload.get("corrections") if isinstance(payload.get("corrections"), dict) else {},
        quality_grade=payload.get("quality_grade"),
        review_note=payload.get("review_note"),
        episode_info=episode,
    )


def cross_platform_review_steps(payload: dict[str, Any]) -> list[JobStep]:
    raw_datasets = payload.get("datasets")
    if not isinstance(raw_datasets, list) or not raw_datasets:
        raise ValueError("请至少提交一个待审核数据集")

    prepared: list[tuple[Path, Path, list[Any]]] = []
    seen_outputs: set[Path] = set()
    for raw in raw_datasets:
        if not isinstance(raw, dict):
            raise ValueError("审核数据集参数格式错误")
        source = Path(str(raw.get("source_path") or "")).expanduser()
        output = Path(str(raw.get("output_path") or "")).expanduser()
        annotations = raw.get("annotations")
        if not isinstance(annotations, list) or not annotations:
            raise ValueError(f"数据集 {source} 没有待处理标注")
        source, output, _ = cross_platform.validate_review_request(source, output, annotations)
        if output in seen_outputs:
            raise ValueError(f"多个数据集使用了相同输出目录: {output}")
        seen_outputs.add(output)
        prepared.append((source, output, annotations))

    steps: list[JobStep] = []
    for source, output, annotations in prepared:
        def rebuild_step(
            job: dict[str, Any],
            source_path: Path = source,
            output_path: Path = output,
            changes: list[Any] = annotations,
        ) -> None:
            result = cross_platform.rebuild_reviewed_dataset(
                source_path,
                output_path,
                changes,
                progress=lambda line: append_job(job, line),
                stop_requested=lambda: bool(job.get("stop_requested")),
            )
            with JOBS_LOCK:
                job.setdefault("result", []).append(result)
            append_job(
                job,
                f"审核后数据集已生成: {output_path} "
                f"({result['source_episodes']} -> {result['output_episodes']} episodes)",
            )

        rebuild_step.__name__ = f"重建 LeRobot 审核数据集 {source.name}"
        steps.append(rebuild_step)
    return steps


def manual_screening_extract_step(group: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
    def step(job: dict[str, Any]) -> None:
        result = manual_screening.extract_dataset_group(
            group,
            progress=lambda line: append_job(job, line),
            stop_requested=lambda: bool(job.get("stop_requested")),
        )
        with JOBS_LOCK:
            job["result"] = {
                "dataset_path": result["dataset_path"],
                "grades": result["grades"],
                "episode_count": result["episode_count"],
                "successful_episode_count": result["successful_episode_count"],
                "manifest": str(manual_screening.group_manifest_path(result["dataset_id"])),
            }
        append_job(
            job,
            "人工筛查截帧完成: "
            f"{result['successful_episode_count']}/{result['episode_count']} episodes",
        )

    grade_label = str(group.get("grade_label") or "未分级")
    step.__name__ = f"截取 LeRobot episode 图片 {group.get('name')} [{grade_label}]"
    return step


def manual_screening_yolo_step(group: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
    def step(job: dict[str, Any]) -> None:
        manifest = manual_screening.load_group_manifest(group)
        if manifest is None:
            raise ValueError("截帧完成后没有找到 A/B/F 数据集组清单")
        report = manual_screening_yolo.detect_manifest(
            manifest,
            storage_root=manual_screening.DEFAULT_STORAGE_ROOT.expanduser().resolve(),
            progress=lambda line: append_job(job, line),
            stop_requested=lambda: bool(job.get("stop_requested")),
        )
        with JOBS_LOCK:
            result = job.setdefault("result", {})
            result["yolo"] = {
                "episode_count": report["episode_count"],
                "correct_count": report["correct_count"],
                "warning_count": report["warning_count"],
                "error_count": report["error_count"],
            }
        append_job(
            job,
            "YOLO 全量识别完成: "
            f"正确 {report['correct_count']}，预警 {report['warning_count']}，"
            f"共 {report['episode_count']} episodes",
        )

    grade_label = str(group.get("grade_label") or "未分级")
    step.__name__ = f"YOLO 扫描全部 episode {group.get('name')} [{grade_label}]"
    return step


def create_or_reuse_manual_screening_job(group: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    group_id = str(group.get("id") or "")
    with MANUAL_SCREENING_JOB_LOCK:
        with JOBS_LOCK:
            existing = next(
                (
                    job
                    for job in JOBS.values()
                    if job.get("manual_screening_group_id") == group_id
                    and job.get("status") in {"queued", "running"}
                ),
                None,
            )
        if existing is not None:
            return existing, True
        grade_label = str(group.get("grade_label") or "未分级")
        job = create_job(
            f"人工筛查截帧及 YOLO 全量识别 {group['name']} [{grade_label}]",
            [manual_screening_extract_step(group), manual_screening_yolo_step(group)],
            metadata={"manual_screening_group_id": group_id},
        )
        return job, False


def lerobot_cfg_for_key(key: str, grade: str | None = None) -> dict[str, Any]:
    with JOBS_LOCK:
        cfg = LEROBOT_REPLAY_CONFIGS.get(key)
    if cfg is None:
        raise RuntimeError("LeRobot replay session not found. Click 打开/刷新 LeRobot 回放 first.")
    return resolve_lerobot_replay_cfg(cfg, grade)


def serve_lerobot_video(handler: BaseHTTPRequestHandler, path: str, send_body: bool, query: str = "") -> None:
    prefix = "/lerobot-video/"
    if not path.startswith(prefix):
        json_response(handler, {"error": "not found"}, HTTPStatus.NOT_FOUND)
        return
    rest = path[len(prefix) :]
    key, _, rel = rest.partition("/")
    key = unquote(key)
    rel = unquote(rel)
    if not key or not rel:
        json_response(handler, {"error": "missing LeRobot video key/path"}, 400)
        return
    try:
        grade = parse_qs(query).get("grade", [""])[-1]
        cfg = lerobot_cfg_for_key(key, grade)
        root = lerobot_dataset_dir(cfg)
        video_path = (root / rel).resolve()
        video_path.relative_to(root)
        if not video_path.is_file():
            raise FileNotFoundError(video_path)
        size = video_path.stat().st_size
        start = 0
        end = size - 1
        status = 200
        range_header = handler.headers.get("Range", "")
        if range_header.startswith("bytes="):
            status = 206
            range_text = range_header.split("=", 1)[1].split(",", 1)[0].strip()
            start_text, _, end_text = range_text.partition("-")
            start = int(start_text) if start_text else 0
            end = int(end_text) if end_text else size - 1
            start = max(0, min(start, size - 1))
            end = max(start, min(end, size - 1))
        length = end - start + 1
        handler.send_response(status)
        handler.send_header("Content-Type", "video/mp4")
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", str(length))
        if status == 206:
            handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        handler.end_headers()
        if not send_body:
            return
        with video_path.open("rb") as file_obj:
            file_obj.seek(start)
            remaining = length
            while remaining > 0:
                chunk = file_obj.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                remaining -= len(chunk)
    except Exception as exc:
        json_response(handler, {"error": str(exc)}, 404)


def serve_manual_screening_image(handler: BaseHTTPRequestHandler, path: str, send_body: bool) -> None:
    prefix = "/manual-screening-image/"
    if not path.startswith(prefix):
        json_response(handler, {"error": "not found"}, HTTPStatus.NOT_FOUND)
        return
    dataset_key, separator, relative_text = unquote(path[len(prefix) :]).partition("/")
    try:
        if not separator or not re.fullmatch(r"[0-9a-f]{20}", dataset_key):
            raise ValueError("invalid manual screening image path")
        root = (manual_screening.DEFAULT_STORAGE_ROOT / "extracted" / dataset_key).resolve()
        image_path = (root / relative_text).resolve()
        image_path.relative_to(root)
        if image_path.suffix.lower() not in {".jpg", ".jpeg", ".png"} or not image_path.is_file():
            raise FileNotFoundError(image_path)
        data = image_path.read_bytes() if send_body else b""
        size = image_path.stat().st_size
        content_type = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(size))
        handler.send_header("Cache-Control", "private, max-age=3600")
        handler.end_headers()
        if send_body:
            handler.wfile.write(data)
    except Exception as exc:
        json_response(handler, {"error": str(exc)}, 404)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/manual-screening-image/"):
            serve_manual_screening_image(self, parsed.path, send_body=False)
            return
        if parsed.path.startswith("/lerobot-video/"):
            serve_lerobot_video(self, parsed.path, send_body=False, query=parsed.query)
            return
        target_path = replay_proxy_path(parsed.path, parsed.query)
        if target_path is not None:
            proxy_replay(self, target_path, send_body=False)
            return
        json_response(self, {"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            text_response(self, HTML)
            return
        if parsed.path == "/cross-platform/":
            text_response(self, CROSS_PLATFORM_HTML)
            return
        if parsed.path == "/lerobot-visualization/":
            text_response(self, LEROBOT_VISUALIZATION_HTML)
            return
        if parsed.path == "/manual-screening/":
            text_response(self, MANUAL_SCREENING_HTML)
            return
        if parsed.path == "/manual-screening-records/":
            text_response(self, MANUAL_SCREENING_RECORDS_HTML)
            return
            return
        if parsed.path == "/lerobot-replay/":
            text_response(self, LEROBOT_REPLAY_HTML)
            return
        if parsed.path.startswith("/manual-screening-image/"):
            serve_manual_screening_image(self, parsed.path, send_body=True)
            return
        if parsed.path.startswith("/lerobot-video/"):
            serve_lerobot_video(self, parsed.path, send_body=True, query=parsed.query)
            return
        target_path = replay_proxy_path(parsed.path, parsed.query)
        if target_path is not None:
            proxy_replay(self, target_path, send_body=True)
            return
        if parsed.path == "/api/jobs":
            with JOBS_LOCK:
                jobs = [
                    public_job_payload(job)
                    for job in sorted(JOBS.values(), key=lambda item: item["created_at"], reverse=True)
                ]
            json_response(self, {"jobs": jobs})
            return
        if parsed.path.startswith("/api/jobs/"):
            job_id = parsed.path.rsplit("/", 1)[-1]
            query = parse_qs(parsed.query)
            cursor_value = query.get("cursor", [""])[-1]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if job:
                    offset = int(job.get("log_offset", 0))
                    next_index = int(job.get("log_next", offset + len(job.get("log", []))))
                    full_log = list(job.get("log", []))
                    payload = public_job_payload(job)
                    try:
                        cursor = int(cursor_value) if cursor_value else offset
                    except ValueError:
                        cursor = offset
                    if cursor < offset:
                        payload["log_truncated"] = True
                        payload["log"] = [
                            f"[只保留最近 {MAX_JOB_LOG_LINES} 行日志，前面 {offset} 行已省略]",
                            *full_log,
                        ]
                    elif cursor >= next_index:
                        payload["log_truncated"] = False
                        payload["log"] = []
                    else:
                        start = max(0, cursor - offset)
                        payload["log_truncated"] = False
                        payload["log"] = full_log[start:]
                    payload["log_offset"] = offset
                    payload["log_cursor"] = next_index
                else:
                    payload = None
            json_response(self, payload or {"error": "not found"}, 200 if payload else 404)
            return
        if parsed.path == "/api/defaults":
            query = parse_qs(parsed.query)
            payload = {key: values[-1] for key, values in query.items()}
            json_response(self, {"config": stringify_config(derive_paths(payload))})
            return
        if parsed.path == "/api/datasets":
            query = parse_qs(parsed.query)
            machine = query.get("machine", ["agilex"])[-1]
            try:
                response = discover_machine_datasets(machine)
            except ValueError as exc:
                json_response(self, {"error": str(exc)}, 400)
                return
            json_response(self, response)
            return
        if parsed.path == "/api/manual-screening/datasets":
            json_response(self, manual_screening.discover_datasets(parse_qs(parsed.query).get("root", [None])[-1]))
            return
        if parsed.path == "/api/manual-screening/options":
            json_response(self, {"objects": manual_screening.correction_object_options()})
            return
        if parsed.path == "/api/manual-screening/result":
            query = parse_qs(parsed.query)
            group_id = query.get("dataset_group_id", [""])[-1].strip()
            if not group_id:
                json_response(self, {"error": "missing dataset_group_id"}, 400)
                return
            group = manual_screening.find_dataset_group(group_id)
            manifest = manual_screening.load_group_manifest(group)
            if manifest is None:
                json_response(self, {"error": "该 A/B/F 数据集组尚未截取图片"}, 404)
                return
            record_payload = manual_screening.records_for_group(group)
            yolo_report = manual_screening_yolo.load_report(
                manifest,
                storage_root=manual_screening.DEFAULT_STORAGE_ROOT.expanduser().resolve(),
            )
            json_response(
                self,
                {
                    "manifest": manifest,
                    "records": record_payload["records"],
                    "record_file": record_payload["record_file"],
                    "yolo_report": yolo_report,
                },
            )
            return
        if parsed.path == "/api/manual-screening/records":
            query = parse_qs(parsed.query)
            dataset_text = query.get("dataset_path", [""])[-1].strip()
            payload = (
                manual_screening.records_for_dataset(Path(dataset_text))
                if dataset_text
                else manual_screening.load_records()
            )
            json_response(self, payload)
            return
        if parsed.path == "/api/dataset-choice":
            query = parse_qs(parsed.query)
            path_text = query.get("path", [""])[-1].strip()
            if not path_text:
                json_response(self, {"error": "missing dataset directory"}, 400)
                return
            dataset_path = Path(path_text).expanduser().resolve()
            if not dataset_path.is_dir():
                json_response(self, {"error": f"Dataset directory does not exist: {dataset_path}"}, 404)
                return
            json_response(
                self,
                {
                    "dataset": dataset_choice_entry(
                        dataset_path,
                        dataset_path.parent,
                        dataset_path.parent.name,
                    )
                },
            )
            return
        if parsed.path == "/api/mcap-datasets":
            query = parse_qs(parsed.query)
            parent = query.get("parent", [""])[-1].strip()
            if not parent:
                json_response(self, {"parent": "", "parent_is_dataset": False, "selected_path": "", "datasets": []})
                return
            json_response(self, discover_mcap_datasets(parent))
            return
        if parsed.path == "/api/lerobot-replay/episodes":
            query = parse_qs(parsed.query)
            key = query.get("key", [""])[-1]
            grade = query.get("grade", [""])[-1]
            cfg = lerobot_cfg_for_key(key, grade)
            json_response(self, build_lerobot_replay_summary(cfg))
            return
        if parsed.path == "/api/lerobot-replay/episode":
            query = parse_qs(parsed.query)
            key = query.get("key", [""])[-1]
            grade = query.get("grade", [""])[-1]
            episode_index = int(query.get("episode_index", ["0"])[-1])
            cfg = lerobot_cfg_for_key(key, grade)
            json_response(self, load_lerobot_episode_payload(cfg, episode_index))
            return
        json_response(self, {"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            target_path = replay_proxy_path(parsed.path, parsed.query)
            if target_path is not None and parsed.path.startswith("/replay/"):
                proxy_replay(self, target_path, send_body=True, method="POST")
                return
            payload = read_json_body(self)
            if parsed.path == "/api/manual-screening/yolo-detect":
                group_id = str(payload.get("dataset_group_id") or "").strip()
                episode_key = str(payload.get("episode_key") or "").strip()
                if not group_id or not episode_key:
                    json_response(
                        self,
                        {"error": "YOLO 检测缺少 dataset_group_id 或 episode_key"},
                        400,
                    )
                    return
                group = manual_screening.find_dataset_group(group_id)
                manifest = manual_screening.load_group_manifest(group)
                if manifest is None:
                    json_response(self, {"error": "请先为该 A/B/F 数据集组截取图片"}, 404)
                    return
                result = manual_screening_yolo.detect_episode(
                    manifest,
                    episode_key,
                    storage_root=manual_screening.DEFAULT_STORAGE_ROOT.expanduser().resolve(),
                )
                manual_screening_yolo.save_episode_result(
                    manifest,
                    result,
                    storage_root=manual_screening.DEFAULT_STORAGE_ROOT.expanduser().resolve(),
                )
                json_response(self, result)
                return
            if parsed.path == "/api/manual-screening/extract":
                group_id = str(payload.get("dataset_group_id") or "").strip()
                if not group_id:
                    json_response(self, {"error": "请选择 A/B/F LeRobot 数据集组"}, 400)
                    return
                group = manual_screening.find_dataset_group(group_id)
                job, reused = create_or_reuse_manual_screening_job(group)
                json_response(self, {"job": public_job_payload(job), "reused": reused})
                return
            if parsed.path == "/api/manual-screening/records/delete":
                record_ids = payload.get("record_ids")
                if not isinstance(record_ids, list):
                    json_response(self, {"error": "record_ids 必须是数组"}, 400)
                    return
                json_response(self, manual_screening.delete_records(record_ids))
                return
            if parsed.path == "/api/manual-screening/records":
                dataset_text = str(payload.get("dataset_path") or "").strip()
                if not dataset_text:
                    json_response(self, {"error": "missing dataset_path"}, 400)
                    return
                result = manual_screening.save_record(
                    Path(dataset_text),
                    int(payload.get("episode_index")),
                    payload.get("error_types") if isinstance(payload.get("error_types"), list) else [],
                    payload.get("corrections") if isinstance(payload.get("corrections"), dict) else {},
                )
                json_response(self, result)
                return
            if parsed.path == "/api/cross-platform/discover":
                json_response(self, cross_platform.discover_datasets(
                    payload.get("sources") or [], payload.get("robot_type") or "aloha"
                ))
                return
            if parsed.path == "/api/lerobot-visualization/discover":
                root_text = str(payload.get("root") or "").strip()
                if not root_text:
                    json_response(self, {"error": "请输入需要扫描的目录"}, 400)
                    return
                json_response(
                    self,
                    discover_lerobot_visualization_datasets(Path(root_text)),
                )
                return
            if parsed.path == "/api/lerobot-visualization/replay/start":
                json_response(self, start_lerobot_visualization_replay(payload))
                return
            if parsed.path == "/api/lerobot-visualization/records":
                json_response(self, save_lerobot_visualization_screening_record(payload))
                return
            if parsed.path == "/api/cross-platform/analyze":
                json_response(
                    self,
                    cross_platform.analyze_datasets(
                        payload.get("datasets") or [],
                        int(payload.get("max_episodes") or 20),
                        int(payload.get("max_frames") or 1000),
                        robot_type=payload.get("robot_type") or "aloha",
                        stationary_threshold=payload.get("stationary_threshold", 60),
                    ),
                )
                return
            if parsed.path == "/api/cross-platform/replay/start":
                json_response(self, start_cross_platform_lerobot_replay(payload))
                return
            if parsed.path == "/api/cross-platform/review/apply":
                job = create_job("批量生成审核后 LeRobot 数据集", cross_platform_review_steps(payload))
                json_response(self, {"job": public_job_payload(job)})
                return
            if parsed.path == "/api/status":
                json_response(self, dataset_status(payload))
                return
            if parsed.path == "/api/replay/start":
                json_response(self, start_replay(payload))
                return
            if parsed.path == "/api/lerobot-replay/start":
                json_response(self, start_lerobot_replay(payload))
                return
            if parsed.path == "/api/qc-report/quality-grade":
                cfg = derive_paths(payload)
                saved = save_qc_report_quality_grade(
                    cfg,
                    episode_name=str(payload.get("episode_name") or payload.get("episode_id") or ""),
                    quality_grade=str(payload.get("quality_grade") or payload.get("grade") or ""),
                    reason_label=payload.get("reason_label") or payload.get("quality_description") or "",
                    reason_codes=payload.get("reason_codes"),
                )
                invalidate_status_cache()
                json_response(
                    self,
                    {
                        "ok": True,
                        "annotation": saved,
                        "manual_failure_json": str(manual_failure_hdf5_path(cfg)),
                        "status": dataset_status(payload),
                    },
                )
                return
            if parsed.path == "/api/jobs/current/stop":
                result = stop_job_by_id(None)
                json_response(self, result, 200 if result.get("ok") else 404)
                return
            if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/stop"):
                parts = parsed.path.strip("/").split("/")
                job_id = parts[2] if len(parts) >= 3 else ""
                result = stop_job_by_id(job_id)
                json_response(self, result, 200 if result.get("ok") else 404)
                return
            if parsed.path == "/api/run":
                stage = str(payload.get("stage") or "")
                cfg = derive_paths(payload)
                invalidate_status_cache()
                if stage == "convert_qc":
                    job = create_pipeline_job("MCAP 转 HDF5 并质检", [convert_then_qc_step(cfg)], cfg)
                elif stage == "convert":
                    job = create_pipeline_job(
                        "MCAP 转 HDF5",
                        [convert_command(cfg), sync_manual_failures_step(cfg)],
                        cfg,
                    )
                elif stage == "qc":
                    job = create_pipeline_job(
                        "批量质检",
                        [sync_manual_failures_step(cfg), qc_command(cfg)],
                        cfg,
                    )
                elif stage == "repair":
                    names = selected_episode_names(payload)
                    job = create_pipeline_job(
                        "选择 episode 剔除静止帧并复检",
                        repair_commands(cfg, payload.get("qc_report_dir"), names),
                        cfg,
                        metadata={"graceful_stop_only": True},
                    )
                elif stage == "optimize_hdf5":
                    job = create_pipeline_job(
                        "HDF5 episode 编号优化并复检",
                        optimize_hdf5_commands(cfg),
                        cfg,
                        metadata={"graceful_stop_only": True},
                    )
                elif stage == "delete":
                    names = selected_episode_names(payload)
                    commands = [delete_selected_hdf5_step(cfg, names), sync_manual_failures_step(cfg), qc_command(cfg)]
                    job = create_pipeline_job("选择 episode 删除并复检", commands, cfg)
                elif stage in {"delete_grade_c_qc", "delete_failed_qc"}:
                    commands = [sync_manual_failures_step(cfg), delete_quality_grade_hdf5_step(cfg, "C"), qc_command(cfg)]
                    job = create_pipeline_job("批量删除等级 C episode 并复检", commands, cfg)
                elif stage == "renumber":
                    path = write_renumber_plan(cfg)
                    json_response(self, {"ok": True, "path": str(path), "status": dataset_status(payload)})
                    return
                elif stage == "sync_hdf5_grades":
                    job = create_pipeline_job(
                        "批量写入并校验 HDF5 质量等级",
                        hdf5_quality_grade_sync_steps(cfg),
                        cfg,
                    )
                elif stage == "lerobot":
                    job = create_pipeline_job("按质量等级生成 LeRobot", lerobot_commands(cfg), cfg)
                elif stage == "split_lerobot_stages":
                    job = create_pipeline_job(
                        "按左右手阶段切分 LeRobot",
                        split_lerobot_stage_commands(cfg),
                        cfg,
                        metadata={
                            "stage": "split_lerobot_stages",
                            "graceful_stop_only": True,
                            "resource_keys": [
                                f"lerobot:{lerobot_stage_split_base_dir(cfg)}"
                            ],
                        },
                    )
                else:
                    json_response(self, {"error": f"unknown stage: {stage}"}, 400)
                    return
                json_response(self, {"job": public_job_payload(job)})
                return
            json_response(self, {"error": "not found"}, HTTPStatus.NOT_FOUND)
        except JobConflictError as exc:
            json_response(self, {"error": str(exc)}, 409)
        except ValueError as exc:
            json_response(self, {"error": str(exc)}, 400)
        except Exception as exc:
            json_response(self, {"error": str(exc)}, 500)


LEROBOT_REPLAY_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>LeRobot Episode Replay</title>
  <style>
    :root {
      --bg: #0f1114;
      --panel: #171b20;
      --panel-2: #20262c;
      --border: #333b44;
      --text: #edf1f5;
      --muted: #aab4bf;
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
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
      overflow: hidden;
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
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      min-height: 0;
    }
    .card, .panel {
      min-width: 0;
      min-height: 0;
      overflow: hidden;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
    }
    .card {
      display: flex;
      flex-direction: column;
    }
    .card-head, .panel-head {
      min-height: 34px;
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
    .grade-nav {
      display: flex;
      align-items: center;
      gap: 6px;
      min-width: 110px;
    }
    .grade-nav span {
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }
    .grade-nav select {
      height: 28px;
      min-width: 72px;
      padding: 0 8px;
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
      grid-template-rows: auto auto auto auto auto minmax(0, 1fr);
      border-radius: 8px;
    }
    .panel-head {
      height: auto;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto auto;
      row-gap: 8px;
      padding: 8px 10px;
    }
    .panel-title {
      min-width: 0;
      display: flex;
      align-items: center;
    }
    .episode-row {
      grid-column: 1 / -1;
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }
    .embedded-selector-row,
    .embedded-review-strip { display: none; }
    .embedded-selector-row {
      grid-column: 1 / -1;
      grid-template-columns: minmax(150px, .75fr) auto minmax(210px, 1.25fr) auto;
      gap: 6px;
      align-items: center;
      min-width: 0;
    }
    .embedded-selector-row select { width: 100%; min-width: 0; }
    .embedded-review-strip {
      grid-template-columns: 92px minmax(150px, 1fr) auto auto auto;
      gap: 7px;
      align-items: end;
      padding: 8px 10px;
      border-bottom: 1px solid var(--border);
      background: #15191d;
    }
    .embedded-review-strip label {
      display: grid;
      gap: 3px;
      min-width: 0;
      color: var(--muted);
      font-size: 10px;
      font-weight: 700;
    }
    .embedded-review-strip input,
    .embedded-review-strip select {
      width: 100%;
      height: 30px;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0 8px;
      color: var(--text);
      background: var(--panel-2);
    }
    .embedded-review-strip .delete-label { grid-template-columns: auto auto; align-items: center; gap: 6px; padding-bottom: 7px; }
    .embedded-review-strip .delete-label input { width: 16px; height: 16px; padding: 0; }
    .embedded-review-count { align-self: center; color: #f3b34c; font-size: 11px; font-weight: 750; white-space: nowrap; }
    .embedded-review-actions { display: flex; gap: 6px; align-items: center; }
    .embedded-review-actions button { height: 30px; padding: 0 9px; font-size: 11px; }
    .embedded-review-status { grid-column: 1 / -1; min-height: 16px; overflow: hidden; color: var(--muted); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
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
    #titleEpisode {
      margin-left: 8px;
      color: var(--text);
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      font-size: 13px;
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
      gap: 4px;
      background: var(--border);
      border-bottom: 1px solid var(--border);
    }
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
    .meta-strip {
      display: grid;
      gap: 6px;
      padding: 8px 10px;
      border-bottom: 1px solid var(--border);
      background: #15191d;
      font-size: 13px;
    }
    .meta-line {
      display: grid;
      grid-template-columns: 72px minmax(0, 1fr);
      gap: 8px;
      align-items: start;
      min-width: 0;
    }
    .meta-label { color: var(--muted); }
    .meta-value {
      color: #d3d9df;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    }
    #taskText, #subtaskText, #qualityGradeText, #qualityDescriptionText {
      white-space: normal;
      overflow: visible;
      overflow-wrap: anywhere;
      text-overflow: clip;
      line-height: 1.35;
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
    .data-split {
      min-height: 0;
      display: grid;
      grid-template-rows: minmax(0, 1fr) minmax(0, 1fr);
      overflow: hidden;
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
    body.embedded-review .episode-row,
    body.embedded-review .grade-nav { display: none; }
    body.embedded-review .panel-head { grid-template-columns: minmax(0, 1fr) auto; }
    body.embedded-review .embedded-selector-row { display: grid; }
    body.embedded-review .embedded-review-strip { display: grid; }
    @media (max-width: 980px) {
      body { overflow: auto; }
      .app { height: auto; min-height: 100vh; grid-template-columns: 1fr; grid-template-rows: 52vh minmax(760px, 1fr); }
      .panel { grid-template-rows: auto auto auto auto auto minmax(0, 1fr); }
      .chart-wrap { min-height: 0; }
      .controls { grid-template-columns: auto auto minmax(120px, 1fr); }
      .controls select { display: none; }
      .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .meta-value { white-space: normal; }
      .episode-nav { max-width: none; min-width: 0; }
      .episode-nav button { min-width: 50px; }
      .grade-nav { min-width: 0; }
    }
  </style>
</head>
<body>
  <main class="app">
    <section class="video-grid" id="videoGrid"></section>
    <aside class="panel">
      <div class="panel-head">
        <div class="panel-title"><span id="title">LeRobot Episode Replay</span><span id="titleEpisode"></span></div>
        <div class="grade-nav">
          <span>回放等级</span>
          <select id="gradeSelect" title="质量等级"></select>
        </div>
        <span id="stateLabel"></span>
        <div class="episode-row">
          <div class="episode-nav">
            <button id="prevEpisodeBtn" title="上一条 episode">上一条</button>
            <select class="episode-select" id="episodeSelect" title="Episode"></select>
            <button id="nextEpisodeBtn" title="下一条 episode">下一条</button>
          </div>
        </div>
        <div class="embedded-selector-row" id="embeddedSelectorRow">
          <select id="embeddedDatasetSelect" title="数据集"><option value="all">全部数据集</option></select>
          <button id="embeddedPrevEpisodeBtn" title="上一条">上一条</button>
          <select id="embeddedEpisodeSelect" title="Episode"><option value="">暂无 episode</option></select>
          <button id="embeddedNextEpisodeBtn" title="下一条">下一条</button>
        </div>
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
      <div class="meta-strip">
        <div class="meta-line"><span class="meta-label">task</span><span class="meta-value" id="taskText"></span></div>
        <div class="meta-line"><span class="meta-label">subtask</span><span class="meta-value" id="subtaskText"></span></div>
        <div class="meta-line"><span class="meta-label">质量等级</span><span class="meta-value" id="qualityGradeText"></span></div>
        <div class="meta-line"><span class="meta-label">质量描述</span><span class="meta-value" id="qualityDescriptionText"></span></div>
      </div>
      <div class="embedded-review-strip" id="embeddedReviewStrip">
        <label>新等级<select id="embeddedReviewGrade"><option value="">保持原等级</option><option>A</option><option>B</option><option>C</option><option>F</option></select></label>
        <label>审核备注<input id="embeddedReviewReason" placeholder="可选备注" /></label>
        <label class="delete-label"><input id="embeddedReviewExclude" type="checkbox" />人工删除</label>
        <span id="embeddedPendingCount" class="embedded-review-count">0 项待处理</span>
        <div class="embedded-review-actions"><button id="embeddedClearReviewBtn">清空</button><button id="embeddedApplyReviewBtn">生成审核数据集</button></div>
        <div id="embeddedReviewStatus" class="embedded-review-status"></div>
      </div>
      <div class="data-split">
        <div class="chart-wrap">
          <canvas id="chart"></canvas>
          <div class="legend">
            <span><i class="dot state-dot"></i>state</span>
            <span><i class="dot action-dot"></i>action</span>
          </div>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>field</th><th>state</th><th>action</th><th>diff</th></tr></thead>
            <tbody id="jointRows"></tbody>
          </table>
        </div>
      </div>
    </aside>
  </main>

  <script>
    const $ = (id) => document.getElementById(id);
    const params = new URLSearchParams(location.search);
    const key = params.get("key") || "";
    const initialEpisodeText = params.get("episode_index");
    const initialEpisodeIndex = initialEpisodeText === null ? null : Number(initialEpisodeText);
    const embeddedReview = params.get("embedded_review") === "1";
    if (embeddedReview) document.body.classList.add("embedded-review");
    let episodes = [];
    let data = null;
    let videos = [];
    let playing = false;
    let currentFrame = 0;
    let animationHandle = 0;
    let selectedGrade = "";
    let embeddedReviewState = null;
    let embeddedReasonTimer = 0;

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

    function sendEmbeddedReview(action, payload = {}) {
      if (!embeddedReview || window.parent === window) return;
      window.parent.postMessage({type: "lerobot-review-event", action, ...payload}, location.origin);
    }

    function sendVisualizationEpisode(episode = null) {
      if (window.parent === window) return;
      window.parent.postMessage({
        type: "lerobot-visualization-episode",
        episode: episode ? {
          episode_index: Number(episode.episode_index),
          episode_name: episode.episode_name || "",
          task: episode.task || "",
          quality_grade: episode.quality_grade || "",
          quality_description: episode.quality_description || "",
        } : null,
      }, location.origin);
    }

    function applyEmbeddedReviewState(state) {
      if (!embeddedReview || !state) return;
      embeddedReviewState = state;
      const datasetSelect = $("embeddedDatasetSelect");
      const datasetOptions = Array.isArray(state.datasets) ? state.datasets : [];
      datasetSelect.innerHTML = `<option value="all">全部数据集</option>${datasetOptions.map(item => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label)}</option>`).join("")}`;
      datasetSelect.value = state.selected_dataset || "all";

      const episodeSelect = $("embeddedEpisodeSelect");
      const reviewEpisodes = Array.isArray(state.episodes) ? state.episodes : [];
      episodeSelect.innerHTML = reviewEpisodes.length
        ? reviewEpisodes.map(item => `<option value="${escapeHtml(item.key)}">${escapeHtml(item.label)}</option>`).join("")
        : `<option value="">暂无 episode</option>`;
      episodeSelect.value = state.selected_key || "";
      if (episodeSelect.selectedIndex < 0 && reviewEpisodes.length) episodeSelect.selectedIndex = 0;
      episodeSelect.disabled = !reviewEpisodes.length;
      $("embeddedPrevEpisodeBtn").disabled = !reviewEpisodes.length || episodeSelect.selectedIndex <= 0;
      $("embeddedNextEpisodeBtn").disabled = !reviewEpisodes.length || episodeSelect.selectedIndex >= reviewEpisodes.length - 1;

      const review = state.review || {};
      $("embeddedReviewGrade").value = review.quality_grade || "";
      $("embeddedReviewExclude").checked = Boolean(review.exclude);
      if (document.activeElement !== $("embeddedReviewReason")) $("embeddedReviewReason").value = review.reason || "";
      for (const control of [$("embeddedReviewGrade"), $("embeddedReviewExclude"), $("embeddedReviewReason")]) control.disabled = !state.selected_key;
      $("embeddedPendingCount").textContent = `${Number(state.pending_count || 0)} 项待处理`;
      $("embeddedApplyReviewBtn").disabled = Boolean(state.apply_disabled);
      $("embeddedClearReviewBtn").disabled = Number(state.pending_count || 0) === 0;
      $("embeddedReviewStatus").textContent = state.status_text || "";
    }

    function sendEmbeddedReviewChange() {
      sendEmbeddedReview("change", {
        quality_grade: $("embeddedReviewGrade").value,
        exclude: $("embeddedReviewExclude").checked,
        reason: $("embeddedReviewReason").value.trim(),
      });
    }

    function bindEmbeddedReviewControls() {
      if (!embeddedReview) return;
      window.addEventListener("message", event => {
        if (event.origin !== location.origin || event.source !== window.parent) return;
        if (event.data?.type === "lerobot-review-state") applyEmbeddedReviewState(event.data);
      });
      $("embeddedDatasetSelect").addEventListener("change", event => sendEmbeddedReview("dataset", {dataset_id:event.target.value}));
      $("embeddedEpisodeSelect").addEventListener("change", event => sendEmbeddedReview("select", {key:event.target.value}));
      $("embeddedPrevEpisodeBtn").addEventListener("click", () => {
        const select = $("embeddedEpisodeSelect");
        if (select.selectedIndex <= 0) return;
        select.selectedIndex -= 1;
        sendEmbeddedReview("select", {key:select.value});
      });
      $("embeddedNextEpisodeBtn").addEventListener("click", () => {
        const select = $("embeddedEpisodeSelect");
        if (select.selectedIndex < 0 || select.selectedIndex >= select.options.length - 1) return;
        select.selectedIndex += 1;
        sendEmbeddedReview("select", {key:select.value});
      });
      $("embeddedReviewGrade").addEventListener("change", sendEmbeddedReviewChange);
      $("embeddedReviewExclude").addEventListener("change", sendEmbeddedReviewChange);
      $("embeddedReviewReason").addEventListener("input", () => {
        clearTimeout(embeddedReasonTimer);
        embeddedReasonTimer = setTimeout(sendEmbeddedReviewChange, 120);
      });
      $("embeddedClearReviewBtn").addEventListener("click", () => sendEmbeddedReview("clear"));
      $("embeddedApplyReviewBtn").addEventListener("click", () => sendEmbeddedReview("apply"));
      sendEmbeddedReview("ready");
    }

    function videoUrl(rel) {
      const gradeQuery = selectedGrade ? `?grade=${encodeURIComponent(selectedGrade)}` : "";
      return `/lerobot-video/${encodeURIComponent(key)}/${String(rel).split("/").map(encodeURIComponent).join("/")}${gradeQuery}`;
    }

    function basename(path) {
      return String(path || "").split("/").filter(Boolean).slice(-1)[0] || "";
    }

    function displayVideoKey(rawKey) {
      return String(rawKey || "").replace(/^observation\.images\./, "");
    }

    function videoOrderValue(info) {
      const order = {
        head_color: 0,
        hand_left_color: 1,
        hand_right_color: 2,
        head: 3,
      };
      return order[displayVideoKey(info.key)] ?? 99;
    }

    function timestampForFrame(frame) {
      if (!data || !data.timestamps.length) return 0;
      return data.timestamps[Math.max(0, Math.min(frame, data.timestamps.length - 1))] || 0;
    }

    function createVideoCards(videoInfos) {
      const root = $("videoGrid");
      videos = [];
      root.innerHTML = "";
      if (!videoInfos.length) {
        root.innerHTML = `<div class="card"><div class="card-head"><span>video</span><span>missing</span></div></div>`;
        return;
      }
      const sorted = [...videoInfos].sort((a, b) => {
        const orderDiff = videoOrderValue(a) - videoOrderValue(b);
        if (orderDiff) return orderDiff;
        return displayVideoKey(a.key).localeCompare(displayVideoKey(b.key), undefined, {numeric: true});
      });
      const top = sorted.find(v => displayVideoKey(v.key) === "head_color") || sorted[0];
      const rest = sorted.filter(v => v !== top);

      root.appendChild(makeVideoCard(top).card);
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
      head.innerHTML = `<span>${escapeHtml(displayVideoKey(info.key))}</span><span>${escapeHtml(basename(info.file))}</span>`;
      const video = document.createElement("video");
      video.src = info.url;
      video.muted = true;
      video.playsInline = true;
      video.preload = "auto";
      video.addEventListener("loadedmetadata", () => seekVideo(video, currentFrame));
      card.appendChild(head);
      card.appendChild(video);
      videos.push(video);
      return { card, video };
    }

    function seekVideo(video, frame) {
      const time = timestampForFrame(frame);
      if (Number.isFinite(video.duration)) {
        video.currentTime = Math.min(time, Math.max(video.duration - 0.001, 0));
      } else {
        video.currentTime = time;
      }
    }

    async function loadEpisode(index) {
      pause();
      currentFrame = 0;
      const gradeQuery = selectedGrade ? `&grade=${encodeURIComponent(selectedGrade)}` : "";
      const res = await fetch(`/api/lerobot-replay/episode?key=${encodeURIComponent(key)}&episode_index=${index}${gradeQuery}`);
      if (!res.ok) throw new Error(`load LeRobot episode failed: ${res.status}`);
      const payload = await res.json();
      if (payload.error) throw new Error(payload.error);
      const ep = payload.episode || {};
      data = {
        ...payload,
        fps: Number(payload.dataset?.info?.fps || 20),
        frame_count: (payload.frames || []).length,
        timestamps: (payload.frames || []).map(frame => Number(frame.timestamp || 0)),
        state_joint_position: (payload.frames || []).map(frame => frame.state || []),
        action_joint_position: (payload.frames || []).map(frame => frame.actions || []),
        frame_tasks: (payload.frames || []).map(frame => frame.task || ""),
        frame_subtasks: (payload.frames || []).map(frame => frame.subtask || ""),
      };
      $("episodeSelect").value = String(ep.episode_index);
      updateEpisodeButtons();
      $("title").textContent = "LeRobot Episode Replay";
      $("titleEpisode").textContent = ep.episode_name ? `· ${ep.episode_name}` : "";
      $("dataMetric").textContent = ep.source_episode_name || ep.episode_name || "";
      const tasks = Array.isArray(ep.tasks) && ep.tasks.length ? ep.tasks : payload.dataset?.tasks || [];
      $("taskText").textContent = ep.task || data.frame_tasks[0] || tasks.join(", ") || "未提供";
      $("taskText").title = $("taskText").textContent;
      $("subtaskText").textContent = data.frame_subtasks[0] || "未命中";
      $("subtaskText").title = $("subtaskText").textContent;
      $("qualityGradeText").textContent = ep.quality_grade || "未标注";
      $("qualityGradeText").title = $("qualityGradeText").textContent;
      $("qualityDescriptionText").textContent = ep.quality_description || "";
      $("qualityDescriptionText").title = $("qualityDescriptionText").textContent;
      $("frameSlider").max = String(Math.max(0, data.frame_count - 1));
      const videoInfos = Object.entries(ep.video_files || {}).map(([key, file]) => ({
        key,
        file,
        url: videoUrl(file),
      }));
      createVideoCards(videoInfos);
      seekToFrame(0);
      sendVisualizationEpisode(ep);
    }

    function updateEpisodeButtons() {
      const select = $("episodeSelect");
      const prev = $("prevEpisodeBtn");
      const next = $("nextEpisodeBtn");
      if (!select || !prev || !next) return;
      prev.disabled = select.selectedIndex <= 0;
      next.disabled = select.selectedIndex < 0 || select.selectedIndex >= select.options.length - 1;
    }

    function loadAdjacentEpisode(delta) {
      const select = $("episodeSelect");
      if (!select || !select.options.length) return;
      const nextIndex = Math.max(0, Math.min(select.options.length - 1, select.selectedIndex + delta));
      if (nextIndex === select.selectedIndex) return;
      loadEpisode(Number(select.options[nextIndex].value));
    }

    function seekToFrame(frame) {
      if (!data) return;
      currentFrame = Math.max(0, Math.min(data.frame_count - 1, Math.round(frame)));
      for (const video of videos) seekVideo(video, currentFrame);
      updateInfo();
      drawChart();
    }

    function syncFrameFromVideo() {
      if (!data || !videos.length) return;
      const time = videos[0].currentTime || 0;
      let frame = 0;
      let best = Infinity;
      for (let i = 0; i < data.timestamps.length; i++) {
        const diff = Math.abs(data.timestamps[i] - time);
        if (diff < best) {
          best = diff;
          frame = i;
        }
      }
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
      const ts = timestampForFrame(currentFrame);
      $("frameMetric").textContent = `${currentFrame + 1} / ${data.frame_count}`;
      $("timeMetric").textContent = `${formatNumber(ts, 3)}s`;
      $("fpsMetric").textContent = `${formatNumber(data.fps, 2)}`;
      const integrityLabel = data.integrity?.verified ? "已校验 · " : "";
      $("stateLabel").textContent = `${integrityLabel}LeRobot ${data.state_dim}D state / ${data.action_dim}D action`;
      $("taskText").textContent = data.frame_tasks[currentFrame] || data.episode?.task || "未提供";
      $("taskText").title = $("taskText").textContent;
      $("subtaskText").textContent = data.frame_subtasks[currentFrame] || "未命中";
      $("subtaskText").title = $("subtaskText").textContent;
      $("qualityGradeText").textContent = data.episode?.quality_grade || "未标注";
      $("qualityGradeText").title = $("qualityGradeText").textContent;
      $("qualityDescriptionText").textContent = data.episode?.quality_description || "";
      $("qualityDescriptionText").title = $("qualityDescriptionText").textContent;
      renderJointTable();
    }

    const ALOHA_JOINT_LABELS = [
      "left_joint_1", "left_joint_2", "left_joint_3", "left_joint_4", "left_joint_5", "left_joint_6",
      "left_gripper",
      "right_joint_1", "right_joint_2", "right_joint_3", "right_joint_4", "right_joint_5", "right_joint_6",
      "right_gripper",
    ];
    const ALOHA_EXTRA_ROWS = [
      ["waist_height", 14, 14],
      ["base_x", 15, null],
      ["base_y", 16, null],
      ["base_yaw", 17, null],
      ["base_vx", 18, 15],
      ["base_vy", 19, 16],
      ["base_wz", 20, 17],
    ];

    function alohaRowDefinitions() {
      const rows = [];
      const jointCount = Math.min(ALOHA_JOINT_LABELS.length, Math.max(data.state_dim || 0, data.action_dim || 0));
      for (let i = 0; i < jointCount; i++) {
        rows.push({
          label: ALOHA_JOINT_LABELS[i],
          state_index: i < data.state_dim ? i : null,
          action_index: i < data.action_dim ? i : null,
          diff: i < data.state_dim && i < data.action_dim,
        });
      }
      for (const [label, stateIndex, actionIndex] of ALOHA_EXTRA_ROWS) {
        const hasState = stateIndex !== null && stateIndex < data.state_dim;
        const hasAction = actionIndex !== null && actionIndex < data.action_dim;
        if (!hasState && !hasAction) continue;
        rows.push({
          label,
          state_index: hasState ? stateIndex : null,
          action_index: hasAction ? actionIndex : null,
          diff: hasState && hasAction,
        });
      }
      return rows;
    }

    function rowDefinitions() {
      if (!data) return [];
      if (
        (data.state_dim === 14 && data.action_dim === 14)
        || (data.state_dim === 21 && data.action_dim === 18)
      ) {
        return alohaRowDefinitions();
      }
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
      const value = Number(values[idx]);
      return Number.isFinite(value) ? value : undefined;
    }

    function renderJointTable() {
      const state = data.state_joint_position[currentFrame] || [];
      const action = data.action_joint_position[currentFrame] || [];
      const rows = [];
      const definitions = rowDefinitions();
      if (!definitions.length) {
        $("jointRows").innerHTML = `<tr><td colspan="4">no state/action data</td></tr>`;
        return;
      }
      for (const row of definitions) {
        const s = valueAt(state, row.state_index);
        const a = valueAt(action, row.action_index);
        const canDiff = row.diff && s !== undefined && a !== undefined;
        rows.push(`<tr>
          <td>${escapeHtml(row.label)}</td>
          <td>${s === undefined ? "" : formatNumber(s)}</td>
          <td>${a === undefined ? "" : formatNumber(a)}</td>
          <td>${canDiff ? formatNumber(a - s) : ""}</td>
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
      if (!state.length || !action.length || !rows.length) {
        drawChartMessage(ctx, width, height, dpr, "no state/action data");
        return;
      }

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

      const playX = left + currentFrame * xScale;
      ctx.strokeStyle = "#67d391";
      ctx.lineWidth = 1.5 * dpr;
      ctx.beginPath();
      ctx.moveTo(playX, top);
      ctx.lineTo(playX, height - bottom);
      ctx.stroke();
    }

    function drawChartMessage(ctx, width, height, dpr, text) {
      ctx.fillStyle = "#aab4bf";
      ctx.font = `${14 * dpr}px ui-monospace, Consolas, monospace`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(text, width / 2, height / 2);
      ctx.textAlign = "left";
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

    function renderGradeSelect(summary) {
      const gradeSelect = $("gradeSelect");
      const grades = Array.isArray(summary.available_grades) ? summary.available_grades : [];
      selectedGrade = String(summary.selected_grade || selectedGrade || "");
      if (!grades.length) {
        gradeSelect.innerHTML = `<option value="">全部</option>`;
        gradeSelect.value = "";
        gradeSelect.disabled = true;
        return;
      }
      gradeSelect.disabled = false;
      gradeSelect.innerHTML = grades.map(item => {
        const grade = String(item.grade || "");
        const label = grade || "全部";
        const count = Number(item.episode_count || 0);
        return `<option value="${escapeHtml(grade)}">${escapeHtml(label)} · ${count}条</option>`;
      }).join("");
      gradeSelect.value = selectedGrade;
      if (gradeSelect.selectedIndex < 0) {
        gradeSelect.selectedIndex = 0;
        selectedGrade = gradeSelect.value;
      }
    }

    async function loadEpisodes(preferredEpisodeIndex = null) {
      const gradeQuery = selectedGrade ? `&grade=${encodeURIComponent(selectedGrade)}` : "";
      const recordsRes = await fetch(`/api/lerobot-replay/episodes?key=${encodeURIComponent(key)}${gradeQuery}`);
      if (!recordsRes.ok) throw new Error(`load LeRobot episodes failed: ${recordsRes.status}`);
      const recordsPayload = await recordsRes.json();
      if (recordsPayload.error) throw new Error(recordsPayload.error);
      renderGradeSelect(recordsPayload);
      episodes = recordsPayload.episodes || [];
      const select = $("episodeSelect");
      select.innerHTML = episodes.map(ep => {
        return `<option value="${ep.episode_index}">${escapeHtml(ep.episode_name)}</option>`;
      }).join("");
      updateEpisodeButtons();
      if (!episodes.length) {
        pause();
        data = null;
        createVideoCards([]);
        $("title").textContent = "LeRobot Episode Replay";
        $("stateLabel").textContent = "";
        $("jointRows").innerHTML = `<tr><td colspan="4">no episodes</td></tr>`;
        sendVisualizationEpisode(null);
        drawChart();
        return;
      }
      let target = preferredEpisodeIndex;
      if (target === null || !episodes.some(ep => Number(ep.episode_index) === Number(target))) {
        target = Number(episodes[0].episode_index);
      }
      await loadEpisode(Number(target));
    }

    async function boot() {
      bindEmbeddedReviewControls();
      await loadEpisodes(Number.isInteger(initialEpisodeIndex) ? initialEpisodeIndex : null);
      $("episodeSelect").addEventListener("change", (e) => loadEpisode(Number(e.target.value)));
      $("gradeSelect").addEventListener("change", async (e) => {
        selectedGrade = String(e.target.value || "");
        await loadEpisodes();
      });

      $("prevEpisodeBtn").addEventListener("click", () => loadAdjacentEpisode(-1));
      $("nextEpisodeBtn").addEventListener("click", () => loadAdjacentEpisode(1));
      $("playBtn").addEventListener("click", () => playing ? pause() : play());
      $("prevBtn").addEventListener("click", () => { pause(); seekToFrame(currentFrame - 1); });
      $("nextBtn").addEventListener("click", () => { pause(); seekToFrame(currentFrame + 1); });
      $("frameSlider").addEventListener("input", (e) => { pause(); seekToFrame(Number(e.target.value)); });
      $("speedSelect").addEventListener("change", () => {
        const rate = Number($("speedSelect").value);
        for (const video of videos) video.playbackRate = rate;
      });
      window.addEventListener("resize", drawChart);
      window.addEventListener("message", event => {
        if (event.origin !== location.origin || event.source !== window.parent) return;
        if (event.data?.type !== "lerobot-visualization-select-episode") return;
        const episodeIndex = Number(event.data.episode_index);
        if (
          Number.isInteger(episodeIndex)
          && episodes.some(item => Number(item.episode_index) === episodeIndex)
        ) loadEpisode(episodeIndex);
      });
      if ("ResizeObserver" in window) {
        new ResizeObserver(() => drawChart()).observe($("chart"));
      }
      requestAnimationFrame(drawChart);
      setTimeout(drawChart, 250);
      document.addEventListener("keydown", (e) => {
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


LEROBOT_BROWSER_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>LeRobot 数据集回放</title>
  <style>
    :root {
      --bg: #f6f7f9;
      --panel: #ffffff;
      --line: #d9dee7;
      --text: #17202a;
      --muted: #657184;
      --accent: #0f766e;
      --danger: #b42318;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background: var(--bg);
      letter-spacing: 0;
    }
    header {
      height: 56px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 20px;
      border-bottom: 1px solid var(--line);
      background: #fff;
    }
    header h1 { font-size: 18px; margin: 0; font-weight: 700; }
    main {
      display: grid;
      grid-template-columns: minmax(300px, 360px) minmax(0, 1fr);
      gap: 12px;
      padding: 12px;
      min-height: calc(100vh - 56px);
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      min-width: 0;
    }
    h2 { font-size: 15px; margin: 0 0 12px; }
    label { display: block; font-size: 12px; color: var(--muted); margin: 12px 0 4px; }
    input, select {
      width: 100%;
      min-height: 36px;
      border: 1px solid #cbd3df;
      border-radius: 6px;
      padding: 7px 9px;
      font-size: 13px;
      background: #fff;
      color: var(--text);
    }
    button {
      min-height: 36px;
      border: 1px solid #b8c2d0;
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      font-size: 13px;
      cursor: pointer;
    }
    button.primary { width: 100%; margin-top: 14px; background: var(--accent); border-color: var(--accent); color: white; }
    button:disabled { opacity: .55; cursor: wait; }
    .row { display: grid; grid-template-columns: 1fr auto; gap: 8px; align-items: end; }
    .row button { min-width: 72px; }
    .hidden { display: none !important; }
    .hint {
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }
    .log {
      margin-top: 14px;
      min-height: 120px;
      max-height: 240px;
      overflow: auto;
      background: #111827;
      color: #d1fae5;
      border-radius: 8px;
      padding: 10px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12px;
      white-space: pre-wrap;
    }
    .viewer {
      padding: 8px;
      display: grid;
      grid-template-rows: minmax(0, 1fr);
    }
    iframe {
      width: 100%;
      height: calc(100vh - 90px);
      min-height: 820px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }
    @media (max-width: 980px) {
      main { grid-template-columns: 1fr; }
      iframe { height: 86vh; min-height: 720px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>LeRobot 数据集回放</h1>
    <button id="refreshBtn">刷新列表</button>
  </header>
  <main>
    <section>
      <h2>回放数据</h2>
      <label>LeRobot 父级目录</label>
      <div class="row">
        <input id="parentDir" autocomplete="off" placeholder="例如 data/lerobot" />
        <button id="scanBtn" type="button">扫描</button>
      </div>
      <div class="hint">目录结构示例：父级目录 / dataset / A、B、F。</div>

      <label>Dataset</label>
      <select id="datasetSelect">
        <option value="">请先扫描父级目录</option>
      </select>

      <label>质量等级</label>
      <select id="gradeSelect">
        <option value="">请先选择 dataset</option>
      </select>

      <button id="openBtn" class="primary" type="button">打开回放</button>
      <div id="log" class="log">填写 LeRobot 父级目录后点击扫描。</div>
    </section>
    <section class="viewer">
      <iframe id="replayFrame"></iframe>
    </section>
  </main>
  <script>
    let datasets = [];

    function $(id) {
      return document.getElementById(id);
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, ch => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      }[ch]));
    }

    async function postJson(url, data) {
      const res = await fetch(url, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(data),
      });
      const json = await res.json();
      if (!res.ok || json.error) throw new Error(json.error || res.statusText);
      return json;
    }

    function selectedDataset() {
      return datasets[Number($("datasetSelect").value)] || null;
    }

    function selectedGrade() {
      const dataset = selectedDataset();
      if (!dataset) return null;
      return (dataset.grades || [])[Number($("gradeSelect").value)] || null;
    }

    function renderGradeSelect() {
      const dataset = selectedDataset();
      const grades = dataset?.grades || [];
      $("gradeSelect").innerHTML = grades.length
        ? grades.map((item, index) => {
            const count = Number.isFinite(Number(item.episode_count)) ? ` · ${item.episode_count}条` : "";
            return `<option value="${index}">${escapeHtml(item.label || item.grade || "未分级")}${count}</option>`;
          }).join("")
        : `<option value="">没有可用等级</option>`;
      const grade = selectedGrade();
      if (grade) {
        $("log").textContent = `已选择: ${dataset.name} / ${grade.label}\n${grade.path}`;
      }
    }

    function renderDatasetSelect() {
      $("datasetSelect").innerHTML = datasets.length
        ? datasets.map((item, index) => {
            const grades = Array.isArray(item.grades) ? item.grades.map(grade => grade.label || grade.grade || "未分级").join("/") : "";
            const count = Number.isFinite(Number(item.episode_count)) ? ` · ${item.episode_count}条` : "";
            return `<option value="${index}">${escapeHtml(item.name)}${count}${grades ? ` · ${escapeHtml(grades)}` : ""}</option>`;
          }).join("")
        : `<option value="">未找到 dataset</option>`;
      $("datasetSelect").value = datasets.length ? "0" : "";
      renderGradeSelect();
    }

    async function scanDatasets() {
      const parent = String($("parentDir").value || "").trim();
      if (!parent) {
        $("log").textContent = "请先填写 LeRobot 父级目录。";
        return;
      }
      $("scanBtn").disabled = true;
      $("refreshBtn").disabled = true;
      try {
        const res = await fetch(`/api/lerobot-browser/datasets?parent=${encodeURIComponent(parent)}`);
        const data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error || res.statusText);
        datasets = Array.isArray(data.datasets) ? data.datasets : [];
        renderDatasetSelect();
        $("log").textContent = datasets.length
          ? `扫描完成: ${data.parent}\n发现 ${datasets.length} 个 dataset。`
          : `没有发现 LeRobot dataset: ${data.parent}`;
      } catch (err) {
        datasets = [];
        renderDatasetSelect();
        $("log").textContent = String(err);
      } finally {
        $("scanBtn").disabled = false;
        $("refreshBtn").disabled = false;
      }
    }

    async function openReplay() {
      const dataset = selectedDataset();
      const grade = selectedGrade();
      if (!dataset || !grade) {
        $("log").textContent = "请先选择 dataset 和质量等级。";
        return;
      }
      $("openBtn").disabled = true;
      try {
        const data = await postJson("/api/lerobot-replay/start", {
          robot_type: "aloha",
          dataset_name: dataset.name,
          lerobot_root: grade.lerobot_root,
          repo_id: grade.repo_id,
        });
        $("replayFrame").src = data.url || "/lerobot-replay/";
        $("log").textContent = `LeRobot 回放已启动: ${dataset.name} / ${grade.label}\n${grade.path}`;
      } catch (err) {
        $("replayFrame").removeAttribute("src");
        $("log").textContent = String(err);
      } finally {
        $("openBtn").disabled = false;
      }
    }

    $("scanBtn").addEventListener("click", scanDatasets);
    $("refreshBtn").addEventListener("click", scanDatasets);
    $("parentDir").addEventListener("change", scanDatasets);
    $("datasetSelect").addEventListener("change", renderGradeSelect);
    $("gradeSelect").addEventListener("change", renderGradeSelect);
    $("openBtn").addEventListener("click", openReplay);
  </script>
</body>
</html>
"""


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>采集数据质检与转换</title>
  <style>
    :root {
      --bg: #eef4ff;
      --panel: #ffffff;
      --line: #dbe5f4;
      --text: #15233b;
      --muted: #66758c;
      --accent: #2563eb;
      --accent-strong: #1d4ed8;
      --accent-soft: #eaf1ff;
      --violet: #7c3aed;
      --danger: #b42318;
      --warn: #a15c07;
      --ok: #087443;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at 4% 0%, rgba(37, 99, 235, .12), transparent 28rem),
        radial-gradient(circle at 96% 10%, rgba(124, 58, 237, .08), transparent 24rem),
        var(--bg);
      letter-spacing: 0;
    }
    header {
      min-height: 72px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding: 10px 20px;
      border-bottom: 1px solid rgba(219, 229, 244, .9);
      background: rgba(255, 255, 255, .92);
      backdrop-filter: blur(16px);
      position: sticky;
      top: 0;
      z-index: 20;
      box-shadow: 0 4px 18px rgba(30, 64, 175, .06);
    }
    header h1 { font-size: 18px; margin: 0; font-weight: 760; }
    .brand { min-width: 220px; }
    .brand-kicker {
      margin-top: 3px;
      color: var(--muted);
      font-size: 11px;
      letter-spacing: .06em;
      text-transform: uppercase;
    }
    .workspace-switch {
      display: flex;
      align-items: center;
      gap: 5px;
      padding: 5px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #f5f8fd;
    }
    .workspace-btn {
      min-height: 36px;
      padding: 0 16px;
      border: 0;
      background: transparent;
      color: var(--muted);
      font-weight: 650;
    }
    .workspace-btn.active {
      color: #fff;
      background: linear-gradient(135deg, var(--accent), var(--violet));
      box-shadow: 0 5px 14px rgba(37, 99, 235, .24);
    }
    .header-actions { display: flex; align-items: center; gap: 9px; min-width: 220px; justify-content: flex-end; }
    .server-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--ok);
      background: #ecfdf3;
      border: 1px solid #abefc6;
      border-radius: 999px;
      padding: 5px 9px;
      font-size: 11px;
      white-space: nowrap;
    }
    .server-pill::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: #12b76a; }
    main {
      display: grid;
      grid-template-columns: minmax(350px, 430px) minmax(560px, 1fr);
      gap: 16px;
      padding: 18px;
      max-width: 1920px;
      margin: 0 auto;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 16px;
      box-shadow: 0 10px 28px rgba(30, 64, 175, .07);
    }
    .control-panel { align-self: start; }
    h2 { font-size: 15px; margin: 0 0 12px; }
    label { display: block; font-size: 12px; color: var(--muted); margin: 10px 0 4px; }
    input, select {
      width: 100%;
      min-height: 34px;
      border: 1px solid #cbd3df;
      border-radius: 8px;
      padding: 7px 9px;
      font-size: 13px;
      background: #fff;
      color: var(--text);
    }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .hidden { display: none !important; }
    .checks { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 10px; }
    .checks label { display: flex; align-items: center; gap: 8px; margin: 0; color: var(--text); }
    .checks label.disabled { color: var(--muted); opacity: .65; }
    .checks input { width: auto; min-height: auto; }
    .actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 10px; }
    button {
      min-height: 36px;
      border: 1px solid #b8c2d0;
      border-radius: 8px;
      background: #fff;
      color: var(--text);
      font-size: 13px;
      cursor: pointer;
    }
    button.primary { background: var(--accent); border-color: var(--accent); color: white; }
    button.primary:hover { background: var(--accent-strong); border-color: var(--accent-strong); }
    button.danger { background: var(--danger); border-color: var(--danger); color: white; }
    button:disabled { opacity: .55; cursor: wait; }
    .section-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 10px;
    }
    .section-title h2 { margin: 0; }
    .step-badge {
      color: var(--accent);
      background: var(--accent-soft);
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 11px;
      font-weight: 700;
    }
    .hint, .field-note {
      margin-top: 6px;
      color: var(--muted);
      font-size: 11px;
      line-height: 1.45;
    }
    .custom-dataset {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      margin-top: 8px;
      padding: 10px;
      border: 1px dashed #a8bce0;
      border-radius: 10px;
      background: #f8faff;
    }
    .custom-dataset button { padding: 0 12px; }
    .dataset-paths {
      display: grid;
      gap: 0;
      margin-top: 12px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #f8faff;
      overflow: hidden;
    }
    .dataset-path-row {
      display: grid;
      grid-template-columns: 112px minmax(0, 1fr);
      gap: 10px;
      padding: 8px 10px;
      border-bottom: 1px solid #e8eef8;
      align-items: start;
    }
    .dataset-path-row:last-child { border-bottom: 0; }
    .dataset-path-label { color: var(--muted); font-size: 11px; font-weight: 650; }
    .dataset-path-value {
      color: #334155;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 11px;
      line-height: 1.45;
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }
    .task-field {
      margin-top: 13px;
      padding: 12px;
      border: 1px solid #c7d8f6;
      border-radius: 10px;
      background: linear-gradient(135deg, #f7faff, #fbf8ff);
    }
    .task-field label { margin-top: 0; color: #334155; font-weight: 700; }
    .task-field input.auto-value { border-color: #93b4ed; background: #fff; }
    .action-card {
      margin-top: 12px;
      padding: 12px;
      border: 1px solid #c9d9f5;
      border-radius: 11px;
      background: linear-gradient(145deg, #f6f9ff, #fff);
    }
    .action-card h3 { margin: 0; font-size: 13px; }
    .action-card .hint { margin: 3px 0 8px; }
    .stationary-threshold-control {
      display: grid;
      grid-template-columns: minmax(150px, .7fr) minmax(0, 1.3fr);
      gap: 8px 12px;
      align-items: end;
      margin: 9px 0 2px;
      padding: 9px 10px;
      border: 1px solid #d7e2f5;
      border-radius: 9px;
      background: #fff;
    }
    .stationary-threshold-control label { margin: 0 0 4px; font-weight: 700; color: #334155; }
    .stationary-threshold-control .field-note { margin: 0; align-self: center; }
    .lerobot-stage-split-control {
      display: grid;
      gap: 7px;
      margin: 9px 0 2px;
      padding: 9px 10px;
      border: 1px solid #c6d8f7;
      border-radius: 9px;
      background: #f8faff;
    }
    .lerobot-stage-split-control button { width: 100%; }
    .lerobot-stage-split-reason {
      color: #526277;
      font-size: 11px;
      line-height: 1.45;
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }
    .lerobot-stage-split-reason.unavailable { color: #a33a32; }
    .lerobot-stage-split-paths {
      color: #526277;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 10px;
      line-height: 1.45;
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }
    .secondary-actions { margin-top: 8px; }
    details.advanced-settings {
      margin-top: 14px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #fff;
      overflow: hidden;
    }
    details.advanced-settings > summary {
      padding: 11px 12px;
      color: #344054;
      background: #f8fafc;
      cursor: pointer;
      font-size: 13px;
      font-weight: 700;
      user-select: none;
    }
    .advanced-body { padding: 0 12px 12px; }
    .workspace-placeholder {
      max-width: 980px;
      margin: 52px auto;
      padding: 42px;
      text-align: center;
      border: 1px solid #d8d2ff;
      border-radius: 18px;
      background: rgba(255, 255, 255, .94);
      box-shadow: 0 18px 48px rgba(91, 33, 182, .12);
    }
    .placeholder-icon {
      width: 58px;
      height: 58px;
      display: grid;
      place-items: center;
      margin: 0 auto 16px;
      border-radius: 16px;
      color: #fff;
      background: linear-gradient(135deg, var(--accent), var(--violet));
      font-size: 25px;
      font-weight: 800;
    }
    .workspace-placeholder h2 { margin-bottom: 8px; font-size: 22px; }
    .workspace-placeholder p { max-width: 640px; margin: 0 auto; color: var(--muted); line-height: 1.7; }
    .cross-platform-workspace {
      width: 100%;
      padding: 0;
      background: #f3f6fa;
    }
    .cross-platform-workspace iframe {
      display: block;
      width: 100%;
      height: calc(100vh - 74px);
      min-height: 760px;
      border: 0;
      border-radius: 0;
    }
    .path-table {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      overflow: hidden;
      background: #f8fafc;
      font-size: 12px;
    }
    .path-table table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }
    .path-table th,
    .path-table td {
      padding: 7px 8px;
      border-bottom: 1px solid #e5e9f0;
      vertical-align: top;
    }
    .path-table th {
      width: 96px;
      color: var(--muted);
      background: #f1f4f8;
      font-weight: 600;
      text-align: left;
    }
    .path-table td {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      overflow-wrap: anywhere;
      white-space: pre-wrap;
      line-height: 1.4;
    }
    .path-table tr:last-child th,
    .path-table tr:last-child td {
      border-bottom: 0;
    }
    .grid { display: grid; grid-template-rows: auto auto; gap: 16px; }
    body.replay-mode main {
      min-height: calc(100vh - 56px);
      grid-template-columns: minmax(260px, 320px) minmax(0, 1fr);
      gap: 10px;
      padding: 10px;
    }
    body.replay-mode .grid {
      grid-template-rows: minmax(0, 1fr);
      min-height: calc(100vh - 76px);
    }
    body.replay-mode .grid > section:last-child { display: none; }
    body.replay-mode #overviewPanel,
    body.replay-mode #qcPanel,
    body.replay-mode #mappingPanel,
    body.replay-mode .metrics { display: none; }
    body.replay-mode #replayPanel { margin-top: 8px; }
    table { width: 100%; border-collapse: collapse; font-size: 12px; }
    th, td { border-bottom: 1px solid #e5e9f0; text-align: left; padding: 7px 6px; vertical-align: top; }
    th { color: var(--muted); font-weight: 600; background: #fafbfc; }
    .badge { display: inline-flex; align-items: center; min-height: 22px; padding: 2px 8px; border-radius: 999px; font-size: 12px; border: 1px solid var(--line); }
    .badge.ok { color: var(--ok); background: #ecfdf3; border-color: #abefc6; }
    .badge.warn { color: var(--warn); background: #fffaeb; border-color: #fedf89; }
    .badge.bad { color: var(--danger); background: #fef3f2; border-color: #fecdca; }
    .quality-grade-select {
      width: 58px;
      min-height: 28px;
      padding: 2px 6px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      font-size: 12px;
      font-weight: 700;
    }
    .metrics { display: grid; grid-template-columns: repeat(5, minmax(90px, 1fr)); gap: 8px; }
    .metric { border: 1px solid var(--line); border-radius: 8px; padding: 9px; background: #fff; }
    .metric div:first-child { font-size: 12px; color: var(--muted); }
    .metric div:last-child { font-size: 18px; font-weight: 700; margin-top: 4px; }
    .qc-overview {
      margin-top: 12px;
      margin-bottom: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      overflow: hidden;
    }
    .qc-overview-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      border-bottom: 1px solid #e5e9f0;
      background: #fafbfc;
      color: var(--muted);
      font-size: 12px;
    }
    .qc-overview-head strong { color: var(--text); font-size: 14px; }
    .overview-kpis {
      display: grid;
      grid-template-columns: repeat(5, minmax(100px, 1fr));
      gap: 8px;
      padding: 10px;
      border-bottom: 1px solid #e5e9f0;
    }
    .overview-kpi {
      border: 1px solid #e5e9f0;
      border-radius: 6px;
      padding: 8px;
      min-width: 0;
      background: #fcfcfd;
    }
    .overview-kpi .label { color: var(--muted); font-size: 12px; }
    .overview-kpi .value {
      margin-top: 4px;
      font-size: 16px;
      font-weight: 700;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .overview-charts {
      display: grid;
      grid-template-columns: repeat(8, minmax(0, 1fr));
      gap: 10px;
      padding: 10px;
    }
    .overview-chart {
      grid-column: span 4;
      min-width: 0;
      border: 1px solid #e5e9f0;
      border-radius: 6px;
      padding: 8px;
      background: #fff;
    }
    .overview-chart.pie-chart {
      grid-column: span 2;
    }
    .overview-chart h3 {
      margin: 0 0 8px;
      font-size: 13px;
      line-height: 1.2;
    }
    .bar-chart {
      min-width: 0;
    }
    .bar-chart-meta {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 6px;
      color: var(--muted);
      font-size: 11px;
    }
    .bar-body {
      display: grid;
      grid-template-columns: 52px minmax(0, 1fr);
      gap: 6px;
      min-width: 0;
    }
    .bar-y-axis {
      height: 260px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      align-items: flex-end;
      box-sizing: border-box;
      padding: 10px 0 100px;
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 10px;
      line-height: 1;
      text-align: right;
    }
    .bar-plot {
      position: relative;
      height: 260px;
      overflow-x: auto;
      overflow-y: hidden;
      border: 1px solid #e5e9f0;
      border-radius: 6px;
      background:
        linear-gradient(to top, #e5e9f0 1px, transparent 1px) 0 0 / 100% 25%,
        #fcfcfd;
    }
    .bar-items {
      display: flex;
      align-items: stretch;
      gap: 4px;
      height: 100%;
      min-width: 100%;
      padding: 10px 6px 6px;
      box-sizing: border-box;
    }
    .bar-item {
      flex: 0 0 var(--bar-width, 48px);
      display: grid;
      grid-template-rows: minmax(0, 1fr) 92px;
      gap: 3px;
      min-width: 0;
    }
    .bar-column {
      position: relative;
      min-height: 0;
      height: 100%;
    }
    .bar-value {
      width: 100%;
      overflow: hidden;
      text-overflow: clip;
      white-space: nowrap;
      color: #475467;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 10px;
      line-height: 1;
      text-align: center;
    }
    .bar-stack {
      position: absolute;
      left: 0;
      right: 0;
      bottom: 0;
      display: grid;
      grid-template-rows: 12px minmax(2px, 1fr);
      justify-items: center;
      gap: 2px;
      height: var(--bar-height, 0%);
      min-height: 16px;
    }
    .bar-fill-wrap {
      display: flex;
      align-items: flex-end;
      justify-content: center;
      width: 100%;
      height: 100%;
      min-height: 0;
    }
    .bar-fill {
      display: block;
      width: 18px;
      height: 100%;
      min-height: 2px;
      border-radius: 4px 4px 0 0;
      background: #3b82f6;
    }
    .bar-fill.warn { background: #f59e0b; }
    .bar-fill.bad { background: #ef4444; }
    .bar-label {
      overflow: visible;
      text-overflow: clip;
      white-space: nowrap;
      color: #475467;
      font-size: 10px;
      text-align: right;
      line-height: 1.1;
      justify-self: end;
      align-self: start;
      width: max-content;
      max-width: none;
      transform: rotate(-45deg);
      transform-origin: top right;
    }
    .grouped-bar-items {
      display: flex;
      align-items: stretch;
      gap: 6px;
      height: 100%;
      min-width: 100%;
      padding: 10px 6px 6px;
      box-sizing: border-box;
    }
    .grouped-bar-item {
      flex: 0 0 var(--bar-width, 68px);
      display: grid;
      grid-template-rows: minmax(0, 1fr) 92px;
      gap: 3px;
      min-width: 0;
    }
    .grouped-bar-columns {
      display: flex;
      align-items: flex-end;
      justify-content: center;
      gap: 6px;
      min-height: 0;
      height: 100%;
      padding-top: 16px;
      box-sizing: border-box;
    }
    .grouped-bar {
      position: relative;
      display: block;
      width: 16px;
      height: var(--bar-height, 0%);
      min-height: 2px;
      border-radius: 4px 4px 0 0;
      background: #3b82f6;
    }
    .grouped-bar.right { background: #f59e0b; }
    .grouped-bar-value {
      position: absolute;
      left: 50%;
      bottom: calc(100% + 2px);
      transform: translateX(-50%);
      color: #475467;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 10px;
      line-height: 1;
      white-space: nowrap;
    }
    .grouped-legend {
      display: inline-flex;
      gap: 10px;
      align-items: center;
      color: var(--muted);
      font-size: 11px;
    }
    .color-legend {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
      margin: -2px 0 7px;
      color: var(--muted);
      font-size: 12px;
    }
    .color-legend-item {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      white-space: nowrap;
    }
    .legend-swatch {
      width: 10px;
      height: 10px;
      border-radius: 2px;
      display: inline-block;
      box-shadow: inset 0 0 0 1px rgba(16, 24, 40, .12);
    }
    .pie-layout {
      display: grid;
      grid-template-columns: minmax(118px, 150px) minmax(0, 1fr);
      align-items: center;
      gap: 12px;
    }
    .pie-visual {
      position: relative;
      width: min(140px, 100%);
      aspect-ratio: 1;
      border-radius: 50%;
      margin: 0 auto;
      background: #eef2f6;
      box-shadow: inset 0 0 0 1px rgba(16, 24, 40, .08);
    }
    .pie-visual::after {
      content: "";
      position: absolute;
      inset: 30%;
      border-radius: 50%;
      background: #fff;
      box-shadow: 0 0 0 1px rgba(16, 24, 40, .06);
    }
    .pie-legend {
      display: grid;
      gap: 6px;
      min-width: 0;
    }
    .pie-legend-row {
      display: grid;
      grid-template-columns: 10px minmax(0, 1fr) auto;
      align-items: center;
      gap: 6px;
      font-size: 11px;
      color: var(--muted);
      min-height: 18px;
    }
    .pie-dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
    }
    .pie-label {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: #475467;
    }
    .pie-value {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      white-space: nowrap;
      color: #475467;
    }
    .overview-empty {
      padding: 12px;
      color: var(--muted);
      font-size: 12px;
    }
    .tabs {
      display: inline-grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 4px;
      margin-top: 14px;
      padding: 4px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #f1f4f8;
    }
    .tab-btn {
      min-width: 104px;
      min-height: 32px;
      border: 0;
      background: transparent;
      border-radius: 6px;
      color: var(--muted);
    }
    .tab-btn.active {
      background: #fff;
      color: var(--text);
      box-shadow: 0 1px 2px rgba(16, 24, 40, .08);
    }
    .tab-panel { margin-top: 12px; }
    .tab-panel.hidden { display: none; }
    .selection-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      min-height: 32px;
      margin-bottom: 8px;
      color: var(--muted);
      font-size: 12px;
    }
    .selection-bar button {
      min-height: 30px;
      padding: 0 10px;
      font-size: 12px;
    }
    .selection-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .episode-check,
    #selectAllEpisodes {
      width: 16px;
      min-height: 16px;
      padding: 0;
    }
    .table-wrap {
      max-height: 560px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .table-wrap table th { position: sticky; top: 0; z-index: 1; }
    .warning-cell {
      min-width: 260px;
      white-space: pre-wrap;
      line-height: 1.45;
    }
    .quality-check-catalog {
      margin-top: 16px;
      border-top: 1px solid var(--line);
      padding-top: 12px;
    }
    .quality-check-catalog h3 {
      margin: 0 0 10px;
      font-size: 14px;
    }
    .quality-check-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      border-top: 1px solid #e5e9f0;
      border-left: 1px solid #e5e9f0;
    }
    .quality-check-item {
      min-width: 0;
      min-height: 64px;
      padding: 9px 10px;
      border-right: 1px solid #e5e9f0;
      border-bottom: 1px solid #e5e9f0;
      background: #fcfcfd;
    }
    .quality-check-name {
      color: var(--text);
      font-size: 12px;
      font-weight: 700;
    }
    .quality-check-criterion {
      margin-top: 4px;
      color: var(--muted);
      font-size: 11px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }
    .mapping-info {
      margin-bottom: 8px;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .mapping-kpis {
      display: grid;
      grid-template-columns: repeat(5, minmax(90px, 1fr));
      gap: 8px;
      margin-bottom: 10px;
    }
    .mapping-kpis:empty { display: none; }
    .log {
      min-height: 170px;
      max-height: 280px;
      overflow: auto;
      background: #111827;
      color: #d1fae5;
      border-radius: 8px;
      padding: 10px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12px;
      white-space: pre-wrap;
    }
    iframe {
      width: 100%;
      height: 760px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }
    body.replay-mode iframe {
      height: calc(100vh - 120px);
      min-height: 900px;
    }
    @media (max-width: 980px) {
      header { align-items: stretch; flex-direction: column; }
      .workspace-switch { width: 100%; }
      .workspace-btn { flex: 1; }
      .header-actions { min-width: 0; justify-content: space-between; }
      main { grid-template-columns: 1fr; }
      body.replay-mode main { grid-template-columns: 1fr; }
      .metrics { grid-template-columns: repeat(2, 1fr); }
      .overview-kpis { grid-template-columns: repeat(2, 1fr); }
      .mapping-kpis { grid-template-columns: repeat(2, 1fr); }
      .overview-charts { grid-template-columns: 1fr; }
      .overview-chart,
      .overview-chart.pie-chart { grid-column: 1 / -1; }
      .quality-check-grid { grid-template-columns: 1fr; }
      body.replay-mode iframe { height: 86vh; min-height: 760px; }
      .dataset-path-row { grid-template-columns: 1fr; gap: 3px; }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <h1>数据质检工作台</h1>
      <div class="brand-kicker">Embodied Data Quality Console</div>
    </div>
    <nav class="workspace-switch" aria-label="质检模块切换">
      <button id="lerobotVisualizationWorkspaceBtn" class="workspace-btn" type="button">LeRobot 数据可视化</button>
      <button id="collectionWorkspaceBtn" class="workspace-btn active" type="button">采集数据质检与转换</button>
      <button id="lerobotWorkspaceBtn" class="workspace-btn" type="button">跨平台 LeRobot 质检</button>
      <button id="manualScreeningWorkspaceBtn" class="workspace-btn" type="button">数据人工筛查模块</button>
    </nav>
    <div class="header-actions">
      <span class="server-pill">服务端 9988</span>
      <button id="refreshBtn" type="button">刷新状态</button>
    </div>
  </header>
  <div id="lerobotVisualizationWorkspace" class="cross-platform-workspace hidden">
    <iframe id="lerobotVisualizationFrame" src="/lerobot-visualization/" title="LeRobot 数据可视化"></iframe>
  </div>
  <div id="lerobotWorkspace" class="cross-platform-workspace hidden">
    <iframe id="crossPlatformFrame" src="/cross-platform/" title="跨平台 LeRobot 质检"></iframe>
  </div>
  <div id="manualScreeningWorkspace" class="cross-platform-workspace hidden">
    <iframe id="manualScreeningFrame" src="/manual-screening/" title="数据人工筛查模块"></iframe>
  </div>
  <main id="collectionWorkspace">
    <section class="control-panel">
      <div class="section-title">
        <h2>数据集与操作</h2>
        <span class="step-badge">先选数据，再执行</span>
      </div>
      <label>运行机器</label>
      <select id="hostMachine">
        <option value="agilex">AgileX</option>
        <option value="h200" selected>H200</option>
      </select>
      <label>自动扫描数据集</label>
      <select id="sourceDatasetSelect">
        <option value="">正在递归扫描 HDF5 与 MCAP...</option>
      </select>
      <div id="sourceDatasetScanInfo" class="hint">HDF5: /srv/data/datasets/public；MCAP: /mnt/nas/agilex_raw_datasets_mcap/stage2_datasets</div>
      <div id="customDatasetControls" class="custom-dataset hidden">
        <input id="customDatasetPath" autocomplete="off" placeholder="粘贴服务端可访问的数据集目录" />
        <button id="applyCustomDatasetBtn" type="button">载入目录</button>
      </div>
      <div id="mcapDatasetHint" class="hint"></div>
      <select id="mcapDatasetSelect" class="hidden" aria-label="选择 MCAP 子数据集">
        <option value="">请选择 MCAP 数据集</option>
      </select>

      <div id="paths" class="dataset-paths" aria-live="polite">
        <div id="pathRows">
          <div class="dataset-path-row"><span class="dataset-path-label">数据路径</span><span class="dataset-path-value">请选择数据集</span></div>
        </div>
      </div>

      <!-- Core pipeline state stays in the existing payload fields. Paths are derived
           by the Python backend and intentionally shown as read-only text above. -->
      <input id="mcapParentPath" type="hidden" />
      <input id="mcapPath" type="hidden" />
      <datalist id="mcapDatasetOptions"></datalist>
      <input id="datasetName" type="hidden" />
      <input id="hdf5Root" type="hidden" />
      <input id="qcRoot" type="hidden" />
      <input id="lerobotRoot" type="hidden" />

      <div class="task-field">
        <label>任务文本</label>
        <input id="taskText" autocomplete="off" placeholder="仅用于补写缺少任务的 HDF5，不覆盖已有 HDF5 task" />
        <div id="taskTextHint" class="field-note">选中数据集后自动读取 MCAP 任务文本；可人工修改。</div>
      </div>

      <div class="action-card">
        <h3>核心操作</h3>
        <div class="hint">常用操作优先展示；零次方可选择连续静止帧上限。</div>
        <div id="stationaryThresholdControl" class="stationary-threshold-control hidden">
          <div>
            <label for="stationaryThreshold">静止帧剔除档位</label>
            <select id="stationaryThreshold">
              <option value="20">20 帧（激进）</option>
              <option value="40" selected>40 帧（均衡）</option>
              <option value="60">60 帧（保守）</option>
            </select>
          </div>
          <div class="field-note">连续静止超过所选档位才剔除；处理后每段最多保留同样帧数。档位越小，删除越多且不可恢复。</div>
        </div>
        <div class="actions">
          <button class="primary" data-stage="convert_qc">转换并质检</button>
          <button data-stage="qc">仅批量质检</button>
          <button class="primary" data-stage="lerobot">按等级生成 LeRobot</button>
          <button id="replayBtn">打开 HDF5 回放</button>
          <button id="stopJobBtn" class="danger" type="button">停止当前任务</button>
        </div>
        <div class="actions secondary-actions">
          <button data-stage="repair">选择 episode 剔除静止帧</button>
          <button id="optimizeHdf5Btn" data-stage="optimize_hdf5">HDF5 优化（重编号并复检）</button>
          <button data-stage="renumber">生成 LeRobot 编号计划</button>
          <button class="danger" data-stage="delete">选择 episode 删除</button>
        </div>
        <div id="lerobotStageSplitControl" class="lerobot-stage-split-control hidden">
          <button id="splitLerobotStagesBtn" class="primary" data-stage="split_lerobot_stages" disabled>按左右手阶段切分 LeRobot</button>
          <div id="lerobotStageSplitReason" class="lerobot-stage-split-reason unavailable">请选择零次方数据集并等待状态检查。</div>
          <div id="lerobotStageSplitPaths" class="lerobot-stage-split-paths"></div>
        </div>
      </div>

      <details class="advanced-settings" open>
        <summary>转换与输出设置</summary>
        <div class="advanced-body">
          <div class="row">
            <div>
              <label>机器人类型</label>
              <select id="robotType" data-required="1">
                <option value="aloha" selected>松灵机器人（ALOHA）</option>
                <option value="zerith">零次方机器人（23 自由度）</option>
                <option value="g2">G2 机器人（兼容）</option>
              </select>
            </div>
            <div>
              <label>并行转换/质检数</label>
              <input id="convertJobs" type="number" min="1" value="6" />
            </div>
          </div>
          <div id="cameraVariantControls" class="row hidden">
            <div>
              <label>保存摄像头数量</label>
              <select id="cameraCount">
                <option value="3" selected>三路</option>
                <option value="4">四路（含普通头部与广角）</option>
              </select>
            </div>
            <div>
              <label>三路模式头部视角</label>
              <select id="headCameraSource">
                <option value="global" selected>广角头部 /camera_h</option>
                <option value="front">普通头部 /camera_f</option>
              </select>
            </div>
          </div>
          <div id="cameraVariantHint" class="hint hidden">默认保存三路（左右手腕 + 广角头部）；四路模式会同时保存普通头部与广角。</div>
          <label>Profile</label>
          <input id="profile" placeholder="选择机器人后自动生成，也可以手动修改" />
          <div class="row">
            <div>
              <label>LeRobot repo id</label>
              <input id="repoId" placeholder="留空时从数据集目录自动推导" />
            </div>
            <div>
              <label>GPU 编号</label>
              <input id="gpuDevice" value="0" placeholder="例如 0，留空使用 CPU" />
            </div>
          </div>
          <div class="checks">
            <label><input id="useDocker" type="checkbox" /> Docker 转换</label>
            <label><input id="overwriteHdf5" type="checkbox" /> 覆盖 HDF5</label>
            <label><input id="lerobotCuda" type="checkbox" checked /> 填写 GPU 时 LeRobot CUDA resize</label>
            <label id="alohaBaseActionLabel"><input id="alohaIncludeBaseAction" type="checkbox" checked /> ALOHA 包含升降/底盘 state-action（21维/18维）</label>
          </div>
        </div>
      </details>
    </section>
    <div class="grid">
      <section>
        <div class="tabs" role="tablist">
          <button id="overviewTab" class="tab-btn active" type="button">数据概览</button>
          <button id="qcTab" class="tab-btn" type="button">质检报告</button>
          <button id="mappingTab" class="tab-btn" type="button">编号映射</button>
          <button id="replayTab" class="tab-btn" type="button">HDF5 可视化回放</button>
        </div>
        <div id="overviewPanel" class="tab-panel">
          <div id="qcOverview" class="qc-overview"></div>
        </div>
        <div id="qcPanel" class="tab-panel hidden">
          <div class="selection-bar">
            <span id="selectionInfo">已选择 0 条 episode</span>
            <div class="selection-actions">
              <button id="syncHdf5GradesBtn" class="primary" type="button" data-stage="sync_hdf5_grades">批量写入 HDF5 等级</button>
              <button id="selectGradeCBtn" type="button">批量选择质量等级C</button>
              <button id="selectCameraMissingOver20Btn" type="button">批量选择相机缺帧&gt;20</button>
              <button id="selectStationaryOver15Btn" type="button">批量选择最长静止帧&gt;15</button>
              <button id="clearSelectionBtn" type="button">清空选择</button>
            </div>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th><input id="selectAllEpisodes" type="checkbox" /></th><th>episode</th><th>fps</th><th>frames</th><th>相机缺帧</th><th>状态</th><th>质量等级</th><th>质量描述</th><th>warning数</th><th>warning</th><th>最长静止帧</th></tr></thead>
              <tbody id="episodeRows"></tbody>
            </table>
          </div>
          <div class="quality-check-catalog">
            <h3>质检项目</h3>
            <div id="qualityCheckItems" class="quality-check-grid"></div>
          </div>
        </div>
        <div id="mappingPanel" class="tab-panel hidden">
          <div id="mappingInfo" class="mapping-info">未生成编号计划</div>
          <div id="mappingStats" class="mapping-kpis"></div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>质量等级</th><th>MCAP episode</th><th>HDF5 episode</th><th>LeRobot dataset</th><th>LeRobot episode</th><th>编号</th></tr></thead>
              <tbody id="mappingRows"></tbody>
            </table>
          </div>
        </div>
        <div id="replayPanel" class="tab-panel hidden">
          <iframe id="replayFrame"></iframe>
        </div>
      </section>
      <section>
        <h2>任务日志</h2>
        <div id="log" class="log"></div>
      </section>
    </div>
  </main>
  <script>
    let currentJob = null;
    let jobTimer = null;
    let jobLogCursor = 0;
    let jobLogLines = [];
    let selectedEpisodes = new Set();
    let latestEpisodes = [];
    let latestQcRecordByEpisode = new Map();
    let latestStationaryThreshold = 15;
    let latestLerobotStageSplit = null;
    let splitStatusLoading = false;
    let splitJobRunning = false;
    const STATIONARY_THRESHOLD_LEVELS = new Set([20, 40, 60]);
    const STATIONARY_THRESHOLD_STORAGE_KEY = "embodiedPipeline:zerithStationaryThreshold";
    let discoveredSourceDatasets = [];
    let sourceDatasetScanRoot = "/srv/data/datasets/public";
    let currentMachine = "h200";
    let taskTextManuallyEdited = false;
    let autoTaskText = "";
    let datasetScanRequestId = 0;
    let activePanel = "overview";
    let statusAbortController = null;
    let latestStatusRequestId = 0;
    const MAX_CLIENT_LOG_LINES = 1000;
    const CUSTOM_DATASET_VALUE = "__custom_dataset__";
    const HDF5_STAGES = new Set(["qc", "repair", "optimize_hdf5", "delete", "delete_grade_c_qc", "delete_failed_qc", "renumber", "lerobot", "split_lerobot_stages", "replay"]);
    const MCAP_STAGES = new Set(["convert", "convert_qc"]);
    const PATH_HISTORY_FIELDS = ["mcapParentPath", "mcapPath", "hdf5Root", "qcRoot", "lerobotRoot"];
    const AUTO_RESTORE_PATH_FIELDS = new Set([]);
    const PATH_HISTORY_PREFIX = "embodiedPipeline:lastPath:";

    function fieldValue(id) {
      return String(document.getElementById(id).value || "").trim();
    }

    function pathHistoryKey(id) {
      return `${PATH_HISTORY_PREFIX}${id}`;
    }

    function restorePathHistory(id) {
      const input = document.getElementById(id);
      if (!input || fieldValue(id)) return;
      const value = localStorage.getItem(pathHistoryKey(id)) || "";
      if (value) input.value = value;
    }

    function savePathHistory(id) {
      const value = fieldValue(id);
      if (!value) return;
      localStorage.setItem(pathHistoryKey(id), value);
    }

    function saveAllPathHistory() {
      for (const id of PATH_HISTORY_FIELDS) savePathHistory(id);
    }

    function initPathHistory() {
      for (const id of PATH_HISTORY_FIELDS) {
        const input = document.getElementById(id);
        input.addEventListener("change", () => savePathHistory(id));
        input.addEventListener("blur", () => savePathHistory(id));
        if (AUTO_RESTORE_PATH_FIELDS.has(id)) {
          input.addEventListener("focus", () => restorePathHistory(id));
          input.addEventListener("click", () => restorePathHistory(id));
        }
      }
    }

    function sourceDatasetChoiceLabel(item) {
      const rel = item.relative_path || item.path || item.name || "";
      const count = Number(item.episode_count || 0);
      const type = String(item.dataset_type || "dataset").toUpperCase();
      return `${type} · ${rel}（${count} 条）`;
    }

    function selectedSourceDatasetChoice() {
      const select = document.getElementById("sourceDatasetSelect");
      return discoveredSourceDatasets.find(item => String(item.path || "") === select.value) || null;
    }

    function updateSourceDatasetScanInfo(message = "") {
      const info = document.getElementById("sourceDatasetScanInfo");
      if (message) {
        info.textContent = message;
        return;
      }
      const selected = selectedSourceDatasetChoice();
      if (selected) {
        info.textContent = `已选择 ${selected.relative_path || selected.path}，${Number(selected.episode_count || 0)} 条数据`;
        return;
      }
      if (discoveredSourceDatasets.length) {
        const layoutText = currentMachine === "h200" ? "递归 HDF5 数据集目录" : "两层数据集目录";
        info.textContent = `${sourceDatasetScanRoot} 下发现 ${discoveredSourceDatasets.length} 个${layoutText}`;
        return;
      }
      const layoutText = currentMachine === "h200" ? "递归 HDF5 数据集目录" : "两层数据集目录";
      info.textContent = `${sourceDatasetScanRoot} 下未发现${layoutText}`;
    }

    function sourceChoiceIncludesHdf5(item, hdf5Path) {
      const target = String(hdf5Path || "").trim();
      if (!target) return false;
      const variants = item?.processed_variants || {};
      return Object.values(variants).some(entries =>
        (Array.isArray(entries) ? entries : []).some(entry =>
          String(entry?.hdf5_root || "") === target
        )
      );
    }

    function syncSourceDatasetSelectToPath(path, hdf5Path = "") {
      const select = document.getElementById("sourceDatasetSelect");
      if (!select) return;
      const value = String(path || "").trim();
      const found = discoveredSourceDatasets.find(item => {
        const root = String(item.path || "").replace(/\/+$/, "");
        return root === value || (root && value.startsWith(`${root}/`)) || sourceChoiceIncludesHdf5(item, hdf5Path);
      });
      select.value = found ? String(found.path || "") : "";
      updateSourceDatasetScanInfo();
    }

    async function loadSourceDatasetChoices(machine = fieldValue("hostMachine") || "h200") {
      const select = document.getElementById("sourceDatasetSelect");
      const requestId = ++datasetScanRequestId;
      currentMachine = machine;
      select.disabled = true;
      try {
        const res = await fetch(`/api/datasets?machine=${encodeURIComponent(machine)}`);
        const data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error || res.statusText);
        if (requestId !== datasetScanRequestId) return;
        sourceDatasetScanRoot = data.scan_root || sourceDatasetScanRoot;
        if (Array.isArray(data.scan_roots) && data.scan_roots.length) {
          sourceDatasetScanRoot = data.scan_roots.map(item => `${String(item.type || "").toUpperCase()}: ${item.path}`).join("；");
        }
        discoveredSourceDatasets = Array.isArray(data.datasets) ? data.datasets : [];
        select.innerHTML = "";
        select.append(new Option("请选择数据集目录", ""));
        for (const item of discoveredSourceDatasets) {
          select.append(new Option(sourceDatasetChoiceLabel(item), item.path || ""));
        }
        select.append(new Option("自定义目录…", CUSTOM_DATASET_VALUE));
        syncSourceDatasetSelectToPath(fieldValue("mcapPath"), fieldValue("hdf5Root"));
        if (!data.scan_root_exists) {
          updateSourceDatasetScanInfo(`扫描目录不存在或不可访问: ${sourceDatasetScanRoot}；可使用自定义目录`);
        }
      } catch (err) {
        if (requestId !== datasetScanRequestId) return;
        discoveredSourceDatasets = [];
        select.innerHTML = "";
        select.append(new Option("扫描失败", ""));
        select.append(new Option("自定义目录…", CUSTOM_DATASET_VALUE));
        updateSourceDatasetScanInfo(`扫描数据集失败: ${err}`);
      } finally {
        if (requestId === datasetScanRequestId) select.disabled = false;
      }
    }

    function resetTaskTextForDataset() {
      taskTextManuallyEdited = false;
      autoTaskText = "";
      const input = document.getElementById("taskText");
      input.value = "";
      input.classList.remove("auto-value");
      document.getElementById("taskTextHint").textContent = "正在读取 MCAP 任务文本…";
    }

    function clearDatasetInputs() {
      for (const id of ["mcapParentPath", "mcapPath", "datasetName", "hdf5Root", "qcRoot", "lerobotRoot", "repoId"]) {
        document.getElementById(id).value = "";
      }
      clearMcapDatasetPicker("");
      resetTaskTextForDataset();
    }

    async function applyCustomDatasetPath() {
      const path = fieldValue("customDatasetPath");
      if (!path) {
        document.getElementById("mcapDatasetHint").textContent = "请先粘贴数据集目录。";
        return;
      }
      const button = document.getElementById("applyCustomDatasetBtn");
      const scanId = datasetScanRequestId;
      button.disabled = true;
      try {
        const res = await fetch(`/api/dataset-choice?path=${encodeURIComponent(path)}`);
        const data = await res.json();
        if (scanId !== datasetScanRequestId || path !== fieldValue("customDatasetPath")) return;
        if (!res.ok || data.error) throw new Error(data.error || res.statusText);
        const choice = data.dataset || {};
        discoveredSourceDatasets = [
          ...discoveredSourceDatasets.filter(item => String(item.path || "") !== String(choice.path || "")),
          choice,
        ];
        const select = document.getElementById("sourceDatasetSelect");
        for (const oldOption of Array.from(select.options)) {
          if (oldOption.value === String(choice.path || path)) oldOption.remove();
        }
        const customOption = Array.from(select.options).find(option => option.value === CUSTOM_DATASET_VALUE);
        const option = new Option(`自定义 · ${choice.path || path}`, choice.path || path);
        select.add(option, customOption || null);
        select.value = choice.path || path;
        await applySourceDatasetChoice();
      } catch (err) {
        document.getElementById("mcapDatasetHint").textContent = `自定义目录载入失败: ${err}`;
      } finally {
        button.disabled = false;
      }
    }

    function selectedCameraNamespace() {
      const cameraCount = fieldValue("cameraCount");
      if (cameraCount === "4") return "four_camera";
      return `three_camera_${fieldValue("headCameraSource") || "front"}`;
    }

    function clearDatasetStatusViews(message) {
      latestStatusRequestId += 1;
      if (statusAbortController) {
        statusAbortController.abort();
        statusAbortController = null;
      }
      latestEpisodes = [];
      latestQcRecordByEpisode = new Map();
      latestLerobotStageSplit = null;
      splitStatusLoading = false;
      selectedEpisodes.clear();
      renderQcOverview({records: []});
      renderQualityCheckItems([]);
      renderRenumberMapping({renumber_plan_exists: false, renumber_plan: []});
      document.getElementById("episodeRows").innerHTML = `<tr><td colspan="11">暂无 episode</td></tr>`;
      document.getElementById("pathRows").innerHTML = `
        <div class="dataset-path-row">
          <span class="dataset-path-label">状态</span>
          <span class="dataset-path-value">${escapeHtml(message || "未选择可用数据")}</span>
        </div>`;
      document.getElementById("replayFrame").removeAttribute("src");
      updateLerobotStageSplitControl();
      updateSelectionInfo();
      document.getElementById("log").textContent = message || "";
    }

    function applyProcessedDatasetVariant(choice) {
      if (!choice || choice.has_mcap) return false;
      document.getElementById("mcapPath").value = "";
      document.getElementById("mcapParentPath").value = choice.path || choice.parent_path || "";
      const namespace = selectedCameraNamespace();
      const variants = choice.processed_variants || {};
      const entries = Array.isArray(variants[namespace]) ? variants[namespace] : [];
      const currentName = fieldValue("datasetName");
      const processed = entries.find(item => item.dataset_name === currentName) || entries[0];
      if (!processed) {
        const message = `该目录没有 ${namespace} 的 HDF5 数据。`;
        for (const id of ["datasetName", "hdf5Root", "qcRoot", "lerobotRoot", "repoId"]) {
          document.getElementById(id).value = "";
        }
        clearMcapDatasetPicker(message);
        clearDatasetStatusViews(message);
        updateSourceDatasetScanInfo(`已选择 ${choice.relative_path || choice.path}，但没有 ${namespace} 的 HDF5 数据`);
        return true;
      }
      document.getElementById("datasetName").value = processed.dataset_name || "";
      document.getElementById("hdf5Root").value = processed.hdf5_root || "";
      document.getElementById("qcRoot").value = processed.qc_root || "";
      document.getElementById("lerobotRoot").value = processed.lerobot_root || "";
      document.getElementById("repoId").value = processed.dataset_name || "";
      clearMcapDatasetPicker("原始 MCAP 不存在，已直接加载现有 HDF5 和质检报告。");
      updateSourceDatasetScanInfo(
        `已选择 ${choice.relative_path || choice.path}，直接加载 ${namespace} 的 ${Number(processed.episode_count || 0)} 条 HDF5 数据`
      );
      return true;
    }

    function applyDirectHdf5Dataset(choice) {
      if (!choice || choice.dataset_type !== "hdf5") return false;
      if (choice.robot_type) {
        document.getElementById("robotType").value = choice.robot_type;
        document.getElementById("profile").value = "";
        updateAlohaBaseActionOption();
      }
      document.getElementById("mcapPath").value = "";
      document.getElementById("mcapParentPath").value = choice.parent_path || "";
      document.getElementById("datasetName").value = choice.name || "";
      document.getElementById("hdf5Root").value = choice.path || "";
      document.getElementById("qcRoot").value = "";
      document.getElementById("lerobotRoot").value = "";
      document.getElementById("repoId").value = choice.name || "";
      clearMcapDatasetPicker("已递归发现 HDF5 数据集，可直接质检或回放。");
      updateSourceDatasetScanInfo(
        `已选择 ${choice.relative_path || choice.path}，${Number(choice.episode_count || 0)} 条 HDF5 数据`
      );
      return true;
    }

    async function applySourceDatasetChoice() {
      const select = document.getElementById("sourceDatasetSelect");
      const customControls = document.getElementById("customDatasetControls");
      if (select.value === CUSTOM_DATASET_VALUE) {
        customControls.classList.remove("hidden");
        document.getElementById("customDatasetPath").focus();
        updateSourceDatasetScanInfo("粘贴当前服务端可访问的数据集目录，然后点击“载入目录”。");
        return;
      }
      customControls.classList.add("hidden");
      const selected = selectedSourceDatasetChoice();
      if (!selected) {
        clearDatasetInputs();
        clearDatasetStatusViews("请选择数据集目录");
        updateSourceDatasetScanInfo();
        return;
      }
      resetTaskTextForDataset();
      document.getElementById("mcapParentPath").value = selected.parent_path || "";
      if (applyDirectHdf5Dataset(selected)) {
        localStorage.removeItem(pathHistoryKey("mcapPath"));
        savePathHistory("mcapParentPath");
        savePathHistory("hdf5Root");
        await refreshStatus();
        return;
      }
      if (applyProcessedDatasetVariant(selected)) {
        localStorage.removeItem(pathHistoryKey("mcapPath"));
        savePathHistory("mcapParentPath");
        for (const id of ["hdf5Root", "qcRoot", "lerobotRoot"]) {
          savePathHistory(id);
        }
        if (fieldValue("hdf5Root")) await refreshStatus();
        return;
      }
      for (const id of ["datasetName", "hdf5Root", "qcRoot", "lerobotRoot", "repoId"]) {
        document.getElementById(id).value = "";
      }
      document.getElementById("mcapPath").value = selected.path || "";
      savePathHistory("mcapPath");
      savePathHistory("mcapParentPath");
      for (const id of ["hdf5Root", "qcRoot", "lerobotRoot"]) {
        localStorage.removeItem(pathHistoryKey(id));
      }
      updateSourceDatasetScanInfo();
      await updateMcapDatasetsFromParent({refresh: false, parentOverride: selected.path || ""});
      await refreshStatus();
    }

    function missingRequiredFields(stage = "status") {
      const missing = [];
      const hasMcap = !!fieldValue("mcapPath");
      const hasHdf5 = !!fieldValue("hdf5Root");
      const hasLerobot = !!fieldValue("lerobotRoot");
      const onlyLerobotStatus = stage === "status" && hasLerobot && !hasMcap && !hasHdf5;
      if (!fieldValue("robotType") && !onlyLerobotStatus) {
        missing.push("机器人");
      }
      if (stage === "status" && !hasMcap && !hasHdf5 && !hasLerobot) {
        missing.push("MCAP 数据集路径、HDF5 路径或 LeRobot 输出根目录");
      } else if (MCAP_STAGES.has(stage) && !hasMcap) {
        missing.push("MCAP 数据集路径");
      } else if (HDF5_STAGES.has(stage) && !hasHdf5 && !hasMcap) {
        missing.push("MCAP 数据集路径或 HDF5 路径");
      }
      return missing;
    }

    function showRequiredMessage(missing) {
      const text = missing.length ? `请填写必填项：${missing.join("、")}` : "";
      if (text) {
        document.getElementById("log").textContent = text;
        document.getElementById("pathRows").innerHTML = `
          <div class="dataset-path-row">
            <span class="dataset-path-label">状态</span>
            <span class="dataset-path-value">${escapeHtml(text)}</span>
          </div>`;
      }
      return text;
    }

    function validateRequired(stage = "status") {
      const missing = missingRequiredFields(stage);
      showRequiredMessage(missing);
      return missing.length === 0;
    }

    function cameraVariantPayload() {
      const cameraControls = document.getElementById("cameraVariantControls");
      if (cameraControls.classList.contains("hidden")) return {};
      const cameraCount = document.getElementById("cameraCount");
      const headCamera = document.getElementById("headCameraSource");
      return {
        camera_count: Number(cameraCount.value),
        head_camera_source: headCamera.value,
      };
    }

    function syncCameraVariantControls(cfg) {
      const cameraControls = document.getElementById("cameraVariantControls");
      const cameraHint = document.getElementById("cameraVariantHint");
      const cameraCount = document.getElementById("cameraCount");
      const headCamera = document.getElementById("headCameraSource");
      const selectable = Boolean(cfg.camera_variant_selectable);
      if (selectable) {
        cameraCount.value = String(cfg.camera_count || 3);
        headCamera.value = String(cfg.head_camera_source || "global");
        document.getElementById("profile").value = cfg.profile || "";
      }
      cameraControls.classList.toggle("hidden", !selectable);
      cameraHint.classList.toggle("hidden", !selectable);
      headCamera.disabled = !selectable || cameraCount.value !== "3";
      document.getElementById("profile").readOnly = selectable;
    }

    async function bootstrapCameraVariantControls() {
      const query = new URLSearchParams({
        robot_type: document.getElementById("robotType").value || "aloha",
      });
      const res = await fetch(`/api/defaults?${query}`);
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || res.statusText);
      syncCameraVariantControls(data.config || {});
    }

    function payload(extra = {}) {
      saveAllPathHistory();
      return {
        mcap_path: document.getElementById("mcapPath").value,
        mcap_parent_path: document.getElementById("mcapParentPath").value,
        dataset_name: document.getElementById("datasetName").value,
        hdf5_root: document.getElementById("hdf5Root").value,
        qc_root: document.getElementById("qcRoot").value,
        lerobot_root: document.getElementById("lerobotRoot").value,
        robot_type: document.getElementById("robotType").value,
        // An untouched auto-discovered value remains display-only so the converter
        // can continue reading each episode's own MCAP instruction sidecar.
        task_text: taskTextManuallyEdited ? document.getElementById("taskText").value : "",
        profile: document.getElementById("profile").value,
        repo_id: document.getElementById("repoId").value,
        gpu_device: document.getElementById("gpuDevice").value,
        convert_jobs: Number(document.getElementById("convertJobs").value || 6),
        use_docker: document.getElementById("useDocker").checked,
        overwrite_hdf5: document.getElementById("overwriteHdf5").checked,
        lerobot_cuda: document.getElementById("lerobotCuda").checked,
        aloha_include_base_action: document.getElementById("alohaIncludeBaseAction").checked,
        stationary_threshold: document.getElementById("robotType").value === "zerith"
          ? Number(document.getElementById("stationaryThreshold").value)
          : "",
        ...cameraVariantPayload(),
        ...extra,
      };
    }

    async function postJson(url, data, options = {}) {
      const res = await fetch(url, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(data),
        ...options,
      });
      const json = await res.json();
      if (!res.ok || json.error) throw new Error(json.error || res.statusText);
      return json;
    }

    function clearMcapDatasetPicker(message = "") {
      document.getElementById("mcapDatasetOptions").innerHTML = "";
      const select = document.getElementById("mcapDatasetSelect");
      select.innerHTML = `<option value="">请选择 MCAP 数据集</option>`;
      select.classList.add("hidden");
      document.getElementById("mcapDatasetHint").textContent = message;
    }

    function renderMcapDatasetPicker(data) {
      const mcapInput = document.getElementById("mcapPath");
      const datalist = document.getElementById("mcapDatasetOptions");
      const select = document.getElementById("mcapDatasetSelect");
      const hint = document.getElementById("mcapDatasetHint");
      const datasets = Array.isArray(data.datasets) ? data.datasets : [];
      let changed = false;

      if (data.parent_is_dataset && data.selected_path) {
        document.getElementById("mcapParentPath").value = data.selected_parent || data.parent || "";
        if (mcapInput.value !== data.selected_path) {
          mcapInput.value = data.selected_path;
          changed = true;
        }
        datalist.innerHTML = `<option value="${escapeHtml(data.selected_path)}"></option>`;
        select.innerHTML = `<option value="${escapeHtml(data.selected_path)}">${escapeHtml(data.selected_path)}</option>`;
        select.classList.add("hidden");
        hint.textContent = "父级目录本身是 MCAP 数据集，已填入 MCAP 数据集路径。";
        return changed;
      }

      datalist.innerHTML = datasets.map(item =>
        `<option value="${escapeHtml(item.path)}">${escapeHtml(item.name || item.path)}</option>`
      ).join("");
      if (!datasets.length) {
        select.innerHTML = `<option value="">未找到 MCAP 数据集</option>`;
        select.classList.add("hidden");
        hint.textContent = data.parent ? `未在 ${data.parent} 下发现 MCAP 数据集。` : "";
        return changed;
      }

      if (datasets.length === 1) {
        const only = datasets[0];
        document.getElementById("mcapParentPath").value = data.selected_parent || data.parent || "";
        if (mcapInput.value !== only.path) {
          mcapInput.value = only.path;
          changed = true;
        }
        select.innerHTML = `<option value="${escapeHtml(only.path)}">${escapeHtml(only.name || only.path)}</option>`;
        select.value = only.path;
        select.classList.add("hidden");
        hint.textContent = `已自动选择唯一 MCAP 数据集：${only.name || only.path}`;
        return changed;
      }

      const current = fieldValue("mcapPath");
      const currentInOptions = datasets.some(item => item.path === current);
      document.getElementById("mcapParentPath").value = data.selected_parent || data.parent || "";
      if (!currentInOptions && current) {
        mcapInput.value = "";
        changed = true;
      }
      select.innerHTML = [
        `<option value="">请选择 MCAP 数据集</option>`,
        ...datasets.map(item => {
          const countText = Number.isFinite(Number(item.mcap_count)) ? ` · ${item.mcap_count} 个 MCAP` : "";
          return `<option value="${escapeHtml(item.path)}">${escapeHtml(item.name || item.path)}${escapeHtml(countText)}</option>`;
        }),
      ].join("");
      select.classList.remove("hidden");
      select.value = currentInOptions ? current : "";
      hint.textContent = `发现 ${datasets.length} 个 MCAP 数据集，请继续选择。`;
      return changed;
    }

    async function updateMcapDatasetsFromParent({refresh = true, parentOverride = ""} = {}) {
      const parent = String(parentOverride || fieldValue("mcapParentPath") || "").trim();
      if (!parent) {
        clearMcapDatasetPicker("");
        return;
      }
      try {
        const res = await fetch(`/api/mcap-datasets?parent=${encodeURIComponent(parent)}`);
        const data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error || res.statusText);
        const changed = renderMcapDatasetPicker(data);
        savePathHistory("mcapParentPath");
        if (refresh && changed) await refreshStatus();
      } catch (err) {
        clearMcapDatasetPicker(String(err));
      }
    }

    function setLogTextFromLines() {
      const log = document.getElementById("log");
      log.textContent = jobLogLines.join("\n");
      log.scrollTop = log.scrollHeight;
    }

    function resetJobLog(text = "") {
      jobLogCursor = 0;
      jobLogLines = text ? [text] : [];
      setLogTextFromLines();
    }

    function appendJobLog(lines) {
      if (!Array.isArray(lines) || !lines.length) return;
      jobLogLines.push(...lines.map(line => String(line)));
      if (jobLogLines.length > MAX_CLIENT_LOG_LINES) {
        jobLogLines = jobLogLines.slice(-MAX_CLIENT_LOG_LINES);
        if (!String(jobLogLines[0] || "").startsWith("[仅显示最近")) {
          jobLogLines[0] = `[仅显示最近 ${MAX_CLIENT_LOG_LINES} 行日志]`;
        }
      }
      setLogTextFromLines();
    }

    function badge(status) {
      const s = status || "未质检";
      let cls = "";
      if (s === "保留" || s === "通过" || s === "成功" || s === "采集成功" || s === "A") cls = "ok";
      else if (s === "修复") cls = "warn";
      else if (s === "B") cls = "warn";
      else if (s === "删除" || s === "失败" || s === "采集失败" || s === "F") cls = "bad";
      return `<span class="badge ${cls}">${s}</span>`;
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, ch => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      }[ch]));
    }

    function finiteNumber(value) {
      const number = Number(value);
      return Number.isFinite(number) ? number : null;
    }

    function formatChartValue(value, unit = "", digits = 2, fixed = false) {
      const number = finiteNumber(value);
      if (number === null) return "";
      const text = fixed
        ? number.toFixed(digits)
        : (Number.isInteger(number) ? String(number) : number.toFixed(digits).replace(/\.?0+$/, ""));
      return `${text}${unit}`;
    }

    function average(values) {
      const nums = values.map(finiteNumber).filter(value => value !== null);
      if (!nums.length) return null;
      return nums.reduce((acc, value) => acc + value, 0) / nums.length;
    }

    function maxValue(values) {
      const nums = values.map(finiteNumber).filter(value => value !== null);
      return nums.length ? Math.max(...nums) : null;
    }

    function kpi(label, value) {
      return `<div class="overview-kpi"><div class="label">${escapeHtml(label)}</div><div class="value">${escapeHtml(value)}</div></div>`;
    }

    function barClass(metric, value) {
      const number = finiteNumber(value);
      if (number === null) return "";
      if (metric === "fps" && number < 29) return "bad";
      if (metric === "camera_completeness_pct" && number < 100) return number < 95 ? "bad" : "warn";
      if (metric === "max_stationary_run_frames" && number > latestStationaryThreshold) return "bad";
      return "";
    }

    const PIE_COLORS = ["#3b82f6", "#12b76a", "#f59e0b", "#ef4444", "#8b5cf6", "#06b6d4", "#64748b", "#f97316"];

    function valueBarChart(records, key, title, unit = "", digits = 2, fixedMax = null, fixedDigits = false) {
      const items = records
        .map(item => ({name: String(item.episode_id || ""), value: finiteNumber(item[key])}))
        .filter(item => item.value !== null);
      if (!items.length) {
        return `<div class="overview-chart"><h3>${escapeHtml(title)}</h3><div class="overview-empty">无数据</div></div>`;
      }
      const max = fixedMax ?? Math.max(...items.map(item => item.value), 1);
      const scaleMax = Math.max(max, 1e-9);
      const barWidth = 48;
      const plotWidth = Math.max(240, items.length * (barWidth + 4) + 12);
      const ticks = [1, 0.75, 0.5, 0.25, 0].map(ratio => `
        <span>${escapeHtml(formatChartValue(max * ratio, unit, digits, fixedDigits))}</span>
      `).join("");
      const bars = items.map(item => {
        const pct = Math.max(0, Math.min(100, (item.value / scaleMax) * 100));
        const valueText = formatChartValue(item.value, unit, digits, fixedDigits);
        return `<div class="bar-item" title="${escapeHtml(item.name)}: ${escapeHtml(valueText)}">
          <div class="bar-column">
            <div class="bar-stack" style="--bar-height:${pct}%">
              <div class="bar-value">${escapeHtml(valueText)}</div>
              <div class="bar-fill-wrap">
                <span class="bar-fill ${barClass(key, item.value)}"></span>
              </div>
            </div>
          </div>
          <div class="bar-label">${escapeHtml(item.name)}</div>
        </div>`;
      }).join("");
      return `<div class="overview-chart">
        <h3>${escapeHtml(title)}</h3>
        <div class="bar-chart">
          <div class="bar-chart-meta">
            <span>纵轴刻度</span>
            <span>最大 ${escapeHtml(formatChartValue(max, unit, digits, fixedDigits))} · ${items.length}条</span>
          </div>
          <div class="bar-body">
            <div class="bar-y-axis">${ticks}</div>
            <div class="bar-plot">
              <div class="bar-items" style="width:${plotWidth}px; --bar-width:${barWidth}px">${bars}</div>
            </div>
          </div>
        </div>
      </div>`;
    }

    function integerAxisTicks(maxValue, maxTicks = 5) {
      const max = Math.max(1, Math.ceil(Number(maxValue) || 1));
      if (max <= maxTicks - 1) {
        return Array.from({length: max + 1}, (_, index) => max - index);
      }
      const step = Math.max(1, Math.ceil(max / (maxTicks - 1)));
      const ticks = [];
      for (let value = max; value > 0; value -= step) ticks.push(value);
      if (ticks[ticks.length - 1] !== 0) ticks.push(0);
      return ticks;
    }

    function gripperCloseGroupedBarChart(records) {
      const items = records
        .map(item => ({
          name: String(item.episode_id || ""),
          left: finiteNumber(item.left_gripper_close_events),
          right: finiteNumber(item.right_gripper_close_events),
        }))
        .filter(item => item.left !== null || item.right !== null);
      if (!items.length) {
        return `<div class="overview-chart"><h3>夹爪闭合次数</h3><div class="overview-empty">无数据</div></div>`;
      }
      const max = Math.max(1, ...items.flatMap(item => [item.left ?? 0, item.right ?? 0]));
      const barWidth = 68;
      const plotWidth = Math.max(240, items.length * (barWidth + 6) + 12);
      const ticks = integerAxisTicks(max).map(value => `<span>${escapeHtml(value)}</span>`).join("");
      const bars = items.map(item => {
        const left = item.left ?? "无数据";
        const right = item.right ?? "无数据";
        const leftPct = Math.max(0, Math.min(100, (item.left ?? 0) / max * 100));
        const rightPct = Math.max(0, Math.min(100, (item.right ?? 0) / max * 100));
        return `<div class="grouped-bar-item" title="${escapeHtml(item.name)}: 左手 ${escapeHtml(left)}，右手 ${escapeHtml(right)}">
          <div class="grouped-bar-columns">
            <span class="grouped-bar left" style="--bar-height:${leftPct}%"><span class="grouped-bar-value">${escapeHtml(left)}</span></span>
            <span class="grouped-bar right" style="--bar-height:${rightPct}%"><span class="grouped-bar-value">${escapeHtml(right)}</span></span>
          </div>
          <div class="bar-label">${escapeHtml(item.name)}</div>
        </div>`;
      }).join("");
      return `<div class="overview-chart">
        <h3>夹爪闭合次数</h3>
        <div class="color-legend">
          <span class="color-legend-item"><span class="legend-swatch" style="background:#3b82f6"></span>左手闭合次数</span>
          <span class="color-legend-item"><span class="legend-swatch" style="background:#f59e0b"></span>右手闭合次数</span>
        </div>
        <div class="bar-chart">
          <div class="bar-chart-meta">
            <span>纵轴刻度：闭合次数</span>
            <span>最大 ${escapeHtml(formatChartValue(max, "", 0, false))} · ${items.length}条</span>
          </div>
          <div class="bar-body">
            <div class="bar-y-axis">${ticks}</div>
            <div class="bar-plot">
              <div class="grouped-bar-items" style="width:${plotWidth}px; --bar-width:${barWidth}px">${bars}</div>
            </div>
          </div>
        </div>
      </div>`;
    }

    function pieChart(title, entries) {
      const items = entries.filter(item => Number(item.count) > 0);
      if (!items.length) {
        return `<div class="overview-chart pie-chart"><h3>${escapeHtml(title)}</h3><div class="overview-empty">无数据</div></div>`;
      }
      const total = items.reduce((acc, item) => acc + Number(item.count), 0);
      let cursor = 0;
      const segments = items.map((item, index) => {
        const start = cursor;
        const end = cursor + Number(item.count) / total * 100;
        cursor = end;
        const color = item.color || PIE_COLORS[index % PIE_COLORS.length];
        return `${color} ${start.toFixed(3)}% ${end.toFixed(3)}%`;
      }).join(", ");
      const legend = items.map((item, index) => {
        const color = item.color || PIE_COLORS[index % PIE_COLORS.length];
        const count = Number(item.count);
        const pct = total ? (count / total * 100).toFixed(1).replace(/\.0$/, "") : "0";
        return `<div class="pie-legend-row" title="${escapeHtml(item.label)}">
          <span class="pie-dot" style="background:${escapeHtml(color)}"></span>
          <span class="pie-label">${escapeHtml(item.label)}</span>
          <span class="pie-value">${count} / ${pct}%</span>
        </div>`;
      }).join("");
      return `<div class="overview-chart pie-chart">
        <h3>${escapeHtml(title)}</h3>
        <div class="pie-layout">
          <div class="pie-visual" style="background: conic-gradient(${segments})"></div>
          <div class="pie-legend">${legend}</div>
        </div>
      </div>`;
    }

    function distributionPieChart(records, key, title, unit = "") {
      const counts = new Map();
      for (const item of records) {
        const value = finiteNumber(item[key]);
        if (value === null) continue;
        const label = Number.isInteger(value) ? String(value) : String(value.toFixed(2));
        counts.set(label, (counts.get(label) || 0) + 1);
      }
      const entries = Array.from(counts.entries()).sort((a, b) => Number(a[0]) - Number(b[0]));
      if (!entries.length) {
        return `<div class="overview-chart"><h3>${escapeHtml(title)}</h3><div class="overview-empty">无数据</div></div>`;
      }
      return pieChart(title, entries.map(([label, count], index) => ({
        label: `${label}${unit}`,
        count,
        color: PIE_COLORS[index % PIE_COLORS.length],
      })));
    }

    function targetValuePieChart(records, key, title, expected, unit = "") {
      const buckets = [
        {label: `${expected}${unit}`, count: 0, color: "#12b76a"},
        {label: `非${expected}${unit}`, count: 0, color: "#ef4444"},
        {label: "无数据", count: 0, color: "#98a2b3"},
      ];
      for (const item of records) {
        const value = finiteNumber(item[key]);
        if (value === null) buckets[2].count += 1;
        else if (Math.round(value) === expected) buckets[0].count += 1;
        else buckets[1].count += 1;
      }
      return pieChart(title, buckets);
    }

    function qualityGradePieChart(overview, records) {
      const counts = overview?.quality_grade_counts || {};
      const buckets = [
        {label: "A", count: Number(counts.A || 0), color: "#12b76a"},
        {label: "B", count: Number(counts.B || 0), color: "#f59e0b"},
        {label: "C", count: Number(counts.C || 0), color: "#3b82f6"},
        {label: "F", count: Number(counts.F || 0), color: "#ef4444"},
        {label: "无等级", count: Number(counts.no_grade || 0), color: "#98a2b3"},
      ];
      if (!Object.keys(counts).length) {
        const byLabel = new Map(buckets.map(item => [item.label, item]));
        for (const item of records) {
          const grade = String(item.quality_grade || "").trim().toUpperCase();
          if (byLabel.has(grade)) byLabel.get(grade).count += 1;
          else byLabel.get("无等级").count += 1;
        }
      }
      return pieChart("质量等级", buckets);
    }

    function renderQcOverview(overview) {
      const root = document.getElementById("qcOverview");
      const records = Array.isArray(overview?.records) ? overview.records : [];
      const avgFps = average(records.map(item => item.fps));
      const avgDuration = average(records.map(item => item.duration_sec));
      const successStatuses = new Set(["保留", "通过", "成功", "采集成功"]);
      const neutralStatuses = new Set(["", "未质检"]);
      const fallbackSuccessCount = records.filter(item => successStatuses.has(String(item.status || ""))).length;
      const fallbackFailureCount = records.filter(item => {
        const status = String(item.status || "");
        return !successStatuses.has(status) && !neutralStatuses.has(status);
      }).length;
      const statusCounts = overview?.status_counts || {};
      const totalCount = Number.isFinite(Number(statusCounts.total)) ? Number(statusCounts.total) : records.length;
      const successCount = Number.isFinite(Number(statusCounts.success)) ? Number(statusCounts.success) : fallbackSuccessCount;
      const failureCount = Number.isFinite(Number(statusCounts.failure)) ? Number(statusCounts.failure) : fallbackFailureCount;
      const standards = overview?.standards || {};
      const expectedStateDim = Number(standards.state_dim || 21);
      const expectedActionDim = Number(standards.action_dim || 18);
      const expectedCameraViews = Number(standards.camera_view_count || 3);
      const charts = records.length ? [
          qualityGradePieChart(overview, records),
          targetValuePieChart(records, "state_dim", `state 维度（标准 ${expectedStateDim}D）`, expectedStateDim, "D"),
          targetValuePieChart(records, "action_dim", `action 维度（标准 ${expectedActionDim}D）`, expectedActionDim, "D"),
          targetValuePieChart(records, "camera_view_count", `相机视角（标准 ${expectedCameraViews}个）`, expectedCameraViews, "个"),
          gripperCloseGroupedBarChart(records),
          valueBarChart(records, "fps", "FPS", "Hz", 2, Math.max(30, maxValue(records.map(item => item.fps)) || 30), true),
          valueBarChart(records, "duration_sec", "时长", "s", 2),
          valueBarChart(records, "max_state_step", "最大 state 跳变", "", 4),
          valueBarChart(records, "stationary_frames", "action 静止帧数量", "帧", 0),
          valueBarChart(records, "max_stationary_run_frames", "最长静止帧", "帧", 0),
        ].join("")
        : `<div class="overview-empty">暂无可统计的 final_report_table.md / qc_report.json。运行质检后显示图表。</div>`;
      root.innerHTML = `
        <div class="qc-overview-head">
          <strong>数据概览</strong>
          <span>${escapeHtml(overview?.source_report_dir || "")}</span>
        </div>
        <div class="overview-kpis">
          ${kpi("总 Episode", totalCount)}
          ${kpi("成功 Episode", successCount)}
          ${kpi("失败 Episode", failureCount)}
          ${kpi("平均 FPS", avgFps === null ? "-" : formatChartValue(avgFps, "Hz", 2, true))}
          ${kpi("平均时长", avgDuration === null ? "-" : formatChartValue(avgDuration, "s", 1))}
        </div>
        <div class="overview-charts">${charts}</div>`;
    }

    function renderQualityCheckItems(items) {
      const root = document.getElementById("qualityCheckItems");
      const checks = Array.isArray(items) ? items : [];
      root.innerHTML = checks.map(item => `
        <div class="quality-check-item">
          <div class="quality-check-name">${escapeHtml(item.name || "")}</div>
          <div class="quality-check-criterion">${escapeHtml(item.criterion || "")}</div>
        </div>
      `).join("");
    }

    function selectedList() {
      return Array.from(selectedEpisodes).sort((a, b) => a.localeCompare(b, undefined, {numeric: true}));
    }

    function updateSelectionInfo() {
      const count = selectedEpisodes.size;
      document.getElementById("selectionInfo").textContent = `已选择 ${count} 条 episode`;
      const all = Array.from(document.querySelectorAll(".episode-check"));
      const checked = all.filter(input => input.checked).length;
      const selectAll = document.getElementById("selectAllEpisodes");
      if (selectAll) {
        selectAll.checked = all.length > 0 && checked === all.length;
        selectAll.indeterminate = checked > 0 && checked < all.length;
      }
    }

    function syncSelectionCheckboxes() {
      document.querySelectorAll(".episode-check").forEach(input => {
        input.checked = selectedEpisodes.has(input.dataset.episode);
      });
      updateSelectionInfo();
    }

    function firstNumberFromText(value) {
      const match = String(value ?? "").match(/-?\d+(?:\.\d+)?/);
      if (!match) return null;
      const number = Number(match[0]);
      return Number.isFinite(number) ? number : null;
    }

    function longestStationaryForSelection(item) {
      const display = String(item.longest_stationary_display ?? "").trim();
      if (display.includes("已剔除")) return null;
      const displayNumber = firstNumberFromText(display);
      if (displayNumber !== null) return displayNumber;
      return finiteNumber(item.max_stationary_run_frames);
    }

    function qcRecordForEpisode(item) {
      const episodeId = String(item?.episode_id || "");
      return episodeId ? latestQcRecordByEpisode.get(episodeId) || {} : {};
    }

    function cameraMissingForSelection(item) {
      const direct = finiteNumber(item?.camera_missing);
      if (direct !== null) return direct;
      return finiteNumber(qcRecordForEpisode(item).camera_missing);
    }

    function qualityGradeControl(item) {
      const episodeId = String(item.episode_id || "");
      const grade = String(item.quality_grade || "A").trim().toUpperCase() || "A";
      const options = ["A", "B", "C", "F"].map(value =>
        `<option value="${value}" ${grade === value ? "selected" : ""}>${value}</option>`
      ).join("");
      return `<select class="quality-grade-select" data-episode="${escapeHtml(episodeId)}" data-current-grade="${escapeHtml(grade)}" title="质量等级">${options}</select>`;
    }

    async function saveQualityGrade(select) {
      const episodeName = String(select.dataset.episode || "");
      const previousGrade = String(select.dataset.currentGrade || "A");
      const nextGrade = String(select.value || "").trim().toUpperCase();
      if (!episodeName || !nextGrade || nextGrade === previousGrade) return;
      select.disabled = true;
      try {
        await postJson("/api/qc-report/quality-grade", payload({
          episode_name: episodeName,
          quality_grade: nextGrade,
        }));
        document.getElementById("log").textContent = `已更新 ${episodeName} 质量等级为 ${nextGrade}`;
        await refreshStatus();
        setPanel("qc");
      } catch (err) {
        select.value = previousGrade;
        document.getElementById("log").textContent = `质量等级保存失败: ${err}`;
      } finally {
        select.disabled = false;
      }
    }

    function selectEpisodesByPredicate(predicate) {
      selectedEpisodes.clear();
      latestEpisodes.forEach(item => {
        const episodeId = String(item.episode_id || "");
        if (episodeId && predicate(item)) {
          selectedEpisodes.add(episodeId);
        }
      });
      syncSelectionCheckboxes();
    }

    function bindQualityGradeControls() {
      document.querySelectorAll(".quality-grade-select").forEach(select => {
        select.addEventListener("change", () => {
          saveQualityGrade(select).catch(err => {
            document.getElementById("log").textContent = `质量等级保存失败: ${err}`;
          });
        });
      });
    }

    function bindEpisodeSelection() {
      document.querySelectorAll(".episode-check").forEach(input => {
        input.addEventListener("change", () => {
          if (input.checked) selectedEpisodes.add(input.dataset.episode);
          else selectedEpisodes.delete(input.dataset.episode);
          updateSelectionInfo();
        });
      });
      const selectAll = document.getElementById("selectAllEpisodes");
      selectAll.addEventListener("change", () => {
        document.querySelectorAll(".episode-check").forEach(input => {
          input.checked = selectAll.checked;
          if (input.checked) selectedEpisodes.add(input.dataset.episode);
          else selectedEpisodes.delete(input.dataset.episode);
        });
        updateSelectionInfo();
      });
      updateSelectionInfo();
    }

    function setPanel(name) {
      activePanel = name;
      const showOverview = name === "overview";
      const showQc = name === "qc";
      const showReplay = name === "replay";
      const showMapping = name === "mapping";
      document.getElementById("overviewPanel").classList.toggle("hidden", !showOverview);
      document.getElementById("qcPanel").classList.toggle("hidden", !showQc);
      document.getElementById("mappingPanel").classList.toggle("hidden", !showMapping);
      document.getElementById("replayPanel").classList.toggle("hidden", !showReplay);
      document.getElementById("overviewTab").classList.toggle("active", showOverview);
      document.getElementById("qcTab").classList.toggle("active", showQc);
      document.getElementById("mappingTab").classList.toggle("active", showMapping);
      document.getElementById("replayTab").classList.toggle("active", showReplay);
      document.body.classList.toggle("replay-mode", showReplay);
    }

    function setWorkspace(name) {
      const showLerobotVisualization = name === "lerobot-visualization";
      const showCollection = name === "collection";
      const showLerobot = name === "lerobot";
      const showManualScreening = name === "manual-screening";
      document.getElementById("lerobotVisualizationWorkspace").classList.toggle("hidden", !showLerobotVisualization);
      document.getElementById("collectionWorkspace").classList.toggle("hidden", !showCollection);
      document.getElementById("lerobotWorkspace").classList.toggle("hidden", !showLerobot);
      document.getElementById("manualScreeningWorkspace").classList.toggle("hidden", !showManualScreening);
      document.getElementById("lerobotVisualizationWorkspaceBtn").classList.toggle("active", showLerobotVisualization);
      document.getElementById("collectionWorkspaceBtn").classList.toggle("active", showCollection);
      document.getElementById("lerobotWorkspaceBtn").classList.toggle("active", showLerobot);
      document.getElementById("manualScreeningWorkspaceBtn").classList.toggle("active", showManualScreening);
      document.getElementById("refreshBtn").classList.toggle("hidden", !showCollection);
      if (!showCollection) document.body.classList.remove("replay-mode");
      else setPanel(activePanel);
    }

    function syncDetectedTaskText(rawMcapTasks, hdf5Tasks, lerobotTasks) {
      const input = document.getElementById("taskText");
      const hint = document.getElementById("taskTextHint");
      const rawTasks = Array.isArray(rawMcapTasks) ? rawMcapTasks : [];
      const fallbackTasks = [
        ...(Array.isArray(hdf5Tasks) ? hdf5Tasks : []),
        ...(Array.isArray(lerobotTasks) ? lerobotTasks : []),
      ];
      const detected = rawTasks[0] || fallbackTasks[0] || "";
      if (!taskTextManuallyEdited) {
        autoTaskText = detected;
        input.value = detected;
        input.classList.toggle("auto-value", Boolean(detected));
      }
      if (taskTextManuallyEdited) {
        hint.textContent = "已使用人工修改文本；只补写缺少 task 的 HDF5，不覆盖已有 task。";
      } else if (rawTasks.length > 1) {
        hint.textContent = `MCAP 中检测到 ${rawTasks.length} 种任务文本。当前显示第一种；未修改时转换仍按每个 episode 自己的 MCAP 文本读取。`;
      } else if (rawTasks.length === 1) {
        hint.textContent = "已从 MCAP 自动读取。未修改时按 episode 元数据写入；可直接编辑后人工覆盖缺失 task。";
      } else if (detected) {
        hint.textContent = "MCAP 中未发现任务文本，当前显示已有 HDF5/LeRobot task；可人工修改。";
      } else {
        hint.textContent = "MCAP 中未发现任务文本；如需补写，请人工输入。";
      }
    }

    async function refreshStatus() {
      if (!validateRequired()) return;
      latestLerobotStageSplit = null;
      splitStatusLoading = true;
      updateLerobotStageSplitControl();
      const requestId = ++latestStatusRequestId;
      if (statusAbortController) {
        statusAbortController.abort();
      }
      const controller = new AbortController();
      statusAbortController = controller;
      let data;
      try {
        data = await postJson("/api/status", payload(), {signal: controller.signal});
      } catch (err) {
        if (err?.name === "AbortError") return;
        if (requestId === latestStatusRequestId) {
          splitStatusLoading = false;
          latestLerobotStageSplit = {available:false,reason:"切分条件读取失败，请刷新状态重试"};
          updateLerobotStageSplitControl();
        }
        throw err;
      }
      if (requestId !== latestStatusRequestId) return;
      if (statusAbortController === controller) {
        statusAbortController = null;
      }
      const cfg = data.config;
      syncCameraVariantControls(cfg);
      splitStatusLoading = false;
      latestLerobotStageSplit = data.lerobot_stage_split || null;
      document.getElementById("lerobotRoot").value = cfg.lerobot_root;
      updateLerobotStageSplitControl();
      const configuredStationaryThreshold = Number(cfg.stationary_threshold);
      if (STATIONARY_THRESHOLD_LEVELS.has(configuredStationaryThreshold)) {
        document.getElementById("stationaryThreshold").value = String(configuredStationaryThreshold);
      }
      const inputMcapParent = fieldValue("mcapParentPath");
      const inputMcap = fieldValue("mcapPath");
      const inputHdf5 = fieldValue("hdf5Root");
      const inputQc = fieldValue("qcRoot");
      const inputLerobot = fieldValue("lerobotRoot");
      syncSourceDatasetSelectToPath(inputMcap, inputHdf5);
      const rawMcapTasks = Array.isArray(data.raw_mcap_tasks) ? data.raw_mcap_tasks : [];
      const hdf5Tasks = Array.isArray(data.hdf5_tasks) ? data.hdf5_tasks : [];
      const lerobotTasks = Array.isArray(data.lerobot_tasks) ? data.lerobot_tasks : [];
      syncDetectedTaskText(rawMcapTasks, hdf5Tasks, lerobotTasks);
      const pathRows = [
        ["MCAP 父级目录", inputMcapParent || "(未填写)"],
        ["MCAP 数据集路径", cfg.mcap_path_provided ? cfg.mcap_path : "(原始 MCAP 不存在，仅使用现有数据)"],
        ["HDF5 路径", cfg.hdf5_root],
        ["质检报告目录", cfg.qc_root],
        ["LeRobot 路径", cfg.lerobot_root],
      ];
      document.getElementById("pathRows").innerHTML = pathRows.map(([label, value]) =>
        `<div class="dataset-path-row">
          <span class="dataset-path-label">${escapeHtml(label)}</span>
          <span class="dataset-path-value">${escapeHtml(value)}</span>
        </div>`
      ).join("");
      const summary = data.summary || {};
      const qcOverview = data.qc_overview || {};
      const qcRecords = Array.isArray(qcOverview.records) ? qcOverview.records : [];
      latestQcRecordByEpisode = new Map(qcRecords.map(item => [String(item.episode_id || ""), item]));
      renderQcOverview(qcOverview);
      renderQualityCheckItems(data.quality_check_items);
      latestStationaryThreshold = Number(data.stationary_threshold || 15);
      document.getElementById("selectStationaryOver15Btn").textContent = `批量选择最长静止帧>${latestStationaryThreshold}`;
      latestEpisodes = Array.isArray(data.episodes) ? data.episodes : [];
      const currentIds = new Set(latestEpisodes.map(item => String(item.episode_id || "")));
      selectedEpisodes = new Set(selectedList().filter(name => currentIds.has(name)));
      const rows = latestEpisodes.map(item => {
        const cameraMissing = cameraMissingForSelection(item);
        return `
        <tr>
          <td><input class="episode-check" type="checkbox" data-episode="${escapeHtml(item.episode_id || "")}" ${selectedEpisodes.has(String(item.episode_id || "")) ? "checked" : ""} /></td>
          <td>${escapeHtml(item.episode_id || "")}</td>
          <td>${escapeHtml(formatChartValue(item.fps, "", 2, true))}</td>
          <td>${escapeHtml(item.frame_count ?? "")}</td>
          <td>${escapeHtml(cameraMissing === null ? "" : cameraMissing)}</td>
          <td>${badge(item.collection_status)}</td>
          <td>${qualityGradeControl(item)}</td>
          <td>${escapeHtml(String(item.quality_description || "").slice(0, 240))}</td>
          <td>${escapeHtml(item.warning_count ?? "")}</td>
          <td class="warning-cell">${escapeHtml(String(item.warning_text || "").slice(0, 720))}</td>
          <td>${escapeHtml(item.longest_stationary_display ?? "")}</td>
        </tr>`;
      }).join("");
      document.getElementById("episodeRows").innerHTML = rows || `<tr><td colspan="11">暂无 episode</td></tr>`;
      bindEpisodeSelection();
      bindQualityGradeControls();
      renderRenumberMapping(data);
      updateAlohaBaseActionOption();
    }

    function updateLerobotStageSplitControl() {
      const isZerith = document.getElementById("robotType").value === "zerith";
      const control = document.getElementById("lerobotStageSplitControl");
      const button = document.getElementById("splitLerobotStagesBtn");
      const reason = document.getElementById("lerobotStageSplitReason");
      const paths = document.getElementById("lerobotStageSplitPaths");
      control.classList.toggle("hidden", !isZerith);
      if (!isZerith) {
        button.disabled = true;
        button.title = "仅零次方机器人支持按左右手阶段切分 LeRobot";
        reason.textContent = button.title;
        reason.classList.add("unavailable");
        paths.textContent = "";
        return;
      }

      const selected = Boolean(fieldValue("hdf5Root") && fieldValue("datasetName"));
      const status = latestLerobotStageSplit;
      const available = selected && !splitStatusLoading && !splitJobRunning && Boolean(status?.available);
      const statusReason = !selected ? "请先选择数据集" : splitJobRunning ? "正在切分左右手阶段" : splitStatusLoading ? "正在检查切分条件…" : String(
        status?.reason || "请先生成 LeRobot 数据，再刷新状态"
      );
      button.setAttribute("aria-busy", String(splitJobRunning));
      button.textContent = splitJobRunning ? "正在切分…" : "按左右手阶段切分";
      button.disabled = !available;
      button.title = statusReason;
      reason.textContent = statusReason;
      reason.classList.toggle("unavailable", !available);
      const gradeRows = Array.isArray(status?.grades) ? status.grades : [];
      paths.textContent = gradeRows.map(item => {
        const compatibility = item.compatible
          ? `两阶段预检通过（${Number(item.checked_episode_count || 0)} 条）`
          : `不可切分：${String(item.reason || "阶段预检未通过")}`;
        return [
          `${String(item.grade || "?")} · ${compatibility}`,
          `源: ${String(item.source_dataset || "")}`,
          `左手: ${String(item.left_output || "")}`,
          `右手: ${String(item.right_output || "")}`,
        ].join("\n");
      }).join("\n\n");
    }

    function updateAlohaBaseActionOption() {
      const isAloha = document.getElementById("robotType").value === "aloha";
      const isZerith = document.getElementById("robotType").value === "zerith";
      const label = document.getElementById("alohaBaseActionLabel");
      const input = document.getElementById("alohaIncludeBaseAction");
      input.disabled = !isAloha;
      label.classList.toggle("disabled", !isAloha);
      const optimize = document.getElementById("optimizeHdf5Btn");
      optimize.disabled = !isZerith;
      optimize.title = isZerith ? "按采集时间原地重编号为 episode1…N，不保留旧目录备份" : "仅零次方 UUID HDF5 数据需要此优化";
      document.getElementById("stationaryThresholdControl").classList.toggle("hidden", !isZerith);
      updateLerobotStageSplitControl();
    }

    function renderRenumberMapping(data) {
      const plan = Array.isArray(data.renumber_plan) ? data.renumber_plan : [];
      if (!data.renumber_plan_exists) {
        document.getElementById("mappingInfo").textContent = "未生成编号计划";
        document.getElementById("mappingStats").innerHTML = "";
        document.getElementById("mappingRows").innerHTML = `<tr><td colspan="6">点击“生成编号计划”后显示映射</td></tr>`;
        return;
      }
      const gradeGroups = data.renumber_grade_groups || {};
      const gradeCounts = ["A", "B", "C", "F"].map(grade => {
        const fromGroup = Number(gradeGroups?.[grade]?.count);
        const count = Number.isFinite(fromGroup)
          ? fromGroup
          : plan.filter(item => String(item.quality_grade || "A").toUpperCase() === grade).length;
        return {grade, count};
      });
      document.getElementById("mappingInfo").textContent = `编号计划: ${data.renumber_plan_path}`;
      document.getElementById("mappingStats").innerHTML = [
        kpi("总数", `${plan.length} 条`),
        ...gradeCounts.map(item => kpi(`等级 ${item.grade}`, `${item.count} 条`)),
      ].join("");
      document.getElementById("mappingRows").innerHTML = plan.map(item => `
        <tr>
          <td>${badge(item.quality_grade || "")}</td>
          <td>${escapeHtml(item.source_mcap_episode_name || item.source_episode_name || "")}</td>
          <td>${escapeHtml(item.hdf5_episode_name || item.source_episode_name || "")}</td>
          <td>${escapeHtml(item.lerobot_dataset_dir || "")}</td>
          <td>${escapeHtml(item.lerobot_episode_name || "")}</td>
          <td>${escapeHtml(item.lerobot_episode_index ?? "")}</td>
        </tr>`).join("") || `<tr><td colspan="6">编号计划为空</td></tr>`;
    }

    async function runStage(stage) {
      if (!validateRequired(stage)) return;
      const extra = {stage};
      if (stage === "repair" || stage === "delete") {
        const names = selectedList();
        if (!names.length) {
          setPanel("qc");
          document.getElementById("log").textContent = "请先在质检报告表格中选择 episode。";
          return;
        }
        if (stage === "delete" && !confirm(`确定删除选中的 ${names.length} 条 HDF5 episode？不会删除 raw MCAP。`)) {
          return;
        }
        if (stage === "repair") {
          const threshold = Number(document.getElementById("stationaryThreshold").value);
          if (!confirm(`确定按 ${threshold} 帧档位处理选中的 ${names.length} 条 episode？连续静止超过 ${threshold} 帧时会均匀保留最多 ${threshold} 帧，删除的帧不可恢复。`)) {
            return;
          }
        }
        extra.episode_names = names;
      }
      if ((stage === "delete_grade_c_qc" || stage === "delete_failed_qc") && !confirm("确定删除所有质量等级为 C 的 HDF5 episode 并重新质检？不会删除 raw MCAP。")) {
        return;
      }
      if (stage === "optimize_hdf5" && !confirm("确定按采集时间原地重命名为 episode1、episode2…并重新质检？成功后不保留旧目录或数据备份，仅保留 UUID 映射元数据用于溯源。")) {
        return;
      }
      if (stage === "lerobot" && !confirm("按等级生成会覆盖已存在的同名 A/B/C/F LeRobot 等级目录，旧目录不会备份；各等级独立生成，中途失败不会回滚已经完成的等级。确定继续吗？")) {
        return;
      }
      if (stage === "split_lerobot_stages") {
        const splitStatus = latestLerobotStageSplit;
        if (!splitStatus?.available) {
          document.getElementById("log").textContent = String(
            splitStatus?.reason || "当前零次方 LeRobot 数据不可执行阶段切分，请先刷新状态。"
          );
          updateLerobotStageSplitControl();
          return;
        }
        const gradeRows = Array.isArray(splitStatus.grades) ? splitStatus.grades : [];
        const outputs = gradeRows.flatMap(item => [item.left_output, item.right_output])
          .filter(Boolean).join("\n");
        const grades = gradeRows.map(item => item.grade).filter(Boolean).join("/");
        const message = [
          `确定将零次方 LeRobot 的 ${grades} 等级按左右手阶段切分吗？`,
          "将使用 --overwrite 覆盖下列输出目录，不保留旧输出备份；源 A/B/C/F 等级目录不会被修改。",
          outputs,
          "各等级逐个执行；中途失败不会回滚已成功完成的其他等级。",
        ].join("\n\n");
        if (!confirm(message)) return;
      }
      document.querySelectorAll("button").forEach(btn => btn.disabled = true);
      try {
        const data = await postJson("/api/run", payload(extra));
        if (data.path) {
          document.getElementById("log").textContent = `编号计划已生成: ${data.path}`;
          await refreshStatus();
          setPanel("mapping");
          return;
        }
        currentJob = data.job.id;
        splitJobRunning = stage === "split_lerobot_stages";
        updateLerobotStageSplitControl();
        resetJobLog();
        pollJob();
      } catch (err) {
        document.getElementById("log").textContent = String(err);
      } finally {
        document.querySelectorAll("button").forEach(btn => btn.disabled = false);
        updateAlohaBaseActionOption();
      }
    }

    async function pollJob() {
      if (!currentJob) return;
      try {
        const res = await fetch(`/api/jobs/${currentJob}?cursor=${jobLogCursor}`);
        const job = await res.json();
        if (!res.ok || job.error) throw new Error(job.error || res.statusText);
        if (job.log_truncated) {
          jobLogLines = [];
        }
        appendJobLog(job.log || []);
        const nextCursor = Number(job.log_cursor);
        if (Number.isFinite(nextCursor)) jobLogCursor = nextCursor;
        if (job.status === "running" || job.status === "queued") {
          clearTimeout(jobTimer);
          jobTimer = setTimeout(pollJob, 1200);
        } else {
          splitJobRunning = false;
          updateLerobotStageSplitControl();
          await refreshStatus();
          const replayMessages = await refreshOpenReplaysSilently();
          appendJobLog(replayMessages);
          currentJob = null;
        }
      } catch (err) {
        appendJobLog([`日志刷新失败: ${err}`]);
        clearTimeout(jobTimer);
        jobTimer = setTimeout(pollJob, 2000);
      }
    }

    async function stopCurrentJob() {
      if (!confirm("确定停止当前正在运行的任务？")) return;
      const target = currentJob ? `/api/jobs/${encodeURIComponent(currentJob)}/stop` : "/api/jobs/current/stop";
      try {
        const data = await postJson(target, {});
        if (data.job_id) currentJob = data.job_id;
        appendJobLog([`已发送停止请求: ${currentJob || data.job_id || "current"}`]);
        clearTimeout(jobTimer);
        pollJob();
      } catch (err) {
        appendJobLog([`停止失败: ${err}`]);
      }
    }

    async function runQcAfterReplayFrameDelete(detail = {}) {
      await refreshStatus();
      if (currentJob) {
        appendJobLog(["HDF5 回放已删帧；当前已有任务在运行，未自动启动复检。"]);
        return;
      }
      if (!validateRequired("qc")) return;
      const episode = detail?.episode_name || detail?.episode_dir || "";
      resetJobLog(`HDF5 回放已删帧${episode ? `: ${episode}` : ""}，开始自动复检...`);
      try {
        const data = await postJson("/api/run", payload({stage: "qc"}));
        currentJob = data.job.id;
        pollJob();
      } catch (err) {
        appendJobLog([`自动复检启动失败: ${err}`]);
      }
    }

    async function refreshOpenReplaysSilently() {
      const messages = [];
      const replayFrame = document.getElementById("replayFrame");
      if (replayFrame.src) {
        try {
          const data = await postJson("/api/replay/start", payload());
          replayFrame.src = data.url || "/replay/";
        } catch (err) {
          messages.push(`HDF5 回放静默刷新失败: ${err}`);
        }
      }
      setPanel(activePanel);
      return messages;
    }

    async function startReplay() {
      setPanel("replay");
      if (!validateRequired("replay")) return;
      try {
        const data = await postJson("/api/replay/start", payload());
        const url = data.url || "/replay/";
        document.getElementById("replayFrame").src = url;
        document.getElementById("log").textContent = `回放已启动: ${url}`;
      } catch (err) {
        document.getElementById("replayFrame").removeAttribute("src");
        document.getElementById("log").textContent = String(err);
      }
    }

    document.querySelectorAll("[data-stage]").forEach(btn => {
      btn.addEventListener("click", () => runStage(btn.dataset.stage));
    });
    document.getElementById("lerobotVisualizationWorkspaceBtn").addEventListener("click", () => setWorkspace("lerobot-visualization"));
    document.getElementById("collectionWorkspaceBtn").addEventListener("click", () => setWorkspace("collection"));
    document.getElementById("lerobotWorkspaceBtn").addEventListener("click", () => setWorkspace("lerobot"));
    document.getElementById("manualScreeningWorkspaceBtn").addEventListener("click", () => setWorkspace("manual-screening"));
    document.getElementById("stopJobBtn").addEventListener("click", stopCurrentJob);
    document.getElementById("overviewTab").addEventListener("click", () => setPanel("overview"));
    document.getElementById("qcTab").addEventListener("click", () => setPanel("qc"));
    document.getElementById("mappingTab").addEventListener("click", () => setPanel("mapping"));
    document.getElementById("replayTab").addEventListener("click", () => {
      setPanel("replay");
      if (!document.getElementById("replayFrame").src) {
        startReplay();
      }
    });
    document.getElementById("refreshBtn").addEventListener("click", refreshStatus);
    document.getElementById("replayBtn").addEventListener("click", startReplay);
    document.getElementById("sourceDatasetSelect").addEventListener("change", () => {
      applySourceDatasetChoice().catch(err => document.getElementById("log").textContent = String(err));
    });
    document.getElementById("hostMachine").addEventListener("change", async event => {
      currentMachine = String(event.target.value || "h200");
      document.getElementById("customDatasetControls").classList.add("hidden");
      document.getElementById("customDatasetPath").value = "";
      clearDatasetInputs();
      clearDatasetStatusViews(`正在扫描 ${currentMachine === "h200" ? "H200" : "AgileX"} 数据集…`);
      await loadSourceDatasetChoices(currentMachine);
      clearDatasetStatusViews("请选择数据集目录");
    });
    document.getElementById("applyCustomDatasetBtn").addEventListener("click", applyCustomDatasetPath);
    document.getElementById("customDatasetPath").addEventListener("keydown", event => {
      if (event.key === "Enter") applyCustomDatasetPath();
    });
    document.getElementById("taskText").addEventListener("input", () => {
      const current = fieldValue("taskText");
      taskTextManuallyEdited = current !== autoTaskText;
      const hint = document.getElementById("taskTextHint");
      document.getElementById("taskText").classList.toggle("auto-value", !taskTextManuallyEdited && Boolean(autoTaskText));
      hint.textContent = taskTextManuallyEdited
        ? "已人工修改；只补写缺少 task 的 HDF5，不覆盖已有 task。"
        : "已恢复 MCAP 自动读取；转换时继续按每个 episode 的元数据写入。";
    });
    document.getElementById("clearSelectionBtn").addEventListener("click", () => {
      selectedEpisodes.clear();
      syncSelectionCheckboxes();
    });
    document.getElementById("selectGradeCBtn").addEventListener("click", () => {
      selectEpisodesByPredicate(item => String(item.quality_grade || "").trim().toUpperCase() === "C");
    });
    document.getElementById("selectCameraMissingOver20Btn").addEventListener("click", () => {
      selectEpisodesByPredicate(item => {
        const value = cameraMissingForSelection(item);
        return value !== null && value > 20;
      });
    });
    document.getElementById("selectStationaryOver15Btn").addEventListener("click", () => {
      selectEpisodesByPredicate(item => {
        const value = longestStationaryForSelection(item);
        return value !== null && value > latestStationaryThreshold;
      });
    });
    document.getElementById("mcapParentPath").addEventListener("change", () => updateMcapDatasetsFromParent({refresh: true}));
    document.getElementById("mcapParentPath").addEventListener("blur", () => updateMcapDatasetsFromParent({refresh: false}));
    document.getElementById("mcapDatasetSelect").addEventListener("change", async event => {
      const value = String(event.target.value || "");
      if (!value) return;
      resetTaskTextForDataset();
      document.getElementById("mcapPath").value = value;
      await refreshStatus();
    });
    document.getElementById("mcapPath").addEventListener("change", refreshStatus);
    document.getElementById("datasetName").addEventListener("change", refreshStatus);
    document.getElementById("hdf5Root").addEventListener("change", refreshStatus);
    document.getElementById("qcRoot").addEventListener("change", refreshStatus);
    document.getElementById("lerobotRoot").addEventListener("change", refreshStatus);
    async function updateProfileForRobot() {
      const robot = document.getElementById("robotType").value;
      updateAlohaBaseActionOption();
      if (!robot) {
        await refreshStatus();
        return;
      }
      const query = new URLSearchParams({
        robot_type: robot,
        mcap_path: document.getElementById("mcapPath").value,
        hdf5_root: document.getElementById("hdf5Root").value,
        dataset_name: document.getElementById("datasetName").value,
      });
      try {
        const res = await fetch(`/api/defaults?${query}`);
        const data = await res.json();
        if (data.config && data.config.profile) {
          document.getElementById("profile").value = data.config.profile;
        }
        if (data.config) syncCameraVariantControls(data.config);
      } catch (err) {
        document.getElementById("log").textContent = String(err);
      }
      await refreshStatus();
    }

    document.getElementById("robotType").addEventListener("change", updateProfileForRobot);
    document.getElementById("cameraCount").addEventListener("change", async () => {
      const cameraCount = document.getElementById("cameraCount");
      document.getElementById("headCameraSource").disabled = cameraCount.value !== "3";
      document.getElementById("profile").value = "";
      applyProcessedDatasetVariant(selectedSourceDatasetChoice());
      await refreshStatus();
    });
    document.getElementById("headCameraSource").addEventListener("change", async () => {
      document.getElementById("profile").value = "";
      applyProcessedDatasetVariant(selectedSourceDatasetChoice());
      await refreshStatus();
    });
    document.getElementById("alohaIncludeBaseAction").addEventListener("change", refreshStatus);
    document.getElementById("stationaryThreshold").addEventListener("change", event => {
      const value = Number(event.target.value);
      if (STATIONARY_THRESHOLD_LEVELS.has(value)) {
        localStorage.setItem(STATIONARY_THRESHOLD_STORAGE_KEY, String(value));
      }
      refreshStatus();
    });
    document.getElementById("profile").addEventListener("change", refreshStatus);
    window.addEventListener("message", event => {
      if (event?.data?.type === "pipeline-manual-failure-updated") {
        refreshStatus().catch(err => document.getElementById("log").textContent = String(err));
      } else if (event?.data?.type === "pipeline-hdf5-frames-deleted") {
        runQcAfterReplayFrameDelete(event.data.payload || {}).catch(err => {
          document.getElementById("log").textContent = String(err);
        });
      }
    });
    initPathHistory();
    const savedStationaryThreshold = Number(localStorage.getItem(STATIONARY_THRESHOLD_STORAGE_KEY));
    if (STATIONARY_THRESHOLD_LEVELS.has(savedStationaryThreshold)) {
      document.getElementById("stationaryThreshold").value = String(savedStationaryThreshold);
    }
    setWorkspace("collection");
    updateAlohaBaseActionOption();
    bootstrapCameraVariantControls()
      .catch(err => document.getElementById("log").textContent = String(err));
    loadSourceDatasetChoices()
      .catch(err => document.getElementById("log").textContent = String(err))
      .finally(() => {
        if (fieldValue("mcapPath") || fieldValue("hdf5Root") || fieldValue("lerobotRoot")) {
          refreshStatus().catch(err => document.getElementById("log").textContent = String(err));
        }
      });
  </script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local pipeline web console.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8890)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Pipeline web console: http://127.0.0.1:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for process in REPLAY_PROCESSES.values():
            if process.poll() is None:
                process.terminate()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
