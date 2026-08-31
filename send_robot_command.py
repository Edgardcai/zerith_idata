#!/usr/bin/env python3
"""Guarded one-shot H1 arm/gripper command utility.

The default behavior is offline validation only. Real SDK calls require both
--execute and the exact --confirm-motion phrase printed by --help.
"""

from __future__ import annotations

import argparse
import json
import math
import select
import sys
import time
from typing import Any, Sequence

from h1_sdk_common import (
    GRIPPER_MOTOR_IDS,
    H1SafetyError,
    INIT_STATE_NAMES,
    MODE_NAMES,
    MOTION_CONFIRMATION,
    LIFT_MOTOR_ID,
    arm_specs,
    decode_motor_error,
    enum_int,
    load_sdk,
    make_robot,
    motor_index,
    pose_to_dict,
    quaternion_angular_distance,
    read_arm_positions,
    require_finite,
    validate_duration,
    validate_gripper_position,
    validate_hold_seconds,
    validate_interpolation_rate,
    validate_joint_target,
    validate_lift_position,
    validate_max_delta,
    validate_quaternion,
    vector_distance,
)


# This is an application-level working pose. The vendor robot_init() API has no
# target arguments, so these values are applied only after robot_init succeeds.
OPERATIONAL_INITIAL_LEFT_TARGET = (0.0, 0.0, 0.0, -1.20, 0.0, 0.0, 0.98)
OPERATIONAL_INITIAL_RIGHT_TARGET = (0.0, 0.0, 0.0, -1.20, 0.0, 0.0, 0.98)
OPERATIONAL_INITIAL_GRIPPER_POSITION = 0.02
OPERATIONAL_INITIAL_LIFT_POSITION = 0.40


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate or execute one guarded H1 command. Without --execute this "
            "program never loads the SDK and never connects to the robot."
        )
    )
    parser.add_argument("--sdk-root", help="H1 Python SDK root; defaults to SDK 1.3.9")
    parser.add_argument(
        "--robot-address",
        help="Optional value passed verbatim to H1Robot(...); omit on robot host",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually connect, switch mode, initialize, and execute the command",
    )
    parser.add_argument(
        "--confirm-motion",
        default="",
        help=f"Required with --execute; exact value: {MOTION_CONFIRMATION}",
    )
    parser.add_argument(
        "--countdown",
        type=float,
        default=5.0,
        help="Seconds before switching mode/initializing (minimum 3, default 5)",
    )

    commands = parser.add_subparsers(dest="command", required=True)

    joint = commands.add_parser(
        "joint",
        help="LOW_LEVEL: send seven arm joint positions with interpolation",
    )
    joint.add_argument("--arm", choices=("left", "right"), required=True)
    joint_target = joint.add_mutually_exclusive_group(required=True)
    joint_target.add_argument(
        "--target",
        nargs=7,
        type=float,
        metavar=("Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7"),
        help="Absolute seven-joint target in radians",
    )
    joint_target.add_argument(
        "--delta",
        nargs=7,
        type=float,
        metavar=("D1", "D2", "D3", "D4", "D5", "D6", "D7"),
        help="Delta added to the seven measured joint positions, in radians",
    )
    joint.add_argument("--duration", type=float, default=3.0)
    joint.add_argument("--rate", type=float, default=100.0)
    joint.add_argument("--hold-seconds", type=float, default=1.0)
    joint.add_argument(
        "--max-start-delta",
        type=float,
        default=0.15,
        help="Reject any joint farther from measured start (default 0.15 rad)",
    )
    joint.add_argument(
        "--limit-margin",
        type=float,
        default=0.02,
        help="Extra margin inside documented soft limits (default 0.02 rad)",
    )

    dual_joint = commands.add_parser(
        "dual-joint",
        help="LOW_LEVEL: interpolate both 7-DOF arms and then command both grippers",
    )
    dual_joint.add_argument(
        "--left-target",
        nargs=7,
        type=float,
        required=True,
        metavar=("LQ1", "LQ2", "LQ3", "LQ4", "LQ5", "LQ6", "LQ7"),
        help="Absolute left-arm target in radians",
    )
    dual_joint.add_argument(
        "--right-target",
        nargs=7,
        type=float,
        required=True,
        metavar=("RQ1", "RQ2", "RQ3", "RQ4", "RQ5", "RQ6", "RQ7"),
        help="Absolute right-arm target in radians",
    )
    dual_joint.add_argument(
        "--gripper-position",
        type=float,
        required=True,
        help="Absolute position applied to both grippers, in radians",
    )
    dual_joint.add_argument(
        "--lift-position",
        type=float,
        help="Optional lift-column position in metres (documented range 0-0.8)",
    )
    dual_joint.add_argument("--duration", type=float, default=8.0)
    dual_joint.add_argument("--rate", type=float, default=100.0)
    dual_joint.add_argument("--hold-seconds", type=float, default=3.0)
    dual_joint.add_argument(
        "--max-start-delta",
        type=float,
        default=1.5,
        help="Reject any arm joint farther from measured start (maximum 1.5 rad)",
    )
    dual_joint.add_argument(
        "--max-gripper-start-delta",
        type=float,
        default=1.5,
        help="Reject gripper target farther from measured start (maximum 1.5 rad)",
    )
    dual_joint.add_argument(
        "--hold-until-enter",
        action="store_true",
        help=(
            "Continuously hold arm/lift/gripper targets until Enter is pressed; "
            "then perform normal robot_deinit"
        ),
    )
    dual_joint.add_argument(
        "--limit-margin",
        type=float,
        default=0.02,
        help="Extra margin inside documented arm soft limits (default 0.02 rad)",
    )

    initial_pose = commands.add_parser(
        "initial-pose",
        help=(
            "Move to the saved application-level initial pose after vendor "
            "robot_init, then hold until Enter"
        ),
    )
    initial_pose.add_argument("--duration", type=float, default=8.0)
    initial_pose.add_argument("--rate", type=float, default=100.0)
    initial_pose.set_defaults(
        left_target=OPERATIONAL_INITIAL_LEFT_TARGET,
        right_target=OPERATIONAL_INITIAL_RIGHT_TARGET,
        gripper_position=OPERATIONAL_INITIAL_GRIPPER_POSITION,
        lift_position=OPERATIONAL_INITIAL_LIFT_POSITION,
        hold_seconds=3.0,
        max_start_delta=1.5,
        max_gripper_start_delta=1.5,
        hold_until_enter=True,
        limit_margin=0.02,
    )

    pose = commands.add_parser(
        "pose",
        help="HIGH_LEVEL: send an arm end pose using setArmMove_high",
    )
    pose.add_argument("--arm", choices=("left", "right"), required=True)
    pose_target = pose.add_mutually_exclusive_group(required=True)
    pose_target.add_argument(
        "--relative-position",
        nargs=3,
        type=float,
        metavar=("DX", "DY", "DZ"),
        help="Offset from current relative pose in metres; keeps current rotation",
    )
    pose_target.add_argument(
        "--position",
        nargs=3,
        type=float,
        metavar=("X", "Y", "Z"),
        help="Absolute position in the SDK relative-zero frame, metres",
    )
    pose.add_argument(
        "--quaternion",
        nargs=4,
        type=float,
        metavar=("QX", "QY", "QZ", "QW"),
        help="Required with --position; target quaternion in qx qy qz qw order",
    )
    pose.add_argument("--duration", type=float, default=3.0)
    pose.add_argument("--hold-seconds", type=float, default=1.0)
    pose.add_argument(
        "--max-cartesian-delta",
        type=float,
        default=0.05,
        help="Maximum distance from measured pose (default 0.05 m)",
    )
    pose.add_argument(
        "--max-rotation-delta",
        type=float,
        default=0.25,
        help="Maximum quaternion angular distance (default 0.25 rad)",
    )

    gripper = commands.add_parser(
        "gripper",
        help="Send one left/right gripper position with hold-torque enabled",
    )
    gripper.add_argument("--arm", choices=("left", "right"), required=True)
    gripper_target = gripper.add_mutually_exclusive_group(required=True)
    gripper_target.add_argument("--position", type=float, help="Absolute position in rad")
    gripper_target.add_argument("--delta", type=float, help="Delta from measured position in rad")
    gripper.add_argument(
        "--control-mode",
        choices=("high", "low"),
        default="high",
        help="SDK mode/interface to use (default high)",
    )
    gripper.add_argument("--hold-seconds", type=float, default=1.0)
    gripper.add_argument(
        "--max-start-delta",
        type=float,
        default=0.15,
        help="Reject target farther from measured position (default 0.15 rad)",
    )

    commands.add_parser(
        "deinit",
        help="Explicitly call robot_deinit on an initialized robot; this also moves",
    )
    return parser


