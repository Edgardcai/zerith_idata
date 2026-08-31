from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from .robot_service import (
    MOTOR_SPEC_BY_ID,
    RobotCommandRejected,
    RobotConflict,
    RobotService,
    RobotServiceError,
    RobotUnavailable,
)


VOICE_COMMANDS = frozenset(
    {
        "stop",
        "forward",
        "backward",
        "turn_left",
        "turn_right",
        "turn_around",
        "wave",
        "handshake",
    }
)


@dataclass(frozen=True)
class ChassisPreset:
    left_speed: float
    right_speed: float
    duration_s: float
    acknowledgement: str


# These are fixed operator-requested pulses, not metric distances or calibrated
# angles.  The SDK publishes neither a wheel-speed limit nor the geometry/
# odometry needed to promise metres or degrees.
CHASSIS_PRESETS: dict[str, ChassisPreset] = {
    "forward": ChassisPreset(1.5, 1.5, 1.0, "正在前进"),
    "backward": ChassisPreset(-1.5, -1.5, 1.0, "正在后退"),
    "turn_left": ChassisPreset(-1.5, 1.5, 3.5, "正在左转"),
    "turn_right": ChassisPreset(1.5, -1.5, 3.0, "正在右转"),
    "turn_around": ChassisPreset(1.5, -1.5, 8.0, "正在转身"),
}


@dataclass(frozen=True)
class ArmWavePlan:
    joint_ids: tuple[int, ...]
    shoulder_pitch_id: int
    shoulder_yaw_id: int
    elbow_id: int


LEFT_WAVE = ArmWavePlan(tuple(range(7, 14)), 7, 9, 10)
RIGHT_WAVE = ArmWavePlan(tuple(range(15, 22)), 15, 17, 18)
WAVE_PLANS = (LEFT_WAVE, RIGHT_WAVE)
ARM_NEUTRAL_TARGETS = {
    motor_id: 0.0
    for plan in WAVE_PLANS
    for motor_id in plan.joint_ids
}


