from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .profiles import RobotProfile


@dataclass(frozen=True)
class StateFrame:
    frame_idx: int
    timestamp: float
    state: list[float]


@dataclass(frozen=True)
class EpisodeData:
    root: Path
    episode_id: str
    meta: dict[str, Any]
    state_frames: list[StateFrame]
    actions: list[list[float]]
    camera_counts: dict[str, int]
    stationary_state_vectors: list[list[float]] = field(default_factory=list)
    stationary_action_vectors: list[list[float]] = field(default_factory=list)

    @property
    def n_frames(self) -> int:
        return len(self.state_frames)

    @property
    def duration_sec(self) -> float:
        if len(self.state_frames) < 2:
            return 0.0
        return max(0.0, self.state_frames[-1].timestamp - self.state_frames[0].timestamp)


def read_raw_episode(
    episode_dir: str | Path,
    profile: RobotProfile,
    *,
    actions_json: str | Path | None = None,
) -> EpisodeData:
    root = Path(episode_dir).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(root)
    if root.is_file() and root.suffix.lower() == ".parquet":
        return _read_parquet_episode(root, profile, actions_json=actions_json)
    hdf5_path = _find_g2_hdf5_path(root)
    if hdf5_path is not None:
        episode_root = _infer_hdf5_episode_root(root, hdf5_path)
        adapter = str(profile.raw.get("adapter") or "").lower()
        if adapter in {"zerith", "zerith_columnar"}:
            from dataqc.io import is_simulation
            if is_simulation(episode_root):
                from integrations.simulation import read_legacy_episode
                return read_legacy_episode(episode_root, profile, actions_json=actions_json)
            return _read_zerith_hdf5_episode(
                episode_root,
                hdf5_path,
                profile,
                actions_json=actions_json,
            )
        if adapter in {"aloha", "aloha_stationary"}:
            return _read_aloha_hdf5_episode(episode_root, hdf5_path, profile, actions_json=actions_json)
        return _read_g2_hdf5_episode(episode_root, hdf5_path, profile, actions_json=actions_json)
    meta = _read_json_if_exists(root / "meta.json")
    episode_id = str(meta.get("episode_id") or root.name)
    state_frames = _read_state_csv(root / "state.csv")
    actions = _read_actions_jsonl(root / "actions.jsonl", profile.action_dim)
    if actions_json:
        actions.extend(_read_action_payload(Path(actions_json), profile.action_dim))
    camera_counts = _count_cameras(root, profile)
    return EpisodeData(
        root=root,
        episode_id=episode_id,
        meta=meta,
        state_frames=state_frames,
        actions=actions,
        camera_counts=camera_counts,
    )


def _find_g2_hdf5_path(path: Path) -> Path | None:
    if path.is_dir():
        candidates = (
            path / "episode.hdf5",
            path / "episode.h5",
            path / "states" / "aligned_joints.h5",
            path / "states" / "aligned_joints.hdf5",
            path / "aligned_joints.h5",
            path / "aligned_joints.hdf5",
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None
    if not path.is_file():
        return None
    if path.suffix.lower() in {".h5", ".hdf5"}:
        return path
    try:
        import h5py  # type: ignore
    except ImportError:
        return None
    return path if h5py.is_hdf5(path) else None


def _infer_hdf5_episode_root(input_path: Path, hdf5_path: Path) -> Path:
    if input_path.is_dir():
        return input_path
    if hdf5_path.name.startswith("aligned_joints") and hdf5_path.parent.name == "states":
        return hdf5_path.parent.parent
    return hdf5_path.parent


def _read_g2_hdf5_episode(
    root: Path,
    hdf5_path: Path,
    profile: RobotProfile,
    *,
    actions_json: str | Path | None = None,
) -> EpisodeData:
    try:
        import h5py  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Reading G2 HDF5 episodes requires h5py. "
            "Install quality_pipeline/requirements.txt in the active venv."
        ) from exc

    state_frames: list[StateFrame] = []
    actions: list[list[float]] = []
    stationary_state_vectors: list[list[float]] = []
    stationary_action_vectors: list[list[float]] = []
    camera_counts = {camera.raw_key: 0 for camera in profile.cameras}
    fps = float(profile.raw.get("fps", {}).get("record") or 30.0)
    schema_detail: dict[str, Any] = {}

    with h5py.File(hdf5_path, "r") as f:
        frame_keys = _numeric_hdf5_keys(f.keys())
        if not frame_keys:
            raise ValueError(f"{hdf5_path}: missing numeric frame groups")

        timestamp0 = _read_hdf5_scalar(f, f"{frame_keys[0]}/main_timestamp")
        for idx, key in enumerate(frame_keys):
            timestamp_raw = _read_hdf5_scalar(f, f"{key}/main_timestamp")
            timestamp = _normalise_timestamp(timestamp_raw, timestamp0, idx, fps)
            state = _g2_state_from_hdf5_frame(f, key, profile.state_dim)
            action = _g2_action_from_hdf5_frame(f, key, profile.action_dim)
            stationary_state_vectors.append(_stationary_state_vector_from_hdf5_frame(f, key))
            stationary_action_vectors.append(_stationary_action_vector_from_hdf5_frame(f, key))
            frame_idx = int(key)
            state_frames.append(StateFrame(frame_idx=frame_idx, timestamp=timestamp, state=state))
            if len(action) == profile.action_dim and all(math.isfinite(v) for v in action):
                actions.append(action)
            for camera in profile.cameras:
                if f"{key}/timestamp/camera/{camera.raw_key}" in f:
                    camera_counts[camera.raw_key] += 1

    camera_counts = _fill_camera_counts_from_videos(root, profile, camera_counts)
    if actions_json:
        actions.extend(_read_action_payload(Path(actions_json), profile.action_dim))

    meta = _g2_hdf5_meta(root, hdf5_path)
    return EpisodeData(
        root=root,
        episode_id=str(meta.get("episode_id") or root.name or hdf5_path.stem),
        meta=meta,
        state_frames=state_frames,
        actions=actions,
        camera_counts=camera_counts,
        stationary_state_vectors=stationary_state_vectors,
        stationary_action_vectors=stationary_action_vectors,
    )


