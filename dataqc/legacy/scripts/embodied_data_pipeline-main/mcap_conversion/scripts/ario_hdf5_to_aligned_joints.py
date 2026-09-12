#!/usr/bin/env python3
# -- coding: UTF-8
"""
Convert an ARIO/ALOHA-style HDF5 file to an ICRA-WBC-style aligned_joints.h5.

ALOHA mapping used here:
  - puppet joints -> state/joint/*
  - master joints -> action/joint/position
  - puppet end poses -> state/end/*
  - /LiftMotorStatePub back_height -> action/waist/position
  - /LiftMotorStatePub back_height -> state/waist/position
  - /action/chassis -> action/robot/velocity
  - /localization/pose or /localization/pos -> state/robot/base_state[x,y,yaw]
  - /odom -> state/robot/base_state[vx,vy,wz]

G2 mapping used here:
  - /hal/joint_state -> state arms/grippers/waist
  - /pico_g2/action_joints -> action arms/grippers/waist/base velocity
  - /odom -> state/robot/*

Fields that do not exist in the ARIO/ALOHA source are omitted, except for
compatibility fields requested downstream; missing optional lift/base fields are
zero-filled.
"""

import argparse
import math
from pathlib import Path

import h5py
import numpy as np


G2_STATE_LEFT_ARM = list(range(8, 15))
G2_STATE_RIGHT_ARM = list(range(15, 22))
G2_STATE_WAIST = list(range(0, 5))
G2_ACTION_LEFT_ARM = list(range(0, 7))
G2_ACTION_RIGHT_ARM = list(range(8, 15))
G2_ACTION_WAIST = list(range(16, 21))


def seconds_to_ns(value):
    text = str(value)
    if isinstance(value, bytes):
        text = value.decode("utf-8")
    if "." not in text:
        return int(text) * 1_000_000_000
    sec, frac = text.split(".", 1)
    frac = "".join(ch for ch in frac if ch.isdigit())
    frac = (frac + "000000000")[:9]
    return int(sec) * 1_000_000_000 + int(frac)


def timestamp_from_index_path(value, fallback_ns):
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    name = Path(str(value)).name
    stem = name.rsplit(".", 1)[0]
    try:
        return seconds_to_ns(stem)
    except ValueError:
        return fallback_ns


def quat_from_rpy(roll, pitch, yaw):
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


def pose_xyzrpy_to_position_orientation(pose):
    position = np.asarray(pose[:3], dtype=np.float64)
    orientation = quat_from_rpy(float(pose[3]), float(pose[4]), float(pose[5]))
    return position, orientation


def create_dataset(group, name, data, dtype=None):
    if dtype is not None:
        data = np.asarray(data, dtype=dtype)
    group.create_dataset(name, data=data)


def require_dataset(root, name):
    if name not in root:
        raise KeyError(f"Missing required dataset: {name}")
    return root[name]


def optional_dataset(root, name):
    if name not in root:
        return None
    dataset = root[name]
    if isinstance(dataset, h5py.Dataset) and dataset.shape and dataset.shape[0] == 0:
        return None
    return dataset


def optional_named_dataset(root, group_name, preferred_names=()):
    for name in preferred_names:
        dataset = optional_dataset(root, f"{group_name}/{name}")
        if dataset is not None:
            return dataset
    if group_name not in root or not isinstance(root[group_name], h5py.Group):
        return None
    for name in sorted(root[group_name].keys()):
        dataset = optional_dataset(root, f"{group_name}/{name}")
        if dataset is not None:
            return dataset
    return None


def optional_first_dataset(root, names):
    for name in names:
        dataset = optional_dataset(root, name)
        if dataset is not None:
            return dataset
    return None


def dataset_row(dataset, frame_idx):
    if dataset is None:
        return np.array([], dtype=np.float64)
    if dataset.shape and frame_idx >= dataset.shape[0]:
        return np.array([], dtype=np.float64)
    return np.asarray(dataset[frame_idx], dtype=np.float64).reshape(-1)


def fit_dim(values, dim):
    values = np.asarray(values, dtype=np.float64).reshape(-1)
    out = np.zeros(dim, dtype=np.float64)
    copy_len = min(dim, values.size)
    if copy_len:
        out[:copy_len] = values[:copy_len]
    return out


def take_indices(values, indices):
    values = np.asarray(values, dtype=np.float64).reshape(-1)
    out = np.zeros(len(indices), dtype=np.float64)
    for out_idx, source_idx in enumerate(indices):
        if source_idx < values.size:
            out[out_idx] = values[source_idx]
    return out


