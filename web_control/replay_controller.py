"""HDF5 episode discovery and single-owner real-robot replay control."""

from __future__ import annotations

from pathlib import Path
import math
import json
import os
import threading
import time
from typing import Any, Callable

import h5py
import numpy as np

from .replay_timing import recorded_timeline, resample_recorded_frames

from .robot_service import (
    MOTOR_SPEC_BY_ID,
    POLICY_WIRE_MOTOR_IDS,
    RobotCommandRejected,
    RobotConflict,
    RobotService,
)


DEFAULT_DATASET_DIR = "/data/zerith_data/Pepsi_DailyCOrangeJuice2"
REPLAY_CONFIRMATION = "我确认实体急停可用并开始真机回放"
REPLAY_SOURCES = frozenset(("state", "action"))
REPLAY_MODES = frozenset(("arms", "full"))
REPLAY_SPEED_MIN = 0.5
REPLAY_SPEED_MAX = 2.0
REPLAY_DISCOVERY_ROOT = Path("/data")
MAX_DISCOVERY_EPISODE_FILES = 10_000
MAX_EPISODE_FILES = 2000
MAX_EPISODE_FRAMES = 200_000
BASE_ZERO_TOLERANCE = 1e-9
ACTIVE_PHASES = frozenset(("loading", "aligning", "running", "stopping"))

SOURCE_DATASETS = {
    "state": {
        "arm": "observation/state/arm/position",
        "effector": "observation/state/effector/position",
        "waist": "observation/state/waist/position",
        "head": "observation/state/head/position",
        "base": "observation/state/base/velocity",
    },
    "action": {
        "arm": "action/arm/position",
        "effector": "action/effector/position",
        "waist": "action/waist/position",
        "head": "action/head/position",
        "base": "action/base/velocity",
    },
}


def _json_scalar(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, np.generic):
        return value.item()
    return value


def _finite_float(value: Any, name: str) -> float:
    if isinstance(value, (bool, np.bool_)):
        raise RobotCommandRejected(f"{name} 不接受布尔值")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise RobotCommandRejected(f"{name} 必须是数值") from exc
    if not math.isfinite(parsed):
        raise RobotCommandRejected(f"{name} 必须是有限数")
    return parsed


def _dataset_root(value: Any) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise RobotCommandRejected("数据集目录不能为空")
    if len(value) > 4096:
        raise RobotCommandRejected("数据集目录过长")
    path = Path(value.strip()).expanduser()
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise RobotCommandRejected(f"数据集目录不存在或不可读: {path}") from exc
    if not path.is_dir():
        raise RobotCommandRejected(f"数据集路径不是目录: {path}")
    return path


def _episode_path(root: Path, relative_path: Any) -> Path:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise RobotCommandRejected("请选择一条 HDF5 数据")
    relative = Path(relative_path.strip())
    if relative.is_absolute():
        raise RobotCommandRejected("episode 必须使用扫描结果中的相对路径")
    try:
        path = (root / relative).resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as exc:
        raise RobotCommandRejected("episode 不在所选数据集目录中") from exc
    if not path.is_file() or path.suffix.lower() not in (".hdf5", ".h5"):
        raise RobotCommandRejected("episode 不是可读的 HDF5 文件")
    return path


ALIGNED_FORMAT = "icra_wbc_aligned_joints"
ALIGNED_FIELDS = (
    [f"left.joint{i}" for i in range(1, 8)] + ["left.gripper"]
    + [f"right.joint{i}" for i in range(1, 8)] + ["right.gripper"]
    + ["lift.height", "waist.pitch", "waist.yaw", "head.yaw", "head.pitch",
       "speed.linear", "speed.angular"]
)


def _is_aligned(file: h5py.File) -> bool:
    return _json_scalar(file.attrs.get("format", "")) == ALIGNED_FORMAT


