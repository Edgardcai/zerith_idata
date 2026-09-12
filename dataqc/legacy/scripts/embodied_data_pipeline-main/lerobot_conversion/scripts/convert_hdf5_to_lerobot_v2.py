#!/usr/bin/env python3
"""Convert aligned_joints.h5 episode folders to LeRobot v2 format."""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, as_completed, wait
import inspect
import json
import multiprocessing as mp
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from types import MethodType
from typing import Any

import cv2
import numpy as np
from tqdm import tqdm

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quality_pipeline.episode_io import read_raw_episode
from quality_pipeline.profiles import RobotProfile, load_profile
from quality_pipeline.task_names import require_hdf5_task

try:
    from lerobot.datasets.lerobot_dataset import HF_LEROBOT_HOME, LeRobotDataset
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "LeRobot is required. Install quality_pipeline/requirements.txt in the active environment."
    ) from exc


CAMERA_FALLBACKS = {
    "hand_left_color": ("hand_left_color", "head_stereo_left"),
    "hand_right_color": ("hand_right_color", "head_stereo_right"),
}
NUMBER_RE = re.compile(r"(\d+)")
EPISODE_MAPPING_NAME = "episode_name_mapping.json"
ALOHA_LEROBOT_STATE_EXTRA_DIM = 7
ALOHA_LEROBOT_ACTION_EXTRA_DIM = 4
QUALITY_GRADES = ("A", "B", "C", "F")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert ICRA-WBC-like aligned_joints.h5 episodes to LeRobot v2."
    )
    parser.add_argument(
        "--data-dir",
        required=True,
        help="Input episode directory, aligned_joints.h5 file, or a root containing episode folders.",
    )
    parser.add_argument(
        "--profile",
        default="robot_profiles/aloha.yaml",
        help="Robot profile YAML. Default: robot_profiles/aloha.yaml.",
    )
    parser.add_argument(
        "--repo-id",
        default="",
        help=(
            "LeRobot repo id. Default: env LEROBOT_REPO_ID, ALOHA_LEROBOT_REPO_ID, "
            "G2_LEROBOT_REPO_ID, or <profile_id>_lerobot."
        ),
    )
    parser.add_argument(
        "--output-root",
        default="",
        help="Root directory for LeRobot datasets. Default: env HF_LEROBOT_HOME or LeRobot default.",
    )
    parser.add_argument("--fps", type=int, default=0, help="Output FPS. Default: profile fps.record.")
    parser.add_argument("--robot-type", default="", help="Robot type. Default: profile lerobot_robot_type.")
    parser.add_argument(
        "--key-style",
        choices=("profile", "pi05"),
        default="profile",
        help=(
            "Feature key style. 'profile' uses observation.* keys from robot_profiles. "
            "'pi05' matches the referenced G2 script: head_color, state, actions."
        ),
    )
    parser.add_argument(
        "--aloha-action-mode",
        choices=("joints", "joints_base"),
        default="joints_base",
        help=(
            "ALOHA LeRobot state/action layout. 'joints' writes 14D state and 14D action; "
            "'joints_base' writes 21D state (14 joints + lift + base_state) and "
            "18D action (14 joints + lift + chassis velocity). "
            "Default: joints_base."
        ),
    )
    parser.add_argument(
        "--image-size",
        nargs=2,
        type=int,
        metavar=("WIDTH", "HEIGHT"),
        default=(224, 224),
        help="Resize RGB video frames before writing. Default: 224 224.",
    )
    parser.add_argument("--num-workers", type=int, default=4, help="Episode decoder worker processes.")
    parser.add_argument(
        "--queue-size",
        type=int,
        default=0,
        help="Decoded episode queue size. Default: num-workers * 5.",
    )
    parser.add_argument(
        "--image-writer-processes",
        type=int,
        default=5,
        help="LeRobot image writer processes. Default: 5.",
    )
    parser.add_argument(
        "--image-writer-threads",
        type=int,
        default=10,
        help="LeRobot image writer threads. Default: 10.",
    )
    parser.add_argument(
        "--episode-filter",
        default="",
        help="Optional episode id filter, for example 1,3,9 or 1-10.",
    )
    parser.add_argument(
        "--quality-grade",
        choices=QUALITY_GRADES,
        default="",
        help="Authoritative quality grade for every selected episode.",
    )
    parser.add_argument(
        "--unordered",
        action="store_true",
        help="Write episodes in completion order instead of raw numeric episode id order.",
    )
    parser.add_argument(
        "--preserve-episode-index",
        action="store_true",
        help=(
            "Try to write LeRobot episode indices equal to raw numeric episode ids. "
            "Many LeRobot v2 versions only support contiguous episode indices, so the default is off."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Resume an existing output dataset. Completed source episodes are skipped from "
            f"meta/{EPISODE_MAPPING_NAME}."
        ),
    )
    parser.add_argument("--overwrite", action="store_true", help="Remove existing output dataset before writing.")
    parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help=(
            "Do not skip source episodes that are already listed in "
            f"meta/{EPISODE_MAPPING_NAME}."
        ),
    )
    parser.add_argument("--push-to-hub", action="store_true", help="Push dataset to Hugging Face Hub after conversion.")
    parser.add_argument(
        "--no-videos",
        action="store_true",
        help=(
            "Store camera frames as parquet-embedded images. By default camera frames are "
            "encoded as videos/chunk-*/<camera_key>/episode_*.mp4."
        ),
    )
    parser.add_argument("--video-backend", default=None, help="Optional LeRobot video backend.")
    parser.add_argument(
        "--gpu-accel",
        action="store_true",
        help="Enable CUDA preprocessing resize and ffmpeg NVENC video encoding.",
    )
    parser.add_argument(
        "--preprocess-device",
        choices=("cpu", "cuda", "auto"),
        default="cpu",
        help="Device used for video frame resize during preprocessing. Default: cpu.",
    )
    parser.add_argument(
        "--gpu-device",
        default="0",
        help="GPU index used by CUDA resize and NVENC encoding. Default: 0.",
    )
    parser.add_argument(
        "--gpu-resize-batch-size",
        type=int,
        default=64,
        help="Frame batch size for CUDA resize. Default: 64.",
    )
    parser.add_argument(
        "--gpu-encode-videos",
        action="store_true",
        help="Use ffmpeg NVENC for LeRobot output video encoding.",
    )
    parser.add_argument(
        "--gpu-video-encoder",
        choices=("h264_nvenc", "hevc_nvenc", "av1_nvenc"),
        default="h264_nvenc",
        help="ffmpeg NVENC encoder used with --gpu-encode-videos or --gpu-accel. Default: h264_nvenc.",
    )
    parser.add_argument(
        "--gpu-encode-preset",
        default="p4",
        help="NVENC preset passed to ffmpeg. Default: p4.",
    )
    parser.add_argument(
        "--gpu-encode-cq",
        type=int,
        default=23,
        help="NVENC constant quality value passed to ffmpeg. Lower is higher quality. Default: 23.",
    )
    parser.add_argument(
        "--cpu-video-encoder",
        default="libx264",
        help=(
            "ffmpeg CPU video encoder used when NVENC is not enabled/available. "
            "Default: libx264. This replaces LeRobot's default AV1 encoder."
        ),
    )
    parser.add_argument(
        "--cpu-video-crf",
        type=int,
        default=20,
        help="CPU H.264 constant quality value. Lower is higher quality. Default: 20.",
    )
    parser.add_argument(
        "--cpu-video-preset",
        default="fast",
        help="CPU H.264 preset passed to ffmpeg. Default: fast.",
    )
    parser.add_argument(
        "--task",
        default="",
        help="Task override for non-ALOHA profiles; ALOHA tasks must come from HDF5.",
    )
    parser.add_argument(
        "--default-task",
        default="",
        help="Fallback for non-ALOHA profiles; ALOHA tasks must come from HDF5.",
    )
    parser.add_argument(
        "--validation-retries",
        type=int,
        default=1,
        help=(
            "After writing each LeRobot episode, validate parquet/video frame counts. "
            "If validation fails, rewrite that episode from HDF5 this many times. Default: 1."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Print conversion plan without writing output.")
    return parser.parse_args()


def resolve_repo_id(profile: RobotProfile, explicit: str) -> str:
    if explicit:
        return explicit
    env_candidates = [
        "LEROBOT_REPO_ID",
        f"{profile.profile_id.upper()}_LEROBOT_REPO_ID",
        "G2_LEROBOT_REPO_ID",
        "ALOHA_LEROBOT_REPO_ID",
    ]
    for name in env_candidates:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return f"{profile.profile_id}_lerobot"


def resolve_output_path(repo_id: str, output_root: str) -> Path:
    root = Path(output_root).expanduser() if output_root else Path(
        os.environ.get("HF_LEROBOT_HOME", ROOT / "data" / "lerobot")
    )
    root = root.resolve()
    repo_path = Path(str(repo_id).strip())
    if not str(repo_path) or repo_path.is_absolute():
        raise ValueError(f"LeRobot repo id must be a non-empty relative path: {repo_id!r}")
    output_path = (root / repo_path).resolve()
    try:
        output_path.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"LeRobot repo id escapes output root {root}: {repo_id!r}"
        ) from exc
    if output_path == root:
        raise ValueError(f"LeRobot repo id resolves to the output root itself: {repo_id!r}")
    return output_path


def is_lerobot_dataset_complete(output_path: Path) -> bool:
    required_meta = (
        output_path / "meta" / "info.json",
        output_path / "meta" / "tasks.jsonl",
        output_path / "meta" / "episodes.jsonl",
    )
    if not all(path.is_file() for path in required_meta):
        return False

    info = read_lerobot_info(output_path)
    if not info:
        return False

    episode_rows = read_jsonl(output_path / "meta" / "episodes.jsonl")
    episode_indices: list[int] = []
    for row in episode_rows:
        try:
            episode_indices.append(int(row.get("episode_index")))
        except (TypeError, ValueError):
            return False
    if len(episode_indices) != len(set(episode_indices)):
        return False

    try:
        total_episodes = int(info.get("total_episodes"))
    except (TypeError, ValueError):
        return False
    if total_episodes != len(episode_indices):
        return False

    expected_indices = sorted(episode_indices)
    parquet_files = sorted(output_path.glob("data/chunk-*/episode_*.parquet"))
    if len(parquet_files) != len(expected_indices):
        return False
    for episode_index in expected_indices:
        if not (output_path / lerobot_relative_data_path(info, episode_index)).is_file():
            return False

    video_keys = lerobot_video_keys(info)
    expected_video_count = len(expected_indices) * len(video_keys)
    try:
        total_videos = int(info.get("total_videos", expected_video_count))
    except (TypeError, ValueError):
        return False
    if total_videos != expected_video_count:
        return False

    video_files = sorted(output_path.glob("videos/chunk-*/*/episode_*.mp4"))
    if len(video_files) != expected_video_count:
        return False
    for video_key in video_keys:
        for episode_index in expected_indices:
            if not (output_path / lerobot_relative_video_path(info, episode_index, video_key)).is_file():
                return False
    return True


def lerobot_feature_schema_matches(output_path: Path, features: dict[str, dict[str, Any]]) -> bool:
    info = read_lerobot_info(output_path)
    existing = info.get("features")
    if not isinstance(existing, dict):
        return False
    for key, expected in features.items():
        current = existing.get(key)
        if not isinstance(current, dict):
            return False
        if str(current.get("dtype")) != str(expected.get("dtype")):
            return False
        if tuple(current.get("shape") or ()) != tuple(expected.get("shape") or ()):
            return False
    return True


def discover_h5_paths(data_dir: Path) -> list[Path]:
    data_dir = data_dir.expanduser().resolve()
    if data_dir.is_file():
        return [data_dir]
    if not data_dir.exists():
        raise FileNotFoundError(data_dir)
    for direct_name in ("episode.hdf5", "episode.h5"):
        direct = data_dir / direct_name
        if direct.is_file():
            return [direct.resolve()]

    preferred = sorted(data_dir.glob("**/aligned_joints.h5")) + sorted(
        data_dir.glob("**/aligned_joints.hdf5")
    )
    if preferred:
        return [path.resolve() for path in preferred if "record" not in path.parts]

    columnar = sorted(data_dir.glob("*/episode.hdf5")) + sorted(data_dir.glob("*/episode.h5"))
    if columnar:
        return [path.resolve() for path in columnar]

    return sorted(
        path.resolve()
        for path in data_dir.glob("**/*")
        if path.is_file() and path.suffix.lower() in {".h5", ".hdf5"} and "record" not in path.parts
    )


def parse_episode_filter(text: str) -> set[int] | None:
    if not text.strip():
        return None
    selected: set[int] = set()
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start_s, end_s = token.split("-", 1)
            start = int(start_s)
            end = int(end_s)
            if end < start:
                raise ValueError(f"Invalid episode range: {token}")
            selected.update(range(start, end + 1))
        else:
            selected.add(int(token))
    return selected


def natural_sort_key(text: str) -> list[Any]:
    parts: list[Any] = []
    for part in NUMBER_RE.split(text):
        if not part:
            continue
        parts.append((0, int(part)) if part.isdigit() else (1, part.lower()))
    return parts


def natural_path_key(path: Path) -> list[Any]:
    key: list[Any] = []
    for part in path.parts:
        key.extend(natural_sort_key(part))
        key.append((2, "/"))
    return key


def episode_sort_key(h5_path: Path, data_root: Path) -> tuple[list[Any], str, str]:
    episode_dir = episode_dir_from_h5(h5_path)
    try:
        relative_episode_dir = episode_dir.relative_to(data_root)
    except ValueError:
        relative_episode_dir = episode_dir
    return natural_path_key(relative_episode_dir), str(episode_dir), h5_path.name


def indexed_h5_paths(
    h5_paths: list[Path],
    data_root: Path,
    episode_filter: set[int] | None,
) -> list[tuple[int, Path]]:
    ordered_paths = sorted(h5_paths, key=lambda path: episode_sort_key(path, data_root))
    indexed = [(idx, path) for idx, path in enumerate(ordered_paths)]
    if episode_filter is not None:
        indexed = [(idx, path) for idx, path in indexed if idx in episode_filter]
    return indexed


def feature_keys(profile: RobotProfile, key_style: str) -> tuple[str, str, dict[str, str]]:
    if key_style == "pi05":
        return "state", "actions", {camera.raw_key: camera.raw_key for camera in profile.cameras}
    state_key = str(profile.raw["state"].get("feature") or "observation.state")
    action_key = str(profile.raw["action"].get("feature") or "action")
    image_keys = {camera.raw_key: camera.lerobot_key for camera in profile.cameras}
    return state_key, action_key, image_keys


def is_aloha_profile(profile: RobotProfile) -> bool:
    adapter = str(profile.raw.get("adapter") or "").lower()
    profile_id = str(profile.profile_id or "").lower()
    return adapter == "aloha" or profile_id == "aloha"


def aloha_uses_base_action(aloha_action_mode: str) -> bool:
    return aloha_action_mode == "joints_base"


def layout_names(items: list[dict[str, Any]]) -> set[str]:
    return {str(item.get("name") or "") for item in items}


def aloha_profile_has_state_base_layout(profile: RobotProfile) -> bool:
    names = layout_names(profile.state_layout())
    has_base_state = "base_state" in names or {"base_pose2d", "base_velocity"}.issubset(names)
    return "waist_height" in names and has_base_state


def aloha_profile_has_action_base_layout(profile: RobotProfile) -> bool:
    names = layout_names(profile.action_layout())
    return {"waist_height", "base_velocity"}.issubset(names)


def aloha_should_append_state_extras(profile: RobotProfile, aloha_action_mode: str) -> bool:
    return (
        is_aloha_profile(profile)
        and aloha_uses_base_action(aloha_action_mode)
        and not aloha_profile_has_state_base_layout(profile)
    )


def aloha_should_append_action_extras(profile: RobotProfile, aloha_action_mode: str) -> bool:
    return (
        is_aloha_profile(profile)
        and aloha_uses_base_action(aloha_action_mode)
        and not aloha_profile_has_action_base_layout(profile)
    )


def lerobot_state_dim(profile: RobotProfile, aloha_action_mode: str = "joints_base") -> int:
    if aloha_should_append_state_extras(profile, aloha_action_mode):
        return profile.state_dim + ALOHA_LEROBOT_STATE_EXTRA_DIM
    return profile.state_dim


def lerobot_action_dim(profile: RobotProfile, aloha_action_mode: str = "joints_base") -> int:
    if aloha_should_append_action_extras(profile, aloha_action_mode):
        return profile.action_dim + ALOHA_LEROBOT_ACTION_EXTRA_DIM
    return profile.action_dim


def build_features(
    profile: RobotProfile,
    key_style: str,
    image_size: tuple[int, int],
    use_videos: bool,
    aloha_action_mode: str = "joints_base",
) -> dict[str, dict[str, Any]]:
    width, height = image_size
    state_key, action_key, image_keys = feature_keys(profile, key_style)
    features: dict[str, dict[str, Any]] = {
        state_key: {
            "dtype": "float32",
            "shape": (lerobot_state_dim(profile, aloha_action_mode),),
            "names": ["state"],
        },
        action_key: {
            "dtype": "float32",
            "shape": (lerobot_action_dim(profile, aloha_action_mode),),
            "names": ["actions"],
        },
    }
    for raw_key, lerobot_key in image_keys.items():
        features[lerobot_key] = {
            "dtype": "video" if use_videos else "image",
            "shape": (height, width, 3),
            "names": ["height", "width", "channel"],
        }
    return features


def episode_dir_from_h5(h5_path: Path) -> Path:
    if h5_path.parent.name == "states":
        return h5_path.parent.parent
    return h5_path.parent


def video_path_for_camera(episode_dir: Path, raw_key: str) -> Path | None:
    names = CAMERA_FALLBACKS.get(raw_key, (raw_key,))
    for video_root in (
        episode_dir / "videos",
        episode_dir / "videos" / "rs",
        episode_dir / "observations" / "videos",
    ):
        for name in names:
            path = video_root / f"{name}.mp4"
            if path.is_file():
                return path
    return None


def lerobot_fail_closed(profile: RobotProfile) -> bool:
    config = profile.raw.get("lerobot_conversion")
    return bool(config.get("fail_closed", False)) if isinstance(config, dict) else False


def raise_for_strict_preprocess_failures(
    fail_closed: bool,
    failures: list[tuple[Path, BaseException]],
) -> None:
    if not fail_closed or not failures:
        return
    details = "; ".join(f"{path}: {error}" for path, error in failures[:5])
    if len(failures) > 5:
        details += f"; ... and {len(failures) - 5} more"
    raise RuntimeError(f"LeRobot preprocessing failed for {len(failures)} episode(s): {details}")


def decoded_video_frame_count(video_path: Path) -> int:
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        capture.release()
        raise ValueError(f"video is unreadable: {video_path}")
    count = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            if frame is None:
                raise ValueError(f"video returned an empty frame at index {count}: {video_path}")
            count += 1
    finally:
        capture.release()
    return count


def strict_preflight_required_cameras(
    h5_paths: list[Path],
    profile: RobotProfile,
    task_override: str = "",
    default_task: str = "",
) -> None:
    """Validate strict inputs completely before any output can be overwritten."""
    if not lerobot_fail_closed(profile):
        return

    failures: list[tuple[Path, BaseException]] = []
    for h5_path in h5_paths:
        episode_dir = episode_dir_from_h5(h5_path)
        try:
            episode = read_raw_episode(episode_dir, profile)
        except BaseException as error:
            failures.append((h5_path, error))
            continue
        if not episode.state_frames or not episode.actions:
            failures.append((h5_path, ValueError("missing state or action frames")))
            continue
        expected_frames = len(episode.state_frames)
        if len(episode.actions) != expected_frames:
            failures.append(
                (
                    h5_path,
                    ValueError(
                        f"state/action frame count mismatch: "
                        f"state={expected_frames}, action={len(episode.actions)}"
                    ),
                )
            )
            continue
        try:
            state_values = np.asarray([frame.state for frame in episode.state_frames], dtype=np.float64)
            action_values = np.asarray(episode.actions, dtype=np.float64)
            if state_values.ndim != 2 or state_values.shape[1] != profile.state_dim:
                raise ValueError(
                    f"state dimension mismatch: got {state_values.shape}, "
                    f"expected (*, {profile.state_dim})"
                )
            if action_values.ndim != 2 or action_values.shape[1] != profile.action_dim:
                raise ValueError(
                    f"action dimension mismatch: got {action_values.shape}, "
                    f"expected (*, {profile.action_dim})"
                )
            if not np.isfinite(state_values).all():
                raise ValueError("state contains NaN or Inf")
            if not np.isfinite(action_values).all():
                raise ValueError("action contains NaN or Inf")
            task_for_profile(
                profile,
                h5_path,
                episode.meta,
                task_override,
                default_task,
            )
        except BaseException as error:
            failures.append((h5_path, error))
            continue
        for camera in profile.cameras:
            if not camera.required:
                continue
            video_path = video_path_for_camera(episode_dir, camera.raw_key)
            if video_path is None:
                expected_name = CAMERA_FALLBACKS.get(camera.raw_key, (camera.raw_key,))[0]
                failures.append(
                    (
                        h5_path,
                        FileNotFoundError(
                            f"required camera '{camera.raw_key}' video is missing: "
                            f"{episode_dir / 'videos' / f'{expected_name}.mp4'}"
                        ),
                    )
                )
                continue
            try:
                video_frames = decoded_video_frame_count(video_path)
            except BaseException as error:
                failures.append(
                    (
                        h5_path,
                        ValueError(
                            f"required camera '{camera.raw_key}' video is unreadable: "
                            f"{video_path}: {error}"
                        ),
                    )
                )
                continue
            if video_frames != expected_frames:
                failures.append(
                    (
                        h5_path,
                        ValueError(
                            f"required camera '{camera.raw_key}' frame count mismatch: "
                            f"video={video_frames}, state_action={expected_frames}, path={video_path}"
                        ),
                    )
                )

    if failures:
        details = "; ".join(f"{path}: {error}" for path, error in failures[:10])
        if len(failures) > 10:
            details += f"; ... and {len(failures) - 10} more"
        raise RuntimeError(
            f"LeRobot strict preflight failed for {len(failures)} input issue(s): {details}"
        )


def requested_gpu_device_index(gpu_device: str) -> str:
    text = str(gpu_device).strip()
    if text.startswith("cuda:"):
        return text.split(":", 1)[1]
    if text == "cuda":
        return "0"
    return text or "0"


def gpu_device_index(gpu_device: str) -> str:
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


def cuda_device_name(gpu_device: str) -> str:
    return f"cuda:{gpu_device_index(gpu_device)}"


def torch_cuda_available(gpu_device: str) -> bool:
    try:
        import torch
    except ImportError:
        return False
    if not torch.cuda.is_available():
        return False
    try:
        index = int(gpu_device_index(gpu_device))
    except ValueError:
        return True
    return index < torch.cuda.device_count()


def require_torch_cuda(gpu_device: str) -> None:
    if torch_cuda_available(gpu_device):
        return
    raise RuntimeError(
        f"CUDA preprocessing requested, but PyTorch CUDA is not available for GPU {gpu_device}."
    )


def resize_batch_cuda(
    frames_bgr: list[np.ndarray],
    image_size: tuple[int, int],
    gpu_device: str,
) -> np.ndarray:
    import torch
    import torch.nn.functional as torch_f

    target_w, target_h = image_size
    device = torch.device(cuda_device_name(gpu_device))
    with torch.inference_mode():
        batch = np.stack(frames_bgr)
        tensor = torch.from_numpy(batch).to(device=device, non_blocking=True)
        tensor = tensor.permute(0, 3, 1, 2).to(dtype=torch.float32)
        tensor = tensor[:, [2, 1, 0], :, :]
        tensor = torch_f.interpolate(tensor, size=(target_h, target_w), mode="area")
        tensor = tensor.clamp_(0, 255).to(dtype=torch.uint8)
        return tensor.permute(0, 2, 3, 1).contiguous().cpu().numpy()


def decode_video_cpu(video_path: Path, image_size: tuple[int, int]) -> np.ndarray:
    target_w, target_h = image_size
    cap = cv2.VideoCapture(str(video_path))
    frames: list[np.ndarray] = []
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frame = cv2.resize(frame, (target_w, target_h), interpolation=cv2.INTER_AREA)
            frames.append(frame)
    finally:
        cap.release()
    if not frames:
        return np.empty((0, target_h, target_w, 3), dtype=np.uint8)
    return np.stack(frames).astype(np.uint8, copy=False)


def decode_video_cuda(
    video_path: Path,
    image_size: tuple[int, int],
    gpu_device: str,
    batch_size: int,
) -> np.ndarray:
    target_w, target_h = image_size
    batch_size = max(1, int(batch_size))
    cap = cv2.VideoCapture(str(video_path))
    resized_batches: list[np.ndarray] = []
    pending: list[np.ndarray] = []
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            pending.append(frame)
            if len(pending) >= batch_size:
                resized_batches.append(resize_batch_cuda(pending, image_size, gpu_device))
                pending = []
        if pending:
            resized_batches.append(resize_batch_cuda(pending, image_size, gpu_device))
    finally:
        cap.release()
    if not resized_batches:
        return np.empty((0, target_h, target_w, 3), dtype=np.uint8)
    return np.concatenate(resized_batches, axis=0).astype(np.uint8, copy=False)


def decode_video(
    video_path: Path,
    image_size: tuple[int, int],
    preprocess_device: str,
    gpu_device: str,
    gpu_resize_batch_size: int,
) -> np.ndarray:
    if preprocess_device == "cuda":
        return decode_video_cuda(video_path, image_size, gpu_device, gpu_resize_batch_size)
    return decode_video_cpu(video_path, image_size)


def task_from_meta(meta: dict[str, Any], override: str, default: str) -> str:
    if override:
        return override
    for key in ("full_instructions_en", "full-instructions-en"):
        value = meta.get(key)
        if isinstance(value, list):
            task_values = [str(item).strip() for item in value if str(item).strip()]
            if task_values:
                return task_values[0]
        if isinstance(value, str) and value.strip():
            return value.strip()
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
            extra = parsed.get("extra")
            if isinstance(extra, list) and extra:
                return _build_language_instruction([str(item) for item in extra])
            description = str(parsed.get("description") or "").strip()
            if description:
                return description
        return value.strip()
    return default


def task_for_profile(
    profile: RobotProfile,
    h5_path: Path,
    meta: dict[str, Any],
    task_override: str,
    default_task: str,
) -> str:
    if is_aloha_profile(profile):
        if str(task_override).strip() or str(default_task).strip():
            raise ValueError(
                "ALOHA LeRobot tasks must come from HDF5; write the task into "
                "aligned_joints.h5 before conversion instead of using --task "
                "or --default-task."
            )
        return require_hdf5_task(h5_path)
    if lerobot_fail_closed(profile):
        authoritative_task = require_hdf5_task(h5_path)
        requested_task = str(task_override or default_task).strip()
        if requested_task and requested_task != authoritative_task:
            raise ValueError(
                f"{h5_path}: task override {requested_task!r} does not match "
                f"authoritative HDF5 task {authoritative_task!r}"
            )
        return authoritative_task
    task = task_from_meta(meta, task_override, default_task)
    return task


def compact_subtask_segments(meta: dict[str, Any]) -> list[dict[str, Any]]:
    raw_segments = meta.get("subtask_segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raw_segments = meta.get("segment_instructions")
    if not isinstance(raw_segments, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw_segments:
        if not isinstance(item, dict):
            continue
        try:
            start = int(item.get("start") if item.get("start") is not None else item.get("start_time"))
            end = int(item.get("end") if item.get("end") is not None else item.get("end_time"))
        except (TypeError, ValueError):
            continue
        subtask = str(item.get("subtask") or "").strip()
        if not subtask:
            description_en = item.get("description_en")
            description_zh = item.get("description_zh")
            if isinstance(description_en, list):
                subtask = " ".join(str(value).strip() for value in description_en if str(value).strip())
            if not subtask and isinstance(description_zh, list):
                subtask = " ".join(str(value).strip() for value in description_zh if str(value).strip())
        segment = {"start": start, "end": end, "subtask": subtask}
        if item.get("id"):
            segment["id"] = str(item["id"])
        try:
            stage_number = int(item.get("stage_number"))
        except (TypeError, ValueError):
            stage_number = 0
        if stage_number > 0:
            segment["stage_number"] = stage_number
        if item.get("source"):
            segment["source"] = str(item["source"])
        out.append(segment)
    return out


def normalise_quality_grade(value: Any, *, allow_empty: bool = True) -> str:
    text = str(value or "").strip().upper()
    if not text and allow_empty:
        return ""
    if text not in QUALITY_GRADES:
        raise ValueError(
            f"invalid quality grade: {value!r}; expected one of {QUALITY_GRADES}"
        )
    return text


def resolve_episode_quality_grade(
    meta: dict[str, Any], explicit_grade: str = ""
) -> str:
    collection_quality = meta.get("collection_quality") if isinstance(meta.get("collection_quality"), dict) else {}
    source_value = (
        meta.get("quality_grade")
        or meta.get("manual_quality_grade")
        or collection_quality.get("grade")
        or ""
    )
    source_grade = normalise_quality_grade(source_value)
    requested_grade = normalise_quality_grade(explicit_grade)
    if source_grade and requested_grade and source_grade != requested_grade:
        raise ValueError(
            "quality grade conflict: "
            f"sidecar={source_grade}, explicit={requested_grade}"
        )
    return requested_grade or source_grade


def compact_quality_meta(meta: dict[str, Any]) -> dict[str, Any]:
    collection_quality = meta.get("collection_quality") if isinstance(meta.get("collection_quality"), dict) else {}
    grade = resolve_episode_quality_grade(meta)
    out: dict[str, Any] = {}
    if grade:
        out["quality_grade"] = grade
    for key in ("reason_labels_zh", "reason_labels_en"):
        value = meta.get(key) or collection_quality.get(key)
        if value not in ("", [], None):
            out[key] = value
    return out


def instruction_meta_from_episode_meta(
    meta: dict[str, Any],
    task: str,
    *,
    authoritative_task: bool = False,
    quality_grade: str = "",
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in (
        "instruction_info_file",
        "full_instructions_en",
    ):
        value = meta.get(key)
        if value not in ("", [], None):
            out[key] = value
    subtask_segments = compact_subtask_segments(meta)
    if subtask_segments:
        out["subtask_segments"] = subtask_segments
        out["stage_count"] = len(subtask_segments)
    transitions = meta.get("subtask_transitions")
    if isinstance(transitions, list) and transitions:
        try:
            normalised_transitions = [int(value) for value in transitions]
        except (TypeError, ValueError):
            normalised_transitions = []
        if normalised_transitions:
            out["subtask_transitions"] = normalised_transitions
    out.update(compact_quality_meta(meta))
    resolved_grade = resolve_episode_quality_grade(meta, quality_grade)
    if resolved_grade:
        out["quality_grade"] = resolved_grade
    items = meta.get("items")
    if isinstance(items, list) and items:
        out["items"] = items
    if task:
        out["task"] = task
        if authoritative_task:
            out["tasks"] = [task]
            out["full_instructions_en"] = [task]
        else:
            full_instructions_en = out.get("full_instructions_en")
            if isinstance(full_instructions_en, list) and full_instructions_en:
                out["tasks"] = [str(value) for value in full_instructions_en if str(value).strip()]
            else:
                out["tasks"] = [task]
                out["full_instructions_en"] = [task]
    return out


def _json_string_to_dict(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _build_language_instruction(extra_steps: list[str]) -> str:
    grasp_re = re.compile(r"grasp\s+(.+?)\s*$", re.IGNORECASE)
    article_re = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)
    for step in extra_steps:
        match = grasp_re.match(step.strip())
        if not match:
            continue
        items = [
            article_re.sub("", part.strip()).rstrip(".").strip()
            for part in re.split(r"\s+and\s+", match.group(1), flags=re.IGNORECASE)
        ]
        items = [item for item in items if item]
        if not items:
            break
        if len(items) == 1:
            return f"Target: {items[0]}. Pick the {items[0]} from the shelf and place it into the cart."
        target_phrase = " and ".join(items)
        action_object = " and ".join(f"the {item}" for item in items)
        return f"Target: {target_phrase}. Pick {action_object} from the shelf and place them into the cart."
    return ", ".join(extra_steps)


def numeric_hdf5_keys(keys: Any) -> list[str]:
    return sorted((str(key) for key in keys if str(key).isdigit()), key=lambda key: int(key))


def hdf5_vector_or_zeros(file_obj: Any, key: str, dim: int) -> list[float]:
    if key not in file_obj:
        return [0.0] * dim
    values = np.asarray(file_obj[key][()], dtype=np.float32).reshape(-1).tolist()
    if len(values) < dim:
        values.extend([0.0] * (dim - len(values)))
    return [float(value) for value in values[:dim]]


def read_aloha_lerobot_action_extras(h5_path: Path) -> np.ndarray:
    try:
        import h5py  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Reading ALOHA LeRobot action extras requires h5py. "
            "Install quality_pipeline/requirements.txt in the active environment."
        ) from exc

    rows: list[list[float]] = []
    with h5py.File(h5_path, "r") as file_obj:
        for key in numeric_hdf5_keys(file_obj.keys()):
            lift_height = hdf5_vector_or_zeros(file_obj, f"{key}/action/waist/position", 1)
            chassis_velocity = hdf5_vector_or_zeros(file_obj, f"{key}/action/robot/velocity", 3)
            rows.append(lift_height + chassis_velocity)
    return np.asarray(rows, dtype=np.float32)


def read_aloha_lerobot_state_extras(h5_path: Path) -> np.ndarray:
    try:
        import h5py  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Reading ALOHA LeRobot state extras requires h5py. "
            "Install quality_pipeline/requirements.txt in the active environment."
        ) from exc

    rows: list[list[float]] = []
    with h5py.File(h5_path, "r") as file_obj:
        for key in numeric_hdf5_keys(file_obj.keys()):
            lift_height = hdf5_vector_or_zeros(file_obj, f"{key}/state/waist/position", 1)
            base_state = hdf5_vector_or_zeros(file_obj, f"{key}/state/robot/base_state", 6)
            if not any(base_state):
                pose2d = hdf5_vector_or_zeros(file_obj, f"{key}/state/robot/pose2d", 3)
                velocity = hdf5_vector_or_zeros(file_obj, f"{key}/state/robot/velocity", 3)
                base_state = pose2d + velocity
            rows.append(lift_height + base_state)
    return np.asarray(rows, dtype=np.float32)


def lerobot_states_from_episode(
    h5_path: Path,
    profile: RobotProfile,
    episode_states: list[list[float]],
    aloha_action_mode: str = "joints_base",
) -> np.ndarray:
    states = np.asarray(episode_states, dtype=np.float32)
    if not aloha_should_append_state_extras(profile, aloha_action_mode):
        return states
    extras = read_aloha_lerobot_state_extras(h5_path)
    n_frames = min(len(states), len(extras))
    if n_frames <= 0:
        return np.empty((0, lerobot_state_dim(profile, aloha_action_mode)), dtype=np.float32)
    return np.concatenate([states[:n_frames], extras[:n_frames]], axis=1).astype(np.float32, copy=False)


def lerobot_actions_from_episode(
    h5_path: Path,
    profile: RobotProfile,
    episode_actions: list[list[float]],
    aloha_action_mode: str = "joints_base",
) -> np.ndarray:
    actions = np.asarray(episode_actions, dtype=np.float32)
    if not aloha_should_append_action_extras(profile, aloha_action_mode):
        return actions
    extras = read_aloha_lerobot_action_extras(h5_path)
    n_frames = min(len(actions), len(extras))
    if n_frames <= 0:
        return np.empty((0, lerobot_action_dim(profile, aloha_action_mode)), dtype=np.float32)
    return np.concatenate([actions[:n_frames], extras[:n_frames]], axis=1).astype(np.float32, copy=False)


def preprocess_episode(
    h5_path_text: str,
    profile_path_text: str,
    image_size: tuple[int, int],
    task_override: str,
    default_task: str,
    preprocess_device: str,
    gpu_device: str,
    gpu_resize_batch_size: int,
    aloha_action_mode: str,
    quality_grade: str = "",
) -> dict[str, Any] | None:
    h5_path = Path(h5_path_text).expanduser().resolve()
    profile = load_profile(profile_path_text)
    episode_dir = episode_dir_from_h5(h5_path)
    episode = read_raw_episode(episode_dir, profile)
    episode_quality_grade = resolve_episode_quality_grade(
        episode.meta, quality_grade
    )
    fail_closed = lerobot_fail_closed(profile)

    if not episode.state_frames or not episode.actions:
        return None

    states = lerobot_states_from_episode(
        h5_path,
        profile,
        [frame.state for frame in episode.state_frames],
        aloha_action_mode,
    )
    actions = lerobot_actions_from_episode(h5_path, profile, episode.actions, aloha_action_mode)
    expected_state_dim = lerobot_state_dim(profile, aloha_action_mode)
    if states.ndim != 2 or states.shape[1] != expected_state_dim:
        raise ValueError(f"{h5_path}: expected state dim {expected_state_dim}, got {states.shape}")
    expected_action_dim = lerobot_action_dim(profile, aloha_action_mode)
    if actions.ndim != 2 or actions.shape[1] != expected_action_dim:
        raise ValueError(f"{h5_path}: expected action dim {expected_action_dim}, got {actions.shape}")
    n_frames = min(len(states), len(actions))
    cam_frames: dict[str, np.ndarray] = {}
    for camera in profile.cameras:
        video_path = video_path_for_camera(episode_dir, camera.raw_key)
        if video_path is None:
            if fail_closed and camera.required:
                expected_name = CAMERA_FALLBACKS.get(camera.raw_key, (camera.raw_key,))[0]
                raise FileNotFoundError(
                    f"{h5_path}: required camera '{camera.raw_key}' video is missing: "
                    f"{episode_dir / 'videos' / f'{expected_name}.mp4'}"
                )
            return None
        frames = decode_video(
            video_path,
            image_size,
            preprocess_device,
            gpu_device,
            gpu_resize_batch_size,
        )
        if len(frames) == 0:
            if fail_closed and camera.required:
                raise ValueError(
                    f"{h5_path}: required camera '{camera.raw_key}' video is unreadable: {video_path}"
                )
            return None
        if fail_closed and camera.required and len(frames) != n_frames:
            raise ValueError(
                f"{h5_path}: required camera '{camera.raw_key}' frame count mismatch: "
                f"video={len(frames)}, state_action={n_frames}, path={video_path}"
            )
        cam_frames[camera.raw_key] = frames
        n_frames = min(n_frames, len(frames))

    if n_frames <= 0:
        return None

    task = task_for_profile(profile, h5_path, episode.meta, task_override, default_task)
    instruction_meta = instruction_meta_from_episode_meta(
        episode.meta,
        task,
        authoritative_task=is_aloha_profile(profile),
        quality_grade=episode_quality_grade,
    )
    if task_override:
        instruction_meta["task"] = task
        instruction_meta["tasks"] = [task]
        instruction_meta["full_instructions_en"] = [task]
    return {
        "cam_frames": {key: value[:n_frames] for key, value in cam_frames.items()},
        "states": states[:n_frames],
        "actions": actions[:n_frames],
        "task": task,
        "instruction_meta": instruction_meta,
        "num_frames": n_frames,
        "episode_dir": str(episode_dir),
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file_obj:
        for line in file_obj:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as file_obj:
        for row in rows:
            file_obj.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp_path.replace(path)


def normalized_existing_path(path_text: str, data_root: Path) -> str:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = data_root / path
    return str(path.resolve())


def completed_source_h5_records(mapping_path: Path, data_root: Path) -> dict[str, dict[str, Any]]:
    completed: dict[str, dict[str, Any]] = {}
    if not mapping_path.exists():
        return completed
    try:
        with mapping_path.open("r", encoding="utf-8") as file_obj:
            mapping = json.load(file_obj)
    except json.JSONDecodeError:
        return completed
    episodes = mapping.get("episodes") if isinstance(mapping, dict) else None
    if not isinstance(episodes, list):
        return completed
    for item in episodes:
        if not isinstance(item, dict):
            continue
        source_h5 = item.get("source_h5") or item.get("raw_path")
        if isinstance(source_h5, str) and source_h5:
            completed[normalized_existing_path(source_h5, data_root)] = item
    return completed


def completed_source_h5_paths(mapping_path: Path, data_root: Path) -> set[str]:
    return set(completed_source_h5_records(mapping_path, data_root))


def completed_record_matches_current_layout(
    record: dict[str, Any],
    profile: RobotProfile,
    aloha_action_mode: str,
) -> bool:
    if not is_aloha_profile(profile):
        return True
    try:
        state_dim = int(record.get("state_dim"))
        action_dim = int(record.get("action_dim"))
    except (TypeError, ValueError):
        return False
    return (
        str(record.get("aloha_action_mode") or "") == aloha_action_mode
        and state_dim == lerobot_state_dim(profile, aloha_action_mode)
        and action_dim == lerobot_action_dim(profile, aloha_action_mode)
    )


def completed_record_task_matches(record: dict[str, Any], expected_task: str) -> bool:
    expected = str(expected_task or "").strip()
    return bool(expected) and str(record.get("task") or "").strip() == expected


def safe_relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def lerobot_episode_name(episode_index: int) -> str:
    return f"episode_{episode_index:06d}"


def init_episode_mapping(
    *,
    repo_id: str,
    data_dir: Path,
    output_path: Path,
    key_style: str,
) -> dict[str, Any]:
    return {
        "repo_id": repo_id,
        "data_dir": str(data_dir),
        "output_dataset": str(output_path),
        "key_style": key_style,
        "mapping_file": f"meta/{EPISODE_MAPPING_NAME}",
        "episodes": [],
    }


def load_episode_mapping(
    mapping_path: Path,
    *,
    repo_id: str,
    data_dir: Path,
    output_path: Path,
    key_style: str,
) -> dict[str, Any]:
    if not mapping_path.exists():
        return init_episode_mapping(
            repo_id=repo_id,
            data_dir=data_dir,
            output_path=output_path,
            key_style=key_style,
        )
    with mapping_path.open("r", encoding="utf-8") as file_obj:
        mapping = json.load(file_obj)
    if not isinstance(mapping, dict):
        raise ValueError(f"Invalid episode mapping JSON: {mapping_path}")
    episodes = mapping.get("episodes")
    if not isinstance(episodes, list):
        mapping["episodes"] = []
    mapping["repo_id"] = repo_id
    mapping["data_dir"] = str(data_dir)
    mapping["output_dataset"] = str(output_path)
    mapping["key_style"] = key_style
    mapping["mapping_file"] = f"meta/{EPISODE_MAPPING_NAME}"
    enrich_episode_mapping_records(mapping, data_dir)
    return mapping


def write_episode_mapping(mapping_path: Path, mapping: dict[str, Any]) -> None:
    episodes = mapping.get("episodes")
    if isinstance(episodes, list):
        episodes.sort(key=lambda item: int(item.get("lerobot_episode_index", -1)))
        mapping["episode_count"] = len(episodes)
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = mapping_path.with_suffix(mapping_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(mapping_path)


def patch_lerobot_episodes_meta(output_path: Path, updates: dict[int, dict[str, Any]]) -> None:
    if not updates:
        return
    stale_keys = {
        "collection_quality",
        "description_en",
        "full_instructions_zh",
        "segment_instructions",
        "manual_quality_grade",
        "manual_review_reason",
        "manual_failure",
        "manual_failure_reason",
        "quality_description",
        "reason_codes",
        "reason_labels",
        "scene",
        "level_definition",
    }
    episodes_path = output_path / "meta" / "episodes.jsonl"
    rows = read_jsonl(episodes_path)
    seen: set[int] = set()
    for row in rows:
        for key in stale_keys:
            row.pop(key, None)
        try:
            episode_index = int(row.get("episode_index"))
        except (TypeError, ValueError):
            continue
        extra = updates.get(episode_index)
        if not extra:
            continue
        row.update(extra)
        seen.add(episode_index)
    for episode_index, extra in sorted(updates.items()):
        if episode_index in seen:
            continue
        row = {"episode_index": episode_index}
        row.update(extra)
        rows.append(row)
    rows.sort(key=lambda item: int(item.get("episode_index", -1)))
    write_jsonl(episodes_path, rows)


def read_lerobot_info(output_path: Path) -> dict[str, Any]:
    info_path = output_path / "meta" / "info.json"
    if not info_path.exists():
        return {}
    try:
        return json.loads(info_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def lerobot_episode_chunk(info: dict[str, Any], episode_index: int) -> int:
    chunks_size = int(info.get("chunks_size") or 1000)
    if chunks_size <= 0:
        chunks_size = 1000
    return episode_index // chunks_size


def lerobot_relative_data_path(info: dict[str, Any], episode_index: int) -> Path:
    pattern = str(info.get("data_path") or "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet")
    text = pattern.format(
        episode_chunk=lerobot_episode_chunk(info, episode_index),
        episode_index=episode_index,
    )
    return Path(text)


def lerobot_relative_video_path(info: dict[str, Any], episode_index: int, video_key: str) -> Path:
    pattern = str(
        info.get("video_path")
        or "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4"
    )
    text = pattern.format(
        episode_chunk=lerobot_episode_chunk(info, episode_index),
        episode_index=episode_index,
        video_key=video_key,
    )
    return Path(text)


def lerobot_video_keys(info: dict[str, Any]) -> list[str]:
    features = info.get("features")
    if not isinstance(features, dict):
        return []
    keys: list[str] = []
    for key, value in features.items():
        if isinstance(value, dict) and value.get("dtype") == "video":
            keys.append(str(key))
    return keys


def episode_meta_row(output_path: Path, episode_index: int) -> dict[str, Any] | None:
    for row in read_jsonl(output_path / "meta" / "episodes.jsonl"):
        try:
            row_episode_index = int(row.get("episode_index"))
        except (TypeError, ValueError):
            continue
        if row_episode_index == episode_index:
            return row
    return None


def parquet_num_rows(path: Path) -> int:
    try:
        import pyarrow.parquet as pq  # type: ignore

        return int(pq.ParquetFile(path).metadata.num_rows)
    except ImportError:
        try:
            import pandas as pd  # type: ignore
        except ImportError as exc:
            raise RuntimeError("pyarrow or pandas is required to validate parquet rows") from exc
        return int(len(pd.read_parquet(path)))


def ffprobe_path() -> str | None:
    return shutil.which("ffprobe")


def parse_frame_rate(value: Any) -> float | None:
    if value in (None, "", "0/0", "N/A"):
        return None
    text = str(value)
    if "/" in text:
        num_text, den_text = text.split("/", 1)
        try:
            denominator = float(den_text)
            if denominator == 0:
                return None
            return float(num_text) / denominator
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def ffprobe_video_frame_count_once(path: Path, *, count_frames: bool) -> int | None:
    probe = ffprobe_path()
    if not probe:
        return None
    cmd = [
        probe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
    ]
    if count_frames:
        cmd.append("-count_frames")
    cmd.extend(
        [
            "-show_entries",
            "stream=nb_read_frames,nb_frames,duration,avg_frame_rate,r_frame_rate",
            "-of",
            "json",
            str(path),
        ]
    )
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=120 if count_frames else 15,
        )
    except subprocess.SubprocessError:
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    streams = payload.get("streams")
    if not isinstance(streams, list) or not streams:
        return None
    stream = streams[0]
    if not isinstance(stream, dict):
        return None
    for key in ("nb_read_frames", "nb_frames"):
        value = stream.get(key)
        if value in (None, "N/A", ""):
            continue
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count >= 0:
            return count
    if not count_frames:
        try:
            duration = float(stream.get("duration"))
        except (TypeError, ValueError):
            duration = 0.0
        frame_rate = parse_frame_rate(stream.get("avg_frame_rate")) or parse_frame_rate(
            stream.get("r_frame_rate")
        )
        if duration > 0 and frame_rate and frame_rate > 0:
            return int(round(duration * frame_rate))
    return None


def ffprobe_video_frame_count(path: Path) -> int | None:
    count = ffprobe_video_frame_count_once(path, count_frames=False)
    if count is not None:
        return count
    return ffprobe_video_frame_count_once(path, count_frames=True)


def video_frame_count(path: Path) -> int:
    count = ffprobe_video_frame_count(path)
    if count is not None:
        return count
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            raise RuntimeError(f"could not open video: {path}")
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if count > 0:
            return count
        decoded = 0
        while True:
            ok, _ = cap.read()
            if not ok:
                break
            decoded += 1
        return decoded
    finally:
        cap.release()


def validate_lerobot_episode(
    output_path: Path,
    episode_index: int,
    expected_frames: int | None = None,
) -> list[str]:
    issues: list[str] = []
    info = read_lerobot_info(output_path)
    if not info:
        return [f"missing or invalid {output_path / 'meta' / 'info.json'}"]

    row = episode_meta_row(output_path, episode_index)
    if row is None:
        issues.append(f"missing meta/episodes.jsonl row for episode_{episode_index:06d}")
    else:
        try:
            meta_length = int(row.get("length"))
        except (TypeError, ValueError):
            meta_length = -1
        if expected_frames is None and meta_length >= 0:
            expected_frames = meta_length
        elif expected_frames is not None and meta_length != expected_frames:
            issues.append(
                f"episode_{episode_index:06d} meta length {meta_length} != expected {expected_frames}"
            )

    data_file = output_path / lerobot_relative_data_path(info, episode_index)
    if not data_file.exists():
        issues.append(f"missing parquet {data_file}")
    else:
        try:
            row_count = parquet_num_rows(data_file)
        except Exception as err:
            issues.append(f"could not read parquet {data_file}: {err}")
        else:
            if expected_frames is None:
                expected_frames = row_count
            if row_count != expected_frames:
                issues.append(
                    f"{data_file.name} rows {row_count} != expected {expected_frames}"
                )

    if expected_frames is None:
        return issues

    for video_key in lerobot_video_keys(info):
        video_file = output_path / lerobot_relative_video_path(info, episode_index, video_key)
        if not video_file.exists():
            issues.append(f"missing video {video_file}")
            continue
        try:
            frames = video_frame_count(video_file)
        except Exception as err:
            issues.append(f"could not read video {video_file}: {err}")
            continue
        if frames != expected_frames:
            issues.append(
                f"{video_key} video frames {frames} != expected {expected_frames}"
            )
    return issues


def replace_episode_jsonl_row(path: Path, episode_index: int, replacement: dict[str, Any]) -> None:
    rows = read_jsonl(path)
    replacement = dict(replacement)
    replacement["episode_index"] = episode_index
    replaced = False
    for idx, row in enumerate(rows):
        try:
            row_episode_index = int(row.get("episode_index"))
        except (TypeError, ValueError):
            continue
        if row_episode_index == episode_index:
            rows[idx] = replacement
            replaced = True
            break
    if not replaced:
        rows.append(replacement)
    rows.sort(key=lambda item: int(item.get("episode_index", -1)))
    write_jsonl(path, rows)


def ensure_lerobot_task_index(output_path: Path, task: str) -> int:
    tasks_path = output_path / "meta" / "tasks.jsonl"
    rows = read_jsonl(tasks_path)
    for row in rows:
        if str(row.get("task") or "") != task:
            continue
        try:
            return int(row.get("task_index"))
        except (TypeError, ValueError):
            break
    task_index = 0
    if rows:
        existing_indices: list[int] = []
        for row in rows:
            try:
                existing_indices.append(int(row.get("task_index")))
            except (TypeError, ValueError):
                continue
        task_index = max(existing_indices, default=-1) + 1
    rows.append({"task_index": task_index, "task": task})
    rows.sort(key=lambda item: int(item.get("task_index", -1)))
    write_jsonl(tasks_path, rows)
    return task_index


def rewrite_parquet_task_index(path: Path, task_index: int) -> None:
    try:
        import pyarrow as pa  # type: ignore
        import pyarrow.parquet as pq  # type: ignore

        table = pq.read_table(path)
        if "task_index" not in table.column_names:
            return
        col_idx = table.column_names.index("task_index")
        old_field = table.schema.field(col_idx)
        values = pa.array([task_index] * table.num_rows, type=old_field.type)
        table = table.set_column(col_idx, old_field, values)
        pq.write_table(table, path)
        return
    except ImportError:
        pass

    try:
        import pandas as pd  # type: ignore
    except ImportError as exc:
        raise RuntimeError("pyarrow or pandas is required to patch parquet task_index") from exc
    df = pd.read_parquet(path)
    if "task_index" not in df.columns:
        return
    df["task_index"] = task_index
    df.to_parquet(path, index=False)


def refresh_lerobot_info_counts(output_path: Path) -> None:
    info_path = output_path / "meta" / "info.json"
    info = read_lerobot_info(output_path)
    if not info:
        return
    episode_rows = read_jsonl(output_path / "meta" / "episodes.jsonl")
    lengths: list[int] = []
    episode_indices: list[int] = []
    for row in episode_rows:
        try:
            episode_indices.append(int(row.get("episode_index")))
            lengths.append(int(row.get("length")))
        except (TypeError, ValueError):
            continue
    total_episodes = len(episode_indices)
    video_count = len(lerobot_video_keys(info))
    chunks_size = int(info.get("chunks_size") or 1000)
    info["total_episodes"] = total_episodes
    info["total_frames"] = int(sum(lengths))
    info["total_videos"] = total_episodes * video_count
    if episode_indices:
        info["total_chunks"] = max(1, max(idx // max(1, chunks_size) for idx in episode_indices) + 1)
    else:
        info["total_chunks"] = 0
    info["total_tasks"] = len(read_jsonl(output_path / "meta" / "tasks.jsonl"))
    info["splits"] = {"train": f"0:{total_episodes}"}
    tmp_path = info_path.with_suffix(info_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(info, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
    tmp_path.replace(info_path)


def copy_temp_episode_to_target(
    *,
    temp_output_path: Path,
    target_output_path: Path,
    target_episode_index: int,
    task: str,
) -> None:
    temp_info = read_lerobot_info(temp_output_path)
    target_info = read_lerobot_info(target_output_path)
    if not temp_info or not target_info:
        raise RuntimeError("missing LeRobot metadata while copying rewritten episode")

    temp_data_file = temp_output_path / lerobot_relative_data_path(temp_info, 0)
    target_data_file = target_output_path / lerobot_relative_data_path(target_info, target_episode_index)
    target_data_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(temp_data_file, target_data_file)
    target_task_index = ensure_lerobot_task_index(target_output_path, task)
    rewrite_parquet_task_index(target_data_file, target_task_index)

    temp_video_keys = set(lerobot_video_keys(temp_info))
    for video_key in lerobot_video_keys(target_info):
        if video_key not in temp_video_keys:
            raise RuntimeError(f"temporary rewrite did not produce video key {video_key}")
        temp_video_file = temp_output_path / lerobot_relative_video_path(temp_info, 0, video_key)
        target_video_file = target_output_path / lerobot_relative_video_path(
            target_info,
            target_episode_index,
            video_key,
        )
        target_video_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(temp_video_file, target_video_file)

    temp_episode_row = episode_meta_row(temp_output_path, 0)
    if temp_episode_row:
        temp_episode_row = dict(temp_episode_row)
        temp_episode_row["episode_index"] = target_episode_index
        replace_episode_jsonl_row(
            target_output_path / "meta" / "episodes.jsonl",
            target_episode_index,
            temp_episode_row,
        )

    temp_stats_rows = read_jsonl(temp_output_path / "meta" / "episodes_stats.jsonl")
    if temp_stats_rows:
        temp_stats_row = dict(temp_stats_rows[0])
        temp_stats_row["episode_index"] = target_episode_index
        replace_episode_jsonl_row(
            target_output_path / "meta" / "episodes_stats.jsonl",
            target_episode_index,
            temp_stats_row,
        )
    refresh_lerobot_info_counts(target_output_path)


def upsert_episode_mapping(mapping: dict[str, Any], record: dict[str, Any]) -> None:
    episodes = mapping.setdefault("episodes", [])
    if not isinstance(episodes, list):
        raise ValueError("Invalid episode mapping: episodes must be a list")
    source_h5 = record["source_h5"]
    lerobot_episode_index = record["lerobot_episode_index"]
    episodes[:] = [
        item
        for item in episodes
        if item.get("source_h5") != source_h5
        and item.get("lerobot_episode_index") != lerobot_episode_index
    ]
    episodes.append(record)


def read_hdf5_episode_meta(episode_dir: Path) -> dict[str, Any]:
    meta_path = episode_dir / "meta" / "episode_meta.json"
    if not meta_path.is_file():
        meta_path = episode_dir / "episode_meta.json"
    if not meta_path.is_file():
        return {}
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def enrich_episode_mapping_records(mapping: dict[str, Any], data_root: Path) -> None:
    episodes = mapping.get("episodes")
    if not isinstance(episodes, list):
        return
    for item in episodes:
        if not isinstance(item, dict):
            continue
        source_h5 = str(item.get("source_h5") or item.get("hdf5_file") or "").strip()
        source_episode_dir = str(item.get("source_episode_dir") or item.get("hdf5_episode_dir") or "").strip()
        h5_path: Path | None = None
        if source_h5:
            h5_path = Path(source_h5)
            if not h5_path.is_absolute():
                h5_path = data_root / h5_path
        elif source_episode_dir:
            episode_dir = Path(source_episode_dir)
            if not episode_dir.is_absolute():
                episode_dir = data_root / episode_dir
            h5_path = episode_dir / "states" / "aligned_joints.h5"
        if h5_path is None:
            continue
        episode_dir = episode_dir_from_h5(h5_path)
        hdf5_meta = read_hdf5_episode_meta(episode_dir)
        source_mcap_files = hdf5_meta.get("source_mcap_files")
        if not isinstance(source_mcap_files, list):
            source_mcap_files = []
        source_mcap_episode_name = str(hdf5_meta.get("source_episode_name") or "").strip() or episode_dir.name
        item.setdefault("source_mcap_episode_name", source_mcap_episode_name)
        item.setdefault("source_mcap_files", [str(path) for path in source_mcap_files])
        item.setdefault("source_mcap_file_names", [Path(str(path)).name for path in source_mcap_files])
        item.setdefault("hdf5_episode_name", episode_dir.name)
        item.setdefault("hdf5_episode_dir", safe_relative_path(episode_dir, data_root))
        item.setdefault("hdf5_file", safe_relative_path(h5_path, data_root))


def episode_mapping_record(
    *,
    dataset: LeRobotDataset,
    data_root: Path,
    output_path: Path,
    source_index: int,
    lerobot_index: int,
    h5_path: Path,
    num_frames: int,
    state_dim: int,
    action_dim: int,
    aloha_action_mode: str,
    task: str = "",
) -> dict[str, Any]:
    source_episode_dir = episode_dir_from_h5(h5_path)
    hdf5_meta = read_hdf5_episode_meta(source_episode_dir)
    source_mcap_files = hdf5_meta.get("source_mcap_files")
    if not isinstance(source_mcap_files, list):
        source_mcap_files = []
    source_mcap_episode_name = str(hdf5_meta.get("source_episode_name") or "").strip()
    if not source_mcap_episode_name:
        source_mcap_episode_name = source_episode_dir.name
    episode_name = lerobot_episode_name(lerobot_index)
    record: dict[str, Any] = {
        "source_episode_index": source_index,
        "source_episode_name": source_episode_dir.name,
        "source_file_name": source_episode_dir.name,
        "source_episode_dir": safe_relative_path(source_episode_dir, data_root),
        "source_h5": safe_relative_path(h5_path, data_root),
        "source_hdf5_file_name": h5_path.name,
        "source_mcap_episode_name": source_mcap_episode_name,
        "source_mcap_files": [str(path) for path in source_mcap_files],
        "source_mcap_file_names": [Path(str(path)).name for path in source_mcap_files],
        "hdf5_episode_name": source_episode_dir.name,
        "hdf5_episode_dir": safe_relative_path(source_episode_dir, data_root),
        "hdf5_file": safe_relative_path(h5_path, data_root),
        "lerobot_dataset_dir": str(output_path),
        "lerobot_episode_index": lerobot_index,
        "lerobot_episode_name": episode_name,
        "converted_file_name": episode_name,
        "num_frames": num_frames,
        "state_dim": state_dim,
        "action_dim": action_dim,
        "aloha_action_mode": aloha_action_mode,
        "task": task,
    }
    meta = getattr(dataset, "meta", None)
    if meta is not None:
        if hasattr(meta, "get_data_file_path"):
            data_file = meta.get_data_file_path(ep_index=lerobot_index)
            record["lerobot_data_file"] = str(data_file)
        if hasattr(meta, "get_video_file_path"):
            record["lerobot_video_files"] = {
                key: str(meta.get_video_file_path(lerobot_index, key))
                for key in getattr(meta, "video_keys", [])
            }
    return record


def ffmpeg_path() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("ffmpeg was not found in PATH; cannot use GPU video encoding.")
    return path


def ffmpeg_encoder_available(encoder: str) -> bool:
    try:
        result = subprocess.run(
            [ffmpeg_path(), "-hide_banner", "-encoders"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.SubprocessError:
        return False
    return encoder in result.stdout


def require_ffmpeg_encoder(encoder: str) -> None:
    if ffmpeg_encoder_available(encoder):
        return
    raise RuntimeError(f"ffmpeg encoder {encoder!r} is not available.")


def compact_error_message(text: str, limit: int = 320) -> str:
    if (
        "No capable devices found" in text
        or "No capable NVENC device" in text
        or "unsupported device" in text
    ):
        return "No capable NVENC device found; this GPU likely does not expose hardware video encoding"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    important = [
        line
        for line in lines
        if "No capable devices found" in line
        or "unsupported device" in line
        or "OpenEncodeSessionEx failed" in line
        or "Error while opening encoder" in line
    ]
    selected = important or lines
    message = "; ".join(dict.fromkeys(selected))
    if not message:
        message = text.strip()
    if len(message) > limit:
        return message[: limit - 3] + "..."
    return message


def encode_png_sequence_nvenc(
    imgs_dir: Path,
    video_path: Path,
    fps: int,
    *,
    encoder: str,
    preset: str,
    cq: int,
    gpu_device: str,
) -> None:
    frame_template = imgs_dir / "frame_%06d.png"
    if not any(imgs_dir.glob("frame_*.png")):
        raise FileNotFoundError(f"No images found in {imgs_dir}.")
    video_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_path(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-framerate",
        str(fps),
        "-i",
        str(frame_template),
        "-c:v",
        encoder,
        "-gpu",
        gpu_device_index(gpu_device),
        "-preset",
        preset,
        "-rc",
        "vbr",
        "-cq",
        str(cq),
        "-b:v",
        "0",
        "-pix_fmt",
        "yuv420p",
        str(video_path),
    ]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        details = compact_error_message(result.stderr or result.stdout)
        raise RuntimeError(f"ffmpeg NVENC failed for {video_path}: {details}")
    if not video_path.exists():
        raise OSError(f"Video encoding did not create {video_path}")


def encode_png_sequence_cpu_h264(
    imgs_dir: Path,
    video_path: Path,
    fps: int,
    *,
    encoder: str,
    preset: str,
    crf: int,
) -> None:
    frame_template = imgs_dir / "frame_%06d.png"
    if not any(imgs_dir.glob("frame_*.png")):
        raise FileNotFoundError(f"No images found in {imgs_dir}.")
    video_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_path(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-framerate",
        str(fps),
        "-i",
        str(frame_template),
        "-an",
        "-c:v",
        encoder,
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-tag:v",
        "avc1",
        str(video_path),
    ]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        details = compact_error_message(result.stderr or result.stdout)
        raise RuntimeError(f"ffmpeg CPU H264 failed for {video_path}: {details}")
    if not video_path.exists():
        raise OSError(f"Video encoding did not create {video_path}")


def remove_encoded_png_sequence(dataset_root: Path, imgs_dir: Path) -> None:
    """Remove an encoded episode's PNGs and any now-empty image parents."""

    images_root = (dataset_root / "images").resolve()
    resolved_imgs_dir = imgs_dir.resolve()
    try:
        resolved_imgs_dir.relative_to(images_root)
    except ValueError as exc:
        raise RuntimeError(
            f"Refusing to remove encoded images outside {images_root}: {resolved_imgs_dir}"
        ) from exc
    if resolved_imgs_dir == images_root:
        raise RuntimeError(f"Refusing to remove the complete images root as one sequence: {images_root}")
    if resolved_imgs_dir.is_dir():
        shutil.rmtree(resolved_imgs_dir)

    parent = resolved_imgs_dir.parent
    while parent == images_root or images_root in parent.parents:
        try:
            parent.rmdir()
        except OSError:
            break
        if parent == images_root:
            break
        parent = parent.parent


def remove_png_sequence_after_video_validation(
    dataset_root: Path,
    imgs_dir: Path,
    video_path: Path,
) -> None:
    """Delete temporary PNGs only after the final MP4 proves lossless frame coverage."""

    png_count = sum(1 for path in imgs_dir.glob("*.png") if path.is_file()) if imgs_dir.is_dir() else 0
    try:
        video_frames = decoded_video_frame_count(video_path)
    except Exception as exc:
        raise RuntimeError(
            f"Refusing to delete PNGs because the encoded video is invalid: {video_path}: {exc}"
        ) from exc
    if video_frames <= 0:
        raise RuntimeError(
            f"Refusing to delete PNGs because the encoded video has no decodable frames: {video_path}"
        )
    if png_count > 0 and video_frames != png_count:
        raise RuntimeError(
            f"Refusing to delete PNGs because frame counts differ: "
            f"png={png_count}, video={video_frames}, path={video_path}"
        )
    remove_encoded_png_sequence(dataset_root, imgs_dir)


def remove_episode_png_sequences_with_videos(dataset: Any, episode_index: int) -> None:
    """Clean every PNG sequence whose final video is complete and decodable."""

    for key in dataset.meta.video_keys:
        video_path = dataset.root / dataset.meta.get_video_file_path(episode_index, key)
        if not video_path.is_file() or video_path.stat().st_size <= 0:
            continue
        imgs_dir = dataset._get_image_file_path(
            episode_index=episode_index,
            image_key=key,
            frame_index=0,
        ).parent
        remove_png_sequence_after_video_validation(dataset.root, imgs_dir, video_path)


def nvenc_runtime_status(
    *,
    encoder: str,
    preset: str,
    cq: int,
    gpu_device: str,
) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="lerobot_nvenc_check_") as tmp_dir_text:
        tmp_dir = Path(tmp_dir_text)
        image_path = tmp_dir / "frame_000000.png"
        video_path = tmp_dir / "check.mp4"
        image = np.zeros((64, 64, 3), dtype=np.uint8)
        if not cv2.imwrite(str(image_path), image):
            return False, f"could not write temporary frame {image_path}"
        try:
            encode_png_sequence_nvenc(
                tmp_dir,
                video_path,
                1,
                encoder=encoder,
                preset=preset,
                cq=cq,
                gpu_device=gpu_device,
            )
        except Exception as err:
            return False, compact_error_message(str(err))
    return True, ""


def enable_nvenc_video_encoding(
    dataset: LeRobotDataset,
    *,
    encoder: str,
    preset: str,
    cq: int,
    gpu_device: str,
) -> None:
    fallback_encode_episode_videos = dataset.encode_episode_videos
    fallback_warning_printed = False

    def encode_episode_videos_nvenc(self: LeRobotDataset, episode_index: int) -> dict[str, str]:
        nonlocal fallback_warning_printed
        video_paths: dict[str, str] = {}
        for key in self.meta.video_keys:
            video_path = self.root / self.meta.get_video_file_path(episode_index, key)
            video_paths[key] = str(video_path)
            img_dir = self._get_image_file_path(
                episode_index=episode_index,
                image_key=key,
                frame_index=0,
            ).parent
            if video_path.is_file() and video_path.stat().st_size > 0:
                try:
                    remove_png_sequence_after_video_validation(self.root, img_dir, video_path)
                except RuntimeError:
                    video_path.unlink()
                else:
                    continue
            if video_path.is_file():
                video_path.unlink()
            try:
                encode_png_sequence_nvenc(
                    img_dir,
                    video_path,
                    self.fps,
                    encoder=encoder,
                    preset=preset,
                    cq=cq,
                    gpu_device=gpu_device,
                )
            except Exception as err:
                for cleanup_key in self.meta.video_keys:
                    cleanup_path = self.root / self.meta.get_video_file_path(
                        episode_index, cleanup_key
                    )
                    if cleanup_path.is_file() and cleanup_path.stat().st_size == 0:
                        cleanup_path.unlink()
                if not fallback_warning_printed:
                    print(
                        f"[WARN] NVENC encoding failed on GPU {gpu_device_index(gpu_device)} "
                        f"with {encoder}: {compact_error_message(str(err))}. "
                        "Falling back to LeRobot default video encoder.",
                        flush=True,
                    )
                    fallback_warning_printed = True
                result = fallback_encode_episode_videos(episode_index)
                remove_episode_png_sequences_with_videos(self, episode_index)
                return result
            remove_png_sequence_after_video_validation(self.root, img_dir, video_path)
        return video_paths

    dataset.encode_episode_videos = MethodType(encode_episode_videos_nvenc, dataset)


def enable_cpu_h264_video_encoding(
    dataset: LeRobotDataset,
    *,
    encoder: str,
    preset: str,
    crf: int,
) -> None:
    fallback_encode_episode_videos = dataset.encode_episode_videos
    fallback_warning_printed = False

    def encode_episode_videos_cpu_h264(self: LeRobotDataset, episode_index: int) -> dict[str, str]:
        nonlocal fallback_warning_printed
        video_paths: dict[str, str] = {}
        for key in self.meta.video_keys:
            video_path = self.root / self.meta.get_video_file_path(episode_index, key)
            video_paths[key] = str(video_path)
            img_dir = self._get_image_file_path(
                episode_index=episode_index,
                image_key=key,
                frame_index=0,
            ).parent
            if video_path.is_file() and video_path.stat().st_size > 0:
                try:
                    remove_png_sequence_after_video_validation(self.root, img_dir, video_path)
                except RuntimeError:
                    video_path.unlink()
                else:
                    continue
            if video_path.is_file():
                video_path.unlink()
            try:
                encode_png_sequence_cpu_h264(
                    img_dir,
                    video_path,
                    self.fps,
                    encoder=encoder,
                    preset=preset,
                    crf=crf,
                )
            except Exception as err:
                for cleanup_key in self.meta.video_keys:
                    cleanup_path = self.root / self.meta.get_video_file_path(
                        episode_index, cleanup_key
                    )
                    if cleanup_path.is_file() and cleanup_path.stat().st_size == 0:
                        cleanup_path.unlink()
                if not fallback_warning_printed:
                    print(
                        f"[WARN] CPU H264 encoding failed with {encoder}: "
                        f"{compact_error_message(str(err))}. "
                        "Falling back to LeRobot default video encoder.",
                        flush=True,
                    )
                    fallback_warning_printed = True
                result = fallback_encode_episode_videos(episode_index)
                remove_episode_png_sequences_with_videos(self, episode_index)
                return result
            remove_png_sequence_after_video_validation(self.root, img_dir, video_path)
        return video_paths

    dataset.encode_episode_videos = MethodType(encode_episode_videos_cpu_h264, dataset)


def resolve_acceleration(args: argparse.Namespace, use_videos: bool) -> tuple[str, bool]:
    preprocess_device = args.preprocess_device
    gpu_encode_videos = bool(args.gpu_encode_videos)

    if args.gpu_accel:
        preprocess_device = "cuda"
        gpu_encode_videos = use_videos

    if preprocess_device == "auto":
        preprocess_device = "cuda" if torch_cuda_available(args.gpu_device) else "cpu"

    if int(args.gpu_resize_batch_size) <= 0:
        raise ValueError("--gpu-resize-batch-size must be positive")

    if preprocess_device == "cuda":
        require_torch_cuda(args.gpu_device)

    if gpu_encode_videos and use_videos:
        require_ffmpeg_encoder(args.gpu_video_encoder)
        nvenc_ok, nvenc_error = nvenc_runtime_status(
            encoder=args.gpu_video_encoder,
            preset=args.gpu_encode_preset,
            cq=args.gpu_encode_cq,
            gpu_device=args.gpu_device,
        )
        if not nvenc_ok:
            print(
                f"[WARN] {args.gpu_video_encoder} is present in ffmpeg but cannot run on GPU "
                f"{gpu_device_index(args.gpu_device)}: {nvenc_error}. "
                "LeRobot default video encoding will be used.",
                flush=True,
            )
            gpu_encode_videos = False

    return preprocess_device, gpu_encode_videos and use_videos


def prepare_dataset(
    *,
    repo_id: str,
    output_path: Path,
    profile: RobotProfile,
    features: dict[str, dict[str, Any]],
    fps: int,
    robot_type: str,
    image_writer_processes: int,
    image_writer_threads: int,
    use_videos: bool,
    video_backend: str | None,
    resume: bool,
    overwrite: bool,
) -> LeRobotDataset:
    if output_path.exists() and not resume:
        if not overwrite:
            raise FileExistsError(f"{output_path} exists. Use --overwrite or --resume.")
        shutil.rmtree(output_path)

    if resume and output_path.exists():
        if not is_lerobot_dataset_complete(output_path):
            print(f"[WARN] Existing LeRobot output is incomplete; rebuilding: {output_path}")
            shutil.rmtree(output_path)
        elif not lerobot_feature_schema_matches(output_path, features):
            print(f"[WARN] Existing LeRobot feature schema changed; rebuilding: {output_path}")
            shutil.rmtree(output_path)
        else:
            dataset = LeRobotDataset(repo_id=repo_id, root=output_path, download_videos=False)
            if image_writer_processes > 0 or image_writer_threads > 0:
                dataset.start_image_writer(image_writer_processes, image_writer_threads)
            return dataset

    return LeRobotDataset.create(
        repo_id=repo_id,
        root=output_path,
        robot_type=robot_type or str(profile.raw.get("lerobot_robot_type") or profile.profile_id),
        fps=fps,
        features=features,
        use_videos=use_videos,
        image_writer_processes=image_writer_processes,
        image_writer_threads=image_writer_threads,
        video_backend=video_backend,
    )


def write_episode(
    dataset: LeRobotDataset,
    *,
    episode_index: int,
    episode_path: Path,
    episode_data: dict[str, Any],
    profile: RobotProfile,
    key_style: str,
    ordered: bool,
    preserve_episode_index: bool,
) -> tuple[int, int]:
    if ordered and preserve_episode_index and hasattr(dataset, "create_episode_buffer"):
        dataset.episode_buffer = dataset.create_episode_buffer(episode_index=episode_index)
        lerobot_episode_index = episode_index
    else:
        lerobot_episode_index = int(dataset.meta.total_episodes)

    state_key, action_key, image_keys = feature_keys(profile, key_style)
    cam_frames = episode_data["cam_frames"]
    states = episode_data["states"]
    actions = episode_data["actions"]
    task = episode_data["task"]
    num_frames = int(episode_data["num_frames"])
    add_frame_parameters = inspect.signature(dataset.add_frame).parameters
    add_frame_accepts_task = "task" in add_frame_parameters

    for frame_idx in range(num_frames):
        frame: dict[str, Any] = {
            state_key: states[frame_idx],
            action_key: actions[frame_idx],
        }
        for raw_key, lerobot_key in image_keys.items():
            frame[lerobot_key] = cam_frames[raw_key][frame_idx]
        if add_frame_accepts_task:
            dataset.add_frame(frame, task=task)
        else:
            frame["task"] = task
            dataset.add_frame(frame)
    dataset.save_episode()
    return lerobot_episode_index, num_frames


def rewrite_lerobot_episode_from_temp(
    *,
    output_path: Path,
    repo_id: str,
    profile: RobotProfile,
    features: dict[str, dict[str, Any]],
    fps: int,
    robot_type: str,
    image_writer_processes: int,
    image_writer_threads: int,
    use_videos: bool,
    video_backend: str | None,
    gpu_encode_videos: bool,
    gpu_video_encoder: str,
    gpu_encode_preset: str,
    gpu_encode_cq: int,
    gpu_device: str,
    cpu_video_encoder: str,
    cpu_video_preset: str,
    cpu_video_crf: int,
    target_episode_index: int,
    source_episode_index: int,
    source_h5_path: Path,
    episode_data: dict[str, Any],
    key_style: str,
) -> None:
    expected_frames = int(episode_data["num_frames"])
    with tempfile.TemporaryDirectory(prefix="lerobot_episode_rewrite_") as tmp_dir_text:
        temp_output_path = Path(tmp_dir_text) / "dataset"
        temp_dataset = LeRobotDataset.create(
            repo_id=repo_id,
            root=temp_output_path,
            robot_type=robot_type,
            fps=fps,
            features=features,
            use_videos=use_videos,
            image_writer_processes=image_writer_processes,
            image_writer_threads=image_writer_threads,
            video_backend=video_backend,
        )
        if gpu_encode_videos:
            enable_nvenc_video_encoding(
                temp_dataset,
                encoder=gpu_video_encoder,
                preset=gpu_encode_preset,
                cq=gpu_encode_cq,
                gpu_device=gpu_device,
            )
        elif use_videos:
            enable_cpu_h264_video_encoding(
                temp_dataset,
                encoder=cpu_video_encoder,
                preset=cpu_video_preset,
                crf=cpu_video_crf,
            )
        try:
            write_episode(
                temp_dataset,
                episode_index=0,
                episode_path=source_h5_path,
                episode_data=episode_data,
                profile=profile,
                key_style=key_style,
                ordered=True,
                preserve_episode_index=True,
            )
        finally:
            stop_writer = getattr(temp_dataset, "stop_image_writer", None)
            if callable(stop_writer):
                stop_writer()

        temp_issues = validate_lerobot_episode(temp_output_path, 0, expected_frames)
        if temp_issues:
            joined = "; ".join(temp_issues)
            raise RuntimeError(
                f"temporary rewrite failed validation for source episode {source_episode_index}: {joined}"
            )

        copy_temp_episode_to_target(
            temp_output_path=temp_output_path,
            target_output_path=output_path,
            target_episode_index=target_episode_index,
            task=str(episode_data.get("task") or ""),
        )

    final_issues = validate_lerobot_episode(output_path, target_episode_index, expected_frames)
    if final_issues:
        joined = "; ".join(final_issues)
        raise RuntimeError(
            f"rewrite validation failed for {lerobot_episode_name(target_episode_index)} "
            f"from {source_h5_path}: {joined}"
        )


def run_conversion(args: argparse.Namespace) -> int:
    profile_path = Path(args.profile).expanduser().resolve()
    profile = load_profile(profile_path)
    if is_aloha_profile(profile) and (str(args.task).strip() or str(args.default_task).strip()):
        raise ValueError(
            "ALOHA LeRobot tasks must come from HDF5; write the task into "
            "aligned_joints.h5 before conversion instead of using --task or --default-task."
        )
    image_size = (int(args.image_size[0]), int(args.image_size[1]))
    if image_size[0] <= 0 or image_size[1] <= 0:
        raise ValueError("--image-size values must be positive")

    repo_id = resolve_repo_id(profile, args.repo_id)
    output_path = resolve_output_path(repo_id, args.output_root)
    fps = int(args.fps or profile.raw.get("fps", {}).get("record") or 30)
    robot_type = args.robot_type or str(profile.raw.get("lerobot_robot_type") or profile.profile_id)
    queue_size = int(args.queue_size or max(1, args.num_workers) * 5)
    ordered = not args.unordered
    default_task = args.default_task or profile.default_task
    validation_retries = max(0, int(args.validation_retries))
    fail_closed = lerobot_fail_closed(profile)

    data_root = Path(args.data_dir).expanduser().resolve()
    all_h5_paths = discover_h5_paths(data_root)
    selected_filter = parse_episode_filter(args.episode_filter)
    indexed_paths = indexed_h5_paths(all_h5_paths, data_root, selected_filter)
    selected_input_count = len(indexed_paths)
    mapping_path = output_path / "meta" / EPISODE_MAPPING_NAME
    skip_existing = not args.no_skip_existing and not args.overwrite
    resume_mode = args.resume or (skip_existing and output_path.exists())
    use_videos = not args.no_videos
    preprocess_device, gpu_encode_videos = resolve_acceleration(args, use_videos)
    features = build_features(
        profile,
        args.key_style,
        image_size,
        use_videos=use_videos,
        aloha_action_mode=args.aloha_action_mode,
    )
    rebuild_existing_output = False
    if resume_mode and output_path.exists():
        rebuild_existing_output = (
            not is_lerobot_dataset_complete(output_path)
            or not lerobot_feature_schema_matches(output_path, features)
        )
    skipped_existing = 0
    invalid_existing: dict[str, int] = {}
    if ((skip_existing and output_path.exists()) or args.resume) and not rebuild_existing_output:
        all_completed_records = completed_source_h5_records(mapping_path, data_root)
        selected_sources = {str(path.resolve()) for _, path in indexed_paths}
        completed_records = {
            source_path: record
            for source_path, record in all_completed_records.items()
            if completed_record_matches_current_layout(record, profile, args.aloha_action_mode)
        }
        if is_aloha_profile(profile) or args.task:
            for source_path, record in sorted(completed_records.items()):
                if source_path not in selected_sources:
                    continue
                expected_task = (
                    require_hdf5_task(Path(source_path))
                    if is_aloha_profile(profile)
                    else str(args.task).strip()
                )
                if completed_record_task_matches(record, expected_task):
                    continue
                try:
                    lerobot_episode_index = int(record.get("lerobot_episode_index"))
                except (TypeError, ValueError):
                    continue
                invalid_existing[source_path] = lerobot_episode_index
                print(
                    f"[WARN] Existing {lerobot_episode_name(lerobot_episode_index)} "
                    "has a different task and will be rewritten from HDF5.",
                    flush=True,
                )
        records_to_validate = [
            (source_path, record)
            for source_path, record in sorted(completed_records.items())
            if source_path in selected_sources and source_path not in invalid_existing
        ]
        if output_path.exists() and records_to_validate:

            def validate_completed_record(
                source_path: str,
                record: dict[str, Any],
            ) -> tuple[str, int | None, list[str]]:
                try:
                    lerobot_episode_index = int(record.get("lerobot_episode_index"))
                except (TypeError, ValueError):
                    return source_path, None, []
                try:
                    expected_frames = int(record.get("num_frames"))
                except (TypeError, ValueError):
                    expected_frames = None
                issues = validate_lerobot_episode(output_path, lerobot_episode_index, expected_frames)
                return source_path, lerobot_episode_index, issues

            max_validation_workers = min(8, max(1, len(records_to_validate)))
            with ThreadPoolExecutor(max_workers=max_validation_workers) as validation_pool:
                futures = [
                    validation_pool.submit(validate_completed_record, source_path, record)
                    for source_path, record in records_to_validate
                ]
                for future in as_completed(futures):
                    source_path, lerobot_episode_index, issues = future.result()
                    if lerobot_episode_index is None:
                        continue
                    if not issues:
                        continue
                    invalid_existing[source_path] = lerobot_episode_index
                    print(
                        f"[WARN] Existing {lerobot_episode_name(lerobot_episode_index)} "
                        f"failed validation and will be rewritten from HDF5: {'; '.join(issues)}",
                        flush=True,
                    )

        completed_paths = set(completed_records) - set(invalid_existing)
        before_count = len(indexed_paths)
        indexed_paths = [
            (idx, path)
            for idx, path in indexed_paths
            if str(path.resolve()) not in completed_paths
        ]
        skipped_existing = before_count - len(indexed_paths)
    print(f"Input episodes : {len(indexed_paths)}")
    print(f"Output dataset : {output_path}")
    print(f"Repo id        : {repo_id}")
    print(f"Robot type     : {robot_type}")
    print(f"FPS            : {fps}")
    print(f"State dim      : {lerobot_state_dim(profile, args.aloha_action_mode)}")
    print(f"Action dim     : {lerobot_action_dim(profile, args.aloha_action_mode)}")
    if is_aloha_profile(profile):
        print(f"ALOHA layout   : {args.aloha_action_mode}")
    print(f"Feature keys   : {', '.join(features)}")
    print(f"Image size     : {image_size[0]}x{image_size[1]}")
    print(f"Image storage  : {'videos' if use_videos else 'parquet images'}")
    print(f"Resume mode    : {resume_mode}")
    print(f"Skip existing  : {skip_existing}")
    print(f"Skipped existing episodes: {skipped_existing}")
    print(f"Invalid existing episodes to rewrite: {len(invalid_existing)}")
    print(
        "Preprocess dev : "
        f"{cuda_device_name(args.gpu_device) if preprocess_device == 'cuda' else preprocess_device}"
    )
    if preprocess_device == "cuda":
        print(f"GPU resize batch: {args.gpu_resize_batch_size}")
        if args.num_workers > 2:
            print(
                "GPU resize note : multiple CUDA workers share the same GPU; "
                "for stability, consider --num-workers 1 or 2."
            )
    video_encoder_text = (
        f"{args.gpu_video_encoder} via ffmpeg/NVENC; fallback to CPU {args.cpu_video_encoder} on failure"
        if gpu_encode_videos
        else f"{args.cpu_video_encoder} via ffmpeg CPU, crf={args.cpu_video_crf}, preset={args.cpu_video_preset}"
    )
    print(f"Video encoder  : {video_encoder_text}")
    print(f"Output validate: on, rewrite retries={validation_retries}")
    if is_aloha_profile(profile):
        print("Task source    : HDF5 task metadata (required)")
    else:
        print(f"Default task   : {default_task or '<empty>'}")
    print(f"Ordered write  : {ordered}")
    print(f"Episode mapping: {mapping_path}")
    if args.dry_run:
        for episode_index, path in indexed_paths[:20]:
            source_name = episode_dir_from_h5(path).name
            print(f"  {source_name} -> {lerobot_episode_name(episode_index)}")
        if len(indexed_paths) > 20:
            print(f"  ... {len(indexed_paths) - 20} more episodes")
        return 0

    # Validate the mandatory encoder before prepare_dataset can remove an
    # existing destination during overwrite/rebuild.
    if use_videos:
        require_ffmpeg_encoder(args.cpu_video_encoder)

    if fail_closed and not indexed_paths:
        all_selected_already_complete = (
            selected_input_count > 0
            and skipped_existing == selected_input_count
            and not args.overwrite
        )
        if all_selected_already_complete:
            print("No pending episodes: every selected input is already complete.")
            return 0
        raise ValueError(
            "Strict LeRobot conversion has no input episodes selected; "
            "refusing to create or overwrite the destination."
        )

    strict_preflight_required_cameras(
        [path for _, path in indexed_paths],
        profile,
        task_override=args.task,
        default_task=default_task,
    )

    dataset = prepare_dataset(
        repo_id=repo_id,
        output_path=output_path,
        profile=profile,
        features=features,
        fps=fps,
        robot_type=robot_type,
        image_writer_processes=args.image_writer_processes,
        image_writer_threads=args.image_writer_threads,
        use_videos=use_videos,
        video_backend=args.video_backend,
        resume=resume_mode,
        overwrite=args.overwrite,
    )
    if use_videos:
        enable_cpu_h264_video_encoding(
            dataset,
            encoder=args.cpu_video_encoder,
            preset=args.cpu_video_preset,
            crf=args.cpu_video_crf,
        )
    if gpu_encode_videos:
        enable_nvenc_video_encoding(
            dataset,
            encoder=args.gpu_video_encoder,
            preset=args.gpu_encode_preset,
            cq=args.gpu_encode_cq,
            gpu_device=args.gpu_device,
        )
    episode_mapping = load_episode_mapping(
        mapping_path,
        repo_id=repo_id,
        data_dir=data_root,
        output_path=output_path,
        key_style=args.key_style,
    )
    write_episode_mapping(mapping_path, episode_mapping)

    result_queue: queue.Queue = queue.Queue(maxsize=queue_size)
    sentinel = object()
    producer_error: list[BaseException] = []
    preprocess_failures: list[tuple[Path, BaseException]] = []
    max_inflight = max(1, args.num_workers) + 2

    def producer() -> None:
        try:
            pending_iter = iter(indexed_paths)
            with ProcessPoolExecutor(max_workers=args.num_workers, mp_context=mp.get_context("spawn")) as pool:
                inflight: dict[Any, tuple[int, Path]] = {}
                for _ in range(max_inflight):
                    try:
                        episode_index, path = next(pending_iter)
                    except StopIteration:
                        break
                    inflight[pool.submit(
                        preprocess_episode,
                        str(path),
                        str(profile_path),
                        image_size,
                        args.task,
                        default_task,
                        preprocess_device,
                        args.gpu_device,
                        args.gpu_resize_batch_size,
                        args.aloha_action_mode,
                        args.quality_grade,
                    )] = (episode_index, path)

                while inflight:
                    done, _ = wait(inflight, return_when=FIRST_COMPLETED)
                    for future in done:
                        episode_index, path = inflight.pop(future)
                        episode_data = None
                        exc: BaseException | None = None
                        try:
                            episode_data = future.result()
                        except BaseException as err:
                            exc = err

                        try:
                            next_episode_index, next_path = next(pending_iter)
                            inflight[pool.submit(
                                preprocess_episode,
                                str(next_path),
                                str(profile_path),
                                image_size,
                                args.task,
                                default_task,
                                preprocess_device,
                                args.gpu_device,
                                args.gpu_resize_batch_size,
                                args.aloha_action_mode,
                                args.quality_grade,
                            )] = (next_episode_index, next_path)
                        except StopIteration:
                            pass

                        result_queue.put((episode_index, path, episode_data, exc))
        except BaseException as err:
            producer_error.append(err)
        finally:
            result_queue.put(sentinel)

    producer_thread = threading.Thread(target=producer, name="hdf5-lerobot-decoder", daemon=True)
    producer_thread.start()

    written = 0
    skipped = 0
    next_write_pos = 0
    pending_results: dict[int, tuple[Path, dict[str, Any] | None, BaseException | None]] = {}
    episode_meta_updates: dict[int, dict[str, Any]] = {}
    progress = tqdm(total=len(indexed_paths), desc="Preprocess+Write")

    def rewrite_output_episode(
        *,
        target_episode_index: int,
        source_episode_index: int,
        source_h5_path: Path,
        episode_data: dict[str, Any],
        attempts: int,
    ) -> None:
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                print(
                    f"[WARN] Rewrite {lerobot_episode_name(target_episode_index)} "
                    f"from {source_h5_path} (attempt {attempt}/{attempts})",
                    flush=True,
                )
                rewrite_lerobot_episode_from_temp(
                    output_path=output_path,
                    repo_id=repo_id,
                    profile=profile,
                    features=features,
                    fps=fps,
                    robot_type=robot_type,
                    image_writer_processes=args.image_writer_processes,
                    image_writer_threads=args.image_writer_threads,
                    use_videos=use_videos,
                    video_backend=args.video_backend,
                    gpu_encode_videos=gpu_encode_videos,
                    gpu_video_encoder=args.gpu_video_encoder,
                    gpu_encode_preset=args.gpu_encode_preset,
                    gpu_encode_cq=args.gpu_encode_cq,
                    gpu_device=args.gpu_device,
                    cpu_video_encoder=args.cpu_video_encoder,
                    cpu_video_preset=args.cpu_video_preset,
                    cpu_video_crf=args.cpu_video_crf,
                    target_episode_index=target_episode_index,
                    source_episode_index=source_episode_index,
                    source_h5_path=source_h5_path,
                    episode_data=episode_data,
                    key_style=args.key_style,
                )
                return
            except Exception as err:
                last_error = err
                print(
                    f"[WARN] Rewrite attempt {attempt}/{attempts} failed for "
                    f"{lerobot_episode_name(target_episode_index)}: {err}",
                    flush=True,
                )
        if last_error is not None:
            raise last_error

    def write_result(
        episode_index: int,
        path: Path,
        episode_data: dict[str, Any] | None,
        exc: BaseException | None,
    ) -> None:
        nonlocal written, skipped
        if exc is not None:
            skipped += 1
            preprocess_failures.append((path, exc))
            print(f"Error preprocessing {path}: {exc}")
            return
        if episode_data is None:
            skipped += 1
            preprocess_failures.append(
                (path, RuntimeError("missing frames, actions, or required videos"))
            )
            print(f"Skip {path}: missing frames, actions, or required videos")
            return
        source_path_key = str(path.resolve())
        num_frames = int(episode_data["num_frames"])
        if source_path_key in invalid_existing:
            lerobot_episode_index = invalid_existing[source_path_key]
            rewrite_output_episode(
                target_episode_index=lerobot_episode_index,
                source_episode_index=episode_index,
                source_h5_path=path,
                episode_data=episode_data,
                attempts=max(1, validation_retries),
            )
        else:
            lerobot_episode_index, num_frames = write_episode(
                dataset,
                episode_index=episode_index,
                episode_path=path,
                episode_data=episode_data,
                profile=profile,
                key_style=args.key_style,
                ordered=ordered,
                preserve_episode_index=args.preserve_episode_index,
            )
            validation_issues = validate_lerobot_episode(output_path, lerobot_episode_index, num_frames)
            if validation_issues:
                if validation_retries <= 0:
                    joined = "; ".join(validation_issues)
                    raise RuntimeError(
                        f"{lerobot_episode_name(lerobot_episode_index)} failed validation: {joined}"
                    )
                print(
                    f"[WARN] {lerobot_episode_name(lerobot_episode_index)} failed validation: "
                    f"{'; '.join(validation_issues)}",
                    flush=True,
                )
                rewrite_output_episode(
                    target_episode_index=lerobot_episode_index,
                    source_episode_index=episode_index,
                    source_h5_path=path,
                    episode_data=episode_data,
                    attempts=validation_retries,
                )
        mapping_record = episode_mapping_record(
            dataset=dataset,
            data_root=data_root,
            output_path=output_path,
            source_index=episode_index,
            lerobot_index=lerobot_episode_index,
            h5_path=path,
            num_frames=num_frames,
            state_dim=lerobot_state_dim(profile, args.aloha_action_mode),
            action_dim=lerobot_action_dim(profile, args.aloha_action_mode),
            aloha_action_mode=args.aloha_action_mode if is_aloha_profile(profile) else "",
            task=str(episode_data.get("task") or ""),
        )
        upsert_episode_mapping(episode_mapping, mapping_record)
        write_episode_mapping(mapping_path, episode_mapping)
        instruction_meta = episode_data.get("instruction_meta")
        if isinstance(instruction_meta, dict) and instruction_meta:
            episode_meta_updates[lerobot_episode_index] = dict(instruction_meta)
            patch_lerobot_episodes_meta(output_path, {lerobot_episode_index: episode_meta_updates[lerobot_episode_index]})
        written += 1

    def write_ready_ordered() -> None:
        nonlocal next_write_pos
        while next_write_pos < len(indexed_paths):
            episode_index, expected_path = indexed_paths[next_write_pos]
            if episode_index not in pending_results:
                return
            path, episode_data, exc = pending_results.pop(episode_index)
            if path != expected_path:
                raise RuntimeError(f"Unexpected path for episode {episode_index}: {path} != {expected_path}")
            write_result(episode_index, path, episode_data, exc)
            progress.update(1)
            next_write_pos += 1

    while True:
        item = result_queue.get()
        if item is sentinel:
            break
        episode_index, path, episode_data, exc = item
        if ordered:
            pending_results[episode_index] = (path, episode_data, exc)
            write_ready_ordered()
        else:
            write_result(episode_index, path, episode_data, exc)
            progress.update(1)

    if ordered:
        write_ready_ordered()
    progress.close()
    producer_thread.join()

    if producer_error:
        raise producer_error[0]
    if ordered and pending_results:
        pending = ", ".join(str(idx) for idx in sorted(pending_results)[:10])
        raise RuntimeError(f"Could not write all episodes in order; pending episode ids: {pending}")
    raise_for_strict_preprocess_failures(fail_closed, preprocess_failures)

    if hasattr(dataset, "consolidate"):
        dataset.consolidate()
    patch_lerobot_episodes_meta(output_path, episode_meta_updates)

    print(f"Dataset saved to {output_path}")
    print(f"Written episodes: {written}, skipped: {skipped}, total frames: {dataset.num_frames}")
    if args.push_to_hub:
        dataset.push_to_hub(tags=[profile.profile_id], private=False, push_videos=True)
    return 0


def main() -> int:
    return run_conversion(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
