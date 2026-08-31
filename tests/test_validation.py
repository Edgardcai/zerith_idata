from __future__ import annotations

import math
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock


CONTROL_DIR = Path(__file__).resolve().parents[1]
if str(CONTROL_DIR) not in sys.path:
    sys.path.insert(0, str(CONTROL_DIR))

from h1_sdk_common import (  # noqa: E402
    H1SafetyError,
    check_local_zcm_permissions,
    decode_motor_error,
    motor_index,
    quaternion_angular_distance,
    validate_gripper_position,
    validate_interpolation_rate,
    validate_joint_target,
    validate_lift_position,
    validate_max_delta,
    validate_quaternion,
)


class ValidationTests(unittest.TestCase):
    def test_left_zero_joint_target_is_inside_local_limits(self) -> None:
        self.assertEqual(
            validate_joint_target("left", [0.0] * 7),
            [0.0] * 7,
        )

    def test_right_shoulder_roll_uses_asymmetric_limit(self) -> None:
        with self.assertRaises(H1SafetyError):
            validate_joint_target("right", [0.0, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0])

    def test_joint_soft_limit_margin_is_enforced(self) -> None:
        with self.assertRaises(H1SafetyError):
            validate_joint_target("left", [-2.7, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])

    def test_non_finite_joint_target_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_joint_target("left", [0.0, 0.0, math.nan, 0.0, 0.0, 0.0, 0.0])

    def test_max_start_delta_is_enforced(self) -> None:
        with self.assertRaises(H1SafetyError):
            validate_max_delta([0.0], [0.2], 0.15, ["joint"])

    def test_documented_interpolation_range(self) -> None:
        self.assertEqual(validate_interpolation_rate(100.0), 100.0)
        self.assertEqual(validate_interpolation_rate(500.0), 500.0)
        with self.assertRaises(ValueError):
            validate_interpolation_rate(30.0)

    def test_gripper_local_margin(self) -> None:
        self.assertEqual(validate_gripper_position(0.5), 0.5)
        with self.assertRaises(H1SafetyError):
            validate_gripper_position(0.0)
        with self.assertRaises(H1SafetyError):
            validate_gripper_position(1.5)

    def test_lift_position_documented_endpoints(self) -> None:
        self.assertEqual(validate_lift_position(0.0), 0.0)
        self.assertEqual(validate_lift_position(0.4), 0.4)
        self.assertEqual(validate_lift_position(0.8), 0.8)
        with self.assertRaises(H1SafetyError):
            validate_lift_position(-0.001)
        with self.assertRaises(H1SafetyError):
            validate_lift_position(0.801)

    def test_quaternion_validation_and_distance(self) -> None:
        identity = validate_quaternion([0.0, 0.0, 0.0, 1.0])
        same_rotation = [0.0, 0.0, 0.0, -1.0]
        self.assertAlmostEqual(
            quaternion_angular_distance(identity, same_rotation),
            0.0,
        )
        quarter_turn = [0.0, 0.0, math.sin(math.pi / 4), math.cos(math.pi / 4)]
        self.assertAlmostEqual(
            quaternion_angular_distance(identity, quarter_turn),
            math.pi / 2,
        )
        with self.assertRaises(H1SafetyError):
            validate_quaternion([0.0, 0.0, 0.0, 2.0])

    def test_motor_error_bits(self) -> None:
        self.assertEqual(
            decode_motor_error((1 << 0) | (1 << 3)),
            ["disconnected", "overheat"],
        )

    def test_motor_id_is_converted_to_sdk_enum(self) -> None:
        class FakeMotorIndex(int):
            pass

        class FakeSdk:
            EtherCAT_Motor_Index = FakeMotorIndex

        converted = motor_index(FakeSdk, 13)
        self.assertIsInstance(converted, FakeMotorIndex)
        self.assertEqual(int(converted), 13)

    def test_unwritable_zcm_file_is_rejected_before_sdk_constructor(self) -> None:
        fake_path = mock.Mock()
        fake_path.exists.return_value = True
        fake_path.stat.return_value.st_mode = 0o100644
        fake_path.stat.return_value.st_uid = 0
        fake_path.stat.return_value.st_gid = 0
        with mock.patch.object(os, "access", return_value=False):
            with self.assertRaises(PermissionError):
                check_local_zcm_permissions(fake_path)

    def test_joint_cli_defaults_to_offline_dry_run(self) -> None:
        command = [
            sys.executable,
            str(CONTROL_DIR / "send_robot_command.py"),
            "joint",
            "--arm",
            "left",
            "--delta",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0.02",
        ]
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DRY-RUN ONLY", result.stdout)
        self.assertIn('"will_connect": false', result.stdout)

    def test_dual_joint_closed_gripper_dry_run(self) -> None:
        command = [
            sys.executable,
            str(CONTROL_DIR / "send_robot_command.py"),
            "dual-joint",
            "--left-target",
            "0", "0", "0", "-1.20", "0", "0", "0.98",
            "--right-target",
            "0", "0", "0", "-1.20", "0", "0", "0.98",
            "--gripper-position",
            "0.02",
            "--lift-position",
            "0.40",
            "--duration",
            "8",
            "--rate",
            "100",
            "--hold-until-enter",
        ]
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"command": "dual-joint"', result.stdout)
        self.assertIn('"both_grippers_target_rad": 0.02', result.stdout)
        self.assertIn('"lift_target_m": 0.4', result.stdout)
        self.assertIn('"hold": "until Enter, then robot_deinit"', result.stdout)
        self.assertIn("DRY-RUN ONLY", result.stdout)

    def test_operational_initial_pose_preset_dry_run(self) -> None:
        command = [
            sys.executable,
            str(CONTROL_DIR / "send_robot_command.py"),
            "initial-pose",
        ]
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"command": "initial-pose"', result.stdout)
        self.assertIn('"left_target_rad": [', result.stdout)
        self.assertIn('"both_grippers_target_rad": 0.02', result.stdout)
        self.assertIn('"lift_target_m": 0.4', result.stdout)
        self.assertIn('"hold": "until Enter, then robot_deinit"', result.stdout)
        self.assertIn("application-level pose", result.stdout)
        self.assertIn("DRY-RUN ONLY", result.stdout)


if __name__ == "__main__":
    unittest.main()
