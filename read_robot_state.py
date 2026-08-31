#!/usr/bin/env python3
"""Read ZERITH H1 state without switching mode or initializing the robot."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import sys
import time
from typing import Any, Callable

from h1_sdk_common import (
    HIGH_LEVEL_STATE_NAMES,
    INIT_STATE_NAMES,
    MODE_NAMES,
    MOTOR_NAMES,
    enum_int,
    load_sdk,
    make_robot,
    motor_index,
    motor_state_to_dict,
    pose_to_dict,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read H1 robot state. This script calls robot_connect() only; it does "
            "not switch control mode, call robot_init(), or send motion commands."
        )
    )
    parser.add_argument("--sdk-root", help="H1 Python SDK root; defaults to SDK 1.3.9")
    parser.add_argument(
        "--robot-address",
        help=(
            "Optional value passed verbatim to H1Robot(...). Omit when running "
            "on the robot host with the local SDK service."
        ),
    )
    parser.add_argument("--rate", type=float, default=1.0, help="Polling rate in Hz (0.1-30)")
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="Number of snapshots; 0 means run until Ctrl+C",
    )
    parser.add_argument("--compact", action="store_true", help="Print one JSON object per line")
    parser.add_argument("--output", type=Path, help="Also append snapshots as JSONL")
    parser.add_argument("--include-joystick", action="store_true")
    parser.add_argument(
        "--include-dexterous-hands",
        action="store_true",
        help="Poll getHandState; enable only when dexterous hands are installed",
    )
    parser.add_argument(
        "--include-force",
        action="store_true",
        help="Poll six-axis force sensors; H1 PRO normally does not have MAX sensors",
    )
    return parser


def _read_object(
    snapshot: dict[str, Any],
    errors: list[str],
    key: str,
    function: Callable[[], Any],
    serializer: Callable[[Any], Any],
) -> None:
    try:
        ok, value = function()
        if ok:
            snapshot[key] = serializer(value)
        else:
            errors.append(f"{key}: SDK returned ok=false")
    except Exception as exc:  # Vendor SDK exceptions are not documented by type.
        errors.append(f"{key}: {type(exc).__name__}: {exc}")


def _plain_object(value: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: getattr(value, field) for field in fields}


def _json_safe(value: Any) -> Any:
    """Convert non-finite SDK floats to JSON null instead of crashing a monitor."""

    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


def collect_snapshot(robot: Any, sdk: Any, args: argparse.Namespace) -> dict[str, Any]:
    errors: list[str] = []
    mode = enum_int(robot.getCurrentMode())
    init_state = enum_int(robot.getInitState())

    snapshot: dict[str, Any] = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "monotonic_ns": time.monotonic_ns(),
        "connected": bool(robot.isRobotConnected()),
        "control_mode": {"value": mode, "name": MODE_NAMES.get(mode, "unknown")},
        "init_state": {
            "value": init_state,
            "name": INIT_STATE_NAMES.get(init_state, "unknown"),
        },
    }

    _read_object(
        snapshot,
        errors,
        "robot_info",
        robot.getRobotInfo,
        lambda value: _plain_object(
            value,
            (
                "robot_name",
                "robot_type",
                "firmware_version",
                "hardware_version",
                "software_version",
                "manufacturer",
            ),
        ),
    )
    _read_object(
        snapshot,
        errors,
        "power",
        robot.getPowerChargeState,
        lambda value: {
            "soc_percent": int(value.soc),
            "temperature": int(value.temperature),
            "status": int(value.status),
        },
    )
    _read_object(
        snapshot,
        errors,
        "imu",
        robot.getIMU_State,
        lambda value: {
            "rpy_rad": list(value.rpy),
            "omega_rad_s": list(value.omega),
            "accel_m_s2": list(value.accel),
            "quaternion_wxyz": list(value.quat),
            "error_flag": int(value.error_flag),
        },
    )

    motors: dict[str, Any] = {}
    for motor_id in range(23):
        name = MOTOR_NAMES[motor_id]
        try:
            ok, state = robot.getMotorState(motor_index(sdk, motor_id))
            if ok:
                motors[name] = {"motor_id": motor_id, **motor_state_to_dict(state)}
            else:
                errors.append(f"motor[{motor_id}:{name}]: SDK returned ok=false")
        except Exception as exc:
            errors.append(f"motor[{motor_id}:{name}]: {type(exc).__name__}: {exc}")
    snapshot["motors"] = motors

    try:
        ok, wheel_speed, algorithm_speed = robot.getChassisSpeedState()
        if ok:
            snapshot["chassis_speed"] = {
                "wheel_actual": list(wheel_speed),
                "algorithm": {
                    "linear_m_s": float(algorithm_speed[0]),
                    "angular_rad_s": float(algorithm_speed[1]),
                },
            }
        else:
            errors.append("chassis_speed: SDK returned ok=false")
    except Exception as exc:
        errors.append(f"chassis_speed: {type(exc).__name__}: {exc}")

    arm_relative: dict[str, Any] = {}
    for arm_name, arm_value in (
        ("left", sdk.ArmAction.LEFT_ARM),
        ("right", sdk.ArmAction.RIGHT_ARM),
    ):
        try:
            ok, pose = robot.getHandRelative(arm_value)
            if ok:
                arm_relative[arm_name] = pose_to_dict(pose)
            else:
                errors.append(f"arm_relative.{arm_name}: SDK returned ok=false")
        except Exception as exc:
            errors.append(f"arm_relative.{arm_name}: {type(exc).__name__}: {exc}")
    snapshot["arm_relative"] = arm_relative

    _read_object(snapshot, errors, "head_relative", robot.getHeadRelative, pose_to_dict)
    _read_object(
        snapshot,
        errors,
        "head_camera_relative",
        robot.getHeadCameraRelative,
        pose_to_dict,
    )

    hand_camera_relative: dict[str, Any] = {}
    for arm_name, arm_value in (
        ("left", sdk.ArmAction.LEFT_ARM),
        ("right", sdk.ArmAction.RIGHT_ARM),
    ):
        try:
            ok, pose = robot.getHandCameraRelative(arm_value)
            if ok:
                hand_camera_relative[arm_name] = pose_to_dict(pose)
            else:
                errors.append(f"hand_camera_relative.{arm_name}: SDK returned ok=false")
        except Exception as exc:
            errors.append(f"hand_camera_relative.{arm_name}: {type(exc).__name__}: {exc}")
    snapshot["hand_camera_relative"] = hand_camera_relative

    try:
        ok, gripper_mode = robot.getGripperControlMode()
        if ok:
            snapshot["gripper_control_mode"] = {
                "value": int(gripper_mode),
                "name": "hold_torque" if int(gripper_mode) == 0 else "free_mit",
            }
        else:
            errors.append("gripper_control_mode: SDK returned ok=false")
    except Exception as exc:
        errors.append(f"gripper_control_mode: {type(exc).__name__}: {exc}")

    try:
        ok, fixed_rod = robot.getFixedRodState()
        if ok:
            snapshot["fixed_rod"] = {
                "value": int(fixed_rod),
                "name": "fixed/down" if int(fixed_rod) == 1 else "retracted/up",
            }
        else:
            errors.append("fixed_rod: SDK returned ok=false")
    except Exception as exc:
        errors.append(f"fixed_rod: {type(exc).__name__}: {exc}")

    _read_object(
        snapshot,
        errors,
        "high_level_state",
        robot.getHighLevelState,
        lambda value: {
            "state": int(value.state),
            "state_name": HIGH_LEVEL_STATE_NAMES.get(int(value.state), "unknown"),
            "progress_percent": int(value.progress),
        },
    )

    if args.include_joystick:
        joystick_fields = (
            "a", "b", "x", "y", "leftShoulder", "rightShoulder",
            "leftStick", "rightStick", "start", "back", "dpadUp",
            "dpadDown", "dpadLeft", "dpadRight", "leftX", "leftY",
            "rightX", "rightY", "leftTrigger", "rightTrigger", "error_flag",
        )
        _read_object(
            snapshot,
            errors,
            "joystick",
            robot.getJoystickState,
            lambda value: _plain_object(value, joystick_fields),
        )

    if args.include_dexterous_hands:
        hands: dict[str, Any] = {}
        for arm_name, arm_value in (
            ("left", sdk.ArmAction.LEFT_ARM),
            ("right", sdk.ArmAction.RIGHT_ARM),
        ):
            try:
                ok, hand = robot.getHandState(arm_value)
                if ok:
                    hands[arm_name] = {
                        "position": list(hand.hand_position),
                        "angle_x100": list(hand.hand_angle),
                        "current_ma": list(hand.hand_current),
                        "status": list(hand.hand_status),
                        "hand_type": int(hand.hand_type),
                        "error_flag": int(hand.error_flag),
                    }
                else:
                    errors.append(f"hands.{arm_name}: SDK returned ok=false")
            except Exception as exc:
                errors.append(f"hands.{arm_name}: {type(exc).__name__}: {exc}")
        snapshot["dexterous_hands"] = hands

    if args.include_force:
        _read_object(
            snapshot,
            errors,
            "force6",
            robot.getForceSensorState,
            lambda value: {
                "fx": list(value.Fx),
                "fy": list(value.Fy),
                "fz": list(value.Fz),
                "tx": list(value.Tx),
                "ty": list(value.Ty),
                "tz": list(value.Tz),
                "error_flag": list(value.Error_flag),
            },
        )

    snapshot["errors"] = errors
    return snapshot


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if not 0.1 <= args.rate <= 30.0:
        parser.error("--rate must be between 0.1 and 30 Hz")
    if args.count < 0:
        parser.error("--count must be >= 0")

    try:
        sdk = load_sdk(args.sdk_root)
        robot = make_robot(sdk, args.robot_address)
        if not robot.robot_connect():
            print("ERROR: robot_connect() returned false", file=sys.stderr)
            return 2
    except Exception as exc:
        print(f"ERROR: failed to create/connect H1 SDK: {exc}", file=sys.stderr)
        return 2

    interval = 1.0 / args.rate
    produced = 0
    try:
        while args.count == 0 or produced < args.count:
            started = time.monotonic()
            snapshot = _json_safe(collect_snapshot(robot, sdk, args))
            encoded = json.dumps(
                snapshot,
                ensure_ascii=False,
                indent=None if args.compact or args.count != 1 else 2,
                separators=(",", ":") if args.compact else None,
                allow_nan=False,
            )
            print(encoded, flush=True)

            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(snapshot, ensure_ascii=False, allow_nan=False))
                    stream.write("\n")

            produced += 1
            remaining = interval - (time.monotonic() - started)
            if remaining > 0 and (args.count == 0 or produced < args.count):
                time.sleep(remaining)
    except KeyboardInterrupt:
        print("\nStopped by operator; no robot mode or init state was changed.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