def _read_aloha_hdf5_episode(
    root: Path,
    hdf5_path: Path,
    profile: RobotProfile,
    *,
    actions_json: str | Path | None = None,
) -> EpisodeData:
    try:
        import h5py  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Reading ALOHA HDF5 episodes requires h5py. "
            "Install quality_pipeline/requirements.txt in the active venv."
        ) from exc

    state_frames: list[StateFrame] = []
    actions: list[list[float]] = []
    stationary_state_vectors: list[list[float]] = []
    stationary_action_vectors: list[list[float]] = []
    camera_counts = {camera.raw_key: 0 for camera in profile.cameras}
    fps = float(profile.raw.get("fps", {}).get("record") or 30.0)
    timestamp_aliases = {
        "hand_left_color": "head_stereo_left",
        "hand_right_color": "head_stereo_right",
    }

    with h5py.File(hdf5_path, "r") as f:
        frame_keys = _numeric_hdf5_keys(f.keys())
        if not frame_keys:
            raise ValueError(f"{hdf5_path}: missing numeric frame groups")

        timestamp0 = _read_hdf5_scalar(f, f"{frame_keys[0]}/main_timestamp")
        for idx, key in enumerate(frame_keys):
            timestamp_raw = _read_hdf5_scalar(f, f"{key}/main_timestamp")
            timestamp = _normalise_timestamp(timestamp_raw, timestamp0, idx, fps)
            state = _aloha_state_from_hdf5_frame(f, key, profile.state_dim)
            action = _aloha_action_from_hdf5_frame(f, key, profile.action_dim)
            frame_idx = int(key)
            state_frames.append(StateFrame(frame_idx=frame_idx, timestamp=timestamp, state=state))
            if len(action) == profile.action_dim and all(math.isfinite(v) for v in action):
                actions.append(action)
            for camera in profile.cameras:
                raw_key = camera.raw_key
                alias = timestamp_aliases.get(raw_key, raw_key)
                if f"{key}/timestamp/camera/{raw_key}" in f or f"{key}/timestamp/camera/{alias}" in f:
                    camera_counts[raw_key] += 1

    camera_counts = _fill_camera_counts_from_videos(root, profile, camera_counts)
    if actions_json:
        actions.extend(_read_action_payload(Path(actions_json), profile.action_dim))

    meta = _aloha_hdf5_meta(root, hdf5_path)
    return EpisodeData(
        root=root,
        episode_id=str(meta.get("episode_id") or root.name or hdf5_path.stem),
        meta=meta,
        state_frames=state_frames,
        actions=actions,
        camera_counts=camera_counts,
        stationary_state_vectors=stationary_state_vectors,
        stationary_action_vectors=stationary_action_vectors,
    )


ZERITH_STATE_DATASETS = (
    ("observation/state/arm/position", 14),
    ("observation/state/effector/position", 2),
    ("observation/state/waist/position", 3),
    ("observation/state/head/position", 2),
    ("observation/state/base/velocity", 2),
)

ZERITH_ACTION_DATASETS = (
    ("action/arm/position", 14),
    ("action/effector/position", 2),
    ("action/waist/position", 3),
    ("action/head/position", 2),
    ("action/base/velocity", 2),
)