def take_scalar(values, index):
    values = np.asarray(values, dtype=np.float64).reshape(-1)
    if index < values.size:
        return float(values[index])
    return 0.0


def camera_timestamp(src_data, camera_key, frame_idx, fallback_ns):
    dataset = src_data.get(camera_key)
    if dataset is None:
        return fallback_ns
    return timestamp_from_index_path(dataset[frame_idx], fallback_ns)


def load_aloha_source(source_path):
    src = h5py.File(source_path, "r")
    size = int(src["size"][()]) if "size" in src else len(src["timestamp"])

    data = {
        "size": size,
        "timestamp": require_dataset(src, "timestamp"),
        "state_left_pos": require_dataset(src, "arm/jointStatePosition/puppetLeft"),
        "state_right_pos": require_dataset(src, "arm/jointStatePosition/puppetRight"),
        "state_left_vel": require_dataset(src, "arm/jointStateVelocity/puppetLeft"),
        "state_right_vel": require_dataset(src, "arm/jointStateVelocity/puppetRight"),
        "state_left_effort": require_dataset(src, "arm/jointStateEffort/puppetLeft"),
        "state_right_effort": require_dataset(src, "arm/jointStateEffort/puppetRight"),
        "action_left_pos": require_dataset(src, "arm/jointStatePosition/masterLeft"),
        "action_right_pos": require_dataset(src, "arm/jointStatePosition/masterRight"),
        "pose_left": require_dataset(src, "localization/pose/puppetLeft"),
        "pose_right": require_dataset(src, "localization/pose/puppetRight"),
        "camera_left": src.get("camera/color/left"),
        "camera_front": src.get("camera/color/front"),
        "camera_right": src.get("camera/color/right"),
        "camera_head": src.get("camera/color/head"),
        "base_vel": optional_named_dataset(src, "robotBase/vel", ("chassis", "chasis")),
        "odom_vel": optional_first_dataset(
            src,
            (
                "robotBase/vel/odom",
            ),
        ),
        "pose_base": optional_first_dataset(
            src,
            (
                "localization/pose/base",
                "localization/pose/base_pos",
                "localization/pose/basePos",
            ),
        ),
        "lift_height": optional_named_dataset(src, "lift/motor", ("body",)),
        "file": src,
    }
    return data


def load_g2_source(source_path):
    src = h5py.File(source_path, "r")
    size = int(src["size"][()]) if "size" in src else len(src["timestamp"])

    data = {
        "size": size,
        "timestamp": require_dataset(src, "timestamp"),
        "state_pos": require_dataset(src, "arm/jointStatePosition/state"),
        "state_vel": optional_dataset(src, "arm/jointStateVelocity/state"),
        "state_effort": optional_dataset(src, "arm/jointStateEffort/state"),
        "action_pos": require_dataset(src, "arm/jointStatePosition/action"),
        "pose_base": optional_dataset(src, "localization/pose/base"),
        "camera_left": src.get("camera/color/left"),
        "camera_front": src.get("camera/color/front"),
        "camera_right": src.get("camera/color/right"),
        "file": src,
    }
    return data


def load_source(source_path, robot):
    if robot == "g2":
        return load_g2_source(source_path)
    return load_aloha_source(source_path)