def _load_aligned_source(file: h5py.File, source: str) -> np.ndarray:
    fields = file.attrs.get("state_fields_json")
    if fields is not None:
        try:
            valid_fields = json.loads(_json_scalar(fields)) == ALIGNED_FIELDS
        except (TypeError, ValueError) as exc:
            raise RobotCommandRejected("state_fields_json 无法解析") from exc
        if not valid_fields:
            raise RobotCommandRejected("aligned_joints 的 23 维字段顺序不匹配")
    keys = sorted((key for key in file if key.isascii() and key.isdecimal()), key=int)
    if not 1 <= len(keys) <= MAX_EPISODE_FRAMES:
        raise RobotCommandRejected(f"HDF5 可回放帧数必须在 1..{MAX_EPISODE_FRAMES}")
    if [int(key) for key in keys] != list(range(len(keys))):
        raise RobotCommandRejected("aligned_joints 帧号必须从 0 开始且连续")
    values = np.empty((len(keys), 23), dtype=np.float64)
    for index, key in enumerate(keys):
        dataset_path = f"{key}/{source}/vector"
        if dataset_path not in file:
            raise RobotCommandRejected(f"缺少 /{dataset_path}")
        dataset = file[dataset_path]
        if not isinstance(dataset, h5py.Dataset) or dataset.shape != (23,):
            raise RobotCommandRejected(f"/{dataset_path} 形状应为 (23,)")
        try:
            values[index] = np.asarray(dataset[:], dtype=np.float64)
        except (TypeError, ValueError, OSError) as exc:
            raise RobotCommandRejected(f"无法读取 /{dataset_path} 数值字段") from exc
    if not np.isfinite(values).all():
        raise RobotCommandRejected(f"{source} 包含 NaN 或 Inf")
    # Float32 export can represent the 0.8 m SDK endpoint as 0.8000000119.
    # Snap only sub-1e-7 boundary roundoff; real limit violations stay rejected.
    for column, motor_id in enumerate(POLICY_WIRE_MOTOR_IDS):
        spec = MOTOR_SPEC_BY_ID[motor_id]
        for bound in (spec.minimum, spec.maximum):
            near = np.abs(values[:, column] - bound) <= 1e-7
            values[near, column] = bound
    return values


def _control_rate(file: h5py.File) -> float:
    candidate = file.attrs.get("fps") if _is_aligned(file) else file.attrs.get("control_frequency")
    if candidate is None and "timestamp" in file:
        candidate = file["timestamp"].attrs.get("rate_hz")
    rate = _finite_float(30.0 if candidate is None else candidate, "control_frequency")
    if not 1.0 <= rate <= 500.0:
        raise RobotCommandRejected("control_frequency 必须在 1..500 Hz")
    return rate


def _source_shape(file: h5py.File, source: str) -> tuple[int, dict[str, tuple[int, ...]]]:
    expected_widths = {"arm": 14, "effector": 2, "waist": 3, "head": 2, "base": 2}
    shapes: dict[str, tuple[int, ...]] = {}
    frame_count: int | None = None
    for name, dataset_path in SOURCE_DATASETS[source].items():
        if dataset_path not in file:
            raise RobotCommandRejected(f"缺少 /{dataset_path}")
        dataset = file[dataset_path]
        shape = tuple(int(value) for value in dataset.shape)
        shapes[name] = shape
        if len(shape) != 2 or shape[1] != expected_widths[name]:
            raise RobotCommandRejected(
                f"/{dataset_path} 形状应为 (T,{expected_widths[name]})，实际 {shape}"
            )
        if frame_count is None:
            frame_count = shape[0]
        elif shape[0] != frame_count:
            raise RobotCommandRejected("同一来源的 HDF5 字段帧数不一致")
    if frame_count is None or frame_count < 1:
        raise RobotCommandRejected("HDF5 没有可回放帧")
    if frame_count > MAX_EPISODE_FRAMES:
        raise RobotCommandRejected(f"单条 HDF5 帧数超过 {MAX_EPISODE_FRAMES}")
    return frame_count, shapes