def _read_zerith_hdf5_episode(
    root: Path,
    hdf5_path: Path,
    profile: RobotProfile,
    *,
    actions_json: str | Path | None = None,
) -> EpisodeData:
    try:
        import h5py  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Reading Zerith HDF5 episodes requires h5py. "
            "Install quality_pipeline/requirements.txt in the active venv."
        ) from exc

    state_frames: list[StateFrame] = []
    actions: list[list[float]] = []
    stationary_state_vectors: list[list[float]] = []
    stationary_action_vectors: list[list[float]] = []
    camera_counts = {camera.raw_key: 0 for camera in profile.cameras}
    fps = float(profile.raw.get("fps", {}).get("record") or 30.0)

    with h5py.File(hdf5_path, "r") as f:
        timestamp_key = "timestamp/t"
        if timestamp_key not in f:
            raise ValueError(f"{hdf5_path}: missing {timestamp_key}")
        timestamp_values = _hdf5_column_rows(f, timestamp_key, 1)
        frame_count = len(timestamp_values)
        if frame_count <= 0:
            raise ValueError(f"{hdf5_path}: empty {timestamp_key}")

        frame_dataset_shapes: dict[str, list[int]] = {}
        mismatched_frame_datasets: list[dict[str, Any]] = []

        def inspect_frame_dataset(name: str, item: Any) -> None:
            if not hasattr(item, "shape") or not name.startswith(("action/", "observation/", "timestamp/")):
                return
            shape = [int(value) for value in item.shape]
            frame_dataset_shapes[name] = shape
            if not shape or shape[0] != frame_count:
                mismatched_frame_datasets.append(
                    {"dataset": name, "shape": shape, "expected_frames": frame_count}
                )

        f.visititems(inspect_frame_dataset)
        root_attrs: dict[str, Any] = {}
        for key in ("total_frames", "control_frequency", "action_mode"):
            value = f.attrs.get(key)
            if hasattr(value, "item"):
                value = value.item()
            root_attrs[key] = value
        schema_detail = {
            "frame_count": frame_count,
            "frame_dataset_count": len(frame_dataset_shapes),
            "frame_dataset_shapes": frame_dataset_shapes,
            "mismatched_frame_datasets": mismatched_frame_datasets,
            "root_attrs": root_attrs,
        }

        state_columns = [
            _hdf5_column_rows(f, key, width, expected_frames=frame_count)
            for key, width in ZERITH_STATE_DATASETS
        ]
        action_columns = [
            _hdf5_column_rows(f, key, width, expected_frames=frame_count)
            for key, width in ZERITH_ACTION_DATASETS
        ]
        timestamp0 = timestamp_values[0][0]
        for idx in range(frame_count):
            state = _fit_dim(_zerith_row_from_columns(state_columns, idx), profile.state_dim)
            action = _fit_dim(_zerith_row_from_columns(action_columns, idx), profile.action_dim)
            timestamp = _normalise_timestamp(timestamp_values[idx][0], timestamp0, idx, fps)
            state_frames.append(StateFrame(frame_idx=idx, timestamp=timestamp, state=state))
            stationary_state_vectors.append(state)
            stationary_action_vectors.append(action)
            # Keep non-finite rows so the finite-values QC can fail visibly.
            actions.append(action)

        for camera in profile.cameras:
            color_key = f"observation/images/rs/{camera.raw_key}/color"
            if color_key in f and len(f[color_key].shape) == 1:
                camera_counts[camera.raw_key] = int(f[color_key].shape[0])

    external_camera_videos = _inspect_external_camera_videos(root, profile)
    for camera in profile.cameras:
        if int(camera_counts.get(camera.raw_key, 0)) > 0:
            continue
        detail = external_camera_videos.get(camera.raw_key, {})
        camera_counts[camera.raw_key] = int(detail.get("decoded_frame_count") or 0)
    if actions_json:
        actions.extend(_read_action_payload(Path(actions_json), profile.action_dim))

    meta = _zerith_hdf5_meta(root, hdf5_path)
    meta["zerith_hdf5_schema"] = schema_detail
    # LeRobot consumes these MP4 files, not the variable-length encoded image
    # blobs stored inside episode.hdf5.  Keep an independent, fully decoded
    # inventory so QC cannot pass an episode that conversion will reject.
    meta["zerith_external_camera_videos"] = external_camera_videos
    return EpisodeData(
        root=root,
        episode_id=str(meta.get("episode_id") or root.name or hdf5_path.stem),
        meta=meta,
        state_frames=state_frames,
        actions=actions,
        camera_counts=camera_counts,
        stationary_state_vectors=stationary_state_vectors,
        stationary_action_vectors=stationary_action_vectors,
    )