def write_aloha_frame(out, frame_idx, src_data):
    group = out.create_group(str(frame_idx))

    main_ts_ns = seconds_to_ns(src_data["timestamp"][frame_idx])
    state_left_pos = np.asarray(src_data["state_left_pos"][frame_idx], dtype=np.float64)
    state_right_pos = np.asarray(src_data["state_right_pos"][frame_idx], dtype=np.float64)
    state_left_vel = np.asarray(src_data["state_left_vel"][frame_idx], dtype=np.float64)
    state_right_vel = np.asarray(src_data["state_right_vel"][frame_idx], dtype=np.float64)
    state_left_effort = np.asarray(src_data["state_left_effort"][frame_idx], dtype=np.float64)
    state_right_effort = np.asarray(src_data["state_right_effort"][frame_idx], dtype=np.float64)
    action_left_pos = np.asarray(src_data["action_left_pos"][frame_idx], dtype=np.float64)
    action_right_pos = np.asarray(src_data["action_right_pos"][frame_idx], dtype=np.float64)

    state_joint_pos = np.concatenate([state_left_pos, state_right_pos])
    state_joint_vel = np.concatenate([state_left_vel, state_right_vel])
    state_joint_effort = np.concatenate([state_left_effort, state_right_effort])
    action_joint_pos = np.concatenate([action_left_pos, action_right_pos])

    left_end_pos, left_end_quat = pose_xyzrpy_to_position_orientation(src_data["pose_left"][frame_idx])
    right_end_pos, right_end_quat = pose_xyzrpy_to_position_orientation(src_data["pose_right"][frame_idx])
    end_position = np.stack([left_end_pos, right_end_pos])
    end_orientation = np.stack([left_end_quat, right_end_quat])
    create_dataset(group, "main_timestamp", np.uint64(main_ts_ns))

    create_dataset(group, "state/joint/position", state_joint_pos)
    create_dataset(group, "state/joint/velocity", state_joint_vel)
    create_dataset(group, "state/joint/effort", state_joint_effort)

    create_dataset(group, "action/joint/position", action_joint_pos)

    create_dataset(group, "state/left_effector/position", [state_left_pos[6]])
    create_dataset(group, "state/right_effector/position", [state_right_pos[6]])
    create_dataset(group, "action/left_effector/position", [action_left_pos[6]])
    create_dataset(group, "action/right_effector/position", [action_right_pos[6]])

    create_dataset(group, "state/end/position", end_position)
    create_dataset(group, "state/end/orientation", end_orientation)

    lift_height = take_scalar(dataset_row(src_data["lift_height"], frame_idx), 0)
    action_robot_velocity = fit_dim(dataset_row(src_data["base_vel"], frame_idx), 3)
    state_robot_velocity = fit_dim(dataset_row(src_data["odom_vel"], frame_idx), 3)
    pose_base = dataset_row(src_data["pose_base"], frame_idx)
    if pose_base.size >= 6:
        robot_position, robot_orientation = pose_xyzrpy_to_position_orientation(pose_base)
        base_pose2d = np.array(
            [float(pose_base[0]), float(pose_base[1]), float(pose_base[5])],
            dtype=np.float64,
        )
    else:
        robot_position = np.zeros(3, dtype=np.float64)
        robot_orientation = np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float64)
        base_pose2d = np.zeros(3, dtype=np.float64)
    base_state = np.concatenate([base_pose2d, state_robot_velocity])

    create_dataset(group, "state/waist/position", [lift_height])
    create_dataset(group, "state/waist/velocity", [0.0])
    create_dataset(group, "state/waist/effort", [0.0])
    create_dataset(group, "action/waist/position", [lift_height])
    create_dataset(group, "state/robot/position", robot_position)
    create_dataset(group, "state/robot/orientation", robot_orientation)
    create_dataset(group, "state/robot/pose2d", base_pose2d)
    create_dataset(group, "state/robot/velocity", state_robot_velocity)
    create_dataset(group, "state/robot/base_state", base_state)
    create_dataset(group, "action/robot/velocity", action_robot_velocity)

    camera_ts = {
        "head_stereo_left": camera_timestamp(src_data, "camera_left", frame_idx, main_ts_ns),
        "head_color": camera_timestamp(src_data, "camera_front", frame_idx, main_ts_ns),
        "head_stereo_right": camera_timestamp(src_data, "camera_right", frame_idx, main_ts_ns),
    }
    if src_data["camera_head"] is not None:
        camera_ts["head"] = camera_timestamp(src_data, "camera_head", frame_idx, main_ts_ns)
    for key, value in camera_ts.items():
        create_dataset(group, f"timestamp/camera/{key}", [value], dtype=np.uint64)