class VoiceMotionController:
    """Lease-bound, opt-in adapter around the web console's sole RobotService."""

    def __init__(
        self,
        robot: RobotService,
        *,
        chassis_refresh_s: float = 0.12,
        wave_cycles: int = 5,
        wave_setup_segment_s: float = 0.6,
        wave_segment_s: float = 1.2,
        wave_endpoint_pause_s: float = 0.5,
        wave_neutral_s: float = 1.5,
        handshake_segment_s: float = 1.0,
        handshake_hold_s: float = 10.0,
    ) -> None:
        self.robot = robot
        self._chassis_refresh_s = float(chassis_refresh_s)
        self._wave_cycles = int(wave_cycles)
        self._wave_setup_segment_s = float(wave_setup_segment_s)
        self._wave_segment_s = float(wave_segment_s)
        self._wave_endpoint_pause_s = float(wave_endpoint_pause_s)
        self._wave_neutral_s = float(wave_neutral_s)
        self._handshake_segment_s = float(handshake_segment_s)
        self._handshake_hold_s = float(handshake_hold_s)
        if self._wave_cycles < 1:
            raise ValueError("wave_cycles must be positive")
        if min(
            self._wave_setup_segment_s,
            self._wave_segment_s,
            self._wave_neutral_s,
            self._handshake_segment_s,
        ) <= 0:
            raise ValueError("wave durations must be positive")
        if self._wave_endpoint_pause_s < 0:
            raise ValueError("wave endpoint pause cannot be negative")
        if self._handshake_hold_s < 0:
            raise ValueError("handshake hold cannot be negative")
        self._lock = threading.RLock()
        self._enabled = False
        self._lease_id: str | None = None
        self._active_action: str | None = None
        self._action_started_at: float | None = None
        self._last_action: str | None = None
        self._last_error: str | None = None
        self._cancel_action = threading.Event()
        self._action_thread: threading.Thread | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_authorization_locked()
            robot_state = self.robot.state()
            ready = bool(
                robot_state.get("connected")
                and robot_state.get("init_state") == 2
                and robot_state.get("control_mode") == 1
                and not robot_state.get("busy")
            )
            return {
                "available": True,
                "enabled": self._enabled,
                "ready": ready,
                "active_action": self._active_action,
                "last_action": self._last_action,
                "last_error": self._last_error,
                "supported_commands": sorted(VOICE_COMMANDS),
                "note": "底盘使用 1.5 rad/s 固定时长动作；转身不代表已标定角度",
            }

    def set_enabled(self, lease_id: str, enabled: bool) -> dict[str, Any]:
        if not enabled:
            self.disable(reason="operator_disabled", requested_lease=lease_id)
            return self.status()

        self.robot.validate_motion_ready(lease_id)
        with self._lock:
            if self._active_action is not None:
                raise RobotConflict("语音动作仍在执行，不能重复开启")
            self._enabled = True
            self._lease_id = lease_id
            self._last_error = None
            self._cancel_action.clear()
        return self.status()

    def disable(
        self,
        *,
        reason: str,
        requested_lease: str = "",
    ) -> None:
        with self._lock:
            lease_id = self._lease_id
            was_enabled = self._enabled
            if requested_lease and lease_id and requested_lease != lease_id:
                raise RobotConflict("语音运动控制属于其他控制租约")
            self._enabled = False
            self._lease_id = None
            self._cancel_action.set()
            if was_enabled:
                self._last_action = f"disabled:{reason}"
        if lease_id and self.robot.has_live_lease(lease_id):
            try:
                self.robot.stop_motion(lease_id, renew_lease=False)
            except RobotServiceError as exc:
                with self._lock:
                    self._last_error = str(exc)

    def cancel_active(self, lease_id: str) -> None:
        with self._lock:
            if self._lease_id and lease_id != self._lease_id:
                raise RobotConflict("语音运动控制属于其他控制租约")
            self._cancel_action.set()

    def execute(self, command: str) -> dict[str, Any]:
        command = str(command).strip()
        if command not in VOICE_COMMANDS:
            raise RobotCommandRejected(f"不支持的语音动作：{command}")

        if command == "stop":
            return self._execute_stop()

        with self._lock:
            self._refresh_authorization_locked()
            if not self._enabled or not self._lease_id:
                raise RobotConflict("网页尚未开启语音运动控制")
            if self._active_action is not None:
                raise RobotConflict(f"语音动作 {self._active_action} 正在执行")
            lease_id = self._lease_id
            self._cancel_action.clear()
            self._active_action = command
            self._action_started_at = time.monotonic()
            self._last_error = None

            if command in CHASSIS_PRESETS:
                preset = CHASSIS_PRESETS[command]
                try:
                    # Validate and start the physical command before reporting
                    # acceptance.  Subsequent refreshes run asynchronously.
                    self.robot.command_chassis(
                        lease_id,
                        preset.left_speed,
                        preset.right_speed,
                        renew_lease=False,
                    )
                except BaseException:
                    self._active_action = None
                    self._action_started_at = None
                    raise
                target = self._run_chassis_action
                args = (command, lease_id, preset)
                acknowledgement = preset.acknowledgement
            elif command == "wave":
                try:
                    self._prepare_wave(lease_id)
                except BaseException:
                    self._active_action = None
                    self._action_started_at = None
                    raise
                target = self._run_wave_action
                args = (lease_id,)
                acknowledgement = "正在挥手"
            elif command == "handshake":
                try:
                    self._prepare_handshake(lease_id)
                except BaseException:
                    self._active_action = None
                    self._action_started_at = None
                    raise
                target = self._run_handshake_action
                args = (lease_id,)
                acknowledgement = "正在握手"
            else:  # VOICE_COMMANDS and the stop fast path make this unreachable.
                self._active_action = None
                self._action_started_at = None
                raise RobotCommandRejected(f"未实现的语音动作：{command}")

            thread = threading.Thread(
                target=target,
                args=args,
                name=f"voice-motion-{command}",
                daemon=True,
            )
            self._action_thread = thread
            thread.start()
        return {
            "accepted": True,
            "command": command,
            "message": acknowledgement,
        }

    def close(self) -> None:
        try:
            self.disable(reason="service_stopped")
        except RobotServiceError:
            pass
        thread = self._action_thread
        if thread is not None:
            thread.join(timeout=2.0)

    def _execute_stop(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_authorization_locked()
            if not self._enabled or not self._lease_id:
                raise RobotConflict("网页尚未开启语音运动控制")
            lease_id = self._lease_id
            self._cancel_action.set()
        result = self.robot.stop_motion(lease_id, renew_lease=False)
        with self._lock:
            self._last_action = "stop"
        return {
            "accepted": True,
            "command": "stop",
            "message": "已发送软件停止指令",
            "result": result,
        }

    def _run_chassis_action(
        self,
        command: str,
        lease_id: str,
        preset: ChassisPreset,
    ) -> None:
        deadline = time.monotonic() + preset.duration_s
        error: str | None = None
        try:
            while not self._cancel_action.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if self._cancel_action.wait(min(self._chassis_refresh_s, remaining)):
                    break
                if not self.robot.has_live_lease(lease_id):
                    break
                self.robot.command_chassis(
                    lease_id,
                    preset.left_speed,
                    preset.right_speed,
                    renew_lease=False,
                )
        except Exception as exc:
            error = str(exc)
        finally:
            if self.robot.has_live_lease(lease_id):
                try:
                    self.robot.command_chassis(
                        lease_id,
                        0.0,
                        0.0,
                        renew_lease=False,
                    )
                except RobotServiceError as exc:
                    error = error or str(exc)
            self._finish_action(command, error)

    def _prepare_wave(self, lease_id: str) -> None:
        requested = {
            LEFT_WAVE.shoulder_pitch_id: -0.3,
            LEFT_WAVE.elbow_id: -0.9,
            LEFT_WAVE.shoulder_yaw_id: -0.4,
            RIGHT_WAVE.shoulder_pitch_id: -0.3,
            RIGHT_WAVE.elbow_id: -0.9,
            RIGHT_WAVE.shoulder_yaw_id: -0.4,
        }
        self._prepare_arm_action(lease_id, requested, "挥手")

    def _prepare_handshake(self, lease_id: str) -> None:
        requested = {
            RIGHT_WAVE.shoulder_pitch_id: -0.85,
            RIGHT_WAVE.elbow_id: 0.6,
        }
        self._prepare_arm_action(lease_id, requested, "握手")

    def _prepare_arm_action(
        self,
        lease_id: str,
        requested: dict[int, float],
        action_label: str,
    ) -> None:
        # Confirm readiness and all fourteen arm joints before reporting that
        # the sequential gesture was accepted.
        self.robot.validate_motion_ready(lease_id, renew_lease=False)
        state = self.robot.state()
        motors = state.get("motors", {})
        for motor_id in ARM_NEUTRAL_TARGETS:
            motor = motors.get(str(motor_id), {})
            if not motor.get("ok", False):
                raise RobotUnavailable(f"无法读取双臂关节 {motor_id} 当前角度")
        for motor_id, target in requested.items():
            spec = MOTOR_SPEC_BY_ID[motor_id]
            if not spec.minimum <= target <= spec.maximum:
                raise RobotCommandRejected(
                    f"{action_label}目标超出关节 {motor_id} 的 SDK 限位"
                )

    def _run_wave_action(
        self,
        lease_id: str,
    ) -> None:
        command = "wave"
        error: str | None = None
        try:
            if not self._wave_can_continue(lease_id):
                return
            self.robot.move_joints(
                lease_id,
                ARM_NEUTRAL_TARGETS,
                duration_s=self._wave_neutral_s,
                smooth=True,
                renew_lease=False,
            )
            for plan in WAVE_PLANS:
                if not self._move_arm_pose(
                    lease_id,
                    plan,
                    shoulder_pitch=-0.3,
                    duration_s=self._wave_setup_segment_s,
                ):
                    return
                if not self._move_arm_pose(
                    lease_id,
                    plan,
                    shoulder_pitch=-0.3,
                    elbow=-0.9,
                    duration_s=self._wave_setup_segment_s,
                ):
                    return
                if not self._move_arm_pose(
                    lease_id,
                    plan,
                    shoulder_pitch=-0.3,
                    elbow=-0.9,
                    shoulder_yaw=-0.4,
                    duration_s=self._wave_setup_segment_s,
                ):
                    return
                if not self._pause_wave_endpoint(lease_id):
                    return
                for _cycle in range(self._wave_cycles):
                    if not self._move_arm_pose(
                        lease_id,
                        plan,
                        shoulder_pitch=-0.3,
                        elbow=-0.9,
                        shoulder_yaw=0.4,
                        duration_s=self._wave_segment_s,
                    ):
                        return
                    if not self._pause_wave_endpoint(lease_id):
                        return
                    if not self._move_arm_pose(
                        lease_id,
                        plan,
                        shoulder_pitch=-0.3,
                        elbow=-0.9,
                        shoulder_yaw=-0.4,
                        duration_s=self._wave_segment_s,
                    ):
                        return
                    if not self._pause_wave_endpoint(lease_id):
                        return
                if not self._wave_can_continue(lease_id):
                    return
                self.robot.move_joints(
                    lease_id,
                    {motor_id: 0.0 for motor_id in plan.joint_ids},
                    duration_s=self._wave_neutral_s,
                    smooth=True,
                    renew_lease=False,
                )
        except Exception as exc:
            error = str(exc)
        finally:
            self._finish_action(command, error)

    def _run_handshake_action(self, lease_id: str) -> None:
        command = "handshake"
        error: str | None = None
        try:
            if not self._wave_can_continue(lease_id):
                return
            self.robot.move_joints(
                lease_id,
                ARM_NEUTRAL_TARGETS,
                duration_s=self._wave_neutral_s,
                smooth=True,
                renew_lease=False,
            )
            for shoulder_pitch, elbow in (
                (-0.4, 0.0),
                (-0.4, 0.6),
                (-0.85, 0.6),
            ):
                if not self._move_arm_pose(
                    lease_id,
                    RIGHT_WAVE,
                    shoulder_pitch=shoulder_pitch,
                    elbow=elbow,
                    duration_s=self._handshake_segment_s,
                ):
                    return
            if not self._pause_arm_action(
                lease_id,
                self._handshake_hold_s,
                monitor_joint_ids=RIGHT_WAVE.joint_ids,
            ):
                return
            self.robot.move_joints(
                lease_id,
                {motor_id: 0.0 for motor_id in RIGHT_WAVE.joint_ids},
                duration_s=self._wave_neutral_s,
                smooth=True,
                renew_lease=False,
            )
        except Exception as exc:
            error = str(exc)
        finally:
            self._finish_action(command, error)

    def _pause_wave_endpoint(self, lease_id: str) -> bool:
        return self._pause_arm_action(lease_id, self._wave_endpoint_pause_s)

    def _pause_arm_action(
        self,
        lease_id: str,
        duration_s: float,
        *,
        monitor_joint_ids: tuple[int, ...] = (),
    ) -> bool:
        deadline = time.monotonic() + duration_s
        while True:
            if not self._wave_can_continue(lease_id):
                return False
            if monitor_joint_ids:
                motors = self.robot.state().get("motors", {})
                for motor_id in monitor_joint_ids:
                    motor = motors.get(str(motor_id), {})
                    error_flag = int(motor.get("error", 0) or 0)
                    if error_flag:
                        # A completed trajectory is normally refreshed by the
                        # RobotService background hold loop.  Drop the entire
                        # faulted arm before surfacing the error so a long
                        # handshake pause cannot keep refreshing a bad pose.
                        self.robot.drop_arm_holds(
                            lease_id,
                            monitor_joint_ids,
                            renew_lease=False,
                        )
                        raise RobotCommandRejected(
                            f"握手停留期间电机 {motor_id} "
                            f"error_flag=0x{error_flag:04x}"
                        )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            if self._cancel_action.wait(min(0.05, remaining)):
                return False

    def _move_arm_pose(
        self,
        lease_id: str,
        plan: ArmWavePlan,
        *,
        shoulder_pitch: float = 0.0,
        shoulder_yaw: float = 0.0,
        elbow: float = 0.0,
        duration_s: float,
    ) -> bool:
        if not self._wave_can_continue(lease_id):
            return False
        # Send the complete seven-axis pose on every segment.  Re-reading the
        # other six feedback positions for each yaw sweep would turn small
        # shoulder tracking errors into new hold targets and accumulate drift.
        targets = {motor_id: 0.0 for motor_id in plan.joint_ids}
        targets.update(
            {
                plan.shoulder_pitch_id: shoulder_pitch,
                plan.shoulder_yaw_id: shoulder_yaw,
                plan.elbow_id: elbow,
            }
        )
        self.robot.move_joints(
            lease_id,
            targets,
            duration_s=duration_s,
            smooth=True,
            renew_lease=False,
        )
        return True

    def _wave_can_continue(self, lease_id: str) -> bool:
        return not self._cancel_action.is_set() and self.robot.has_live_lease(lease_id)

    def _finish_action(self, command: str, error: str | None) -> None:
        with self._lock:
            self._last_action = command
            self._last_error = error
            if self._active_action == command:
                self._active_action = None
                self._action_started_at = None
            self._action_thread = None

    def _refresh_authorization_locked(self) -> None:
        if self._enabled and (
            not self._lease_id or not self.robot.has_live_lease(self._lease_id)
        ):
            self._enabled = False
            self._lease_id = None
            self._cancel_action.set()
            self._last_action = "disabled:lease_expired"


class VoiceMotionInternalServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        controller: VoiceMotionController,
    ) -> None:
        host, _port = address
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("语音运动内部接口只允许监听回环地址")
        self.controller = controller
        super().__init__(address, VoiceMotionInternalHandler)


class VoiceMotionInternalHandler(BaseHTTPRequestHandler):
    server: VoiceMotionInternalServer
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != "/v1/status":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._send_json(HTTPStatus.OK, self.server.controller.status())

    def do_POST(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != "/v1/execute":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length < 0 or length > 4096:
                raise RobotCommandRejected("请求体过大")
            value = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if not isinstance(value, dict):
                raise RobotCommandRejected("JSON 顶层必须是对象")
            result = self.server.controller.execute(str(value.get("command", "")))
            self._send_json(HTTPStatus.ACCEPTED, result)
        except RobotConflict as exc:
            self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except RobotCommandRejected as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except RobotUnavailable as exc:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"无效请求：{exc}"})
        except RobotServiceError as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _send_json(self, status: int | HTTPStatus, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


__all__ = [
    "CHASSIS_PRESETS",
    "VOICE_COMMANDS",
    "VoiceMotionController",
    "VoiceMotionInternalServer",
]