def _hdf5_column_rows(
    f: Any,
    key: str,
    width: int,
    *,
    expected_frames: int | None = None,
) -> list[list[float]]:
    if key not in f:
        raise ValueError(f"{f.filename}: missing {key}")
    dataset = f[key]
    expected_shape = (expected_frames, width) if expected_frames is not None else None
    if len(dataset.shape) == 1 and width == 1:
        rows = [[float(value)] for value in dataset[...].tolist()]
    elif len(dataset.shape) == 2 and int(dataset.shape[1]) == width:
        rows = [[float(value) for value in row] for row in dataset[...].tolist()]
    else:
        raise ValueError(
            f"{f.filename}: expected {key} shape (N, {width}), got {dataset.shape}"
        )
    if expected_shape is not None and len(rows) != expected_shape[0]:
        raise ValueError(
            f"{f.filename}: expected {key} {expected_shape[0]} frames, got {len(rows)}"
        )
    return rows


def _zerith_row_from_columns(columns: list[list[list[float]]], frame_idx: int) -> list[float]:
    arm, effector, waist, head, base = (column[frame_idx] for column in columns)
    # Canonical 23D order supplied by the robot owner:
    # left arm 7, left gripper, right arm 7, right gripper,
    # lift, waist pitch/yaw, head yaw/pitch, base linear/angular velocity.
    return (
        arm[:7]
        + effector[:1]
        + arm[7:14]
        + effector[1:2]
        + waist[:3]
        + head[:2]
        + base[:2]
    )


def _numeric_hdf5_keys(keys: Any) -> list[str]:
    return sorted((str(key) for key in keys if str(key).isdigit()), key=lambda key: int(key))


def _read_hdf5_scalar(f: Any, key: str) -> float | None:
    if key not in f:
        return None
    try:
        value = f[key][()]
    except Exception:
        return None
    values = _flatten_floats(value)
    return values[0] if values else None


def _normalise_timestamp(
    raw_value: float | None,
    first_value: float | None,
    fallback_idx: int,
    fps: float,
) -> float:
    if raw_value is None or first_value is None:
        return fallback_idx / fps if fps > 0 else float(fallback_idx)
    delta = float(raw_value) - float(first_value)
    magnitude = max(abs(float(raw_value)), abs(float(first_value)))
    if magnitude > 1e17:
        return delta / 1e9
    if magnitude > 1e14:
        return delta / 1e6
    if magnitude > 1e11:
        return delta / 1e3
    return delta


def _g2_state_from_hdf5_frame(f: Any, frame_key: str, dim: int) -> list[float]:
    joint = _read_hdf5_vector(f, f"{frame_key}/state/joint/position", 14)
    left_gripper = _read_hdf5_vector(f, f"{frame_key}/state/left_effector/position", 1)
    right_gripper = _read_hdf5_vector(f, f"{frame_key}/state/right_effector/position", 1)
    waist = _read_hdf5_vector(f, f"{frame_key}/state/waist/position", 5)
    base_position = _read_hdf5_vector(f, f"{frame_key}/state/robot/position", 3)
    orientation = _read_hdf5_vector(f, f"{frame_key}/state/robot/orientation", 4)
    row = (
        joint[:7]
        + left_gripper[:1]
        + joint[7:14]
        + right_gripper[:1]
        + waist
        + base_position
        + orientation[2:4]
    )
    return _fit_dim(row, dim)


def _aloha_state_from_hdf5_frame(f: Any, frame_key: str, dim: int) -> list[float]:
    joint = _read_hdf5_vector(f, f"{frame_key}/state/joint/position", 14)
    lift_height = _read_hdf5_vector(f, f"{frame_key}/state/waist/position", 1)
    base_state_key = f"{frame_key}/state/robot/base_state"
    if base_state_key in f:
        base_state = _read_hdf5_vector(f, base_state_key, 6)
    else:
        pose2d = _read_hdf5_vector(f, f"{frame_key}/state/robot/pose2d", 3)
        velocity = _read_hdf5_vector(f, f"{frame_key}/state/robot/velocity", 3)
        base_state = pose2d + velocity
    return _fit_dim(joint + lift_height[:1] + base_state[:6], dim)


def _aloha_action_from_hdf5_frame(f: Any, frame_key: str, dim: int) -> list[float]:
    joint = _read_hdf5_vector(f, f"{frame_key}/action/joint/position", 14)
    lift_height = _read_hdf5_vector(f, f"{frame_key}/action/waist/position", 1)
    base_velocity = _read_hdf5_vector(f, f"{frame_key}/action/robot/velocity", 3)
    return _fit_dim(joint + lift_height[:1] + base_velocity[:3], dim)