def _load_source(file: h5py.File, source: str) -> np.ndarray:
    if _is_aligned(file):
        return _load_aligned_source(file, source)
    frame_count, _ = _source_shape(file, source)
    paths = SOURCE_DATASETS[source]
    try:
        arm = np.asarray(file[paths["arm"]][:], dtype=np.float64)
        effector = np.asarray(file[paths["effector"]][:], dtype=np.float64)
        waist = np.asarray(file[paths["waist"]][:], dtype=np.float64)
        head = np.asarray(file[paths["head"]][:], dtype=np.float64)
        base = np.asarray(file[paths["base"]][:], dtype=np.float64)
    except (TypeError, ValueError, OSError) as exc:
        raise RobotCommandRejected(f"无法读取 {source} 数值字段: {exc}") from exc
    values = np.concatenate(
        (
            arm[:, :7],
            effector[:, :1],
            arm[:, 7:],
            effector[:, 1:2],
            waist,
            head,
            base,
        ),
        axis=1,
    )
    if values.shape != (frame_count, 23):
        raise RobotCommandRejected(f"{source} 拼接后形状不是 (T,23): {values.shape}")
    if not np.isfinite(values).all():
        raise RobotCommandRejected(f"{source} 包含 NaN 或 Inf")
    return values


def inspect_episode(path: Path, root: Path) -> dict[str, Any]:
    with h5py.File(path, "r") as file:
        sources: dict[str, bool] = {}
        source_errors: dict[str, str] = {}
        source_frames: dict[str, int] = {}
        base_abs_max: dict[str, float | None] = {}
        for source in sorted(REPLAY_SOURCES):
            try:
                if _is_aligned(file):
                    values = _load_aligned_source(file, source)
                    frames = len(values)
                    base = values[:, 21:23]
                else:
                    frames, _ = _source_shape(file, source)
                    base = np.asarray(
                        file[SOURCE_DATASETS[source]["base"]][:],
                        dtype=np.float64,
                    )
                if not np.isfinite(base).all():
                    raise RobotCommandRejected(f"{source} base 包含 NaN 或 Inf")
                sources[source] = True
                source_frames[source] = frames
                base_abs_max[source] = float(np.max(np.abs(base))) if base.size else 0.0
            except (KeyError, OSError, RobotCommandRejected, ValueError) as exc:
                sources[source] = False
                source_errors[source] = str(exc)
                base_abs_max[source] = None
        rate_hz = _control_rate(file)
        frame_count = max(source_frames.values(), default=0)
        if not any(sources.values()):
            raise RobotCommandRejected("state/action 均不符合回放格式: " + "; ".join(f"{key}: {error}" for key, error in source_errors.items()))
        timing = {'basis': 'nominal_rate'}
        duration_s = frame_count / rate_hz
        if not _is_aligned(file) and 'timestamp/t' in file:
            try:
                _, timing = recorded_timeline(file['timestamp/t'][:], frame_count, rate_hz)
            except ValueError as exc:
                raise RobotCommandRejected(str(exc)) from exc
            duration_s = timing['recorded_duration_s']
        return {
            "path": path.relative_to(root).as_posix(),
            "episode_id": str(_json_scalar(file.attrs.get("episode_id", file.attrs.get("source_demo", path.parent.name)))),
            "format": ALIGNED_FORMAT if _is_aligned(file) else "episode",
            "task_name": str(_json_scalar(file.attrs.get("task_name", ""))),
            "frames": frame_count,
            "rate_hz": rate_hz,
            "duration_s": duration_s,
            "timing": timing,
            "action_mode": str(_json_scalar(file.attrs.get("action_mode", "unknown"))),
            "sources": sources,
            "source_errors": source_errors,
            "base_abs_max": base_abs_max,
        }