def validate_offline(args: argparse.Namespace) -> dict[str, Any]:
    if args.countdown < 3.0 or args.countdown > 30.0:
        raise ValueError("countdown must be between 3 and 30 seconds")

    plan: dict[str, Any] = {
        "dry_run": not args.execute,
        "command": args.command,
        "will_connect": bool(args.execute),
        "will_switch_mode": bool(args.execute and args.command != "deinit"),
        "will_call_robot_init": bool(args.execute and args.command != "deinit"),
        "will_call_robot_deinit_after_success": bool(
            args.execute and args.command != "deinit"
        ),
        "warning": (
            "robot_init and robot_deinit both cause physical motion; VR/teleop "
            "must not control the robot at the same time"
        ),
    }

    if args.command == "joint":
        duration = validate_duration(args.duration)
        rate = validate_interpolation_rate(args.rate)
        hold_seconds = validate_hold_seconds(args.hold_seconds)
        if args.max_start_delta <= 0.0 or args.max_start_delta > 0.5:
            raise ValueError("max-start-delta must be > 0 and <= 0.5 rad")
        if not 0.0 <= args.limit_margin <= 0.1:
            raise ValueError("limit-margin must be between 0.0 and 0.1 rad")
        if args.target is not None:
            target = validate_joint_target(
                args.arm,
                args.target,
                limit_margin=args.limit_margin,
            )
            target_kind = "absolute"
        else:
            target = require_finite(args.delta, "joint_delta")
            if any(abs(value) > args.max_start_delta for value in target):
                raise H1SafetyError(
                    "a joint delta exceeds --max-start-delta before robot connection"
                )
            target_kind = "delta_from_measured_state"
        plan.update(
            {
                "required_mode": "LOW_LEVEL",
                "arm": args.arm,
                "joint_order": [spec.name for spec in arm_specs(args.arm)],
                "target_kind": target_kind,
                "values_rad": target,
                "duration_s": duration,
                "rate_hz": rate,
                "hold_s": hold_seconds,
                "max_start_delta_rad": args.max_start_delta,
                "limit_margin_rad": args.limit_margin,
            }
        )

    elif args.command in ("dual-joint", "initial-pose"):
        duration = validate_duration(args.duration)
        rate = validate_interpolation_rate(args.rate)
        hold_seconds = validate_hold_seconds(args.hold_seconds)
        if not 0.0 < args.max_start_delta <= 1.5:
            raise ValueError("max-start-delta must be > 0 and <= 1.5 rad")
        if not 0.0 < args.max_gripper_start_delta <= 1.5:
            raise ValueError(
                "max-gripper-start-delta must be > 0 and <= 1.5 rad"
            )
        if not 0.0 <= args.limit_margin <= 0.1:
            raise ValueError("limit-margin must be between 0.0 and 0.1 rad")
        left_target = validate_joint_target(
            "left",
            args.left_target,
            limit_margin=args.limit_margin,
        )
        right_target = validate_joint_target(
            "right",
            args.right_target,
            limit_margin=args.limit_margin,
        )
        gripper_target = validate_gripper_position(args.gripper_position)
        lift_target = (
            validate_lift_position(args.lift_position)
            if args.lift_position is not None
            else None
        )
        plan.update(
            {
                "required_mode": "LOW_LEVEL",
                "arm_command_order": "left then right within each interpolation cycle",
                "left_joint_order": [spec.name for spec in arm_specs("left")],
                "right_joint_order": [spec.name for spec in arm_specs("right")],
                "left_target_rad": left_target,
                "right_target_rad": right_target,
                "both_grippers_target_rad": gripper_target,
                "lift_target_m": lift_target,
                "gripper_timing": "after both arms reach target",
                "gripper_hold_torque": True,
                "duration_s": duration,
                "rate_hz": rate,
                "hold": (
                    "until Enter, then robot_deinit"
                    if args.hold_until_enter
                    else f"{hold_seconds:g} seconds, then robot_deinit"
                ),
                "max_start_delta_rad": args.max_start_delta,
                "max_gripper_start_delta_rad": args.max_gripper_start_delta,
                "limit_margin_rad": args.limit_margin,
            }
        )
        if args.command == "initial-pose":
            plan["preset_scope"] = (
                "application-level pose applied after vendor robot_init; "
                "does not replace the vendor initialization trajectory"
            )

    elif args.command == "pose":
        duration = validate_duration(args.duration)
        hold_seconds = validate_hold_seconds(args.hold_seconds)
        if not 0.001 <= args.max_cartesian_delta <= 0.20:
            raise ValueError("max-cartesian-delta must be between 0.001 and 0.20 m")
        if not 0.01 <= args.max_rotation_delta <= 0.5:
            raise ValueError("max-rotation-delta must be between 0.01 and 0.5 rad")

        if args.relative_position is not None:
            offset = require_finite(args.relative_position, "relative_position")
            if vector_distance(offset, (0.0, 0.0, 0.0)) > args.max_cartesian_delta:
                raise H1SafetyError(
                    "relative position exceeds --max-cartesian-delta"
                )
            if args.quaternion is not None:
                raise ValueError(
                    "--quaternion is not accepted with --relative-position; "
                    "the current rotation is preserved"
                )
            plan_target: dict[str, Any] = {
                "kind": "delta_from_measured_pose",
                "position_delta_m": offset,
                "rotation": "keep_current",
            }
        else:
            if args.quaternion is None:
                raise ValueError("--quaternion is required with absolute --position")
            position = require_finite(args.position, "position")
            quaternion = validate_quaternion(args.quaternion)
            plan_target = {
                "kind": "absolute_in_sdk_relative_zero_frame",
                "position_m": position,
                "quaternion_qx_qy_qz_qw": quaternion,
            }

        plan.update(
            {
                "required_mode": "HIGH_LEVEL",
                "arm": args.arm,
                "target": plan_target,
                "duration_s": duration,
                "hold_s": hold_seconds,
                "max_cartesian_delta_m": args.max_cartesian_delta,
                "max_rotation_delta_rad": args.max_rotation_delta,
            }
        )

    elif args.command == "gripper":
        hold_seconds = validate_hold_seconds(args.hold_seconds)
        if args.max_start_delta <= 0.0 or args.max_start_delta > 0.5:
            raise ValueError("max-start-delta must be > 0 and <= 0.5 rad")
        if args.position is not None:
            value = validate_gripper_position(args.position)
            target_kind = "absolute"
        else:
            value = require_finite([args.delta], "gripper_delta")[0]
            if abs(value) > args.max_start_delta:
                raise H1SafetyError("gripper delta exceeds --max-start-delta")
            target_kind = "delta_from_measured_state"
        plan.update(
            {
                "required_mode": f"{args.control_mode.upper()}_LEVEL",
                "arm": args.arm,
                "target_kind": target_kind,
                "value_rad": value,
                "hold_torque": True,
                "hold_s": hold_seconds,
                "max_start_delta_rad": args.max_start_delta,
            }
        )

    elif args.command == "deinit":
        plan.update(
            {
                "required_mode": "current",
                "action": "robot_deinit only if init_state is Init_Complete",
            }
        )

    return plan