def _g2_action_from_hdf5_frame(f: Any, frame_key: str, dim: int) -> list[float]:
    joint = _read_hdf5_vector(f, f"{frame_key}/action/joint/position", 14)
    left_gripper = _read_hdf5_vector(f, f"{frame_key}/action/left_effector/position", 1)
    right_gripper = _read_hdf5_vector(f, f"{frame_key}/action/right_effector/position", 1)
    waist = _read_hdf5_vector(f, f"{frame_key}/action/waist/position", 5)
    base_velocity_raw = _read_hdf5_vector(f, f"{frame_key}/action/robot/velocity", 6)
    base_velocity = _g2_base_velocity_from_raw(base_velocity_raw)
    row = (
        joint[:7]
        + left_gripper[:1]
        + joint[7:14]
        + right_gripper[:1]
        + waist
        + base_velocity
    )
    return _fit_dim(row, dim)


def _g2_base_velocity_from_raw(raw: list[float]) -> list[float]:
    if len(raw) >= 6:
        return [float(raw[0]), float(raw[1]), float(raw[5])]
    return _fit_dim(raw, 3)


def _read_hdf5_vector(f: Any, key: str, expected_len: int) -> list[float]:
    if key not in f:
        return [0.0] * expected_len
    try:
        values = _flatten_floats(f[key][()])
    except Exception:
        values = []
    return _fit_dim(values, expected_len)


STATIONARY_STATE_PATHS = (
    "state/joint/position",
    "state/left_effector/position",
    "state/right_effector/position",
    "state/waist/position",
    # Do not include base_x/base_y/base_yaw in stationary detection. Base pose can
    # drift while the robot is otherwise holding still; base velocity is still checked.
    "state/robot/velocity",
    "state/end/position",
    "state/end/orientation",
)

STATIONARY_ACTION_PATHS = (
    "action/joint/position",
    "action/left_effector/position",
    "action/right_effector/position",
    "action/waist/position",
    "action/robot/velocity",
)


def _read_existing_hdf5_vector(f: Any, key: str) -> list[float]:
    if key not in f:
        return []
    try:
        return _flatten_floats(f[key][()])
    except Exception:
        return []


def _stationary_vector_from_hdf5_paths(f: Any, frame_key: str, paths: tuple[str, ...]) -> list[float]:
    row: list[float] = []
    for path in paths:
        row.extend(_read_existing_hdf5_vector(f, f"{frame_key}/{path}"))
    return row


def _stationary_state_vector_from_hdf5_frame(f: Any, frame_key: str) -> list[float]:
    return _stationary_vector_from_hdf5_paths(f, frame_key, STATIONARY_STATE_PATHS)


def _stationary_action_vector_from_hdf5_frame(f: Any, frame_key: str) -> list[float]:
    return _stationary_vector_from_hdf5_paths(f, frame_key, STATIONARY_ACTION_PATHS)


def _flatten_floats(value: Any) -> list[float]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, (list, tuple)):
        out: list[float] = []
        for item in value:
            out.extend(_flatten_floats(item))
        return out
    try:
        return [float(value)]
    except (TypeError, ValueError):
        return []


def _fit_dim(values: list[float], dim: int) -> list[float]:
    if len(values) >= dim:
        return [float(v) for v in values[:dim]]
    return [float(v) for v in values] + [0.0] * (dim - len(values))


def _count_video_cameras(root: Path, profile: RobotProfile) -> dict[str, int]:
    counts: dict[str, int] = {}
    for camera in profile.cameras:
        video_path = next(
            (
                candidate
                for candidate in (
                    root / "videos" / f"{camera.raw_key}.mp4",
                    root / "videos" / "rs" / f"{camera.raw_key}.mp4",
                )
                if candidate.is_file()
            ),
            None,
        )
        if video_path is None:
            counts[camera.raw_key] = 0
            continue
        counts[camera.raw_key] = _video_frame_count(video_path)
    return counts


def _fill_camera_counts_from_videos(
    root: Path,
    profile: RobotProfile,
    camera_counts: dict[str, int],
) -> dict[str, int]:
    video_counts = _count_video_cameras(root, profile)
    out = dict(camera_counts)
    for camera in profile.cameras:
        if int(out.get(camera.raw_key, 0)) == 0 and int(video_counts.get(camera.raw_key, 0)) > 0:
            out[camera.raw_key] = int(video_counts[camera.raw_key])
    return out


def _video_frame_count(path: Path) -> int:
    try:
        import cv2  # type: ignore
    except ImportError:
        return 0
    cap = cv2.VideoCapture(str(path))
    try:
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    finally:
        cap.release()
    return count


