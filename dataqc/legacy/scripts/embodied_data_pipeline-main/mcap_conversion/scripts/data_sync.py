#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Data Synchronization Tool (Python Version)
This script synchronizes multi-modal sensor data based on timestamps,
converted from the original C++ ROS implementation.
"""

import os
import sys
import argparse
import yaml
import json
import math
import shutil
import numpy as np
import cv2
from typing import List, Dict, Tuple, Optional, Union


class TimeSeries:
    """时间序列数据项"""
    def __init__(
        self,
        time: float,
        data_list: List,
        sync_list: List,
        source_time: Optional[float] = None,
        fill_method: str = "original",
    ):
        self.time = time
        self.data_list = data_list
        self.sync_list = sync_list
        self.source_time = time if source_time is None else source_time
        self.fill_method = fill_method

    def to_data_list(self):
        """将数据添加到数据列表"""
        self.data_list.append(self)

    def to_sync_list(self):
        """将数据添加到同步列表"""
        self.sync_list.append(self)


class Operator:
    """数据同步器"""

    def __init__(self, args):
        self.args = args
        # 初始化目录路径
        self.init_directories()

        # 初始化时间序列存储
        self.init_time_series()

        # 存储所有时间戳
        self.all_time_series: List[TimeSeries] = []
        self.source_time_series: List[TimeSeries] = []
        self.camera_info_master_times: List[float] = []
        self.camera_info_master_time_keys: set[int] = set()
        self.camera_info_master_name: str = ""
        self.master_sync_times: List[float] = []

    @staticmethod
    def is_optional_base_pose(name: str) -> bool:
        normalized = str(name).lower().replace("_", "").replace("-", "")
        return normalized in {"base", "basepos", "basepose", "robotbase"}

    def init_directories(self):
        """初始化数据目录路径"""
        self.episode_dir = os.path.join(self.args.datasetDir, self.args.episodeName)

        # 构建各类数据的目录路径
        self.camera_color_dirs = [os.path.join(self.episode_dir, "camera/color", name) for name in self.args.cameraColorNames]
        self.camera_depth_dirs = [os.path.join(self.episode_dir, "camera/depth", name) for name in self.args.cameraDepthNames]
        self.camera_point_cloud_dirs = [os.path.join(self.episode_dir, "camera/pointCloud", name) for name in self.args.cameraPointCloudNames]
        self.arm_joint_state_dirs = [os.path.join(self.episode_dir, "arm/jointState", name) for name in self.args.armJointStateNames]
        self.arm_end_pose_dirs = [os.path.join(self.episode_dir, "arm/endPose", name) for name in self.args.armEndPoseNames]
        self.localization_pose_dirs = [os.path.join(self.episode_dir, "localization/pose", name) for name in self.args.localizationPoseNames]
        self.gripper_encoder_dirs = [os.path.join(self.episode_dir, "gripper/encoder", name) for name in self.args.gripperEncoderNames]
        self.imu_9axis_dirs = [os.path.join(self.episode_dir, "imu/9axis", name) for name in self.args.imu9AxisNames]
        self.lidar_point_cloud_dirs = [os.path.join(self.episode_dir, "lidar/pointCloud", name) for name in self.args.lidarPointCloudNames]
        self.robot_base_vel_dirs = [os.path.join(self.episode_dir, "robotBase/vel", name) for name in self.args.robotBaseVelNames]
        self.lift_motor_dirs = [os.path.join(self.episode_dir, "lift/motor", name) for name in self.args.liftMotorNames]

    def init_time_series(self):
        """初始化时间序列存储结构"""
        # 数据时间序列
        self.camera_color_data_time_series = [[] for _ in self.args.cameraColorNames]
        self.camera_depth_data_time_series = [[] for _ in self.args.cameraDepthNames]
        self.camera_point_cloud_data_time_series = [[] for _ in self.args.cameraPointCloudNames]
        self.arm_joint_state_data_time_series = [[] for _ in self.args.armJointStateNames]
        self.arm_end_pose_data_time_series = [[] for _ in self.args.armEndPoseNames]
        self.localization_pose_data_time_series = [[] for _ in self.args.localizationPoseNames]
        self.gripper_encoder_data_time_series = [[] for _ in self.args.gripperEncoderNames]
        self.imu_9axis_data_time_series = [[] for _ in self.args.imu9AxisNames]
        self.lidar_point_cloud_data_time_series = [[] for _ in self.args.lidarPointCloudNames]
        self.robot_base_vel_data_time_series = [[] for _ in self.args.robotBaseVelNames]
        self.lift_motor_data_time_series = [[] for _ in self.args.liftMotorNames]
        self.robot_base_vel_all_time_series = [[] for _ in self.args.robotBaseVelNames]
        self.lift_motor_all_time_series = [[] for _ in self.args.liftMotorNames]
        self.robot_base_vel_available = [False for _ in self.args.robotBaseVelNames]
        self.lift_motor_available = [False for _ in self.args.liftMotorNames]
        self.camera_color_interpolated_counts = [0 for _ in self.args.cameraColorNames]
        self.camera_color_nearest_fill_counts = [0 for _ in self.args.cameraColorNames]
        self.camera_color_source_counts = [0 for _ in self.args.cameraColorNames]
        self.camera_info_counts = [0 for _ in self.args.cameraColorNames]
        self.camera_info_rates_hz = [0.0 for _ in self.args.cameraColorNames]

        # 同步时间序列
        self.camera_color_sync_time_series = [[] for _ in self.args.cameraColorNames]
        self.camera_depth_sync_time_series = [[] for _ in self.args.cameraDepthNames]
        self.camera_point_cloud_sync_time_series = [[] for _ in self.args.cameraPointCloudNames]
        self.arm_joint_state_sync_time_series = [[] for _ in self.args.armJointStateNames]
        self.arm_end_pose_sync_time_series = [[] for _ in self.args.armEndPoseNames]
        self.localization_pose_sync_time_series = [[] for _ in self.args.localizationPoseNames]
        self.gripper_encoder_sync_time_series = [[] for _ in self.args.gripperEncoderNames]
        self.imu_9axis_sync_time_series = [[] for _ in self.args.imu9AxisNames]
        self.lidar_point_cloud_sync_time_series = [[] for _ in self.args.lidarPointCloudNames]
        self.robot_base_vel_sync_time_series = [[] for _ in self.args.robotBaseVelNames]
        self.lift_motor_sync_time_series = [[] for _ in self.args.liftMotorNames]

        # 文件扩展名
        self.camera_color_exts = [".jpg"] * len(self.args.cameraColorNames)
        self.camera_depth_exts = [".png"] * len(self.args.cameraDepthNames)
        self.camera_point_cloud_exts = [".pcd"] * len(self.args.cameraPointCloudNames)
        self.arm_joint_state_exts = [".json"] * len(self.args.armJointStateNames)
        self.arm_end_pose_exts = [".json"] * len(self.args.armEndPoseNames)
        self.localization_pose_exts = [".json"] * len(self.args.localizationPoseNames)
        self.gripper_encoder_exts = [".json"] * len(self.args.gripperEncoderNames)
        self.imu_9axis_exts = [".json"] * len(self.args.imu9AxisNames)
        self.lidar_point_cloud_exts = [".json"] * len(self.args.lidarPointCloudNames)
        self.robot_base_vel_exts = [".json"] * len(self.args.robotBaseVelNames)
        self.lift_motor_exts = [".json"] * len(self.args.liftMotorNames)

    def get_files_in_path(self, path: str, ext: str, data_list: List, sync_list: List) -> int:
        """获取指定路径下指定扩展名的文件，并按时间戳排序"""
        count = 0
        if not os.path.exists(path):
            print(f"Warning: Directory {path} does not exist")
            return count

        for filename in os.listdir(path):
            if filename.endswith(ext):
                try:
                    # 从文件名提取时间戳
                    timestamp = float(filename[:filename.rfind(".")])
                    time_series = TimeSeries(timestamp, data_list, sync_list)
                    self.all_time_series.append(time_series)
                    self.source_time_series.append(time_series)
                    count += 1
                except ValueError:
                    # 跳过无法解析时间戳的文件
                    continue
        return count

    def get_side_channel_files_in_path(self, path: str, ext: str, sync_list: List) -> List[TimeSeries]:
        """Load optional side-channel timestamps without adding them to the main sync clock."""
        series = []
        if not os.path.exists(path):
            print(f"Warning: Directory {path} does not exist")
            return series

        for filename in os.listdir(path):
            if filename.endswith(ext):
                try:
                    timestamp = float(filename[:filename.rfind(".")])
                    series.append(TimeSeries(timestamp, [], sync_list))
                except ValueError:
                    continue
        series.sort(key=lambda x: x.time)
        return series

    def load_camera_info_times(self, camera_dir: str) -> List[float]:
        """Read per-message camera_info timestamps saved by mcap_to_aloha_data.py."""
        path = os.path.join(camera_dir, "camera_info_timestamps.txt")
        if not os.path.exists(path):
            return []
        times: List[float] = []
        with open(path, "r") as file_obj:
            for line in file_obj:
                line = line.strip()
                if not line:
                    continue
                try:
                    times.append(float(line))
                except ValueError:
                    continue
        times.sort()
        unique_times: List[float] = []
        for timestamp in times:
            if unique_times and math.isclose(timestamp, unique_times[-1], rel_tol=0.0, abs_tol=1e-9):
                continue
            unique_times.append(timestamp)
        return unique_times

    def select_camera_info_master_clock(self) -> List[float]:
        """Use the highest-rate camera_info stream as the sync master clock."""
        candidates = []
        for i, name in enumerate(self.args.cameraColorNames):
            times = self.load_camera_info_times(self.camera_color_dirs[i])
            self.camera_info_counts[i] = len(times)
            if len(times) < 2:
                continue
            duration = times[-1] - times[0]
            hz = (len(times) - 1) / duration if duration > 0 else 0.0
            self.camera_info_rates_hz[i] = hz
            candidates.append((hz, len(times), name, times))
        if not candidates:
            return []
        hz, count, name, times = max(candidates, key=lambda item: (item[0], item[1]))
        self.camera_info_master_name = str(name)
        print(
            f"Camera-info master clock: {self.camera_info_master_name} "
            f"({count} frames, {hz:.3f} Hz)"
        )
        return times

    def load_all_time_series(self):
        """加载所有数据源的时间序列"""

        # 加载各类数据的时间戳
        for i, name in enumerate(self.args.cameraColorNames):
            count = self.get_files_in_path(self.camera_color_dirs[i], ".jpg",
                                         self.camera_color_data_time_series[i],
                                         self.camera_color_sync_time_series[i])
            if count == 0:
                count = self.get_files_in_path(self.camera_color_dirs[i], ".png",
                                             self.camera_color_data_time_series[i],
                                             self.camera_color_sync_time_series[i])
                if count > 0:
                    self.camera_color_exts[i] = ".png"
            self.camera_color_source_counts[i] = count

        for i, name in enumerate(self.args.cameraDepthNames):
            count = self.get_files_in_path(self.camera_depth_dirs[i], ".png",
                                         self.camera_depth_data_time_series[i],
                                         self.camera_depth_sync_time_series[i])

        for i, name in enumerate(self.args.cameraPointCloudNames):
            count = self.get_files_in_path(self.camera_point_cloud_dirs[i], ".pcd",
                                         self.camera_point_cloud_data_time_series[i],
                                         self.camera_point_cloud_sync_time_series[i])

        for i, name in enumerate(self.args.armJointStateNames):
            count = self.get_files_in_path(self.arm_joint_state_dirs[i], ".json",
                                         self.arm_joint_state_data_time_series[i],
                                         self.arm_joint_state_sync_time_series[i])

        for i, name in enumerate(self.args.armEndPoseNames):
            count = self.get_files_in_path(self.arm_end_pose_dirs[i], ".json",
                                         self.arm_end_pose_data_time_series[i],
                                         self.arm_end_pose_sync_time_series[i])

        for i, name in enumerate(self.args.localizationPoseNames):
            if self.is_optional_base_pose(name):
                self.get_side_channel_files_in_path(
                    self.localization_pose_dirs[i],
                    ".json",
                    self.localization_pose_sync_time_series[i],
                )
                continue
            count = self.get_files_in_path(self.localization_pose_dirs[i], ".json",
                                         self.localization_pose_data_time_series[i],
                                         self.localization_pose_sync_time_series[i])

        for i, name in enumerate(self.args.gripperEncoderNames):
            count = self.get_files_in_path(self.gripper_encoder_dirs[i], ".json",
                                         self.gripper_encoder_data_time_series[i],
                                         self.gripper_encoder_sync_time_series[i])

        for i, name in enumerate(self.args.imu9AxisNames):
            count = self.get_files_in_path(self.imu_9axis_dirs[i], ".json",
                                         self.imu_9axis_data_time_series[i],
                                         self.imu_9axis_sync_time_series[i])

        for i, name in enumerate(self.args.lidarPointCloudNames):
            count = self.get_files_in_path(self.lidar_point_cloud_dirs[i], ".json",
                                         self.lidar_point_cloud_data_time_series[i],
                                         self.lidar_point_cloud_sync_time_series[i])

        for i, name in enumerate(self.args.robotBaseVelNames):
            self.robot_base_vel_all_time_series[i] = self.get_side_channel_files_in_path(
                self.robot_base_vel_dirs[i],
                ".json",
                self.robot_base_vel_sync_time_series[i],
            )
            self.robot_base_vel_available[i] = len(self.robot_base_vel_all_time_series[i]) > 0

        for i, name in enumerate(self.args.liftMotorNames):
            self.lift_motor_all_time_series[i] = self.get_side_channel_files_in_path(
                self.lift_motor_dirs[i],
                ".json",
                self.lift_motor_sync_time_series[i],
            )
            self.lift_motor_available[i] = len(self.lift_motor_all_time_series[i]) > 0

        self.camera_info_master_times = self.select_camera_info_master_clock()
        self.camera_info_master_time_keys = {round(timestamp * 1_000_000_000) for timestamp in self.camera_info_master_times}

        # 按时间戳排序所有时间序列
        self.all_time_series.sort(key=lambda x: x.time)
        self.source_time_series.sort(key=lambda x: x.time)

    def check_data_adequacy(self, print_info: bool = False) -> Optional[float]:
        """检查数据充足性，返回最早的可用时间戳"""
        result = True
        time = -1

        # 检查所有数据源是否都有数据
        for i, name in enumerate(self.args.cameraColorNames):
            if len(self.camera_color_data_time_series[i]) == 0:
                if print_info:
                    print(f"Camera color {name} has no data")
                result = False
            else:
                time = max(time, self.camera_color_data_time_series[i][-1].time)

        for i, name in enumerate(self.args.cameraDepthNames):
            if len(self.camera_depth_data_time_series[i]) == 0:
                if print_info:
                    print(f"Camera depth {name} has no data")
                result = False
            else:
                time = max(time, self.camera_depth_data_time_series[i][-1].time)

        for i, name in enumerate(self.args.cameraPointCloudNames):
            if len(self.camera_point_cloud_data_time_series[i]) == 0:
                if print_info:
                    print(f"Camera point cloud {name} has no data")
                result = False
            else:
                time = max(time, self.camera_point_cloud_data_time_series[i][-1].time)

        for i, name in enumerate(self.args.armJointStateNames):
            if len(self.arm_joint_state_data_time_series[i]) == 0:
                if print_info:
                    print(f"Arm joint state {name} has no data")
                result = False
            else:
                time = max(time, self.arm_joint_state_data_time_series[i][-1].time)

        for i, name in enumerate(self.args.armEndPoseNames):
            if len(self.arm_end_pose_data_time_series[i]) == 0:
                if print_info:
                    print(f"Arm end pose {name} has no data")
                result = False
            else:
                time = max(time, self.arm_end_pose_data_time_series[i][-1].time)

        for i, name in enumerate(self.args.localizationPoseNames):
            if self.is_optional_base_pose(name):
                continue
            if len(self.localization_pose_data_time_series[i]) == 0:
                if print_info:
                    print(f"Localization pose {name} has no data")
                result = False
            else:
                time = max(time, self.localization_pose_data_time_series[i][-1].time)

        for i, name in enumerate(self.args.gripperEncoderNames):
            if len(self.gripper_encoder_data_time_series[i]) == 0:
                if print_info:
                    print(f"Gripper encoder {name} has no data")
                result = False
            else:
                time = max(time, self.gripper_encoder_data_time_series[i][-1].time)

        for i, name in enumerate(self.args.imu9AxisNames):
            if len(self.imu_9axis_data_time_series[i]) == 0:
                if print_info:
                    print(f"IMU 9-axis {name} has no data")
                result = False
            else:
                time = max(time, self.imu_9axis_data_time_series[i][-1].time)

        for i, name in enumerate(self.args.lidarPointCloudNames):
            if len(self.lidar_point_cloud_data_time_series[i]) == 0:
                if print_info:
                    print(f"Lidar point cloud {name} has no data")
                result = False
            else:
                time = max(time, self.lidar_point_cloud_data_time_series[i][-1].time)

        return time if result else None

    def find_closest_index(self, data_series: List[TimeSeries], target_time: float) -> Tuple[int, float]:
        """找到最接近目标时间的数据索引"""
        if not data_series:
            return -1, float('inf')

        left = 0
        right = len(data_series)
        while left < right:
            mid = (left + right) // 2
            if data_series[mid].time < target_time:
                left = mid + 1
            else:
                right = mid

        candidate_indices = []
        if left > 0:
            candidate_indices.append(left - 1)
        if left < len(data_series):
            candidate_indices.append(left)

        closest_index = candidate_indices[0]
        closest_diff = abs(data_series[closest_index].time - target_time)
        for candidate_index in candidate_indices[1:]:
            time_diff = abs(data_series[candidate_index].time - target_time)
            if time_diff < closest_diff:
                closest_diff = time_diff
                closest_index = candidate_index

        return closest_index, closest_diff

    def image_file_path(self, camera_idx: int, timestamp: float) -> str:
        return os.path.join(
            self.camera_color_dirs[camera_idx],
            f"{timestamp:.6f}{self.camera_color_exts[camera_idx]}",
        )

    def synthesize_camera_color_frame(self, camera_idx: int, target_time: float) -> Optional[TimeSeries]:
        """Create an interpolated camera image at the master timestamp."""
        data_series = self.camera_color_data_time_series[camera_idx]
        if not data_series:
            return None

        output_path = self.image_file_path(camera_idx, target_time)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        if os.path.exists(output_path):
            return TimeSeries(
                target_time,
                [],
                self.camera_color_sync_time_series[camera_idx],
                source_time=target_time,
                fill_method="existing_synthesized",
            )

        right_idx = 0
        while right_idx < len(data_series) and data_series[right_idx].time < target_time:
            right_idx += 1
        left_idx = right_idx - 1

        if 0 <= left_idx and right_idx < len(data_series):
            left_series = data_series[left_idx]
            right_series = data_series[right_idx]
            left_path = self.image_file_path(camera_idx, left_series.time)
            right_path = self.image_file_path(camera_idx, right_series.time)
            left_img = cv2.imread(left_path, cv2.IMREAD_UNCHANGED)
            right_img = cv2.imread(right_path, cv2.IMREAD_UNCHANGED)
            if (
                left_img is not None
                and right_img is not None
                and left_img.shape == right_img.shape
                and not math.isclose(right_series.time, left_series.time, rel_tol=0.0, abs_tol=1e-9)
            ):
                alpha = (target_time - left_series.time) / (right_series.time - left_series.time)
                alpha = min(max(alpha, 0.0), 1.0)
                blended = cv2.addWeighted(left_img, 1.0 - alpha, right_img, alpha, 0.0)
                if cv2.imwrite(output_path, blended):
                    self.camera_color_interpolated_counts[camera_idx] += 1
                    return TimeSeries(
                        target_time,
                        [],
                        self.camera_color_sync_time_series[camera_idx],
                        source_time=target_time,
                        fill_method="interpolated",
                    )

        nearest_idx, _nearest_diff = self.find_closest_index(data_series, target_time)
        if nearest_idx < 0:
            return None
        nearest_path = self.image_file_path(camera_idx, data_series[nearest_idx].time)
        if not os.path.exists(nearest_path):
            return None
        shutil.copyfile(nearest_path, output_path)
        self.camera_color_nearest_fill_counts[camera_idx] += 1
        return TimeSeries(
            target_time,
            [],
            self.camera_color_sync_time_series[camera_idx],
            source_time=data_series[nearest_idx].time,
            fill_method="nearest_fill",
        )

    def print_camera_fill_summary(self):
        for i, name in enumerate(self.args.cameraColorNames):
            interpolated = self.camera_color_interpolated_counts[i]
            nearest = self.camera_color_nearest_fill_counts[i]
            if interpolated or nearest:
                print(
                    f"Camera color {name} filled {interpolated + nearest} frames "
                    f"({interpolated} interpolated, {nearest} nearest)"
                )

    def collect_master_clock_indices(self, frame_time: float) -> Tuple[bool, Dict[str, Union[int, TimeSeries]]]:
        """Find nearest source samples for one camera_info master timestamp."""
        closest_indices: Dict[str, Union[int, TimeSeries]] = {}

        def require_closest(key: str, data_series: List[TimeSeries]) -> bool:
            if not data_series:
                return True
            closest_idx, closest_diff = self.find_closest_index(data_series, frame_time)
            if closest_diff > self.args.timeDiffLimit:
                return False
            closest_indices[key] = closest_idx
            return True

        for i, _name in enumerate(self.args.cameraColorNames):
            key = f'camera_color_{i}'
            data_series = self.camera_color_data_time_series[i]
            closest_idx, closest_diff = self.find_closest_index(data_series, frame_time)
            if closest_idx >= 0 and closest_diff <= self.args.timeDiffLimit:
                closest_indices[key] = closest_idx
                continue
            filled_series = self.synthesize_camera_color_frame(i, frame_time)
            if filled_series is None:
                return False, closest_indices
            closest_indices[key] = filled_series

        for i, _name in enumerate(self.args.cameraDepthNames):
            if not require_closest(f'camera_depth_{i}', self.camera_depth_data_time_series[i]):
                return False, closest_indices

        for i, _name in enumerate(self.args.cameraPointCloudNames):
            if not require_closest(f'camera_point_cloud_{i}', self.camera_point_cloud_data_time_series[i]):
                return False, closest_indices

        for i, _name in enumerate(self.args.armJointStateNames):
            if not require_closest(f'arm_joint_state_{i}', self.arm_joint_state_data_time_series[i]):
                return False, closest_indices

        for i, _name in enumerate(self.args.armEndPoseNames):
            if not require_closest(f'arm_end_pose_{i}', self.arm_end_pose_data_time_series[i]):
                return False, closest_indices

        for i, name in enumerate(self.args.localizationPoseNames):
            if self.is_optional_base_pose(name):
                continue
            if not require_closest(f'localization_pose_{i}', self.localization_pose_data_time_series[i]):
                return False, closest_indices

        for i, _name in enumerate(self.args.gripperEncoderNames):
            if not require_closest(f'gripper_encoder_{i}', self.gripper_encoder_data_time_series[i]):
                return False, closest_indices

        for i, _name in enumerate(self.args.imu9AxisNames):
            if not require_closest(f'imu_9axis_{i}', self.imu_9axis_data_time_series[i]):
                return False, closest_indices

        for i, _name in enumerate(self.args.lidarPointCloudNames):
            if not require_closest(f'lidar_point_cloud_{i}', self.lidar_point_cloud_data_time_series[i]):
                return False, closest_indices

        for i, _name in enumerate(self.args.robotBaseVelNames):
            if self.robot_base_vel_available[i]:
                closest_idx, _closest_diff = self.find_closest_index(
                    self.robot_base_vel_all_time_series[i], frame_time
                )
                if closest_idx >= 0:
                    closest_indices[f'robot_base_vel_{i}'] = closest_idx

        for i, _name in enumerate(self.args.liftMotorNames):
            if self.lift_motor_available[i]:
                closest_idx, _closest_diff = self.find_closest_index(
                    self.lift_motor_all_time_series[i], frame_time
                )
                if closest_idx >= 0:
                    closest_indices[f'lift_motor_{i}'] = closest_idx

        return True, closest_indices

    def append_master_clock_indices(self, closest_indices: Dict[str, Union[int, TimeSeries]]):
        """Append selected samples without trimming, because master clock can look forward and backward."""
        required_groups = [
            ("camera_color", self.camera_color_data_time_series),
            ("camera_depth", self.camera_depth_data_time_series),
            ("camera_point_cloud", self.camera_point_cloud_data_time_series),
            ("arm_joint_state", self.arm_joint_state_data_time_series),
            ("arm_end_pose", self.arm_end_pose_data_time_series),
            ("localization_pose", self.localization_pose_data_time_series),
            ("gripper_encoder", self.gripper_encoder_data_time_series),
            ("imu_9axis", self.imu_9axis_data_time_series),
            ("lidar_point_cloud", self.lidar_point_cloud_data_time_series),
        ]
        for prefix, group in required_groups:
            for i, data_series in enumerate(group):
                key = f"{prefix}_{i}"
                if key in closest_indices:
                    selected = closest_indices[key]
                    if isinstance(selected, TimeSeries):
                        selected.to_sync_list()
                    else:
                        data_series[selected].to_sync_list()

        for i, data_series in enumerate(self.robot_base_vel_all_time_series):
            key = f"robot_base_vel_{i}"
            if key in closest_indices:
                selected = closest_indices[key]
                if isinstance(selected, int):
                    data_series[selected].to_sync_list()

        for i, data_series in enumerate(self.lift_motor_all_time_series):
            key = f"lift_motor_{i}"
            if key in closest_indices:
                selected = closest_indices[key]
                if isinstance(selected, int):
                    data_series[selected].to_sync_list()

    def sync_with_camera_info_master(self) -> int:
        """Synchronize every frame to the highest-rate camera_info stream."""
        for time_series in self.source_time_series:
            time_series.to_data_list()

        if self.check_data_adequacy(True) is None:
            return 0

        self.master_sync_times = []
        frame_count = 0
        for frame_time in self.camera_info_master_times:
            time_diff_pass, closest_indices = self.collect_master_clock_indices(frame_time)
            if not time_diff_pass:
                continue
            self.append_master_clock_indices(closest_indices)
            self.master_sync_times.append(frame_time)
            frame_count += 1
        return frame_count

    def sync(self):
        """执行数据同步"""
        if self.camera_info_master_times:
            print(f"All time series: {len(self.all_time_series)}")
            frame_count = self.sync_with_camera_info_master()
            print(f"Sync frame num: {frame_count}")
            self.print_camera_fill_summary()
            if frame_count == 0:
                self.check_data_adequacy(True)
            self.write_sync_files()
            return

        frame_count = 0
        use_camera_info_master = bool(self.camera_info_master_time_keys)

        print(f"All time series: {len(self.all_time_series)}")

        series_idx = 0
        while series_idx < len(self.all_time_series):
            current_time = self.all_time_series[series_idx].time
            while (
                series_idx < len(self.all_time_series)
                and math.isclose(self.all_time_series[series_idx].time, current_time, rel_tol=0.0, abs_tol=1e-9)
            ):
                self.all_time_series[series_idx].to_data_list()
                series_idx += 1
            if use_camera_info_master and round(current_time * 1_000_000_000) not in self.camera_info_master_time_keys:
                continue
            frame_time = current_time if use_camera_info_master and self.check_data_adequacy() is not None else self.check_data_adequacy()

            if frame_time is not None:
                # 为每个数据源找到最接近的时间戳
                time_diff_pass = True
                closest_indices = {}

                # 检查相机彩色数据
                for i, name in enumerate(self.args.cameraColorNames):
                    if not time_diff_pass:
                        break
                    if self.camera_color_data_time_series[i]:
                        closest_idx, closest_diff = self.find_closest_index(
                            self.camera_color_data_time_series[i], frame_time)
                        if closest_diff > self.args.timeDiffLimit:
                            time_diff_pass = False
                            break
                        closest_indices[f'camera_color_{i}'] = closest_idx

                # 检查相机深度数据
                for i, name in enumerate(self.args.cameraDepthNames):
                    if not time_diff_pass:
                        break
                    if self.camera_depth_data_time_series[i]:
                        closest_idx, closest_diff = self.find_closest_index(
                            self.camera_depth_data_time_series[i], frame_time)
                        if closest_diff > self.args.timeDiffLimit:
                            time_diff_pass = False
                            break
                        closest_indices[f'camera_depth_{i}'] = closest_idx

                # 检查机械臂关节状态
                for i, name in enumerate(self.args.armJointStateNames):
                    if not time_diff_pass:
                        break
                    if self.arm_joint_state_data_time_series[i]:
                        closest_idx, closest_diff = self.find_closest_index(
                            self.arm_joint_state_data_time_series[i], frame_time)
                        if closest_diff > self.args.timeDiffLimit:
                            time_diff_pass = False
                            break
                        closest_indices[f'arm_joint_state_{i}'] = closest_idx

                # 检查定位姿态数据
                for i, name in enumerate(self.args.localizationPoseNames):
                    if self.is_optional_base_pose(name):
                        continue
                    if not time_diff_pass:
                        break
                    if self.localization_pose_data_time_series[i]:
                        closest_idx, closest_diff = self.find_closest_index(
                            self.localization_pose_data_time_series[i], frame_time)
                        if closest_diff > self.args.timeDiffLimit:
                            time_diff_pass = False
                            break
                        closest_indices[f'localization_pose_{i}'] = closest_idx

                # 检查相机点云数据
                for i, name in enumerate(self.args.cameraPointCloudNames):
                    if not time_diff_pass:
                        break
                    if self.camera_point_cloud_data_time_series[i]:
                        closest_idx, closest_diff = self.find_closest_index(
                            self.camera_point_cloud_data_time_series[i], frame_time)
                        if closest_diff > self.args.timeDiffLimit:
                            time_diff_pass = False
                            break
                        closest_indices[f'camera_point_cloud_{i}'] = closest_idx

                # 检查机械臂末端位姿数据
                for i, name in enumerate(self.args.armEndPoseNames):
                    if not time_diff_pass:
                        break
                    if self.arm_end_pose_data_time_series[i]:
                        closest_idx, closest_diff = self.find_closest_index(
                            self.arm_end_pose_data_time_series[i], frame_time)
                        if closest_diff > self.args.timeDiffLimit:
                            time_diff_pass = False
                            break
                        closest_indices[f'arm_end_pose_{i}'] = closest_idx

                # 检查夹爪编码器数据
                for i, name in enumerate(self.args.gripperEncoderNames):
                    if not time_diff_pass:
                        break
                    if self.gripper_encoder_data_time_series[i]:
                        closest_idx, closest_diff = self.find_closest_index(
                            self.gripper_encoder_data_time_series[i], frame_time)
                        if closest_diff > self.args.timeDiffLimit:
                            time_diff_pass = False
                            break
                        closest_indices[f'gripper_encoder_{i}'] = closest_idx

                # 检查IMU 9轴数据
                for i, name in enumerate(self.args.imu9AxisNames):
                    if not time_diff_pass:
                        break
                    if self.imu_9axis_data_time_series[i]:
                        closest_idx, closest_diff = self.find_closest_index(
                            self.imu_9axis_data_time_series[i], frame_time)
                        if closest_diff > self.args.timeDiffLimit:
                            time_diff_pass = False
                            break
                        closest_indices[f'imu_9axis_{i}'] = closest_idx

                # 检查激光雷达点云数据
                for i, name in enumerate(self.args.lidarPointCloudNames):
                    if not time_diff_pass:
                        break
                    if self.lidar_point_cloud_data_time_series[i]:
                        closest_idx, closest_diff = self.find_closest_index(
                            self.lidar_point_cloud_data_time_series[i], frame_time)
                        if closest_diff > self.args.timeDiffLimit:
                            time_diff_pass = False
                            break
                        closest_indices[f'lidar_point_cloud_{i}'] = closest_idx

                # 检查机器人底盘速度数据
                for i, name in enumerate(self.args.robotBaseVelNames):
                    if self.robot_base_vel_available[i]:
                        closest_idx, closest_diff = self.find_closest_index(
                            self.robot_base_vel_all_time_series[i], frame_time)
                        closest_indices[f'robot_base_vel_{i}'] = closest_idx

                # 检查升降电机数据
                for i, name in enumerate(self.args.liftMotorNames):
                    if self.lift_motor_available[i]:
                        closest_idx, closest_diff = self.find_closest_index(
                            self.lift_motor_all_time_series[i], frame_time)
                        closest_indices[f'lift_motor_{i}'] = closest_idx

                # 如果时间差检查通过，将数据加入同步列表
                if time_diff_pass:
                    # 添加相机彩色数据到同步列表
                    for i, name in enumerate(self.args.cameraColorNames):
                        if f'camera_color_{i}' in closest_indices:
                            idx = closest_indices[f'camera_color_{i}']
                            self.camera_color_data_time_series[i][idx].to_sync_list()
                            # 删除已处理的数据
                            del self.camera_color_data_time_series[i][:idx+1]

                    # 添加相机深度数据到同步列表
                    for i, name in enumerate(self.args.cameraDepthNames):
                        if f'camera_depth_{i}' in closest_indices:
                            idx = closest_indices[f'camera_depth_{i}']
                            self.camera_depth_data_time_series[i][idx].to_sync_list()
                            del self.camera_depth_data_time_series[i][:idx+1]

                    # 添加相机点云数据到同步列表
                    for i, name in enumerate(self.args.cameraPointCloudNames):
                        if f'camera_point_cloud_{i}' in closest_indices:
                            idx = closest_indices[f'camera_point_cloud_{i}']
                            self.camera_point_cloud_data_time_series[i][idx].to_sync_list()
                            del self.camera_point_cloud_data_time_series[i][:idx+1]

                    # 添加机械臂关节状态到同步列表
                    for i, name in enumerate(self.args.armJointStateNames):
                        if f'arm_joint_state_{i}' in closest_indices:
                            idx = closest_indices[f'arm_joint_state_{i}']
                            self.arm_joint_state_data_time_series[i][idx].to_sync_list()
                            del self.arm_joint_state_data_time_series[i][:idx+1]

                    # 添加机械臂末端位姿到同步列表
                    for i, name in enumerate(self.args.armEndPoseNames):
                        if f'arm_end_pose_{i}' in closest_indices:
                            idx = closest_indices[f'arm_end_pose_{i}']
                            self.arm_end_pose_data_time_series[i][idx].to_sync_list()
                            del self.arm_end_pose_data_time_series[i][:idx+1]

                    # 添加定位姿态数据到同步列表
                    for i, name in enumerate(self.args.localizationPoseNames):
                        if f'localization_pose_{i}' in closest_indices:
                            idx = closest_indices[f'localization_pose_{i}']
                            self.localization_pose_data_time_series[i][idx].to_sync_list()
                            del self.localization_pose_data_time_series[i][:idx+1]

                    # 添加夹爪编码器数据到同步列表
                    for i, name in enumerate(self.args.gripperEncoderNames):
                        if f'gripper_encoder_{i}' in closest_indices:
                            idx = closest_indices[f'gripper_encoder_{i}']
                            self.gripper_encoder_data_time_series[i][idx].to_sync_list()
                            del self.gripper_encoder_data_time_series[i][:idx+1]

                    # 添加IMU 9轴数据到同步列表
                    for i, name in enumerate(self.args.imu9AxisNames):
                        if f'imu_9axis_{i}' in closest_indices:
                            idx = closest_indices[f'imu_9axis_{i}']
                            self.imu_9axis_data_time_series[i][idx].to_sync_list()
                            del self.imu_9axis_data_time_series[i][:idx+1]

                    # 添加激光雷达点云数据到同步列表
                    for i, name in enumerate(self.args.lidarPointCloudNames):
                        if f'lidar_point_cloud_{i}' in closest_indices:
                            idx = closest_indices[f'lidar_point_cloud_{i}']
                            self.lidar_point_cloud_data_time_series[i][idx].to_sync_list()
                            del self.lidar_point_cloud_data_time_series[i][:idx+1]

                    # 添加机器人底盘速度数据到同步列表
                    for i, name in enumerate(self.args.robotBaseVelNames):
                        if f'robot_base_vel_{i}' in closest_indices:
                            idx = closest_indices[f'robot_base_vel_{i}']
                            self.robot_base_vel_all_time_series[i][idx].to_sync_list()

                    # 添加升降电机数据到同步列表
                    for i, name in enumerate(self.args.liftMotorNames):
                        if f'lift_motor_{i}' in closest_indices:
                            idx = closest_indices[f'lift_motor_{i}']
                            self.lift_motor_all_time_series[i][idx].to_sync_list()

                    frame_count += 1

        print(f"Sync frame num: {frame_count}")
        if frame_count == 0:
            self.check_data_adequacy(True)

        # 写入同步文件
        self.write_sync_files()

    def write_sync_files(self):
        """写入同步文件"""
        if self.master_sync_times:
            master_sync_path = os.path.join(self.episode_dir, "master_sync.txt")
            with open(master_sync_path, 'w') as f:
                for timestamp in self.master_sync_times:
                    f.write(f"{timestamp:.6f}\n")

        # 写入相机彩色数据同步文件
        for i, name in enumerate(self.args.cameraColorNames):
            sync_file_path = os.path.join(self.camera_color_dirs[i], "sync.txt")
            os.makedirs(os.path.dirname(sync_file_path), exist_ok=True)
            with open(sync_file_path, 'w') as f:
                for time_series in self.camera_color_sync_time_series[i]:
                    f.write(f"{time_series.time:.6f}{self.camera_color_exts[i]}\n")

        # 写入相机深度数据同步文件
        for i, name in enumerate(self.args.cameraDepthNames):
            sync_file_path = os.path.join(self.camera_depth_dirs[i], "sync.txt")
            os.makedirs(os.path.dirname(sync_file_path), exist_ok=True)
            with open(sync_file_path, 'w') as f:
                for time_series in self.camera_depth_sync_time_series[i]:
                    f.write(f"{time_series.time:.6f}{self.camera_depth_exts[i]}\n")

        # 写入机械臂关节状态同步文件
        for i, name in enumerate(self.args.armJointStateNames):
            sync_file_path = os.path.join(self.arm_joint_state_dirs[i], "sync.txt")
            os.makedirs(os.path.dirname(sync_file_path), exist_ok=True)
            with open(sync_file_path, 'w') as f:
                for time_series in self.arm_joint_state_sync_time_series[i]:
                    f.write(f"{time_series.time:.6f}{self.arm_joint_state_exts[i]}\n")

        # 写入定位姿态同步文件
        for i, name in enumerate(self.args.localizationPoseNames):
            sync_file_path = os.path.join(self.localization_pose_dirs[i], "sync.txt")
            os.makedirs(os.path.dirname(sync_file_path), exist_ok=True)
            with open(sync_file_path, 'w') as f:
                for time_series in self.localization_pose_sync_time_series[i]:
                    f.write(f"{time_series.time:.6f}{self.localization_pose_exts[i]}\n")

        # 写入相机点云数据同步文件
        for i, name in enumerate(self.args.cameraPointCloudNames):
            sync_file_path = os.path.join(self.camera_point_cloud_dirs[i], "sync.txt")
            os.makedirs(os.path.dirname(sync_file_path), exist_ok=True)
            with open(sync_file_path, 'w') as f:
                for time_series in self.camera_point_cloud_sync_time_series[i]:
                    f.write(f"{time_series.time:.6f}{self.camera_point_cloud_exts[i]}\n")

        # 写入机械臂末端位姿同步文件
        for i, name in enumerate(self.args.armEndPoseNames):
            sync_file_path = os.path.join(self.arm_end_pose_dirs[i], "sync.txt")
            os.makedirs(os.path.dirname(sync_file_path), exist_ok=True)
            with open(sync_file_path, 'w') as f:
                for time_series in self.arm_end_pose_sync_time_series[i]:
                    f.write(f"{time_series.time:.6f}{self.arm_end_pose_exts[i]}\n")

        # 写入夹爪编码器同步文件
        for i, name in enumerate(self.args.gripperEncoderNames):
            sync_file_path = os.path.join(self.gripper_encoder_dirs[i], "sync.txt")
            os.makedirs(os.path.dirname(sync_file_path), exist_ok=True)
            with open(sync_file_path, 'w') as f:
                for time_series in self.gripper_encoder_sync_time_series[i]:
                    f.write(f"{time_series.time:.6f}{self.gripper_encoder_exts[i]}\n")

        # 写入IMU 9轴数据同步文件
        for i, name in enumerate(self.args.imu9AxisNames):
            sync_file_path = os.path.join(self.imu_9axis_dirs[i], "sync.txt")
            os.makedirs(os.path.dirname(sync_file_path), exist_ok=True)
            with open(sync_file_path, 'w') as f:
                for time_series in self.imu_9axis_sync_time_series[i]:
                    f.write(f"{time_series.time:.6f}{self.imu_9axis_exts[i]}\n")

        # 写入激光雷达点云同步文件
        for i, name in enumerate(self.args.lidarPointCloudNames):
            sync_file_path = os.path.join(self.lidar_point_cloud_dirs[i], "sync.txt")
            os.makedirs(os.path.dirname(sync_file_path), exist_ok=True)
            with open(sync_file_path, 'w') as f:
                for time_series in self.lidar_point_cloud_sync_time_series[i]:
                    f.write(f"{time_series.time:.6f}{self.lidar_point_cloud_exts[i]}\n")

        # 写入机器人底盘速度同步文件
        for i, name in enumerate(self.args.robotBaseVelNames):
            sync_file_path = os.path.join(self.robot_base_vel_dirs[i], "sync.txt")
            os.makedirs(os.path.dirname(sync_file_path), exist_ok=True)
            with open(sync_file_path, 'w') as f:
                for time_series in self.robot_base_vel_sync_time_series[i]:
                    f.write(f"{time_series.time:.6f}{self.robot_base_vel_exts[i]}\n")

        # 写入升降电机同步文件
        for i, name in enumerate(self.args.liftMotorNames):
            sync_file_path = os.path.join(self.lift_motor_dirs[i], "sync.txt")
            os.makedirs(os.path.dirname(sync_file_path), exist_ok=True)
            with open(sync_file_path, 'w') as f:
                for time_series in self.lift_motor_sync_time_series[i]:
                    f.write(f"{time_series.time:.6f}{self.lift_motor_exts[i]}\n")

        self.write_sync_summary()

    def build_camera_color_sync_summary(self) -> List[Dict[str, Union[str, int, float]]]:
        summaries: List[Dict[str, Union[str, int, float]]] = []
        for i, name in enumerate(self.args.cameraColorNames):
            sync_series = self.camera_color_sync_time_series[i]
            method_counts: Dict[str, int] = {}
            original_source_counts: Dict[int, int] = {}
            generated_fill_flags: List[bool] = []
            reused_existing_flags: List[bool] = []
            repair_or_reuse_flags: List[bool] = []
            seen_original_sources: Dict[int, int] = {}
            for time_series in sync_series:
                method = getattr(time_series, "fill_method", "original") or "original"
                method_counts[method] = method_counts.get(method, 0) + 1
                generated_fill = method != "original"
                reused_existing = False
                if method == "original":
                    source_key = round(getattr(time_series, "source_time", time_series.time) * 1_000_000_000)
                    original_source_counts[source_key] = original_source_counts.get(source_key, 0) + 1
                    reused_existing = seen_original_sources.get(source_key, 0) > 0
                    seen_original_sources[source_key] = seen_original_sources.get(source_key, 0) + 1
                generated_fill_flags.append(generated_fill)
                reused_existing_flags.append(reused_existing)
                repair_or_reuse_flags.append(generated_fill or reused_existing)

            reused_existing = sum(max(0, count - 1) for count in original_source_counts.values())
            interpolated = self.camera_color_interpolated_counts[i]
            nearest = self.camera_color_nearest_fill_counts[i]
            existing_synthesized = method_counts.get("existing_synthesized", 0)
            generated_fill = interpolated + nearest + existing_synthesized
            max_generated_fill_run = self.longest_true_run(generated_fill_flags)
            max_reused_existing_run = self.longest_true_run(reused_existing_flags)
            max_repair_or_reuse_run = self.longest_true_run(repair_or_reuse_flags)
            summaries.append(
                {
                    "name": str(name),
                    "source_frame_count": int(self.camera_color_source_counts[i]),
                    "camera_info_count": int(self.camera_info_counts[i]),
                    "camera_info_rate_hz": float(self.camera_info_rates_hz[i]),
                    "synced_frame_count": int(len(sync_series)),
                    "matched_source_frame_count": int(method_counts.get("original", 0)),
                    "unique_matched_source_frame_count": int(len(original_source_counts)),
                    "reused_existing_frame_count": int(reused_existing),
                    "interpolated_fill_count": int(interpolated),
                    "nearest_fill_count": int(nearest),
                    "existing_synthesized_count": int(existing_synthesized),
                    "generated_fill_count": int(generated_fill),
                    "repair_or_reuse_frame_count": int(generated_fill + reused_existing),
                    "max_consecutive_generated_fill_frames": int(max_generated_fill_run["length"]),
                    "max_consecutive_generated_fill_start": int(max_generated_fill_run["start"]),
                    "max_consecutive_generated_fill_end": int(max_generated_fill_run["end"]),
                    "max_consecutive_reused_existing_frames": int(max_reused_existing_run["length"]),
                    "max_consecutive_reused_existing_start": int(max_reused_existing_run["start"]),
                    "max_consecutive_reused_existing_end": int(max_reused_existing_run["end"]),
                    "max_consecutive_repair_or_reuse_frames": int(max_repair_or_reuse_run["length"]),
                    "max_consecutive_repair_or_reuse_start": int(max_repair_or_reuse_run["start"]),
                    "max_consecutive_repair_or_reuse_end": int(max_repair_or_reuse_run["end"]),
                }
            )
        return summaries

    @staticmethod
    def longest_true_run(flags: List[bool]) -> Dict[str, int]:
        best_start = -1
        best_end = -1
        best_len = 0
        run_start = -1
        run_len = 0
        for idx, value in enumerate(flags):
            if value:
                if run_len == 0:
                    run_start = idx
                run_len += 1
                if run_len > best_len:
                    best_len = run_len
                    best_start = run_start
                    best_end = idx
            else:
                run_len = 0
                run_start = -1
        return {"length": best_len, "start": best_start, "end": best_end}

    def write_sync_summary(self):
        summary_path = os.path.join(self.episode_dir, "sync_summary.json")
        summary = {
            "sync_mode": "camera_info_master" if self.camera_info_master_times else "timestamp_window",
            "time_diff_limit": self.args.timeDiffLimit,
            "master_camera_color_name": self.camera_info_master_name,
            "master_frame_count": len(self.master_sync_times) if self.master_sync_times else 0,
            "camera_color": self.build_camera_color_sync_summary(),
        }
        with open(summary_path, "w", encoding="utf-8") as file_obj:
            json.dump(summary, file_obj, indent=2, ensure_ascii=False)
            file_obj.write("\n")

    def process(self):
        self.load_all_time_series()
        self.sync()

def get_arguments(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--datasetDir', action='store', type=str, help='datasetDir',
                       default='data', required=False)
    parser.add_argument('--episodeName', action='store', type=str, help='episodeName',
                       default="", required=False)
    parser.add_argument('--timeDiffLimit', action='store', type=float, help='timeDiffLimit',
                       default=0.03, required=False)
    parser.add_argument('--type', action='store', type=str, help='type',
                       default='aloha', required=False)
    parser.add_argument('--paramsFile', action='store', type=str, help='Explicit topic parameter YAML',
                       default='', required=False)
    parser.add_argument('--cameraColorNames', action='store', type=str, help='cameraColorNames',
                       default=[], required=False)
    parser.add_argument('--cameraDepthNames', action='store', type=str, help='cameraDepthNames',
                       default=[], required=False)
    parser.add_argument('--cameraPointCloudNames', action='store', type=str, help='cameraPointCloudNames',
                       default=[], required=False)
    parser.add_argument('--armJointStateNames', action='store', type=str, help='armJointStateNames',
                       default=[], required=False)
    parser.add_argument('--armEndPoseNames', action='store', type=str, help='armEndPoseNames',
                       default=[], required=False)
    parser.add_argument('--localizationPoseNames', action='store', type=str, help='localizationPoseNames',
                       default=[], required=False)
    parser.add_argument('--gripperEncoderNames', action='store', type=str, help='gripperEncoderNames',
                       default=[], required=False)
    parser.add_argument('--imu9AxisNames', action='store', type=str, help='imu9AxisNames',
                       default=[], required=False)
    parser.add_argument('--lidarPointCloudNames', action='store', type=str, help='lidarPointCloudNames',
                       default=[], required=False)
    parser.add_argument('--robotBaseVelNames', action='store', type=str, help='robotBaseVelNames',
                       default=[], required=False)
    parser.add_argument('--liftMotorNames', action='store', type=str, help='liftMotorNames',
                       default=[], required=False)
    args = parser.parse_args(argv)

    config_path = args.paramsFile or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "topic_configs",
        f"{args.type}_data_params.yaml",
    )
    with open(config_path, 'r') as file:
        yaml_data = yaml.safe_load(file)
        args.cameraColorNames = yaml_data.get('/**', {}).get('ros__parameters', {}).get('dataInfo', {}).get('camera', {}).get('color', {}).get('names', [])
        args.cameraDepthNames = yaml_data.get('/**', {}).get('ros__parameters', {}).get('dataInfo', {}).get('camera', {}).get('depth', {}).get('names', [])
        args.cameraPointCloudNames = yaml_data.get('/**', {}).get('ros__parameters', {}).get('dataInfo', {}).get('camera', {}).get('pointCloud', {}).get('names', [])
        args.armJointStateNames = yaml_data.get('/**', {}).get('ros__parameters', {}).get('dataInfo', {}).get('arm', {}).get('jointState', {}).get('names', [])
        args.armEndPoseNames = yaml_data.get('/**', {}).get('ros__parameters', {}).get('dataInfo', {}).get('arm', {}).get('endPose', {}).get('names', [])
        args.localizationPoseNames = yaml_data.get('/**', {}).get('ros__parameters', {}).get('dataInfo', {}).get('localization', {}).get('pose', {}).get('names', [])
        args.gripperEncoderNames = yaml_data.get('/**', {}).get('ros__parameters', {}).get('dataInfo', {}).get('gripper', {}).get('encoder', {}).get('names', [])
        args.imu9AxisNames = yaml_data.get('/**', {}).get('ros__parameters', {}).get('dataInfo', {}).get('imu', {}).get('9axis', {}).get('names', [])
        args.lidarPointCloudNames = yaml_data.get('/**', {}).get('ros__parameters', {}).get('dataInfo', {}).get('lidar', {}).get('pointCloud', {}).get('names', [])
        args.robotBaseVelNames = yaml_data.get('/**', {}).get('ros__parameters', {}).get('dataInfo', {}).get('robotBase', {}).get('vel', {}).get('names', [])
        args.liftMotorNames = yaml_data.get('/**', {}).get('ros__parameters', {}).get('dataInfo', {}).get('lift', {}).get('motor', {}).get('names', [])
    return args


def main():
    args = get_arguments()
    if args.episodeName == "":
        if not os.path.exists(args.datasetDir):
            print(f"Error: Dataset directory {args.datasetDir} does not exist")
            return
        for f in os.listdir(args.datasetDir):
            if not f.endswith(".tar.gz"):
                args.episodeName = f
                print("episode name:", args.episodeName, "processing")
                operator = Operator(args)
                operator.process()
                print("episode name:", args.episodeName, "done")
    else:
        operator = Operator(args)
        operator.process()

    print("Done")


if __name__ == '__main__':
    main()