def write_g2_frame(out, frame_idx, src_data):
    group = out.create_group(str(frame_idx))

    main_ts_ns = seconds_to_ns(src_data["timestamp"][frame_idx])
    state_full = dataset_row(src_data["state_pos"], frame_idx)
    state_vel_full = dataset_row(src_data["state_vel"], frame_idx)
    state_effort_full = dataset_row(src_data["state_effort"], frame_idx)
    action_full = dataset_row(src_data["action_pos"], frame_idx)

    state_joint_pos = np.concatenate(
        [
            take_indices(state_full, G2_STATE_LEFT_ARM),
            take_indices(state_full, G2_STATE_RIGHT_ARM),
        ]
    )
    state_joint_vel = np.concatenate(
        [
            take_indices(state_vel_full, G2_STATE_LEFT_ARM),
            take_indices(state_vel_full, G2_STATE_RIGHT_ARM),
        ]
    )
    state_joint_effort = np.concatenate(
        [
            take_indices(state_effort_full, G2_STATE_LEFT_ARM),
            take_indices(state_effort_full, G2_STATE_RIGHT_ARM),
        ]
    )
    action_joint_pos = np.concatenate(
        [
            take_indices(action_full, G2_ACTION_LEFT_ARM),
            take_indices(action_full, G2_ACTION_RIGHT_ARM),
        ]
    )

    state_left_gripper = take_scalar(state_full, 22)
    state_right_gripper = take_scalar(state_full, 23)
    action_left_gripper = take_scalar(action_full, 7)
    action_right_gripper = take_scalar(action_full, 15)
    state_waist_pos = take_indices(state_full, G2_STATE_WAIST)
    state_waist_vel = take_indices(state_vel_full, G2_STATE_WAIST)
    state_waist_effort = take_indices(state_effort_full, G2_STATE_WAIST)
    action_waist_pos = take_indices(action_full, G2_ACTION_WAIST)

    pose_base = dataset_row(src_data["pose_base"], frame_idx)
    if pose_base.size >= 6:
        robot_position, robot_orientation = pose_xyzrpy_to_position_orientation(pose_base)
    else:
        robot_position = np.zeros(3, dtype=np.float64)
        robot_orientation = np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float64)

    action_robot_velocity = np.zeros(6, dtype=np.float64)
    action_robot_velocity[0] = take_scalar(action_full, 21)
    action_robot_velocity[1] = take_scalar(action_full, 22)
    action_robot_velocity[5] = take_scalar(action_full, 23)

    create_dataset(group, "main_timestamp", np.uint64(main_ts_ns))

    create_dataset(group, "state/joint/position", state_joint_pos)
    create_dataset(group, "state/joint/velocity", state_joint_vel)
    create_dataset(group, "state/joint/effort", state_joint_effort)

    create_dataset(group, "action/joint/position", action_joint_pos)

    create_dataset(group, "state/left_effector/position", [state_left_gripper])
    create_dataset(group, "state/right_effector/position", [state_right_gripper])
    create_dataset(group, "action/left_effector/position", [action_left_gripper])
    create_dataset(group, "action/right_effector/position", [action_right_gripper])

    create_dataset(group, "state/waist/position", state_waist_pos)
    create_dataset(group, "state/waist/velocity", state_waist_vel)
    create_dataset(group, "state/waist/effort", state_waist_effort)
    create_dataset(group, "action/waist/position", action_waist_pos)
    create_dataset(group, "state/robot/position", robot_position)
    create_dataset(group, "state/robot/orientation", robot_orientation)
    create_dataset(group, "action/robot/velocity", action_robot_velocity)

    camera_ts = {
        "hand_left_color": camera_timestamp(src_data, "camera_left", frame_idx, main_ts_ns),
        "head_color": camera_timestamp(src_data, "camera_front", frame_idx, main_ts_ns),
        "hand_right_color": camera_timestamp(src_data, "camera_right", frame_idx, main_ts_ns),
    }
    for key, value in camera_ts.items():
        create_dataset(group, f"timestamp/camera/{key}", [value], dtype=np.uint64)


def convert(source_path, output_path, robot="aloha"):
    source_path = Path(source_path).expanduser().resolve()
    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    src_data = load_source(source_path, robot)
    try:
        with h5py.File(output_path, "w") as out:
            out.attrs["source_format"] = f"ario_{robot}_hdf5"
            if robot == "g2":
                out.attrs["state_mapping"] = "G2 /hal/joint_state arms/grippers/waist plus /odom"
                out.attrs["action_mapping"] = "G2 /pico_g2/action_joints arms/grippers/waist/base velocity"
                out.attrs["zero_filled_fields"] = "missing state velocity/effort fields"
                out.attrs["omitted_fields"] = "head joints and other fields not present in output schema"
                writer = write_g2_frame
            else:
                out.attrs["state_mapping"] = "puppet joints plus lift height and base_state[x,y,yaw,vx,vy,wz] when present"
                out.attrs["action_mapping"] = "master joints plus lift back_height and chassis velocity when present"
                out.attrs["zero_filled_fields"] = "missing optional lift/base state and lift/chassis action fields"
                out.attrs["omitted_fields"] = "other fields not present in source are omitted"
                writer = write_aloha_frame
            for frame_idx in range(src_data["size"]):
                writer(out, frame_idx, src_data)
                if frame_idx and frame_idx % 200 == 0:
                    print(f"converted {frame_idx}/{src_data['size']}")
    finally:
        src_data["file"].close()
    print(f"Done: {output_path}")


def parse_args():
    parser = argparse.ArgumentParser(description="Convert ARIO/ALOHA HDF5 to ICRA-WBC aligned_joints.h5 style.")
    parser.add_argument("--input", required=True, help="Input ARIO/ALOHA HDF5 file.")
    parser.add_argument("--output", required=True, help="Output aligned_joints.h5 file.")
    parser.add_argument(
        "--robot",
        choices=["aloha", "g2"],
        default="aloha",
        help="Source robot layout. Default: aloha.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    convert(args.input, args.output, args.robot)


if __name__ == "__main__":
    main()