def _inspect_external_camera_videos(
    root: Path,
    profile: RobotProfile,
) -> dict[str, dict[str, Any]]:
    """Fully decode external camera videos required by Zerith conversion."""

    details: dict[str, dict[str, Any]] = {}
    try:
        import cv2  # type: ignore
    except ImportError as exc:
        for camera in profile.cameras:
            details[camera.raw_key] = {
                "required": bool(camera.required),
                "path": "",
                "exists": False,
                "readable": False,
                "declared_frame_count": 0,
                "decoded_frame_count": 0,
                "error": f"OpenCV unavailable: {exc}",
            }
        return details

    for camera in profile.cameras:
        candidates = (
            root / "videos" / f"{camera.raw_key}.mp4",
            root / "videos" / "rs" / f"{camera.raw_key}.mp4",
            root / "observations" / "videos" / f"{camera.raw_key}.mp4",
        )
        video_path = next((path for path in candidates if path.is_file()), None)
        detail: dict[str, Any] = {
            "required": bool(camera.required),
            "path": str(video_path or candidates[1]),
            "exists": video_path is not None,
            "readable": False,
            "declared_frame_count": 0,
            "decoded_frame_count": 0,
            "error": "",
        }
        if video_path is None:
            detail["error"] = "required external MP4 is missing"
            details[camera.raw_key] = detail
            continue

        capture = cv2.VideoCapture(str(video_path))
        try:
            detail["declared_frame_count"] = int(
                capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0
            )
            if not capture.isOpened():
                detail["error"] = "video is unreadable"
                details[camera.raw_key] = detail
                continue
            decoded = 0
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                if frame is None:
                    detail["error"] = f"decoder returned an empty frame at index {decoded}"
                    break
                decoded += 1
            detail["decoded_frame_count"] = decoded
            detail["readable"] = not bool(detail["error"])
        except Exception as exc:  # OpenCV backends can raise implementation-specific errors.
            detail["error"] = f"video decode failed: {exc}"
        finally:
            capture.release()
        details[camera.raw_key] = detail
    return details


def _g2_hdf5_meta(root: Path, hdf5_path: Path) -> dict[str, Any]:
    sidecar = _read_json_if_exists(root / "meta" / "episode_meta.json")
    meta = dict(sidecar)
    raw_episode_id = meta.get("episode_id")
    if raw_episode_id is not None:
        meta["raw_episode_id"] = raw_episode_id
    meta["episode_id"] = root.name or hdf5_path.stem
    meta["raw_hdf5_path"] = str(hdf5_path)
    meta["source_format"] = "g2_hdf5"

    prompt = _prompt_from_g2_meta(sidecar)
    if prompt:
        meta["prompt"] = prompt
        meta["prompt_source"] = "meta/episode_meta.json"
    return meta


def _zerith_hdf5_meta(root: Path, hdf5_path: Path) -> dict[str, Any]:
    sidecar = _read_json_if_exists(root / "episode_meta.json")
    meta = dict(sidecar)
    raw_episode_id = (
        meta.get("source_episode_id")
        or meta.get("raw_episode_id")
        or meta.get("episode_id")
    )
    if raw_episode_id is not None:
        meta["raw_episode_id"] = raw_episode_id
    # The directory name is the canonical processing ID. This keeps QC, Web,
    # replay and LeRobot aligned after UUID directories are optimized to
    # episode1..N, while raw_episode_id retains collector provenance.
    meta["episode_id"] = str(root.name or hdf5_path.stem)
    meta["raw_hdf5_path"] = str(hdf5_path)
    meta["source_format"] = "zerith_columnar_hdf5"

    subtask_segments: list[dict[str, Any]] = []
    try:
        import h5py  # type: ignore

        with h5py.File(hdf5_path, "r") as file_obj:
            task = str(file_obj.attrs.get("task_name") or "").strip()
            for key in (
                "action_mode",
                "control_frequency",
                "task_id",
                "total_frames",
                "version",
            ):
                value = file_obj.attrs.get(key)
                if hasattr(value, "item"):
                    value = value.item()
                if value is not None:
                    meta.setdefault(key, value)
            if "subtask_transitions" in file_obj:
                transitions = [
                    int(value)
                    for value in file_obj["subtask_transitions"][...].reshape(-1).tolist()
                ]
                frame_count = (
                    int(file_obj["timestamp/t"].shape[0])
                    if "timestamp/t" in file_obj
                    else 0
                )
                if (
                    frame_count > 0
                    and transitions
                    and transitions[-1] == frame_count
                    and all(0 < end <= frame_count for end in transitions)
                    and transitions == sorted(set(transitions))
                ):
                    start = 0
                    for stage_index, end_exclusive in enumerate(transitions, start=1):
                        subtask_segments.append(
                            {
                                "id": f"stage_{stage_index}",
                                "stage_number": stage_index,
                                "start": start,
                                "end": end_exclusive - 1,
                                "subtask": f"阶段 {stage_index}",
                                "source": "hdf5:/subtask_transitions",
                            }
                        )
                        start = end_exclusive
                    meta["subtask_transitions"] = transitions
                    meta["stage_count"] = len(transitions)
    except OSError:
        task = ""

    if subtask_segments:
        # Keep the collector's right-open transition array as the authority while
        # exposing the inclusive segment representation already consumed by the
        # replay page and LeRobot episode metadata.
        meta["subtask_segments"] = subtask_segments
    if task:
        meta.setdefault("task", task)
        meta.setdefault("tasks", [task])
        meta["prompt"] = task
        meta["prompt_source"] = "HDF5 root attribute task_name"
    return meta