def countdown(seconds: float) -> None:
    print(
        "Physical motion is about to begin. Verify 2 m clearance, emergency "
        "stop readiness, and that VR/teleop is inactive."
    )
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        print(f"Starting in {math.ceil(remaining)}...", flush=True)
        time.sleep(min(1.0, remaining))


def preflight_connected_robot(robot: Any) -> dict[str, Any]:
    if not robot.isRobotConnected():
        raise RuntimeError("SDK heartbeat reports robot disconnected")

    mode = enum_int(robot.getCurrentMode())
    init_state = enum_int(robot.getInitState())
    result: dict[str, Any] = {
        "mode": mode,
        "mode_name": MODE_NAMES.get(mode, "unknown"),
        "init_state": init_state,
        "init_state_name": INIT_STATE_NAMES.get(init_state, "unknown"),
    }

    try:
        ok, power = robot.getPowerChargeState()
        if ok:
            result["battery_soc_percent"] = int(power.soc)
            result["battery_temperature"] = int(power.temperature)
            if int(power.soc) < 10:
                raise H1SafetyError("battery SOC is below the documented 10% control gate")
    except H1SafetyError:
        raise
    except Exception as exc:
        result["battery_read_warning"] = f"{type(exc).__name__}: {exc}"

    return result


def ensure_mode_and_init(robot: Any, mode_value: Any) -> None:
    init_state = enum_int(robot.getInitState())
    if init_state not in (0, 4):
        raise H1SafetyError(
            "mode switch/init refused: init_state is "
            f"{INIT_STATE_NAMES.get(init_state, init_state)}. Put the robot into "
            "Uninit or Deinit_Complete using the approved operator procedure first."
        )

    desired_mode = enum_int(mode_value)
    current_mode = enum_int(robot.getCurrentMode())
    if current_mode != desired_mode:
        if not robot.switchControlMode(mode_value):
            raise RuntimeError("switchControlMode() returned false")
        actual_mode = enum_int(robot.getCurrentMode())
        if actual_mode != desired_mode:
            raise RuntimeError(
                f"mode switch did not settle: expected {desired_mode}, got {actual_mode}"
            )

    print("Calling robot_init(); the lift and arms will move.", flush=True)
    if not robot.robot_init():
        raise RuntimeError("robot_init() returned false")
    actual_init = enum_int(robot.getInitState())
    if actual_init != 2:
        raise RuntimeError(
            "robot_init returned true but init state is "
            f"{INIT_STATE_NAMES.get(actual_init, actual_init)}"
        )


