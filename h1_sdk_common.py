#!/usr/bin/env python3
"""Shared constants and offline validation for H1 SDK utility scripts.

Importing this module never loads the vendor SDK and never connects to a robot.
The binary Python SDK is loaded lazily by :func:`load_sdk`.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import math
import os
from pathlib import Path
import sys
from typing import Any, Iterable, Sequence


DEFAULT_SDK_ROOT = Path(
    "/home/robot/workspace/robot_station/sdk/zerith_h1/1.3.9/"
    "h1_sdk_v1.3.9_python3.10"
)
SDK_ROOT_ENV = "ZERITH_H1_PYTHON_SDK_ROOT"
MOTION_CONFIRMATION = "I_UNDERSTAND_H1_WILL_MOVE"
DEFAULT_ZCM_IPC_PATH = Path("/dev/shm/zcm/ipcshm/default")


@dataclass(frozen=True)
class JointSpec:
    name: str
    motor_id: int
    soft_lower: float
    soft_upper: float


LEFT_ARM_JOINTS = (
    JointSpec("left_shoulder_pitch", 7, -2.7, 1.5),
    JointSpec("left_shoulder_roll", 8, -0.3, 2.0),
    JointSpec("left_shoulder_yaw", 9, -2.9, 2.9),
    JointSpec("left_elbow", 10, -1.3, 1.5),
    JointSpec("left_wrist_roll", 11, -2.9, 2.9),
    JointSpec("left_wrist_yaw", 12, -1.0, 1.0),
    JointSpec("left_wrist_pitch", 13, -1.0, 1.0),
)

RIGHT_ARM_JOINTS = (
    JointSpec("right_shoulder_pitch", 15, -2.7, 1.5),
    JointSpec("right_shoulder_roll", 16, -2.0, 0.3),
    JointSpec("right_shoulder_yaw", 17, -2.9, 2.9),
    JointSpec("right_elbow", 18, -1.3, 1.5),
    JointSpec("right_wrist_roll", 19, -2.9, 2.9),
    JointSpec("right_wrist_yaw", 20, -1.0, 1.0),
    JointSpec("right_wrist_pitch", 21, -1.0, 1.0),
)

ARM_JOINTS = {
    "left": LEFT_ARM_JOINTS,
    "right": RIGHT_ARM_JOINTS,
}

GRIPPER_MOTOR_IDS = {"left": 14, "right": 22}
GRIPPER_HARD_LIMIT = (0.0, 1.5)
LIFT_MOTOR_ID = 2
LIFT_POSITION_LIMIT_M = (0.0, 0.8)

MOTOR_NAMES = {
    0: "left_wheel",
    1: "right_wheel",
    2: "lift",
    3: "waist_pitch",
    4: "waist_yaw",
    5: "head_yaw",
    6: "head_pitch",
    **{spec.motor_id: spec.name for spec in LEFT_ARM_JOINTS},
    14: "left_gripper",
    **{spec.motor_id: spec.name for spec in RIGHT_ARM_JOINTS},
    22: "right_gripper",
}

MODE_NAMES = {
    0: "UNINITIALIZED/VR",
    1: "LOW_LEVEL",
    2: "HIGH_LEVEL",
    3: "GRAVITY_COMPENSATION_LEVEL",
}

INIT_STATE_NAMES = {
    0: "Uninit",
    1: "Initializing",
    2: "Init_Complete",
    3: "Deinitializing",
    4: "Deinit_Complete",
    5: "Error_State",
}

HIGH_LEVEL_STATE_NAMES = {
    0: "inactive",
    1: "activating",
    2: "idle",
    3: "executing",
    4: "completed",
    5: "error",
}

MOTOR_ERROR_BITS = {
    0: "disconnected",
    1: "overvoltage",
    2: "undervoltage",
    3: "overheat",
    4: "blocked",
    5: "overcurrent",
    6: "communication_loss",
    7: "overload",
    8: "battery_low",
    9: "overspeed",
    10: "encoder_fault",
    11: "brake_overvoltage",
    12: "driver_fault",
    13: "coil_overtemperature",
    14: "mos_overtemperature",
    15: "other_error",
}


class H1SafetyError(RuntimeError):
    """A local safety gate rejected a command before or during execution."""


def resolve_sdk_root(explicit_root: str | None = None) -> Path:
    candidate = explicit_root or os.environ.get(SDK_ROOT_ENV)
    return Path(candidate).expanduser().resolve() if candidate else DEFAULT_SDK_ROOT


def load_sdk(explicit_root: str | None = None) -> Any:
    """Load the CPython 3.10 vendor binding without constructing H1Robot."""

    if sys.version_info[:2] != (3, 10):
        raise RuntimeError(
            "H1 SDK 1.3.9 requires Python 3.10; current interpreter is "
            f"{sys.version_info.major}.{sys.version_info.minor}. "
            "Activate the conda environment with: conda activate zerith"
        )

    sdk_root = resolve_sdk_root(explicit_root)
    binding = sdk_root / "lib" / "lib_h1_sdk_python.so"
    if not binding.is_file():
        raise FileNotFoundError(
            f"H1 Python binding not found: {binding}. "
            f"Set {SDK_ROOT_ENV} or pass --sdk-root."
        )

    root_text = str(sdk_root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)

    return importlib.import_module("lib.lib_h1_sdk_python")


def check_local_zcm_permissions(
    ipc_path: Path = DEFAULT_ZCM_IPC_PATH,
) -> None:
    """Fail in Python before the vendor C library aborts on an unwritable IPC file."""

    if not ipc_path.exists():
        return
    if os.access(ipc_path, os.R_OK | os.W_OK):
        return

    file_stat = ipc_path.stat()
    raise PermissionError(
        f"ZCM shared-memory file is not readable/writable by uid={os.geteuid()}: "
        f"{ipc_path} mode={file_stat.st_mode & 0o777:04o} "
        f"uid={file_stat.st_uid} gid={file_stat.st_gid}. "
        "SDKService commonly recreates this file as root:root 0644. "
        "Fix it with: sudo chgrp robot /dev/shm/zcm/ipcshm/default && "
        "sudo chmod 664 /dev/shm/zcm/ipcshm/default"
    )


def make_robot(sdk: Any, robot_address: str | None = None) -> Any:
    """Construct H1Robot. This may initialize SDK transport but sends no motion."""

    if not robot_address:
        check_local_zcm_permissions()
    return sdk.H1Robot(robot_address) if robot_address else sdk.H1Robot()


def enum_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raw = getattr(value, "value", value)
        return int(raw)


def motor_index(sdk: Any, motor_id: int) -> Any:
    """Convert an integer ID to the enum required by the actual pybind API.

    The delivered .pyi annotates several motor-id arguments as int, while the
    1.3.9 binary binding rejects plain ints and requires EtherCAT_Motor_Index.
    """

    return sdk.EtherCAT_Motor_Index(int(motor_id))


def require_finite(values: Iterable[float], label: str) -> list[float]:
    result = [float(value) for value in values]
    for index, value in enumerate(result):
        if not math.isfinite(value):
            raise ValueError(f"{label}[{index}] must be finite, got {value!r}")
    return result


def validate_duration(duration: float) -> float:
    duration = float(duration)
    if not 1.0 <= duration <= 30.0:
        raise ValueError("duration must be between 1.0 and 30.0 seconds")
    return duration


def validate_hold_seconds(hold_seconds: float) -> float:
    hold_seconds = float(hold_seconds)
    if not 0.0 <= hold_seconds <= 10.0:
        raise ValueError("hold-seconds must be between 0.0 and 10.0 seconds")
    return hold_seconds


def validate_interpolation_rate(rate_hz: float) -> float:
    rate_hz = float(rate_hz)
    if not 100.0 <= rate_hz <= 500.0:
        raise ValueError("rate must be in the documented 100-500 Hz range")
    return rate_hz


def validate_joint_target(
    arm: str,
    target: Sequence[float],
    *,
    limit_margin: float = 0.02,
) -> list[float]:
    specs = ARM_JOINTS[arm]
    values = require_finite(target, f"{arm}_arm_target")
    if len(values) != len(specs):
        raise ValueError(f"{arm} arm target must contain exactly 7 values")
    if not 0.0 <= limit_margin <= 0.1:
        raise ValueError("limit margin must be between 0.0 and 0.1 rad")

    for spec, value in zip(specs, values):
        lower = spec.soft_lower + limit_margin
        upper = spec.soft_upper - limit_margin
        if not lower <= value <= upper:
            raise H1SafetyError(
                f"{spec.name} target {value:.6f} rad is outside the local "
                f"safe range [{lower:.3f}, {upper:.3f}] rad"
            )
    return values


def validate_max_delta(
    start: Sequence[float],
    target: Sequence[float],
    max_delta: float,
    names: Sequence[str],
    *,
    safety_cap: float = 0.5,
) -> None:
    if max_delta <= 0.0 or max_delta > safety_cap:
        raise ValueError(
            f"max-start-delta must be > 0 and <= {safety_cap:g} rad"
        )
    for name, before, after in zip(names, start, target):
        delta = abs(float(after) - float(before))
        if delta > max_delta:
            raise H1SafetyError(
                f"{name} changes by {delta:.6f} rad, exceeding "
                f"max-start-delta {max_delta:.6f} rad"
            )


def validate_gripper_position(position: float, margin: float = 0.02) -> float:
    position = require_finite([position], "gripper_position")[0]
    lower, upper = GRIPPER_HARD_LIMIT
    if not lower + margin <= position <= upper - margin:
        raise H1SafetyError(
            f"gripper target {position:.6f} rad is outside the local safe "
            f"range [{lower + margin:.3f}, {upper - margin:.3f}] rad"
        )
    return position


def validate_lift_position(position_m: float) -> float:
    position_m = require_finite([position_m], "lift_position_m")[0]
    lower, upper = LIFT_POSITION_LIMIT_M
    if not lower <= position_m <= upper:
        raise H1SafetyError(
            f"lift target {position_m:.6f} m is outside the documented range "
            f"[{lower:.3f}, {upper:.3f}] m"
        )
    return position_m


def validate_quaternion(values: Sequence[float], tolerance: float = 0.02) -> list[float]:
    quaternion = require_finite(values, "quaternion")
    if len(quaternion) != 4:
        raise ValueError("quaternion must contain [qx, qy, qz, qw]")
    norm = math.sqrt(sum(value * value for value in quaternion))
    if abs(norm - 1.0) > tolerance:
        raise H1SafetyError(
            f"quaternion norm is {norm:.6f}; expected 1.0 +/- {tolerance}"
        )
    return quaternion


def vector_distance(first: Sequence[float], second: Sequence[float]) -> float:
    if len(first) != len(second):
        raise ValueError("vector lengths differ")
    return math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(first, second)))


def quaternion_angular_distance(first: Sequence[float], second: Sequence[float]) -> float:
    first_q = validate_quaternion(first)
    second_q = validate_quaternion(second)
    dot = abs(sum(a * b for a, b in zip(first_q, second_q)))
    return 2.0 * math.acos(max(-1.0, min(1.0, dot)))


def decode_motor_error(error_flag: int) -> list[str]:
    flag = int(error_flag)
    return [name for bit, name in MOTOR_ERROR_BITS.items() if flag & (1 << bit)]


def motor_state_to_dict(state: Any) -> dict[str, Any]:
    flag = int(state.Error_flag)
    return {
        "position": float(state.Position_Actual),
        "speed": float(state.Speed_Actual),
        "torque": float(state.Torque_Actual),
        "kp": float(state.KP_Actual),
        "kd": float(state.KD_Actual),
        "error_flag": flag,
        "errors": decode_motor_error(flag),
    }


def arm_specs(arm: str) -> tuple[JointSpec, ...]:
    try:
        return ARM_JOINTS[arm]
    except KeyError as exc:
        raise ValueError("arm must be 'left' or 'right'") from exc


def read_arm_positions(robot: Any, sdk: Any, arm: str) -> list[float]:
    positions: list[float] = []
    for spec in arm_specs(arm):
        ok, state = robot.getArmState(motor_index(sdk, spec.motor_id))
        if not ok:
            raise RuntimeError(f"getArmState({spec.motor_id}) failed")
        if int(state.Error_flag) != 0:
            raise H1SafetyError(
                f"{spec.name} error_flag=0x{int(state.Error_flag):04x}: "
                f"{decode_motor_error(int(state.Error_flag))}"
            )
        positions.append(float(state.Position_Actual))
    return require_finite(positions, f"{arm}_arm_feedback")


def pose_to_dict(pose: Any) -> dict[str, list[float]]:
    return {
        "position": [float(value) for value in pose.position],
        "rotation_qx_qy_qz_qw": [float(value) for value in pose.rotation],
    }