def _aloha_hdf5_meta(root: Path, hdf5_path: Path) -> dict[str, Any]:
    sidecar = _read_json_if_exists(root / "meta" / "episode_meta.json")
    meta = dict(sidecar)
    for key, value in _read_hdf5_task_meta(hdf5_path).items():
        if key not in meta or meta[key] in ("", [], None):
            meta[key] = value
    raw_episode_id = meta.get("episode_id")
    if raw_episode_id is not None:
        meta["raw_episode_id"] = raw_episode_id
    meta["episode_id"] = root.name or hdf5_path.stem
    meta["raw_hdf5_path"] = str(hdf5_path)
    meta["source_format"] = "aloha_hdf5"

    prompt = _prompt_from_aloha_meta(sidecar)
    if prompt:
        meta["prompt"] = prompt
        meta["prompt_source"] = "meta/episode_meta.json"
    return meta


def _read_hdf5_task_meta(hdf5_path: Path) -> dict[str, Any]:
    try:
        import h5py  # type: ignore
    except ImportError:
        return {}

    def attr_to_text(value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value) if value is not None else ""

    out: dict[str, Any] = {}
    try:
        with h5py.File(hdf5_path, "r") as file_obj:
            for key in ("task", "text"):
                value = attr_to_text(file_obj.attrs.get(key, "")).strip()
                if value:
                    out[key] = value
            tasks_json = attr_to_text(file_obj.attrs.get("tasks_json", "")).strip()
            if tasks_json:
                parsed = json.loads(tasks_json)
                if isinstance(parsed, list):
                    out["tasks"] = parsed
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return out
    return out


def _prompt_from_aloha_meta(meta: dict[str, Any]) -> str:
    task = meta.get("task")
    if isinstance(task, str) and task.strip():
        return task.strip()
    tasks = meta.get("tasks")
    if isinstance(tasks, list):
        task_values = [str(item).strip() for item in tasks if str(item).strip()]
        if task_values:
            return task_values[0]
    for key in ("prompt", "text", "text_zh", "description"):
        value = meta.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        parsed = _json_string_to_dict(value)
        if parsed:
            return _prompt_from_g2_meta(meta)
        return value.strip()
    return ""


def _prompt_from_g2_meta(meta: dict[str, Any]) -> str:
    payload = _json_string_to_dict(meta.get("text"))
    extra = payload.get("extra") if isinstance(payload.get("extra"), list) else []
    steps = [str(item).strip() for item in extra if str(item).strip()]
    targets = _targets_from_g2_steps(steps)
    prefix = f"Target: {' and '.join(targets)}. " if targets else ""
    body = " ".join(steps)
    if body:
        return prefix + body
    description = str(payload.get("description") or "").strip()
    return prefix + description


def _json_string_to_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _targets_from_g2_steps(steps: list[str]) -> list[str]:
    import re

    for step in steps:
        match = re.search(r"\bgrasp\s+the\s+(.+?)(?:\.|$)", step, flags=re.IGNORECASE)
        if not match:
            continue
        raw = match.group(1)
        raw = re.sub(r"^(?:item|items)\b.*$", "", raw, flags=re.IGNORECASE).strip()
        parts = re.split(r"\s+and\s+(?:the\s+)?|,", raw)
        targets = []
        for part in parts:
            target = re.sub(r"^(?:the|a|an)\s+", "", part.strip(), flags=re.IGNORECASE)
            target = target.strip()
            if target:
                targets.append(target)
        if targets:
            return targets
    return []