def send_joint_command(robot: Any, sdk: Any, args: argparse.Namespace) -> dict[str, Any]:
    specs = arm_specs(args.arm)
    start = read_arm_positions(robot, sdk, args.arm)
    if args.target is not None:
        target = list(args.target)
    else:
        target = [before + delta for before, delta in zip(start, args.delta)]

    target = validate_joint_target(
        args.arm,
        target,
        limit_margin=args.limit_margin,
    )
    validate_max_delta(
        start,
        target,
        args.max_start_delta,
        [spec.name for spec in specs],
    )

    steps = max(1, int(round(args.duration * args.rate)))
    period = 1.0 / args.rate
    connection_check_interval = max(1, int(args.rate / 10.0))
    next_tick = time.perf_counter()

    def send_positions(positions: Sequence[float]) -> None:
        for spec, position in zip(specs, positions):
            command = sdk.Motor_Control()
            command.Position = float(position)
            command.Speed = 0.0
            command.Torque = 0.0
            command.KP = -1.0
            command.KD = -1.0
            if not robot.setArm_low(motor_index(sdk, spec.motor_id), command):
                raise RuntimeError(f"setArm_low({spec.motor_id}) returned false")

    for step in range(1, steps + 1):
        if step % connection_check_interval == 0 and not robot.isRobotConnected():
            raise RuntimeError("robot disconnected during interpolation")
        alpha = step / steps
        positions = [before + alpha * (after - before) for before, after in zip(start, target)]
        send_positions(positions)
        next_tick += period
        delay = next_tick - time.perf_counter()
        if delay > 0:
            time.sleep(delay)

    hold_steps = int(round(args.hold_seconds * args.rate))
    for step in range(hold_steps):
        if step % connection_check_interval == 0 and not robot.isRobotConnected():
            raise RuntimeError("robot disconnected while holding final target")
        send_positions(target)
        next_tick += period
        delay = next_tick - time.perf_counter()
        if delay > 0:
            time.sleep(delay)

    final = read_arm_positions(robot, sdk, args.arm)
    return {
        "arm": args.arm,
        "joint_names": [spec.name for spec in specs],
        "start_rad": start,
        "target_rad": target,
        "final_feedback_rad": final,
        "final_error_rad": [after - measured for after, measured in zip(target, final)],
        "samples": steps,
        "rate_hz": args.rate,
    }