def scan_dataset_directory(value: Any) -> dict[str, Any]:
    root = _dataset_root(value)
    candidates = sorted(
        path for pattern in ("*.hdf5", "*.h5") for path in root.rglob(pattern)
    )
    candidates = list(dict.fromkeys(candidates))
    if len(candidates) > MAX_EPISODE_FILES:
        raise RobotCommandRejected(
            f"目录内 HDF5 超过 {MAX_EPISODE_FILES} 个，请选择更具体的上一级目录"
        )
    episodes: list[dict[str, Any]] = []
    invalid: list[dict[str, str]] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
            episodes.append(inspect_episode(resolved, root))
        except (OSError, ValueError, KeyError, RobotCommandRejected) as exc:
            try:
                label = candidate.relative_to(root).as_posix()
            except ValueError:
                label = candidate.name
            invalid.append({"path": label, "error": str(exc)})
    return {
        "dataset_dir": str(root),
        "episodes": episodes,
        "count": len(episodes),
        "invalid": invalid,
        "invalid_count": len(invalid),
    }


def discover_replay_dataset_directories(
    root: str | Path = REPLAY_DISCOVERY_ROOT,
) -> dict[str, Any]:
    """Find replay dataset roots below ``/data`` without exposing file entries.

    Supported layouts are ``dataset/episode-id/episode.hdf5`` and
    ``dataset/demo-id/states/aligned_joints.h5``.  The
    UI therefore offers the directory above each episode directory, while the
    existing episode scanner remains responsible for listing individual files.
    Only files that pass the normal replay schema inspection contribute a
    directory.
    """

    base = Path(root)
    try:
        base = base.resolve(strict=True)
    except OSError as exc:
        raise RobotCommandRejected(f"回放目录发现根路径不存在或不可读: {base}") from exc
    if not base.is_dir():
        raise RobotCommandRejected(f"回放目录发现根路径不是目录: {base}")

    candidates: list[Path] = []
    truncated = False

    def ignore_walk_error(_error: OSError) -> None:
        return

    for current, directory_names, file_names in os.walk(
        base,
        topdown=True,
        onerror=ignore_walk_error,
        followlinks=False,
    ):
        directory_names[:] = sorted(
            name for name in directory_names if not name.startswith(".")
        )
        for file_name in sorted(file_names):
            if file_name.lower() not in ("episode.hdf5", "episode.h5", "aligned_joints.h5"):
                continue
            candidates.append(Path(current) / file_name)
            if len(candidates) >= MAX_DISCOVERY_EPISODE_FILES:
                truncated = True
                break
        if truncated:
            break

    directories: set[Path] = set()
    invalid_count = 0
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(base)
            episode_directory = resolved.parent
            if resolved.name.lower() == "aligned_joints.h5" and episode_directory.name == "states":
                episode_directory = episode_directory.parent
            dataset_directory = (
                episode_directory.parent
                if episode_directory != base
                else episode_directory
            )
            inspect_episode(resolved, dataset_directory)
            directories.add(dataset_directory)
        except (OSError, ValueError, KeyError, RobotCommandRejected):
            invalid_count += 1

    paths = [str(path) for path in sorted(directories, key=lambda item: str(item))]
    return {
        "root": str(base),
        "directories": paths,
        "count": len(paths),
        "inspected_episode_files": len(candidates),
        "invalid_count": invalid_count,
        "truncated": truncated,
    }