def _read_parquet_episode(
    path: Path,
    profile: RobotProfile,
    *,
    actions_json: str | Path | None = None,
) -> EpisodeData:
    try:
        import pyarrow.parquet as pq  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Reading parquet episodes requires pyarrow. "
            "Install quality_pipeline/requirements.txt in the active venv."
        ) from exc

    pf = pq.ParquetFile(path)
    names = set(pf.schema_arrow.names)
    state_col = _first_present(names, ("state", "observation.state"))
    action_col = _first_present(names, ("actions", "action"))
    if state_col is None:
        raise ValueError(f"{path}: missing state column; available columns: {sorted(names)}")

    columns = [state_col]
    if action_col is not None:
        columns.append(action_col)
    for col in ("timestamp", "frame_index", "episode_index", "index", "task_index"):
        if col in names:
            columns.append(col)

    table = pf.read(columns=columns)
    data = table.to_pydict()
    state_rows = data[state_col]
    frame_indices = data.get("frame_index") or list(range(len(state_rows)))
    timestamps = data.get("timestamp")
    if timestamps is None:
        fps = float(profile.raw.get("fps", {}).get("record") or 30.0)
        timestamps = [idx / fps for idx in range(len(state_rows))]

    state_frames: list[StateFrame] = []
    for i, row in enumerate(state_rows):
        try:
            frame_idx = int(frame_indices[i])
            timestamp = float(timestamps[i])
            state = [float(v) for v in row]
        except (TypeError, ValueError, IndexError):
            continue
        state_frames.append(StateFrame(frame_idx=frame_idx, timestamp=timestamp, state=state))

    actions: list[list[float]] = []
    if action_col is not None:
        for row in data.get(action_col, []):
            try:
                action = [float(v) for v in row]
            except (TypeError, ValueError):
                continue
            if len(action) == profile.action_dim and all(math.isfinite(v) for v in action):
                actions.append(action)
    if actions_json:
        actions.extend(_read_action_payload(Path(actions_json), profile.action_dim))

    meta = _parquet_meta(path, data)
    camera_counts = {
        camera.raw_key: int(pf.metadata.num_rows) if camera.raw_key in names else 0
        for camera in profile.cameras
    }
    return EpisodeData(
        root=path.parent,
        episode_id=path.stem,
        meta=meta,
        state_frames=state_frames,
        actions=actions,
        camera_counts=camera_counts,
    )


def _first_present(names: set[str], candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        if name in names:
            return name
    return None


def _parquet_meta(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "episode_id": path.stem,
        "raw_parquet_path": str(path),
        "source_format": "parquet",
    }
    for col in ("episode_index", "task_index"):
        values = data.get(col)
        if not values:
            continue
        unique = sorted({int(v) for v in values if v is not None})
        if len(unique) == 1:
            meta[col] = unique[0]
        else:
            meta[col] = unique[:20]
    if "task_index" in meta:
        meta["task_subtask_idx"] = str(meta["task_index"])
    return meta


def _read_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _read_state_csv(path: Path) -> list[StateFrame]:
    if not path.exists():
        return []
    frames: list[StateFrame] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return []
        state_cols = sorted(
            [name for name in reader.fieldnames if name.startswith("s") and name[1:].isdigit()],
            key=lambda name: int(name[1:]),
        )
        for row in reader:
            try:
                frame_idx = int(float(row.get("frame_idx", len(frames))))
                timestamp = float(row.get("timestamp", "nan"))
                state = [float(row[col]) for col in state_cols]
            except (TypeError, ValueError):
                continue
            frames.append(StateFrame(frame_idx=frame_idx, timestamp=timestamp, state=state))
    return frames


def _read_actions_jsonl(path: Path, action_dim: int) -> list[list[float]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    actions: list[list[float]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            actions.extend(_extract_actions(item, action_dim))
    return actions


def _read_action_payload(path: Path, action_dim: int) -> list[list[float]]:
    with path.expanduser().resolve().open("r", encoding="utf-8") as f:
        item = json.load(f)
    return _extract_actions(item, action_dim)


def _extract_actions(item: Any, action_dim: int) -> list[list[float]]:
    if isinstance(item, dict):
        raw_actions = item.get("actions") or item.get("action")
    else:
        raw_actions = item
    if raw_actions is None:
        return []
    if isinstance(raw_actions, list) and raw_actions and isinstance(raw_actions[0], (int, float)):
        raw_actions = [raw_actions]
    out: list[list[float]] = []
    if not isinstance(raw_actions, list):
        return out
    for raw in raw_actions:
        if not isinstance(raw, list):
            continue
        try:
            action = [float(v) for v in raw]
        except (TypeError, ValueError):
            continue
        if len(action) == action_dim and all(math.isfinite(v) for v in action):
            out.append(action)
    return out


def _count_cameras(root: Path, profile: RobotProfile) -> dict[str, int]:
    counts: dict[str, int] = {}
    image_root = root / "images"
    for camera in profile.cameras:
        camera_dir = image_root / camera.raw_key
        if not camera_dir.exists():
            counts[camera.raw_key] = 0
            continue
        counts[camera.raw_key] = sum(
            1
            for p in camera_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png"}
        )
    return counts