def send_dual_joint_command(
    robot: Any,
    sdk: Any,
    args: argparse.Namespace,
) -> dict[str, Any]:
    left_specs = arm_specs("left")
    right_specs = arm_specs("right")
    left_start = read_arm_positions(robot, sdk, "left")
    right_start = read_arm_positions(robot, sdk, "right")
    left_target = validate_joint_target(
        "left",
        args.left_target,
        limit_margin=args.limit_margin,
    )
    right_target = validate_joint_target(
        "right",
        args.right_target,
        limit_margin=args.limit_margin,
    )
    validate_max_delta(
        left_start,
        left_target,
        args.max_start_delta,
        [spec.name for spec in left_specs],
        safety_cap=1.5,
    )
    validate_max_delta(
        right_start,
        right_target,
        args.max_start_delta,
        [spec.name for spec in right_specs],
        safety_cap=1.5,
    )

    gripper_target = validate_gripper_position(args.gripper_position)
    gripper_start: dict[str, float] = {}
    for arm_name, motor_id in GRIPPER_MOTOR_IDS.items():
        ok, state = robot.getGripperState(motor_index(sdk, motor_id))
        if not ok:
            raise RuntimeError(f"getGripperState({motor_id}) returned false")
        error_flag = int(state.Error_flag)
        if error_flag:
            raise H1SafetyError(
                f"{arm_name} gripper error_flag=0x{error_flag:04x}: "
                f"{decode_motor_error(error_flag)}"
            )
        position = require_finite(
            [state.Position_Actual],
            f"{arm_name}_gripper_feedback",
        )[0]
        if abs(gripper_target - position) > args.max_gripper_start_delta:
            raise H1SafetyError(
                f"{arm_name} gripper changes by "
                f"{abs(gripper_target - position):.6f} rad, exceeding "
                f"--max-gripper-start-delta {args.max_gripper_start_delta:.6f} rad"
            )
        gripper_start[arm_name] = position

    lift_target = (
        validate_lift_position(args.lift_position)
        if args.lift_position is not None
        else None
    )
    lift_start: float | None = None
    if lift_target is not None:
        ok, lift_state = robot.getWaistState(motor_index(sdk, LIFT_MOTOR_ID))
        if not ok:
            raise RuntimeError("getWaistState(MOTOR_LIFT) returned false")
        lift_error = int(lift_state.Error_flag)
        if lift_error:
            raise H1SafetyError(
                f"lift error_flag=0x{lift_error:04x}: "
                f"{decode_motor_error(lift_error)}"
            )
        lift_start = require_finite(
            [lift_state.Position_Actual],
            "lift_feedback",
        )[0]

    steps = max(1, int(round(args.duration * args.rate)))
    period = 1.0 / args.rate
    connection_check_interval = max(1, int(args.rate / 10.0))
    next_tick = time.perf_counter()

    def send_arm_positions(
        specs: Sequence[Any],
        positions: Sequence[float],
    ) -> None:
        for spec, position in zip(specs, positions):
            command = sdk.Motor_Control()
            command.Position = float(position)
            command.Speed = 0.0
            command.Torque = 0.0
            command.KP = -1.0
            command.KD = -1.0
            if not robot.setArm_low(motor_index(sdk, spec.motor_id), command):
                raise RuntimeError(f"setArm_low({spec.motor_id}) returned false")

    def send_lift_target() -> None:
        if lift_target is None:
            return
        command = sdk.Motor_Control()
        command.Position = float(lift_target)
        command.Speed = 0.0
        command.Torque = 0.0
        command.KP = -1.0
        command.KD = -1.0
        if not robot.setWaist_low(motor_index(sdk, LIFT_MOTOR_ID), command):
            raise RuntimeError("setWaist_low(MOTOR_LIFT) returned false")

    def send_gripper_targets() -> None:
        for motor_id in (GRIPPER_MOTOR_IDS["left"], GRIPPER_MOTOR_IDS["right"]):
            command = sdk.Motor_Control()
            command.Position = float(gripper_target)
            command.Speed = 0.0
            command.Torque = 0.0
            command.KP = -1.0
            command.KD = -1.0
            if not robot.setGripper_low(motor_index(sdk, motor_id), command, True):
                raise RuntimeError(f"setGripper_low({motor_id}) returned false")

    if lift_target is not None:
        print(f"Moving lift column to {lift_target:.3f} m before arm interpolation.")
        lift_deadline = time.monotonic() + 10.0
        while True:
            if not robot.isRobotConnected():
                raise RuntimeError("robot disconnected while moving lift column")
            send_lift_target()
            ok, lift_state = robot.getWaistState(motor_index(sdk, LIFT_MOTOR_ID))
            if not ok:
                raise RuntimeError("getWaistState(MOTOR_LIFT) returned false")
            measured_lift = require_finite(
                [lift_state.Position_Actual],
                "lift_feedback",
            )[0]
            if abs(measured_lift - lift_target) <= 0.01:
                break
            if time.monotonic() >= lift_deadline:
                raise RuntimeError(
                    f"lift did not reach {lift_target:.3f} +/- 0.01 m within 10 s; "
                    f"last feedback={measured_lift:.6f} m"
                )
            time.sleep(0.1)

    next_tick = time.perf_counter()

    for step in range(1, steps + 1):
        if step % connection_check_interval == 0 and not robot.isRobotConnected():
            raise RuntimeError("robot disconnected during dual-arm interpolation")
        alpha = step / steps
        left_positions = [
            before + alpha * (after - before)
            for before, after in zip(left_start, left_target)
        ]
        right_positions = [
            before + alpha * (after - before)
            for before, after in zip(right_start, right_target)
        ]
        send_arm_positions(left_specs, left_positions)
        send_arm_positions(right_specs, right_positions)
        next_tick += period
        delay = next_tick - time.perf_counter()
        if delay > 0:
            time.sleep(delay)

    send_gripper_targets()

    support_refresh_interval = max(1, int(round(args.rate)))

    def hold_one_cycle(step: int) -> None:
        nonlocal next_tick
        if step % connection_check_interval == 0 and not robot.isRobotConnected():
            raise RuntimeError("robot disconnected while holding the dual-arm target")
        send_arm_positions(left_specs, left_target)
        send_arm_positions(right_specs, right_target)
        if step % support_refresh_interval == 0:
            send_lift_target()
            send_gripper_targets()
        next_tick += period
        delay = next_tick - time.perf_counter()
        if delay > 0:
            time.sleep(delay)

    if args.hold_until_enter:
        print(
            "Targets reached. Holding continuously; press Enter for normal "
            "robot_deinit. Use the physical emergency stop for an emergency."
        )
        hold_step = 0
        while True:
            hold_one_cycle(hold_step)
            hold_step += 1
            readable, _, _ = select.select([sys.stdin], [], [], 0.0)
            if readable:
                line = sys.stdin.readline()
                if line == "":
                    raise RuntimeError(
                        "terminal input closed while holding; refusing to treat EOF "
                        "as an operator request for robot_deinit"
                    )
                break
        hold_description = "held until operator pressed Enter"
    else:
        hold_steps = int(round(args.hold_seconds * args.rate))
        for step in range(hold_steps):
            hold_one_cycle(step)
        hold_description = f"held for {args.hold_seconds:g} seconds"

    left_final = read_arm_positions(robot, sdk, "left")
    right_final = read_arm_positions(robot, sdk, "right")
    gripper_final: dict[str, float | None] = {}
    for arm_name, motor_id in GRIPPER_MOTOR_IDS.items():
        ok, state = robot.getGripperState(motor_index(sdk, motor_id))
        gripper_final[arm_name] = float(state.Position_Actual) if ok else None
    lift_final: float | None = None
    if lift_target is not None:
        ok, lift_state = robot.getWaistState(motor_index(sdk, LIFT_MOTOR_ID))
        if ok:
            lift_final = float(lift_state.Position_Actual)

    return {
        "left": {
            "joint_names": [spec.name for spec in left_specs],
            "start_rad": left_start,
            "target_rad": left_target,
            "final_feedback_rad": left_final,
        },
        "right": {
            "joint_names": [spec.name for spec in right_specs],
            "start_rad": right_start,
            "target_rad": right_target,
            "final_feedback_rad": right_final,
        },
        "grippers": {
            "start_rad": gripper_start,
            "target_rad": gripper_target,
            "final_feedback_rad": gripper_final,
            "hold_torque": True,
        },
        "lift": {
            "start_m": lift_start,
            "target_m": lift_target,
            "final_feedback_m": lift_final,
        },
        "hold": hold_description,
        "samples": steps,
        "rate_hz": args.rate,
        "simultaneity_note": "14 joint commands are sequential calls within each cycle, not atomic",
    }