def load_replay_episode(
    dataset_dir: Any,
    relative_path: Any,
    source: Any,
    mode: Any,
    speed: Any,
) -> tuple[np.ndarray, dict[str, Any]]:
    root = _dataset_root(dataset_dir)
    path = _episode_path(root, relative_path)
    source_name = str(source).strip().lower()
    mode_name = str(mode).strip().lower()
    if source_name not in REPLAY_SOURCES:
        raise RobotCommandRejected("回放源只支持 state 或 action")
    if mode_name not in REPLAY_MODES:
        raise RobotCommandRejected("回放模式只支持 arms 或 full")
    speed_value = _finite_float(speed, "回放速度")
    if not REPLAY_SPEED_MIN <= speed_value <= REPLAY_SPEED_MAX:
        raise RobotCommandRejected("回放速度必须在 0.5x..2.0x")
    with h5py.File(path, "r") as file:
        if source_name == "action":
            action_mode = str(_json_scalar(file.attrs.get("action_mode", ""))).lower()
            if action_mode != "absolute":
                raise RobotCommandRejected(
                    f"action_mode={action_mode or 'missing'}，只支持 absolute 绝对目标"
                )
        frames = _load_source(file, source_name)
        rate_hz = _control_rate(file)
        metadata = inspect_episode(path, root)
        validate_replay_frames(frames, mode_name)
        timing = {'basis': 'nominal_rate', 'source_frames': len(frames),
                  'playback_frames': len(frames), 'resampled': False}
        if not _is_aligned(file) and 'timestamp/t' in file:
            try:
                frames, rate_hz, timing = resample_recorded_frames(
                    frames, file['timestamp/t'][:], rate_hz, MAX_EPISODE_FRAMES
                )
            except (ValueError, TypeError, OverflowError) as exc:
                raise RobotCommandRejected(str(exc)) from exc
            validate_replay_frames(frames, mode_name)
    metadata.update(
        {
            "dataset_dir": str(root),
            "source": source_name,
            "mode": mode_name,
            "speed": speed_value,
            "frames": len(frames),
            "timing": timing,
            "effective_rate_hz": rate_hz * speed_value,
            "effective_duration_s": len(frames) / (rate_hz * speed_value),
        }
    )
    return frames, metadata


