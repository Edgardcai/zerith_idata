#!/usr/bin/env python3
"""Convert RobotWin-style simulation HDF5 demos to ALOHA episode folders.

The output follows the repository's aligned_joints.h5 episode layout:

  episode/
    states/aligned_joints.h5
    videos/head_color.mp4
    videos/hand_left_color.mp4
    videos/hand_right_color.mp4
    meta/episode_meta.json

For simulation data, the command/action values are treated as the aligned
state values as well. There is no sensor timestamp in the source data, so the
script writes synthetic monotonic frame timestamps only for replay/training
compatibility.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import cv2
import h5py
import numpy as np


VIDEO_MAP = {
    "head_view_camera": "head_color.mp4",
    "left_camera": "hand_left_color.mp4",
    "right_camera": "hand_right_color.mp4",
}
TIMESTAMP_CAMERA_KEYS = [
    "head_color",
    "hand_left_color",
    "hand_right_color",
    "head_stereo_left",
    "head_stereo_right",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert RobotWin simulation HDF5 demos into ALOHA HDF5 episode folders."
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        required=True,
        help="Directory containing one RobotWin .hdf5 file and replay videos.",
    )
    parser.add_argument("--output-root", type=Path, required=True, help="Output episode root.")
    parser.add_argument(
        "--video-dir",
        type=Path,
        help=(
            "Directory containing demo_N_head_view_camera.mp4, demo_N_left_camera.mp4, "
            "and demo_N_right_camera.mp4. Defaults to source-root/replay_videos, then "
            "source-root/<hdf5-stem>."
        ),
    )
    parser.add_argument(
        "--task",
        default=None,
        help="Task text written to metadata. If omitted, it is generated from --left-target/--right-target.",
    )
    parser.add_argument(
        "--left-target",
        default="",
        help="Object name grasped by the left hand, used to generate task and subtask metadata.",
    )
    parser.add_argument(
        "--right-target",
        default="",
        help="Optional object name grasped by the right hand, used to generate task and subtask metadata.",
    )
    parser.add_argument("--jobs", type=int, default=4, help="Number of demos to convert in parallel.")
    parser.add_argument(
        "--replace-output",
        action="store_true",
        help="Remove the output root before conversion. Use only when the output root is disposable.",
    )
    parser.add_argument(
        "--skip-videos",
        action="store_true",
        help="Only write HDF5/meta and do not generate output videos.",
    )
    parser.add_argument(
        "--ffmpeg",
        default="ffmpeg",
        help="ffmpeg executable used to encode expanded H.264 videos. Default: ffmpeg.",
    )
    return parser.parse_args()


def demo_sort_key(name: str) -> int:
    try:
        return int(name.rsplit("_", 1)[1])
    except Exception:
        return 10**9


def find_source_h5(source_root: Path) -> Path:
    files = sorted(source_root.glob("*.hdf5"))
    if len(files) != 1:
        raise ValueError(f"Expected exactly one .hdf5 under {source_root}, found {len(files)}")
    return files[0]


def resolve_video_dir(source_root: Path, source_h5: Path, requested: Path | None) -> Path:
    candidates = []
    if requested is not None:
        candidates.append(requested.expanduser())
    candidates.extend([source_root / "replay_videos", source_root / source_h5.stem])
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_dir():
            return resolved
    raise FileNotFoundError(
        "Video directory not found. Checked: "
        + ", ".join(str(path.expanduser().resolve()) for path in candidates)
    )


def demo_video_paths(video_dir: Path, demo: str) -> dict[str, Path]:
    return {suffix: video_dir / f"{demo}_{suffix}.mp4" for suffix in VIDEO_MAP}


def demo_has_all_videos(video_dir: Path, demo: str) -> bool:
    return all(path.is_file() for path in demo_video_paths(video_dir, demo).values())


def video_info(path: Path) -> tuple[int, float, int, int]:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {path}")
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    if frames <= 0 or width <= 0 or height <= 0:
        raise RuntimeError(f"Invalid video metadata for {path}: frames={frames}, size={width}x{height}")
    return frames, fps, width, height


def output_video_frame_count(path: Path) -> int:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return -1
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return frames


def expand_robotwin_video(
    ffmpeg: str,
    src: Path,
    dst: Path,
    action_frames: int,
    *,
    state_fps: float = 60.0,
    save_every_steps: int = 2,
    initial_video_frames: int = 1,
) -> None:
    src_frame_count, _src_fps, width, height = video_info(src)
    expected = expected_robotwin_video_frames(
        action_frames,
        save_every_steps=save_every_steps,
        initial_video_frames=initial_video_frames,
    )
    if src_frame_count != expected:
        raise ValueError(f"{src}: expected {expected} frames, got {src_frame_count}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp.mp4")
    tmp.unlink(missing_ok=True)
    dst.unlink(missing_ok=True)

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-s",
        f"{width}x{height}",
        "-r",
        f"{state_fps:.8f}",
        "-i",
        "-",
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
        str(tmp),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None

    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        proc.kill()
        raise RuntimeError(f"Cannot open video: {src}")

    try:
        sample_count = math.ceil(action_frames / save_every_steps)
        written = 0
        for video_idx in range(initial_video_frames + sample_count):
            ok, frame = cap.read()
            if not ok:
                raise RuntimeError(f"{src}: failed to read source video frame {video_idx}")
            if video_idx < initial_video_frames:
                continue
            sample_idx = video_idx - initial_video_frames
            start = sample_idx * save_every_steps
            repeat = max(0, min(save_every_steps, action_frames - start))
            for _ in range(repeat):
                proc.stdin.write(frame.tobytes())
                written += 1
        proc.stdin.close()
        stderr = proc.stderr.read().decode("utf-8", errors="replace")
        return_code = proc.wait()
    finally:
        cap.release()

    if return_code != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed for {dst}: {stderr.strip()}")
    if written != action_frames:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{dst}: wrote {written} frames, expected {action_frames}")
    actual = output_video_frame_count(tmp)
    if actual != action_frames:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{dst}: encoded {actual} frames, expected {action_frames}")
    tmp.replace(dst)


def vector(row: Any, dim: int, fill: float = 0.0) -> np.ndarray:
    arr = np.asarray(row, dtype=np.float32).reshape(-1)
    if arr.size >= dim:
        return arr[:dim]
    out = np.full((dim,), fill, dtype=np.float32)
    out[: arr.size] = arr
    return out


def dataset_or_none(group: h5py.Group, rel_path: str) -> h5py.Dataset | None:
    return group[rel_path] if rel_path in group else None


def find_step_index_dataset(group: h5py.Group) -> tuple[str, h5py.Dataset] | tuple[None, None]:
    candidates = [
        "market2_shelf_auto_metadata/frame_steps/step_index",
        "metadata/frame_steps/step_index",
        "frame_steps/step_index",
    ]
    for rel_path in candidates:
        dataset = dataset_or_none(group, rel_path)
        if dataset is not None:
            return rel_path, dataset

    found: list[tuple[str, h5py.Dataset]] = []

    def visitor(name: str, obj: Any) -> None:
        if isinstance(obj, h5py.Dataset) and (name.endswith("/frame_steps/step_index") or name.endswith("step_index")):
            found.append((name, obj))

    group.visititems(visitor)
    return found[0] if found else (None, None)


def vec_at(dataset: h5py.Dataset | np.ndarray | None, idx: int, dim: int, fill: float = 0.0) -> np.ndarray:
    if dataset is None:
        return np.full((dim,), fill, dtype=np.float32)
    return vector(dataset[idx], dim, fill)


def write_ds(group: h5py.Group, name: str, value: Any) -> None:
    group.create_dataset(name, data=value)


def source_length(group: h5py.Group) -> int:
    if "actions" in group:
        return int(group["actions"].shape[0])
    if "obs/actions" in group:
        return int(group["obs/actions"].shape[0])
    raise ValueError(f"{group.name}: missing actions and obs/actions")


def expected_robotwin_video_frames(
    action_frames: int,
    *,
    save_every_steps: int = 2,
    initial_video_frames: int = 1,
    hold_steps: int = 60,
) -> int:
    return initial_video_frames + math.ceil(action_frames / save_every_steps) + math.ceil(hold_steps / save_every_steps)


def clean_target(value: str, fallback: str = "the target item") -> str:
    text = str(value or "").strip()
    return text if text else fallback


def build_task_text(left_target: str, right_target: str) -> str:
    left = clean_target(left_target)
    right = str(right_target or "").strip()
    if not right:
        return f"Target: {left}. Pick the {left} from the shelf and place it into the cart."
    return (
        "First, move the chassis to the shelf. Next, grasp "
        f"{left} with the left hand, followed by grasping {right} with the right hand. "
        "Then, move the chassis to the shopping cart. Afterwards, place "
        f"{left} into the cart with the left hand, then place {right} into the cart with the right hand. "
        "Finally, move the chassis back to the start position."
    )


def step_subtask_map(left_target: str, right_target: str) -> dict[int, str]:
    left = clean_target(left_target)
    right = str(right_target or "").strip()
    if not right:
        return {
            1: "Move chassis to shelf.",
            2: f"Grasp {left} with the left hand.",
            5: "Move chassis to cart.",
            6: f"Place {left} into the cart with the left hand.",
        }
    grasp = f"Grasp {left} with the left hand, then grasp {right} with the right hand."
    place = f"Place {left} into the cart with the left hand, then place {right} into the cart with the right hand."
    return {
        1: "Move chassis to shelf.",
        2: grasp,
        3: grasp,
        4: grasp,
        5: "Move chassis to cart.",
        6: place,
        7: place,
    }


def read_step_indices(source_h5: Path, demo: str, frame_count: int) -> tuple[str | None, np.ndarray | None]:
    with h5py.File(source_h5, "r") as src:
        g = src[f"data/{demo}"]
        rel_path, dataset = find_step_index_dataset(g)
        if dataset is None:
            return None, None
        values = np.asarray(dataset[:frame_count], dtype=np.int64).reshape(-1)
        if values.size < frame_count:
            padded = np.full((frame_count,), -1, dtype=np.int64)
            padded[: values.size] = values
            values = padded
        return rel_path, values[:frame_count]


def build_subtask_metadata(
    step_indices: np.ndarray | None,
    frame_count: int,
    step_map: dict[int, str],
    demo: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if step_indices is None or frame_count <= 0:
        return [], []

    segment_instructions: list[dict[str, Any]] = []
    subtask_segments: list[dict[str, Any]] = []
    values = step_indices[:frame_count]
    start = 0
    for idx in range(1, frame_count + 1):
        if idx < frame_count and int(values[idx]) == int(values[start]):
            continue
        step_index = int(values[start])
        description = step_map.get(step_index)
        if description:
            segment_id = f"{demo}-step-{step_index}-{start}-{idx - 1}"
            segment_instructions.append(
                {
                    "start_time": start,
                    "end_time": idx - 1,
                    "start": start,
                    "end": idx - 1,
                    "step_index": step_index,
                    "description_en": [description],
                    "description_zh": [description],
                    "id": segment_id,
                }
            )
            subtask_segments.append(
                {
                    "start": start,
                    "end": idx - 1,
                    "subtask": description,
                    "step_index": step_index,
                    "description_en": [description],
                    "description_zh": [description],
                    "id": segment_id,
                }
            )
        start = idx
    return segment_instructions, subtask_segments


def resolve_video_policy(frame_count: int, video_meta: dict[str, tuple[int, float, int, int]]) -> tuple[str, float, float]:
    ref_frames, ref_fps, _width, _height = video_meta["head_view_camera"]
    video_fps = ref_fps if math.isfinite(ref_fps) and ref_fps > 0 else 30.0
    frame_counts = {suffix: frames for suffix, (frames, _fps, _w, _h) in video_meta.items()}

    if all(frames == frame_count for frames in frame_counts.values()):
        return "one_to_one_source_video_frames", video_fps, video_fps

    expected = expected_robotwin_video_frames(frame_count)
    if all(frames == expected for frames in frame_counts.values()):
        return "expanded_from_robotwin_every_2_steps_to_one_video_frame_per_hdf5_frame", 60.0, video_fps

    raise ValueError(
        "video frame count does not match a supported RobotWin policy. "
        f"hdf5_frames={frame_count}, one_to_one_expected={frame_count}, "
        f"step2_expected={expected}, got={frame_counts}"
    )


def materialize_video(
    ffmpeg: str,
    src: Path,
    dst: Path,
    frame_count: int,
    state_fps: float,
    video_policy: str,
) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if video_policy == "one_to_one_source_video_frames":
        tmp = dst.with_suffix(".h264.tmp.mp4")
        tmp.unlink(missing_ok=True)
        dst.unlink(missing_ok=True)
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-an",
            "-r",
            f"{state_fps:.8f}",
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
            str(tmp),
        ]
        result = subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg failed for {dst}: {result.stderr.strip()}")
        actual = output_video_frame_count(tmp)
        if actual != frame_count:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"{dst}: encoded {actual} frames, expected {frame_count}")
        tmp.replace(dst)
        return
    if video_policy == "expanded_from_robotwin_every_2_steps_to_one_video_frame_per_hdf5_frame":
        expand_robotwin_video(ffmpeg, src, dst, frame_count, state_fps=state_fps)
        return
    raise ValueError(f"Unsupported video policy: {video_policy}")


def yaw_from_root_pose(root_pose: np.ndarray) -> np.float32:
    quat = np.asarray(root_pose[3:7], dtype=np.float64).reshape(-1)
    if quat.size < 4:
        return np.float32(0.0)
    # RobotWin metadata names quaternions as wxyz. Keep a fallback for xyzw-like rows.
    if abs(quat[0]) >= abs(quat[3]):
        w, x, y, z = quat[:4]
    else:
        x, y, z, w = quat[:4]
    norm = math.sqrt(w * w + x * x + y * y + z * z)
    if norm <= 1e-12:
        return np.float32(0.0)
    w, x, y, z = w / norm, x / norm, y / norm, z / norm
    yaw = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    return np.float32(yaw)


def base_state_from_root(root_pose: np.ndarray, root_velocity: np.ndarray) -> np.ndarray:
    rp = vector(root_pose, 7)
    rv = vector(root_velocity, 6)
    return np.array([rp[0], rp[1], yaw_from_root_pose(rp), rv[0], rv[1], rv[5]], dtype=np.float32)


def write_aligned_h5(
    source_h5: Path,
    demo: str,
    output_h5: Path,
    frame_count: int,
    state_fps: float,
) -> None:
    output_h5.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = output_h5.with_suffix(".h5.tmp")
    tmp_out.unlink(missing_ok=True)

    zero14 = np.zeros((14,), dtype=np.float32)
    zero3 = np.zeros((3,), dtype=np.float32)
    zero4 = np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float32)
    zero6 = np.zeros((6,), dtype=np.float32)
    zero25 = np.zeros((25,), dtype=np.float32)
    with h5py.File(source_h5, "r") as src:
        g = src[f"data/{demo}"]
        src_len = source_length(g)
        src_actions = dataset_or_none(g, "actions")
        obs_actions = dataset_or_none(g, "obs/actions")
        joint_pos25 = dataset_or_none(g, "states/articulation/robot/joint_position")
        joint_vel25 = dataset_or_none(g, "states/articulation/robot/joint_velocity")
        root_pose = dataset_or_none(g, "states/articulation/robot/root_pose")
        root_velocity = dataset_or_none(g, "states/articulation/robot/root_velocity")
        left_eef = dataset_or_none(g, "obs/left_eef_pose")
        right_eef = dataset_or_none(g, "obs/right_eef_pose")
        left_gripper = dataset_or_none(g, "obs/left_gripper_pos")
        right_gripper = dataset_or_none(g, "obs/right_gripper_pos")
        step_index_path, step_index_dataset = find_step_index_dataset(g)

        with h5py.File(tmp_out, "w", libver="latest") as dst:
            dst.attrs["format"] = "icra_wbc_aligned_joints"
            dst.attrs["source"] = "sim_data_0611_robotwin"
            dst.attrs["robot_type"] = "aloha"
            dst.attrs["aloha_action_mode"] = "joints_base"
            dst.attrs["fps"] = float(state_fps)
            dst.attrs["source_hdf5"] = str(source_h5)
            dst.attrs["source_demo"] = demo
            dst.attrs["source_frames"] = int(src_len)
            dst.attrs["frame_index_policy"] = "full_source_no_truncation"
            dst.attrs["state_action_policy"] = "sim_state_action_same"
            dst.attrs["timestamp_policy"] = "synthetic_frame_index_from_video_fps"
            if step_index_path:
                dst.attrs["source_step_index_dataset"] = step_index_path

            for target_idx in range(frame_count):
                source_idx = target_idx
                frame = dst.create_group(str(target_idx))
                timestamp_sec = np.float64(target_idx / state_fps)
                write_ds(frame, "main_timestamp", timestamp_sec)
                frame_meta = frame.create_group("meta")
                write_ds(frame_meta, "source_frame_index", np.int64(source_idx))
                if step_index_dataset is not None:
                    write_ds(frame_meta, "step_index", np.int64(step_index_dataset[source_idx]))
                cam_ts = frame.create_group("timestamp").create_group("camera")
                for key in TIMESTAMP_CAMERA_KEYS:
                    write_ds(cam_ts, key, timestamp_sec)

                state = frame.create_group("state")
                action = frame.create_group("action")

                action_joint = (
                    vector(src_actions[source_idx, :14], 14)
                    if src_actions is not None
                    else vec_at(obs_actions, source_idx, 14) if obs_actions is not None else zero14
                )
                state_joint = action_joint.copy()
                chassis_velocity = (
                    vector(src_actions[source_idx, 14:17], 3)
                    if src_actions is not None and src_actions.shape[1] >= 17
                    else zero3
                )

                sj = state.create_group("joint")
                write_ds(sj, "position", state_joint)
                write_ds(sj, "velocity", vec_at(joint_vel25, source_idx, 14) if joint_vel25 is not None else zero14)
                write_ds(sj, "effort", zero14)
                write_ds(action.create_group("joint"), "position", action_joint)

                left_grip_val = (
                    vec_at(left_gripper, source_idx, 1)
                    if left_gripper is not None
                    else np.array([state_joint[6]], dtype=np.float32)
                )
                right_grip_val = (
                    vec_at(right_gripper, source_idx, 1)
                    if right_gripper is not None
                    else np.array([state_joint[13]], dtype=np.float32)
                )
                write_ds(state.create_group("left_effector"), "position", left_grip_val)
                write_ds(state.create_group("right_effector"), "position", right_grip_val)
                write_ds(action.create_group("left_effector"), "position", np.array([action_joint[6]], dtype=np.float32))
                write_ds(action.create_group("right_effector"), "position", np.array([action_joint[13]], dtype=np.float32))

                left_pose = vec_at(left_eef, source_idx, 7) if left_eef is not None else np.concatenate([zero3, zero4])
                right_pose = vec_at(right_eef, source_idx, 7) if right_eef is not None else np.concatenate([zero3, zero4])
                end = state.create_group("end")
                write_ds(end, "position", np.stack([left_pose[:3], right_pose[:3]]).astype(np.float32))
                write_ds(end, "orientation", np.stack([left_pose[3:7], right_pose[3:7]]).astype(np.float32))

                rp = vec_at(root_pose, source_idx, 7) if root_pose is not None else np.concatenate([zero3, zero4])
                rv = vec_at(root_velocity, source_idx, 6) if root_velocity is not None else zero6
                base_state = base_state_from_root(rp, rv)
                base_state[3:6] = chassis_velocity
                robot = state.create_group("robot")
                write_ds(robot, "position", vector(rp[:3], 3))
                write_ds(robot, "orientation", vector(rp[3:7], 4))
                write_ds(robot, "pose2d", base_state[:3])
                write_ds(robot, "velocity", base_state[3:6])
                write_ds(robot, "base_state", base_state)
                write_ds(robot, "angular_velocity", vector(rv[3:6], 3))
                write_ds(robot, "root_pose", vector(rp, 7))
                write_ds(robot, "root_velocity", vector(rv, 6))

                state_waist = state.create_group("waist")
                write_ds(state_waist, "position", np.zeros((1,), dtype=np.float32))
                write_ds(state_waist, "velocity", np.zeros((1,), dtype=np.float32))
                write_ds(state_waist, "effort", np.zeros((1,), dtype=np.float32))
                write_ds(action.create_group("waist"), "position", np.zeros((1,), dtype=np.float32))
                write_ds(action.create_group("robot"), "velocity", chassis_velocity)

                raw_state = state.create_group("raw")
                write_ds(raw_state, "joint_position_25", vec_at(joint_pos25, source_idx, 25) if joint_pos25 is not None else zero25)
                write_ds(raw_state, "joint_velocity_25", vec_at(joint_vel25, source_idx, 25) if joint_vel25 is not None else zero25)
                write_ds(raw_state, "root_pose", vector(rp, 7))
                write_ds(raw_state, "root_velocity", vector(rv, 6))
                raw_action = action.create_group("raw")
                write_ds(
                    raw_action,
                    "action_17",
                    vector(src_actions[source_idx], 17) if src_actions is not None else np.zeros((17,), dtype=np.float32),
                )

    tmp_out.replace(output_h5)


def convert_one(
    source_h5: str,
    video_dir: str,
    output_root: str,
    demo: str,
    task: str,
    left_target: str,
    right_target: str,
    skip_videos: bool,
    ffmpeg: str,
) -> dict[str, Any]:
    source_h5_path = Path(source_h5)
    video_dir_path = Path(video_dir)
    output_root_path = Path(output_root)
    episode_dir = output_root_path / demo
    states_dir = episode_dir / "states"
    videos_dir = episode_dir / "videos"
    meta_dir = episode_dir / "meta"
    states_dir.mkdir(parents=True, exist_ok=True)
    videos_dir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)

    with h5py.File(source_h5_path, "r") as src:
        g = src[f"data/{demo}"]
        frame_count = source_length(g)

    video_paths = demo_video_paths(video_dir_path, demo)
    missing = [str(path) for path in video_paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"{demo}: missing videos: {missing}")

    video_meta = {suffix: video_info(path) for suffix, path in video_paths.items()}
    video_policy, state_fps, video_fps = resolve_video_policy(frame_count, video_meta)

    output_h5 = states_dir / "aligned_joints.h5"
    write_aligned_h5(source_h5_path, demo, output_h5, frame_count, state_fps)
    step_index_path, step_indices = read_step_indices(source_h5_path, demo, frame_count)
    step_map = step_subtask_map(left_target, right_target)
    segment_instructions, subtask_segments = build_subtask_metadata(step_indices, frame_count, step_map, demo)

    output_videos: dict[str, str] = {}
    if not skip_videos:
        for suffix, dst_name in VIDEO_MAP.items():
            dst = videos_dir / dst_name
            materialize_video(ffmpeg, video_paths[suffix], dst, frame_count, state_fps, video_policy)
            output_videos[dst_name] = str(dst.relative_to(episode_dir))

    meta = {
        "episode_id": demo_sort_key(demo),
        "episode_name": demo,
        "source_episode_name": demo,
        "source_hdf5": str(source_h5_path),
        "source_video_dir": str(video_dir_path),
        "robot_type": "aloha",
        "dataset_type": "aloha",
        "task": task,
        "tasks": [task],
        "full_instructions_en": [task],
        "full_instructions_zh": [task],
        "left_target": clean_target(left_target),
        "right_target": str(right_target or "").strip(),
        "source_step_index_dataset": step_index_path,
        "step_index_subtask_map": {str(key): value for key, value in step_map.items()},
        "segment_instructions": segment_instructions,
        "subtask_segments": subtask_segments,
        "frame_count": frame_count,
        "source_hdf5_frames": frame_count,
        "video_source_frames": {suffix: frames for suffix, (frames, _fps, _w, _h) in video_meta.items()},
        "video_output_frames": frame_count if not skip_videos else None,
        "fps": state_fps,
        "state_fps": state_fps,
        "video_fps": video_fps,
        "duration_sec": (frame_count - 1) / state_fps if frame_count > 0 else 0.0,
        "states_file": "states/aligned_joints.h5",
        "videos": {
            "head_color": "videos/head_color.mp4",
            "hand_left_color": "videos/hand_left_color.mp4",
            "hand_right_color": "videos/hand_right_color.mp4",
        }
        if not skip_videos
        else {},
        "action_layout": {
            "action/joint/position": 14,
            "action/waist/position": 1,
            "action/robot/velocity": 3,
            "combined_lerobot_action_joints_base": 18,
        },
        "state_layout": {
            "state/joint/position": 14,
            "state/robot/position": 3,
            "state/robot/orientation": 4,
            "state/robot/pose2d": 3,
            "state/robot/velocity": 3,
            "state/robot/base_state": 6,
            "state/robot/angular_velocity": 3,
            "state/waist/position": 1,
            "state/raw/joint_position_25": 25,
            "state/raw/root_pose": 7,
            "state/raw/root_velocity": 6,
        },
        "base_state_preserved": True,
        "base_action_preserved": True,
        "frame_policy": "full_source_no_truncation",
        "state_action_policy": "sim_state_action_same",
        "timestamp_policy": "synthetic_frame_index_from_video_fps",
        "video_policy": video_policy,
        "video_frame_mapping": {
            "type": video_policy,
            "action_frames": frame_count,
            "state_fps": state_fps,
            "video_fps": video_fps,
            "save_every_steps": 2,
            "initial_video_frames": 1,
            "hold_steps": 60,
            "hold_video_frames": 30,
            "source_video_frames": {suffix: frames for suffix, (frames, _fps, _w, _h) in video_meta.items()},
            "output_video_frames": frame_count,
            "source_video_frame_for_hdf5_frame": (
                "hdf5_frame"
                if video_policy == "one_to_one_source_video_frames"
                else "initial_video_frames + floor(hdf5_frame / save_every_steps)"
            ),
        },
    }
    (meta_dir / "episode_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "episode": demo,
        "target": str(episode_dir),
        "frames": frame_count,
        "state_fps": state_fps,
        "video_fps": video_fps,
        "video_frames": frame_count,
        "source_video_frames": meta["video_source_frames"],
        "video_policy": video_policy,
    }


def main() -> int:
    args = parse_args()
    if args.jobs < 1:
        raise ValueError("--jobs must be >= 1")
    source_root = args.source_root.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    left_target = str(args.left_target or "").strip()
    right_target = str(args.right_target or "").strip()
    task = str(args.task).strip() if args.task is not None and str(args.task).strip() else build_task_text(left_target, right_target)
    source_h5 = find_source_h5(source_root)
    video_dir = resolve_video_dir(source_root, source_h5, args.video_dir)
    if args.replace_output and output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    with h5py.File(source_h5, "r") as src:
        all_demos = sorted(src["data"].keys(), key=demo_sort_key)
    demos = [demo for demo in all_demos if demo_has_all_videos(video_dir, demo)]
    skipped_without_videos = [demo for demo in all_demos if demo not in set(demos)]
    if not demos:
        raise RuntimeError(f"No demos with complete replay videos found under {video_dir}")
    summary: dict[str, Any] = {
        "source_hdf5": str(source_h5),
        "source_video_dir": str(video_dir),
        "output_root": str(output_root),
        "task": task,
        "left_target": clean_target(left_target),
        "right_target": right_target,
        "source_demo_count": len(all_demos),
        "episode_count": len(demos),
        "skipped_without_videos_count": len(skipped_without_videos),
        "skipped_without_videos": skipped_without_videos,
        "jobs": args.jobs,
        "frame_policy": "full_source_no_truncation",
        "episodes": [],
    }
    if skipped_without_videos:
        print(
            f"skip {len(skipped_without_videos)} demos without complete videos; "
            f"convert {len(demos)} demos: {', '.join(demos)}",
            flush=True,
        )

    started = time.time()
    completed = 0
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        futures = [
            pool.submit(
                convert_one,
                str(source_h5),
                str(video_dir),
                str(output_root),
                demo,
                task,
                left_target,
                right_target,
                args.skip_videos,
                args.ffmpeg,
            )
            for demo in demos
        ]
        for future in as_completed(futures):
            result = future.result()
            completed += 1
            summary["episodes"].append(result)
            print(
                f"[{completed}/{len(demos)}] {result['episode']}: "
                f"{result['frames']} hdf5 frames, {result['video_frames']} video frames",
                flush=True,
            )

    summary["episodes"].sort(key=lambda row: demo_sort_key(row["episode"]))
    summary["elapsed_sec"] = round(time.time() - started, 3)
    (output_root / "sim_data_0611_conversion_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "ok": True,
                "output_root": str(output_root),
                "episodes": len(summary["episodes"]),
                "elapsed_sec": summary["elapsed_sec"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