def send_pose_command(robot: Any, sdk: Any, args: argparse.Namespace) -> dict[str, Any]:
    arm_value = sdk.ArmAction.LEFT_ARM if args.arm == "left" else sdk.ArmAction.RIGHT_ARM
    ok, current_pose = robot.getHandRelative(arm_value)
    if not ok:
        raise RuntimeError("getHandRelative() returned false")
    current = pose_to_dict(current_pose)
    current_position = require_finite(current["position"], "current_position")
    current_rotation = current["rotation_qx_qy_qz_qw"]
    validate_quaternion(current_rotation)

    if args.relative_position is not None:
        target_position = [
            value + delta for value, delta in zip(current_position, args.relative_position)
        ]
        target_rotation = list(current_rotation)
    else:
        target_position = require_finite(args.position, "position")
        target_rotation = validate_quaternion(args.quaternion)

    translation = vector_distance(current_position, target_position)
    if translation > args.max_cartesian_delta:
        raise H1SafetyError(
            f"Cartesian move {translation:.6f} m exceeds "
            f"--max-cartesian-delta {args.max_cartesian_delta:.6f} m"
        )
    rotation = quaternion_angular_distance(current_rotation, target_rotation)
    if rotation > args.max_rotation_delta:
        raise H1SafetyError(
            f"rotation move {rotation:.6f} rad exceeds "
            f"--max-rotation-delta {args.max_rotation_delta:.6f} rad"
        )

    target_pose = sdk.ArmEndPose()
    target_pose.position = list(target_position)
    target_pose.rotation = list(target_rotation)
    if not robot.setArmMove_high(
        arm_value,
        target_pose,
        0.0,
        0.0,
        float(args.duration),
        True,
    ):
        raise RuntimeError("setArmMove_high() returned false")

    if args.hold_seconds > 0:
        time.sleep(args.hold_seconds)

    ok, final_pose = robot.getHandRelative(arm_value)
    final = pose_to_dict(final_pose) if ok else None
    high_level_state = None
    try:
        state_ok, state = robot.getHighLevelState()
        if state_ok:
            high_level_state = {
                "state": int(state.state),
                "progress_percent": int(state.progress),
            }
    except Exception:
        pass

    return {
        "arm": args.arm,
        "start": current,
        "target": {
            "position": target_position,
            "rotation_qx_qy_qz_qw": target_rotation,
        },
        "final_feedback": final,
        "translation_m": translation,
        "rotation_rad": rotation,
        "high_level_state": high_level_state,
    }