def validate_replay_frames(frames: np.ndarray, mode: str) -> None:
    values = np.asarray(frames, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != 23 or values.shape[0] < 1:
        raise RobotCommandRejected(f"回放数据必须是 (T,23)，实际 {values.shape}")
    if not np.isfinite(values).all():
        raise RobotCommandRejected("回放数据包含 NaN 或 Inf")
    stop = 16 if mode == "arms" else 21
    for wire_index, motor_id in enumerate(POLICY_WIRE_MOTOR_IDS[:stop]):
        spec = MOTOR_SPEC_BY_ID[motor_id]
        column = values[:, wire_index]
        minimum = float(np.min(column))
        maximum = float(np.max(column))
        if minimum < spec.minimum or maximum > spec.maximum:
            raise RobotCommandRejected(
                f"回放维度 {wire_index}（{spec.label}/motor {motor_id}）范围 "
                f"[{minimum:g}, {maximum:g}] 超出 SDK [{spec.minimum:g}, {spec.maximum:g}] {spec.unit}"
            )
    if mode == "full":
        base_max = float(np.max(np.abs(values[:, 21:23])))
        if base_max > BASE_ZERO_TOLERANCE:
            raise RobotCommandRejected(
                "全23维回放检测到底盘线/角速度非零；当前 LOW_LEVEL 只能下发左右轮 rad/s，"
                "缺少可靠换算，已拒绝。请选择双臂模式或底盘为零的 action 数据"
            )


class ReplayController:
    """Load one episode and feed it through the existing RobotService owner."""

    def __init__(self, robot: RobotService, *, alignment_duration_s: float = 3.0) -> None:
        self.robot = robot
        self.alignment_duration_s = float(alignment_duration_s)
        self._lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._phase = "idle"
        self._fault: str | None = None
        self._last_stop_reason: str | None = None
        self._progress = 0
        self._total = 0
        self._started_monotonic: float | None = None
        self._metadata: dict[str, Any] | None = None
        self._lease: str | None = None

    def scan(self, dataset_dir: Any) -> dict[str, Any]:
        return scan_dataset_directory(dataset_dir)

    def directories(self) -> dict[str, Any]:
        return discover_replay_dataset_directories()

    def status(self) -> dict[str, Any]:
        with self._lock:
            phase = self._phase
            started = self._started_monotonic
            result = {
                "phase": phase,
                "active": phase in ACTIVE_PHASES,
                "fault": self._fault,
                "last_stop_reason": self._last_stop_reason,
                "progress": self._progress,
                "total": self._total,
                "metadata": dict(self._metadata) if self._metadata else None,
            }
        result["elapsed_s"] = (
            max(0.0, time.monotonic() - started) if started is not None else None
        )
        result["progress_ratio"] = (
            result["progress"] / result["total"] if result["total"] else 0.0
        )
        return result

    def start(
        self,
        dataset_dir: Any,
        relative_path: Any,
        source: Any,
        mode: Any,
        speed: Any,
        lease: str,
        *,
        confirmation: Any,
    ) -> dict[str, Any]:
        if confirmation != REPLAY_CONFIRMATION:
            raise RobotCommandRejected("缺少真机回放确认")
        with self._start_lock:
            self._stop_event.clear()
            with self._lock:
                if self._phase in ACTIVE_PHASES:
                    raise RobotConflict("已有真机回放正在执行，请先停止")
                self._phase = "loading"
                self._fault = None
                self._last_stop_reason = None
                self._progress = 0
                self._total = 0
                self._metadata = None
            try:
                frames, metadata = load_replay_episode(
                    dataset_dir,
                    relative_path,
                    source,
                    mode,
                    speed,
                )
                self.robot.validate_motion_ready(lease, renew_lease=False)
                if self._stop_event.is_set():
                    raise RobotConflict("回放在启动期间已被操作员停止")
            except BaseException as exc:
                with self._lock:
                    stopped = self._stop_event.is_set()
                    self._phase = "idle" if stopped else "fault"
                    self._fault = None if stopped else str(exc)
                raise
            with self._lock:
                self._phase = "aligning"
                self._lease = lease
                self._metadata = metadata
                self._total = int(len(frames))
                self._started_monotonic = time.monotonic()
            thread = threading.Thread(
                target=self._run,
                args=(frames, metadata, lease),
                name="hdf5-real-replay",
                daemon=True,
            )
            with self._lock:
                self._thread = thread
            thread.start()
            return self.status()

    def stop(self, *, reason: str = "operator_stop") -> dict[str, Any]:
        reason = str(reason).strip()[:128] or "operator_stop"
        # Serialize with start through the point where its worker is visible.
        # This closes the otherwise dangerous race where STOP could land after
        # HDF5 loading but before the replay thread had entered RobotService.
        with self._start_lock:
            with self._lock:
                active = self._phase in ACTIVE_PHASES
                thread = self._thread
                if active:
                    self._phase = "stopping"
                self._last_stop_reason = reason
            self._stop_event.set()
            stop_error: str | None = None
            if active:
                try:
                    self.robot.emergency_stop_motion(reason=f"replay:{reason}")
                except BaseException as exc:
                    stop_error = str(exc)
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=12.0)
            with self._lock:
                if self._phase == "stopping":
                    self._phase = "idle"
                if stop_error:
                    self._fault = stop_error
                    self._phase = "fault"
        return self.status()

    def close(self) -> None:
        self.stop(reason="service_shutdown")

    def _run(self, frames: np.ndarray, metadata: dict[str, Any], lease: str) -> None:
        try:
            result = self.robot.replay_trajectory(
                lease,
                frames,
                mode=metadata["mode"],
                rate_hz=metadata["effective_rate_hz"],
                alignment_duration_s=self.alignment_duration_s,
                progress_callback=self._on_progress,
            )
            with self._lock:
                self._progress = int(result["frames_sent"])
                self._phase = "idle" if self._stop_event.is_set() else "completed"
                self._fault = None
        except BaseException as exc:
            stopped = self._stop_event.is_set()
            if not stopped:
                try:
                    self.robot.emergency_stop_motion(reason="replay_fault")
                except BaseException:
                    pass
            with self._lock:
                if stopped:
                    self._phase = "idle"
                else:
                    self._phase = "fault"
                    self._fault = str(exc)
        finally:
            with self._lock:
                self._thread = None
                self._lease = None

    def _on_progress(self, frames_sent: int) -> None:
        with self._lock:
            self._progress = int(frames_sent)
            if self._phase == "aligning":
                self._phase = "running"


__all__ = [
    "ACTIVE_PHASES",
    "DEFAULT_DATASET_DIR",
    "REPLAY_CONFIRMATION",
    "ReplayController",
    "discover_replay_dataset_directories",
    "inspect_episode",
    "load_replay_episode",
    "scan_dataset_directory",
    "validate_replay_frames",
]