def send_gripper_command(robot: Any, sdk: Any, args: argparse.Namespace) -> dict[str, Any]:
    motor_id = GRIPPER_MOTOR_IDS[args.arm]
    ok, state = robot.getGripperState(motor_index(sdk, motor_id))
    if not ok:
        raise RuntimeError(f"getGripperState({motor_id}) returned false")
    error_flag = int(state.Error_flag)
    if error_flag:
        raise H1SafetyError(
            f"gripper error_flag=0x{error_flag:04x}: {decode_motor_error(error_flag)}"
        )
    start = require_finite([state.Position_Actual], "gripper_feedback")[0]
    target = args.position if args.position is not None else start + args.delta
    target = validate_gripper_position(target)
    if abs(target - start) > args.max_start_delta:
        raise H1SafetyError(
            f"gripper change {abs(target - start):.6f} rad exceeds "
            f"--max-start-delta {args.max_start_delta:.6f} rad"
        )

    command = sdk.Motor_Control()
    command.Position = float(target)
    command.Speed = 0.0
    command.Torque = 0.0
    command.KP = -1.0
    command.KD = -1.0
    if args.control_mode == "high":
        sent = robot.setGripper_high(motor_index(sdk, motor_id), command, True)
    else:
        sent = robot.setGripper_low(motor_index(sdk, motor_id), command, True)
    if not sent:
        raise RuntimeError("setGripper command returned false")

    if args.hold_seconds > 0:
        time.sleep(args.hold_seconds)
    ok, final_state = robot.getGripperState(motor_index(sdk, motor_id))
    return {
        "arm": args.arm,
        "motor_id": motor_id,
        "start_rad": start,
        "target_rad": target,
        "final_feedback_rad": float(final_state.Position_Actual) if ok else None,
        "hold_torque": True,
        "direction_note": "0=open/closed direction still requires physical calibration",
    }


def execute_deinit(robot: Any, args: argparse.Namespace) -> int:
    state = enum_int(robot.getInitState())
    if state != 2:
        print(
            json.dumps(
                {
                    "action": "none",
                    "reason": "robot is not Init_Complete",
                    "init_state": state,
                    "init_state_name": INIT_STATE_NAMES.get(state, "unknown"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    countdown(args.countdown)
    print("Calling robot_deinit(); lift and arms will move to the deinit pose.")
    if not robot.robot_deinit():
        print("ERROR: robot_deinit() returned false", file=sys.stderr)
        return 3
    print("robot_deinit completed")
    return 0


def execute_command(args: argparse.Namespace) -> int:
    if (
        args.command in ("dual-joint", "initial-pose")
        and args.hold_until_enter
        and not sys.stdin.isatty()
    ):
        raise H1SafetyError(
            "--hold-until-enter requires an interactive terminal so an explicit "
            "Enter keypress can request robot_deinit"
        )

    sdk = load_sdk(args.sdk_root)
    robot = make_robot(sdk, args.robot_address)
    if not robot.robot_connect():
        raise RuntimeError("robot_connect() returned false")

    preflight = preflight_connected_robot(robot)
    print("Connected state:")
    print(json.dumps(preflight, ensure_ascii=False, indent=2))

    if args.command == "deinit":
        return execute_deinit(robot, args)

    if args.command in ("joint", "dual-joint", "initial-pose"):
        requested_mode = sdk.MotorControlMode.LOW_LEVEL
    elif args.command == "pose":
        requested_mode = sdk.MotorControlMode.HIGH_LEVEL
    else:
        requested_mode = (
            sdk.MotorControlMode.HIGH_LEVEL
            if args.control_mode == "high"
            else sdk.MotorControlMode.LOW_LEVEL
        )

    countdown(args.countdown)
    ensure_mode_and_init(robot, requested_mode)
    command_completed = False
    try:
        if args.command == "joint":
            result = send_joint_command(robot, sdk, args)
        elif args.command in ("dual-joint", "initial-pose"):
            result = send_dual_joint_command(robot, sdk, args)
        elif args.command == "pose":
            result = send_pose_command(robot, sdk, args)
        else:
            result = send_gripper_command(robot, sdk, args)
        command_completed = True
        print("Command result:")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except BaseException:
        print(
            "Motion did not complete normally. robot_deinit is NOT called "
            "automatically because its recovery trajectory also moves the robot. "
            "Use the physical emergency stop if needed, then perform the approved "
            "recovery/deinit procedure when the workspace is safe.",
            file=sys.stderr,
        )
        raise

    if command_completed:
        print(
            "Command completed. Calling robot_deinit next; this moves the lift and "
            "returns the arms to the deinit pose.",
            flush=True,
        )
        if not robot.robot_deinit():
            raise RuntimeError("robot_deinit() returned false after successful command")
        print("robot_deinit completed; SDK returns to the default teleoperation mode.")
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        plan = validate_offline(args)
    except (ValueError, H1SafetyError) as exc:
        parser.error(str(exc))

    print("Validated command plan:")
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if not args.execute:
        print(
            "DRY-RUN ONLY: no SDK was loaded and no robot connection or movement occurred."
        )
        return 0

    if args.confirm_motion != MOTION_CONFIRMATION:
        parser.error(
            "--execute requires --confirm-motion " + MOTION_CONFIRMATION
        )

    try:
        return execute_command(args)
    except KeyboardInterrupt:
        print("Interrupted by operator.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
