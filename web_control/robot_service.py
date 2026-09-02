#!/usr/bin/env python3
"""Single-owner, lease-gated H1 robot service used by the web console.

The vendor SDK is not thread safe and permits only one H1Robot client.  This
module therefore owns the object in one worker thread and serializes every SDK
call.  Construction, connection, mode switching and motion happen only after
an explicit takeover request.

Position limits below are the SDK V4.0 *soft limits* verbatim.  There is no
additional application margin and no application-level per-step delta or
speed limit.  Operational interlocks (lease, mode, init state, finite values,
motor errors and watchdogs) are deliberately separate from geometric limits.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import gc
import math
import queue
import secrets
import threading
import time
from typing import Any, Callable


try:
    from h1_sdk_common import enum_int, load_sdk, make_robot, motor_index
except ImportError:  # pragma: no cover - allows package execution from control/
    from ..h1_sdk_common import enum_int, load_sdk, make_robot, motor_index


class RobotServiceError(RuntimeError):
    """Base error returned to the HTTP API."""


class RobotConflict(RobotServiceError):
    """The request conflicts with the robot/session state."""


class RobotUnavailable(RobotServiceError):
    """The SDK or robot connection is unavailable."""


class RobotCommandRejected(RobotServiceError):
    """A command failed validation or the SDK rejected it."""


class RobotCallTimeout(RobotServiceError):
    """The worker did not finish within the API timeout."""


@dataclass(frozen=True)
class MotorSpec:
    motor_id: int
    key: str
    label: str
    group: str
    unit: str
    minimum: float
    maximum: float
    step: float
    command_kind: str
    interpolate: bool


# SDK V4.0 section 2.2.3 soft limits, without a local margin.
MOTOR_SPECS: tuple[MotorSpec, ...] = (
    MotorSpec(2, "lift", "升降", "body", "m", 0.0, 0.8, 0.01, "waist", False),
    MotorSpec(3, "waist_pitch", "腰俯仰", "body", "rad", 0.0, 1.3, 0.01, "waist", True),
    MotorSpec(4, "waist_yaw", "腰旋转", "body", "rad", -0.7, 0.7, 0.01, "waist", True),
    MotorSpec(5, "head_yaw", "头旋转", "body", "rad", -1.5, 1.5, 0.01, "head", True),
    MotorSpec(6, "head_pitch", "头俯仰", "body", "rad", -0.5, 0.75, 0.01, "head", True),
    MotorSpec(7, "left_shoulder_pitch", "肩俯仰", "left_arm", "rad", -2.7, 1.5, 0.01, "arm", True),
    MotorSpec(8, "left_shoulder_roll", "肩侧摆", "left_arm", "rad", -0.3, 2.0, 0.01, "arm", True),
    MotorSpec(9, "left_shoulder_yaw", "肩旋转", "left_arm", "rad", -2.9, 2.9, 0.01, "arm", True),
    MotorSpec(10, "left_elbow", "肘", "left_arm", "rad", -1.3, 1.5, 0.01, "arm", True),
    MotorSpec(11, "left_wrist_roll", "腕侧摆", "left_arm", "rad", -2.9, 2.9, 0.01, "arm", True),
    MotorSpec(12, "left_wrist_yaw", "腕旋转", "left_arm", "rad", -1.0, 1.0, 0.01, "arm", True),
    MotorSpec(13, "left_wrist_pitch", "腕俯仰", "left_arm", "rad", -1.0, 1.0, 0.01, "arm", True),
    MotorSpec(14, "left_gripper", "夹爪", "left_arm", "rad", 0.0, 1.5, 0.01, "gripper", False),
    MotorSpec(15, "right_shoulder_pitch", "肩俯仰", "right_arm", "rad", -2.7, 1.5, 0.01, "arm", True),
    MotorSpec(16, "right_shoulder_roll", "肩侧摆", "right_arm", "rad", -2.0, 0.3, 0.01, "arm", True),
    MotorSpec(17, "right_shoulder_yaw", "肩旋转", "right_arm", "rad", -2.9, 2.9, 0.01, "arm", True),
    MotorSpec(18, "right_elbow", "肘", "right_arm", "rad", -1.3, 1.5, 0.01, "arm", True),
    MotorSpec(19, "right_wrist_roll", "腕侧摆", "right_arm", "rad", -2.9, 2.9, 0.01, "arm", True),
    MotorSpec(20, "right_wrist_yaw", "腕旋转", "right_arm", "rad", -1.0, 1.0, 0.01, "arm", True),
    MotorSpec(21, "right_wrist_pitch", "腕俯仰", "right_arm", "rad", -1.0, 1.0, 0.01, "arm", True),
    MotorSpec(22, "right_gripper", "夹爪", "right_arm", "rad", 0.0, 1.5, 0.01, "gripper", False),
)

MOTOR_SPEC_BY_ID = {spec.motor_id: spec for spec in MOTOR_SPECS}

LEFT_ARM_MOTOR_IDS = tuple(range(7, 14))
RIGHT_ARM_MOTOR_IDS = tuple(range(15, 22))

# Pi0.5 wire order is intentionally independent from EtherCAT's numeric order:
# left arm + gripper, right arm + gripper, lift, waist pitch/yaw, head yaw/pitch.
# The two chassis entries exist only on the policy wire and are never forwarded
# to position-motor setters.
POLICY_WIRE_MOTOR_IDS = (
    *range(7, 15),
    *range(15, 23),
    2,
    3,
    4,
    5,
    6,
)
POLICY_STATE_DIM = 23
POLICY_POSITION_DIM = len(POLICY_WIRE_MOTOR_IDS)
POLICY_GRIPPER_WIRE_INDICES = (7, 15)
POLICY_BODY_HOLD_SLICE = slice(17, 21)
POLICY_GRIPPER_VALUES = (0.0, 1.5)

if POLICY_POSITION_DIM != 21 or len(set(POLICY_WIRE_MOTOR_IDS)) != 21:
    raise RuntimeError("Pi0.5 policy motor mapping must contain 21 unique motors")

MOTOR_NAMES = {
    0: "left_wheel",
    1: "right_wheel",
    **{spec.motor_id: spec.key for spec in MOTOR_SPECS},
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

HOME_TARGETS = {
    2: 0.40,
    7: 0.0,
    8: 0.0,
    9: 0.0,
    10: -1.20,
    11: 0.0,
    12: 0.0,
    13: 0.98,
    14: 0.02,
    15: 0.0,
    16: 0.0,
    17: 0.0,
    18: -1.20,
    19: 0.0,
    20: 0.0,
    21: 0.98,
    22: 0.02,
}

DEFAULT_JOINT_DURATION_S = 2.0
DEFAULT_MOTION_SPEED_SCALE = 1.0
MIN_MOTION_SPEED_SCALE = 0.2
MAX_MOTION_SPEED_SCALE = 2.0


@dataclass
class _WorkItem:
    operation: str
    args: tuple[Any, ...]
    done: threading.Event
    result: Any = None
    error: BaseException | None = None
    admission_token: object | None = None
    state_lock: Any = field(default_factory=threading.Lock)
    started: bool = False
    cancelled: bool = False


def _finite_float(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RobotCommandRejected(f"{field} 必须是数值") from exc
    if not math.isfinite(number):
        raise RobotCommandRejected(f"{field} 必须是有限数")
    return number


class RobotService:
    """Own and operate one H1Robot from one worker thread."""

    def __init__(
        self,
        *,
        sdk_loader: Callable[[], Any] = load_sdk,
        robot_factory: Callable[[Any], Any] | None = None,
        lease_seconds: float = 3.0,
        chassis_watchdog_seconds: float = 0.35,
        trajectory_rate_hz: float = 100.0,
        state_rate_hz: float = 5.0,
        home_duration_s: float = 8.0,
        home_lift_timeout_s: float = 10.0,
    ) -> None:
        self._sdk_loader = sdk_loader
        self._robot_factory = robot_factory or (lambda sdk: make_robot(sdk))
        self._lease_seconds = float(lease_seconds)
        self._chassis_watchdog_seconds = float(chassis_watchdog_seconds)
        self._trajectory_rate_hz = float(trajectory_rate_hz)
        self._state_period = 1.0 / float(state_rate_hz)
        self._home_duration_s = float(home_duration_s)
        self._home_lift_timeout_s = float(home_lift_timeout_s)

        self._queue: queue.Queue[_WorkItem] = queue.Queue()
        self._stop_event = threading.Event()
        self._cancel_event = threading.Event()
        self._snapshot_lock = threading.RLock()
        self._events_lock = threading.RLock()
        self._lease_lock = threading.RLock()
        self._admission_lock = threading.RLock()
        self._policy_lock = threading.RLock()
        self._lease_id: str | None = None
        self._lease_client_id: str | None = None
        self._lease_deadline = 0.0
        self._admission_token: object | None = None
        self._admitted_operation: str | None = None
        self._policy_session_id: str | None = None
        self._policy_session_lease_id: str | None = None
        self._policy_session_started_monotonic = 0.0
        self._policy_last_end_reason: str | None = None
        self._policy_abort_event = threading.Event()

        self._sdk: Any | None = None
        self._robot: Any | None = None
        self._hold_targets: dict[int, float] = {}
        self._support_targets: dict[int, float] = {}
        self._chassis_target = (0.0, 0.0)
        self._chassis_deadline = 0.0
        self._chassis_was_moving = False
        self._chassis_stop_pending = False
        self._next_state_poll = 0.0
        self._next_hold_tick = 0.0
        self._next_support_tick = 0.0
        self._events: deque[dict[str, Any]] = deque(maxlen=100)

        self._snapshot: dict[str, Any] = {
            "sdk_loaded": False,
            "server_owned": False,
            "connected": False,
            "takeover": False,
            "busy": False,
            "active_operation": None,
            "control_mode": None,
            "control_mode_name": "not connected",
            "init_state": None,
            "init_state_name": "not connected",
            "motors": {},
            "chassis": {
                "left_wheel_rad_s": None,
                "right_wheel_rad_s": None,
                "linear_m_s": None,
                "angular_rad_s": None,
                "command_left_rad_s": 0.0,
                "command_right_rad_s": 0.0,
                "stop_pending": False,
            },
            "power": None,
            "last_error": None,
            "state_monotonic": None,
        }

        self._thread = threading.Thread(
            target=self._worker,
            name="h1-sdk-owner",
            daemon=True,
        )
        self._thread.start()

    @staticmethod
    def config() -> dict[str, Any]:
        return {
            "sdk_version": "1.3.9",
            "control_mode": "LOW_LEVEL",
            "limits_source": "ZERITH H1 PRO SDK V4.0 soft limits; no extra margin",
            "motors": [
                {
                    "id": spec.motor_id,
                    "key": spec.key,
                    "label": spec.label,
                    "group": spec.group,
                    "unit": spec.unit,
                    "min": spec.minimum,
                    "max": spec.maximum,
                    "step": spec.step,
                    "command_kind": spec.command_kind,
                }
                for spec in MOTOR_SPECS
            ],
            "chassis": {
                "kind": "low_level_wheel_speed",
                "wheel_speed": {
                    "unit": "rad/s",
                    "min": None,
                    "max": None,
                    "default": 1.0,
                    "note": "SDK V4.0 marks low-level wheel speed as unlimited",
                },
                "watchdog_ms": 350,
            },
            "motion_speed": {
                "label": "关节/初始位姿速度",
                "unit": "×",
                "min": MIN_MOTION_SPEED_SCALE,
                "max": MAX_MOTION_SPEED_SCALE,
                "step": 0.1,
                "default": DEFAULT_MOTION_SPEED_SCALE,
                "joint_duration_at_1x_s": DEFAULT_JOINT_DURATION_S,
                "note": "速度倍率越大，轨迹用时越短；不影响厂商初始化/反初始化速度",
            },
            "home": {
                "targets": {str(key): value for key, value in HOME_TARGETS.items()},
                "duration_s": 8.0,
                "rate_hz": 100.0,
                "hold_until_deinit": True,
            },
            "policy": {
                "state_dim": POLICY_STATE_DIM,
                "position_dim": POLICY_POSITION_DIM,
                "wire_motor_ids": list(POLICY_WIRE_MOTOR_IDS),
                "gripper_values": list(POLICY_GRIPPER_VALUES),
                "base_motion": "forced_zero_not_forwarded",
            },
        }

    def state(self) -> dict[str, Any]:
        with self._snapshot_lock:
            result = self._deep_copy_snapshot()
        with self._lease_lock:
            lease_valid = bool(
                self._lease_id and time.monotonic() < self._lease_deadline
            )
            lease_client_id = self._lease_client_id if lease_valid else None
        result["takeover"] = lease_valid
        result["takeover_client_id"] = lease_client_id
        with self._policy_lock:
            policy_active = self._policy_session_id is not None
            policy_started = self._policy_session_started_monotonic
            policy_last_end_reason = self._policy_last_end_reason
        now = time.monotonic()
        result["policy_session"] = {
            "active": policy_active,
            "age_ms": (
                max(0.0, (now - policy_started) * 1000.0)
                if policy_active
                else None
            ),
            "last_end_reason": policy_last_end_reason,
        }
        with self._events_lock:
            result["events"] = list(self._events)[-20:]
        state_time = result.get("state_monotonic")
        result["state_age_ms"] = (
            max(0.0, (time.monotonic() - state_time) * 1000.0)
            if isinstance(state_time, (int, float))
            else None
        )
        return result

    def acquire(self, client_id: Any = None) -> dict[str, Any]:
        now = time.monotonic()
        lease_id = secrets.token_urlsafe(24)
        owner_id = str(client_id or "").strip()
        if not owner_id:
            owner_id = f"anonymous-{secrets.token_urlsafe(8)}"
        if len(owner_id) > 128:
            raise RobotCommandRejected("client_id 过长")
        with self._lease_lock:
            if self._lease_id is not None and now < self._lease_deadline:
                raise RobotConflict("已有其他页面持有控制权，请先在原页面释放")
            self._lease_id = lease_id
            self._lease_client_id = owner_id
            self._lease_deadline = now + self._lease_seconds
        try:
            self._call("connect", timeout=20.0, exclusive=True)
        except BaseException:
            with self._lease_lock:
                if self._lease_id == lease_id:
                    self._lease_id = None
                    self._lease_client_id = None
                    self._lease_deadline = 0.0
            raise
        # The browser cannot heartbeat until acquire() returns.  Connecting a
        # real SDK can take longer than the normal short lease, so start the
        # usable lease window after the connection has completed.
        with self._lease_lock:
            if self._lease_id == lease_id:
                self._lease_deadline = time.monotonic() + self._lease_seconds
        self._record_event("takeover_acquired")
        return {
            "lease_id": lease_id,
            "client_id": owner_id,
            "lease_seconds": self._lease_seconds,
            "state": self.state(),
        }

    def heartbeat(self, lease_id: str) -> dict[str, Any]:
        self._require_lease(lease_id, renew=True)
        return {"ok": True, "lease_seconds": self._lease_seconds}

    def has_live_lease(self, lease_id: str) -> bool:
        """Read-only lease check used by bounded auxiliary controllers."""
        now = time.monotonic()
        with self._lease_lock:
            return bool(
                lease_id
                and lease_id == self._lease_id
                and now < self._lease_deadline
            )

    def validate_motion_ready(
        self,
        lease_id: str,
        *,
        renew_lease: bool = True,
    ) -> dict[str, Any]:
        """Validate real SDK readiness without issuing any motion command."""
        self._require_lease(lease_id, renew=renew_lease)
        return self._call(
            "validate_motion",
            lease_id,
            timeout=5.0,
            exclusive=True,
        )

    def read_policy_state(
        self,
        lease_id: str,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Sample the Pi0.5 23-D state on the sole SDK owner thread.

        This operation never renews the lease and never writes a motor command.
        The final two chassis entries are always literal zeros.
        """

        self._require_lease(lease_id, renew=False)
        return self._call(
            "policy_state",
            lease_id,
            session_id,
            timeout=5.0,
            # Session reads already belong to the backend-wide policy owner
            # and serialize on the sole SDK queue.  A dry-run has no session
            # token, so keep that read admission-exclusive from ordinary web
            # commands while its state/image snapshot is assembled.
            exclusive=session_id is None,
        )

    def begin_policy_session(self, lease_id: str) -> dict[str, Any]:
        """Exclusively hand normal motion admission to a Pi0.5 executor."""

        self._require_lease(lease_id, renew=False)
        return self._call(
            "policy_begin",
            lease_id,
            timeout=10.0,
            exclusive=True,
        )

    def end_policy_session(
        self,
        lease_id: str,
        *,
        session_id: str | None = None,
        reason: str = "operator_end",
    ) -> dict[str, Any]:
        """End policy control without deinitializing or changing SDK mode."""

        self._require_lease(lease_id, renew=False)
        parsed_reason = str(reason).strip() or "operator_end"
        if len(parsed_reason) > 128:
            raise RobotCommandRejected("policy session reason 过长")
        return self._call(
            "policy_end",
            lease_id,
            session_id,
            parsed_reason,
            timeout=10.0,
            exclusive=True,
        )

    def policy_hold_and_zero(
        self,
        lease_id: str,
        *,
        session_id: str | None = None,
        reason: str = "operator_stop",
    ) -> dict[str, Any]:
        """Explicit safety alias: end policy, hold feedback and zero chassis."""

        return self.end_policy_session(
            lease_id,
            session_id=session_id,
            reason=reason,
        )

    def policy_observation(
        self,
        lease_id: str,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Compatibility alias for executors that call this an observation."""

        return self.read_policy_state(lease_id, session_id=session_id)

    def policy_step(
        self,
        lease_id: str,
        action: Any,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Validate and send one Pi0.5 action through the SDK owner thread.

        Lease validity is checked but deliberately not renewed.  Strict shape,
        finite-value and SDK soft-limit validation happens again beside the SDK
        calls so a rejected action terminates the active policy session.  The
        bridge does not clamp or reject targets based on their change from the
        latest joint/lift feedback.
        """

        self._require_lease(lease_id, renew=False)
        return self._call(
            "policy_step",
            lease_id,
            session_id,
            action,
            timeout=5.0,
            exclusive=True,
        )

    def drop_arm_holds(
        self,
        lease_id: str,
        motor_ids: tuple[int, ...],
        *,
        renew_lease: bool = True,
    ) -> dict[str, Any]:
        """Stop refreshing an arm after a gesture detects a motor fault."""
        self._require_lease(lease_id, renew=renew_lease)
        parsed_ids = tuple(dict.fromkeys(int(motor_id) for motor_id in motor_ids))
        allowed = set(LEFT_ARM_MOTOR_IDS) | set(RIGHT_ARM_MOTOR_IDS)
        if not parsed_ids or any(motor_id not in allowed for motor_id in parsed_ids):
            raise RobotCommandRejected("只能停止双臂位置关节的后台保持")
        return self._call(
            "drop_arm_holds",
            lease_id,
            parsed_ids,
            timeout=5.0,
            exclusive=True,
        )

    def release(self, lease_id: str) -> dict[str, Any]:
        self._require_lease(lease_id, renew=True)
        self._call("release", timeout=10.0, exclusive=True)
        with self._lease_lock:
            if self._lease_id == lease_id:
                self._lease_id = None
                self._lease_client_id = None
                self._lease_deadline = 0.0
        self._record_event("takeover_released")
        # _release_impl() runs before the public lease is cleared.  Build the
        # response afterwards so callers never receive a stale takeover=true.
        return self.state()

    def initialize(self, lease_id: str) -> dict[str, Any]:
        self._require_lease(lease_id, renew=True)
        return self._call("init", lease_id, timeout=180.0, exclusive=True)

    def deinitialize(self, lease_id: str) -> dict[str, Any]:
        self._require_lease(lease_id, renew=True)
        return self._call("deinit", lease_id, timeout=180.0, exclusive=True)

    def move_joint(
        self,
        lease_id: str,
        motor_id: Any,
        target: Any,
        *,
        duration_s: Any = None,
        speed_scale: Any = None,
        smooth: bool = False,
        renew_lease: bool = True,
    ) -> dict[str, Any]:
        self._require_lease(lease_id, renew=renew_lease)
        try:
            parsed_id = int(motor_id)
        except (TypeError, ValueError) as exc:
            raise RobotCommandRejected("motor_id 必须是整数") from exc
        spec = MOTOR_SPEC_BY_ID.get(parsed_id)
        if spec is None:
            raise RobotCommandRejected(f"电机 {parsed_id} 不支持位置控制")
        parsed_target = _finite_float(target, "target")
        if not spec.minimum <= parsed_target <= spec.maximum:
            raise RobotCommandRejected(
                f"{spec.label} 目标 {parsed_target:g} 超出 SDK 范围 "
                f"[{spec.minimum:g}, {spec.maximum:g}] {spec.unit}"
            )
        if duration_s is not None and speed_scale is not None:
            raise RobotCommandRejected("duration_s 和 speed_scale 不能同时指定")
        if speed_scale is not None:
            parsed_speed_scale = self._parse_motion_speed_scale(speed_scale)
            parsed_duration = DEFAULT_JOINT_DURATION_S / parsed_speed_scale
        else:
            parsed_duration = _finite_float(
                DEFAULT_JOINT_DURATION_S if duration_s is None else duration_s,
                "duration_s",
            )
        if parsed_duration <= 0.0:
            raise RobotCommandRejected("duration_s 必须大于 0")
        if not isinstance(smooth, bool):
            raise RobotCommandRejected("smooth 必须是布尔值")
        return self._call(
            "joint",
            lease_id,
            parsed_id,
            parsed_target,
            parsed_duration,
            smooth,
            timeout=max(30.0, parsed_duration + 15.0),
            exclusive=True,
        )

    def move_joints(
        self,
        lease_id: str,
        targets: dict[Any, Any],
        *,
        duration_s: Any,
        smooth: bool = False,
        renew_lease: bool = True,
    ) -> dict[str, Any]:
        """Move one or both seven-axis arms in one synchronized trajectory.

        This is an internal primitive for bounded gestures.  It deliberately
        accepts only interpolated arm joints; chassis, head, waist and grippers
        remain outside this operation.
        """
        self._require_lease(lease_id, renew=renew_lease)
        if not isinstance(targets, dict) or not targets:
            raise RobotCommandRejected("targets 必须是非空关节目标字典")
        parsed_targets: dict[int, float] = {}
        for motor_id, target in targets.items():
            try:
                parsed_id = int(motor_id)
            except (TypeError, ValueError) as exc:
                raise RobotCommandRejected("targets 的电机 ID 必须是整数") from exc
            spec = MOTOR_SPEC_BY_ID.get(parsed_id)
            if spec is None or spec.command_kind != "arm" or not spec.interpolate:
                raise RobotCommandRejected(f"电机 {parsed_id} 不是可插值的双臂关节")
            parsed_target = _finite_float(target, f"targets[{parsed_id}]")
            if not spec.minimum <= parsed_target <= spec.maximum:
                raise RobotCommandRejected(
                    f"{spec.label} 目标 {parsed_target:g} 超出 SDK 范围 "
                    f"[{spec.minimum:g}, {spec.maximum:g}] {spec.unit}"
                )
            parsed_targets[parsed_id] = parsed_target
        parsed_duration = _finite_float(duration_s, "duration_s")
        if parsed_duration <= 0.0:
            raise RobotCommandRejected("duration_s 必须大于 0")
        if not isinstance(smooth, bool):
            raise RobotCommandRejected("smooth 必须是布尔值")
        return self._call(
            "joints",
            lease_id,
            parsed_targets,
            parsed_duration,
            smooth,
            timeout=max(30.0, parsed_duration + 15.0),
            exclusive=True,
        )

    def move_home(
        self,
        lease_id: str,
        *,
        speed_scale: Any = DEFAULT_MOTION_SPEED_SCALE,
    ) -> dict[str, Any]:
        self._require_lease(lease_id, renew=True)
        parsed_speed_scale = self._parse_motion_speed_scale(speed_scale)
        duration_s = self._home_duration_s / parsed_speed_scale
        return self._call(
            "home",
            lease_id,
            duration_s,
            timeout=duration_s + self._home_lift_timeout_s + 30.0,
            exclusive=True,
        )

    def command_chassis(
        self,
        lease_id: str,
        left_speed: Any,
        right_speed: Any,
        *,
        renew_lease: bool = True,
    ) -> dict[str, Any]:
        self._require_lease(lease_id, renew=renew_lease)
        left = _finite_float(left_speed, "left_speed")
        right = _finite_float(right_speed, "right_speed")
        # The SDK explicitly publishes no low-level wheel-speed limit.  Do not
        # add one here; only reject non-finite values and enforce a watchdog.
        return self._call(
            "chassis",
            lease_id,
            left,
            right,
            timeout=5.0,
            exclusive=True,
        )

    def stop_motion(
        self,
        lease_id: str,
        *,
        renew_lease: bool = True,
    ) -> dict[str, Any]:
        self._require_lease(lease_id, renew=renew_lease)
        self._cancel_event.set()
        self._policy_abort_event.set()
        return self._call("stop", lease_id, timeout=10.0)

    def close(self) -> None:
        """Stop the web backend without triggering robot_deinit motion."""

        self._cancel_event.set()
        self._policy_abort_event.set()
        try:
            self._call("shutdown", timeout=5.0)
        except RobotServiceError:
            pass
        self._stop_event.set()
        self._thread.join(timeout=5.0)

    def _call(
        self,
        operation: str,
        *args: Any,
        timeout: float,
        exclusive: bool = False,
    ) -> Any:
        self._assert_operation_allowed_during_policy(operation)
        admission_token: object | None = None
        if exclusive:
            admission_token = object()
            with self._admission_lock:
                if self._admission_token is not None:
                    raise RobotConflict(
                        f"{self._admitted_operation or '其他操作'} 正在执行，请先等待或停止"
                    )
                self._admission_token = admission_token
                self._admitted_operation = operation
        if not self._thread.is_alive():
            self._release_admission(admission_token)
            raise RobotUnavailable("机器人 SDK 工作线程未运行")
        item = _WorkItem(
            operation=operation,
            args=args,
            done=threading.Event(),
            admission_token=admission_token,
        )
        self._queue.put(item)
        if not item.done.wait(timeout):
            with item.state_lock:
                started = item.started
                if not started:
                    item.cancelled = True
            if started and operation in ("joint", "joints", "home", "policy_step"):
                self._cancel_event.set()
            if operation == "policy_step":
                self._policy_abort_event.set()
            self._record_event(
                "operation_timed_out",
                operation=operation,
                started=started,
            )
            if started:
                raise RobotCallTimeout(f"{operation} 超时；已开始的 SDK 调用状态未知")
            raise RobotCallTimeout(f"{operation} 排队超时，已取消且不会迟到执行")
        if item.error is not None:
            if isinstance(item.error, RobotServiceError):
                raise item.error
            raise RobotServiceError(f"{operation} 失败: {type(item.error).__name__}: {item.error}") from item.error
        return item.result

    def _worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.005)
            except queue.Empty:
                item = None
            if item is not None:
                self._execute_item(item)
            try:
                self._tick()
            except BaseException as exc:  # keep the sole SDK owner alive
                self._set_error(f"后台刷新失败: {type(exc).__name__}: {exc}")
                self._cancel_event.set()

    def _execute_item(self, item: _WorkItem) -> None:
        with item.state_lock:
            if item.cancelled:
                item.error = RobotCallTimeout(
                    f"{item.operation} 在开始执行前已过期并取消"
                )
                self._record_event(
                    "queued_operation_cancelled",
                    operation=item.operation,
                )
                self._release_admission(item.admission_token)
                item.done.set()
                return
            item.started = True
        handlers = {
            "connect": self._connect_impl,
            "validate_motion": self._validate_motion_impl,
            "policy_state": self._policy_state_impl,
            "policy_begin": self._policy_begin_impl,
            "policy_end": self._policy_end_impl,
            "policy_step": self._policy_step_impl,
            "drop_arm_holds": self._drop_arm_holds_impl,
            "release": self._release_impl,
            "init": self._init_impl,
            "deinit": self._deinit_impl,
            "joint": self._joint_impl,
            "joints": self._joints_impl,
            "home": self._home_impl,
            "chassis": self._chassis_impl,
            "stop": self._stop_impl,
            "shutdown": self._shutdown_impl,
        }
        handler = handlers.get(item.operation)
        if handler is None:
            item.error = RobotServiceError(f"未知工作项: {item.operation}")
            self._release_admission(item.admission_token)
            item.done.set()
            return
        self._set_busy(True, item.operation)
        try:
            self._assert_operation_allowed_during_policy(item.operation)
            item.result = handler(*item.args)
            with self._snapshot_lock:
                self._snapshot["last_error"] = None
        except BaseException as exc:
            item.error = exc
            self._set_error(f"{item.operation}: {type(exc).__name__}: {exc}")
            self._record_event("operation_failed", operation=item.operation, error=str(exc))
        finally:
            self._set_busy(False, None)
            self._release_admission(item.admission_token)
            item.done.set()

    def _validate_motion_impl(self, lease_id: str) -> dict[str, Any]:
        self._require_ready(lease_id)
        self._check_power_for_motion(chassis=False)
        self._poll_state(force=True)
        return {"ok": True, "state": self.state()}

    def _policy_state_impl(
        self,
        lease_id: str,
        session_id: str | None,
    ) -> dict[str, Any]:
        self._require_policy_readable(lease_id)
        with self._policy_lock:
            active = self._policy_session_id is not None
        if active:
            active_session_id = self._require_policy_session(
                lease_id,
                session_id,
            )
        elif session_id is not None:
            raise RobotConflict("Pi0.5 policy session 已结束，请操作员重新确认")
        else:
            active_session_id = None
        try:
            state, sampled_at = self._sample_policy_state()
        except BaseException as exc:
            if active_session_id is not None:
                self._terminate_policy_session(
                    f"state_failed:{type(exc).__name__}",
                    expected_session_id=active_session_id,
                )
            raise
        return {
            "ok": True,
            "state": state,
            "state_monotonic": sampled_at,
            "session_id": active_session_id,
        }

    def _policy_begin_impl(self, lease_id: str) -> dict[str, Any]:
        self._require_ready(lease_id)
        self._check_power_for_motion(chassis=False)
        with self._policy_lock:
            if self._policy_session_id is not None:
                raise RobotConflict("已有 Pi0.5 policy session 正在运行")

        state, sampled_at = self._sample_policy_state()
        if not self._send_chassis(0.0, 0.0, require_success=False):
            raise RobotCommandRejected(
                "Pi0.5 session 启动前无法确认底盘双轮零速，请使用实体急停"
            )

        now = time.monotonic()
        session_id = secrets.token_urlsafe(24)
        self._cancel_event.clear()
        self._policy_abort_event.clear()
        self._hold_targets = {
            motor_id: state[index]
            for index, motor_id in enumerate(POLICY_WIRE_MOTOR_IDS)
            if MOTOR_SPEC_BY_ID[motor_id].interpolate
        }
        self._support_targets = {
            motor_id: state[index]
            for index, motor_id in enumerate(POLICY_WIRE_MOTOR_IDS)
            if not MOTOR_SPEC_BY_ID[motor_id].interpolate
        }
        with self._policy_lock:
            if self._policy_session_id is not None:  # defensive; worker is sole writer
                raise RobotConflict("已有 Pi0.5 policy session 正在运行")
            self._policy_session_id = session_id
            self._policy_session_lease_id = lease_id
            self._policy_session_started_monotonic = now
            self._policy_last_end_reason = None
        self._record_event("policy_session_started")
        return {
            "ok": True,
            "session_id": session_id,
            "state": state,
            "state_monotonic": sampled_at,
        }

    def _policy_end_impl(
        self,
        lease_id: str,
        session_id: str | None,
        reason: str,
    ) -> dict[str, Any]:
        self._require_live_lease(lease_id)
        active_session_id = self._require_policy_session(lease_id, session_id)
        result = self._terminate_policy_session(
            reason,
            expected_session_id=active_session_id,
        )
        if not result["ok"]:
            raise RobotCommandRejected(
                "Pi0.5 session 已停止，但部分安全保持/底盘零速命令失败；"
                "请使用实体急停: " + "; ".join(result["failures"])
            )
        return result

    def _policy_step_impl(
        self,
        lease_id: str,
        session_id: str | None,
        action: Any,
    ) -> dict[str, Any]:
        active_session_id = self._require_policy_session(lease_id, session_id)
        try:
            self._require_ready(lease_id)
            self._check_power_for_motion(chassis=False)
            parsed_action = self._parse_policy_action(action)
            latest_state, sampled_at = self._sample_policy_state()
            requested_action = list(parsed_action)
            effective_action = list(requested_action)
            effective_action[POLICY_BODY_HOLD_SLICE] = latest_state[
                POLICY_BODY_HOLD_SLICE
            ]
            effective_action[21] = 0.0
            effective_action[22] = 0.0
            self._validate_policy_targets(effective_action, latest_state)

            sent_action = list(effective_action[:POLICY_POSITION_DIM])
            for index, (motor_id, target) in enumerate(
                zip(POLICY_WIRE_MOTOR_IDS, sent_action)
            ):
                self._require_live_lease(lease_id)
                self._require_policy_session(lease_id, active_session_id)
                if self._cancel_event.is_set() or self._policy_abort_event.is_set():
                    raise RobotCommandRejected(
                        f"Pi0.5 action 在 motor index {index} 前被停止"
                    )
                self._send_position(motor_id, target)

            self._hold_targets = {
                motor_id: sent_action[index]
                for index, motor_id in enumerate(POLICY_WIRE_MOTOR_IDS)
                if MOTOR_SPEC_BY_ID[motor_id].interpolate
            }
            self._support_targets = {
                motor_id: sent_action[index]
                for index, motor_id in enumerate(POLICY_WIRE_MOTOR_IDS)
                if not MOTOR_SPEC_BY_ID[motor_id].interpolate
            }
            with self._policy_lock:
                if self._policy_session_id != active_session_id:
                    raise RobotConflict(
                        "Pi0.5 policy session 已停止，请操作员重新确认"
                    )
            self._record_event("policy_step_sent")
            return {
                "ok": True,
                "session_id": active_session_id,
                "latest_state": latest_state,
                "state": latest_state,
                "state_monotonic": sampled_at,
                "requested_action": requested_action,
                "effective_action": effective_action,
                "sent_action": sent_action,
                "sent_motor_ids": list(POLICY_WIRE_MOTOR_IDS),
            }
        except BaseException as exc:
            self._terminate_policy_session(
                f"step_failed:{type(exc).__name__}",
                expected_session_id=active_session_id,
            )
            raise

    def _drop_arm_holds_impl(
        self,
        lease_id: str,
        motor_ids: tuple[int, ...],
    ) -> dict[str, Any]:
        self._require_live_lease(lease_id)
        for motor_id in motor_ids:
            self._hold_targets.pop(motor_id, None)
        self._record_event("arm_holds_dropped", motor_ids=list(motor_ids))
        return {"ok": True, "motor_ids": list(motor_ids)}

    def _connect_impl(self) -> dict[str, Any]:
        if self._robot is None:
            try:
                sdk = self._sdk_loader()
                robot = self._robot_factory(sdk)
            except BaseException as exc:
                raise RobotUnavailable(f"加载/构造 H1 SDK 失败: {exc}") from exc
            if not robot.robot_connect():
                del robot
                raise RobotUnavailable("robot_connect() 返回 false")
            self._sdk = sdk
            self._robot = robot
            with self._snapshot_lock:
                self._snapshot["sdk_loaded"] = True
                self._snapshot["server_owned"] = True
        self._poll_state(force=True)
        return self.state()

    def _release_impl(self) -> dict[str, Any]:
        if self._robot is None:
            return self.state()
        init_state = enum_int(self._robot.getInitState())
        if init_state not in (0, 4):
            raise RobotConflict(
                "机器人尚未反初始化，不能释放 SDK；请先执行反初始化。"
            )
        self._cancel_event.set()
        self._hold_targets.clear()
        self._support_targets.clear()
        self._reset_chassis_command_state()
        robot = self._robot
        self._robot = None
        self._sdk = None
        del robot
        gc.collect()
        with self._snapshot_lock:
            self._snapshot.update(
                {
                    "sdk_loaded": False,
                    "server_owned": False,
                    "connected": False,
                    "control_mode": None,
                    "control_mode_name": "not connected",
                    "init_state": None,
                    "init_state_name": "not connected",
                    "motors": {},
                    "chassis": {
                        "left_wheel_rad_s": None,
                        "right_wheel_rad_s": None,
                        "linear_m_s": None,
                        "angular_rad_s": None,
                        "command_left_rad_s": 0.0,
                        "command_right_rad_s": 0.0,
                        "stop_pending": False,
                    },
                    "power": None,
                    "state_monotonic": time.monotonic(),
                }
            )
        return self.state()

    def _init_impl(self, lease_id: str) -> dict[str, Any]:
        self._require_live_lease(lease_id)
        robot, sdk = self._require_robot()
        init_state = enum_int(robot.getInitState())
        mode = enum_int(robot.getCurrentMode())
        low_mode = enum_int(sdk.MotorControlMode.LOW_LEVEL)
        if init_state == 2:
            if mode != low_mode:
                raise RobotConflict(
                    "机器人已由其他模式初始化，不能在该状态切到 LOW_LEVEL"
                )
            self._poll_state(force=True)
            return {"ok": True, "already_initialized": True, "state": self.state()}
        if init_state not in (0, 4):
            raise RobotConflict(
                f"当前初始化状态 {INIT_STATE_NAMES.get(init_state, init_state)} 不允许初始化"
            )
        self._check_power_for_motion(chassis=False)
        motor_errors = []
        for motor_id in range(23):
            state = self._read_motor(motor_id)
            if int(state.Error_flag) != 0:
                motor_errors.append(f"{motor_id}:0x{int(state.Error_flag):04x}")
        if motor_errors:
            raise RobotCommandRejected(
                "存在电机错误，初始化已拒绝: " + ", ".join(motor_errors)
            )
        if mode != low_mode:
            if not robot.switchControlMode(sdk.MotorControlMode.LOW_LEVEL):
                raise RobotCommandRejected("switchControlMode(LOW_LEVEL) 返回 false")
            if enum_int(robot.getCurrentMode()) != low_mode:
                raise RobotCommandRejected("控制模式未稳定到 LOW_LEVEL")
        self._record_event("robot_init_started")
        if not robot.robot_init():
            raise RobotCommandRejected("robot_init() 返回 false")
        if enum_int(robot.getInitState()) != 2:
            raise RobotCommandRejected("robot_init() 返回后状态不是 Init_Complete")
        self._cancel_event.clear()
        self._hold_targets.clear()
        self._support_targets.clear()
        self._poll_state(force=True)
        self._record_event("robot_init_completed")
        return {"ok": True, "state": self.state()}

    def _deinit_impl(self, lease_id: str) -> dict[str, Any]:
        self._require_live_lease(lease_id)
        robot, sdk = self._require_robot()
        init_state = enum_int(robot.getInitState())
        if init_state in (0, 4):
            self._poll_state(force=True)
            return {"ok": True, "already_deinitialized": True, "state": self.state()}
        if init_state != 2:
            raise RobotConflict(
                f"当前初始化状态 {INIT_STATE_NAMES.get(init_state, init_state)} 不允许反初始化"
            )
        if enum_int(robot.getCurrentMode()) != enum_int(sdk.MotorControlMode.LOW_LEVEL):
            raise RobotConflict("网页只能反初始化自身 LOW_LEVEL 会话，不能接管 VR/其他模式")
        self._cancel_event.set()
        self._send_chassis(0.0, 0.0, require_success=False)
        self._hold_targets.clear()
        self._support_targets.clear()
        self._record_event("robot_deinit_started")
        if not robot.robot_deinit():
            raise RobotCommandRejected("robot_deinit() 返回 false")
        final_state = enum_int(robot.getInitState())
        if final_state not in (0, 4):
            raise RobotCommandRejected(
                f"robot_deinit() 返回后状态为 {INIT_STATE_NAMES.get(final_state, final_state)}"
            )
        self._reset_chassis_command_state()
        self._poll_state(force=True)
        self._record_event("robot_deinit_completed")
        return {"ok": True, "state": self.state()}

    def _joint_impl(
        self,
        lease_id: str,
        motor_id: int,
        target: float,
        duration_s: float,
        smooth: bool,
    ) -> dict[str, Any]:
        self._require_ready(lease_id)
        self._check_power_for_motion(chassis=False)
        spec = MOTOR_SPEC_BY_ID[motor_id]
        # setArm_low() accepts one motor per call, but the SDK guide requires
        # refreshing all seven motors of the controlled arm every cycle.  A
        # web "single-joint" move therefore changes only the selected target
        # while the other six targets stay at their measured starting pose.
        if spec.group == "left_arm" and spec.command_kind == "arm":
            active_ids = LEFT_ARM_MOTOR_IDS
        elif spec.group == "right_arm" and spec.command_kind == "arm":
            active_ids = RIGHT_ARM_MOTOR_IDS
        else:
            active_ids = (motor_id,)

        starts: dict[int, float] = {}
        for active_id in active_ids:
            state = self._read_motor(active_id)
            if int(state.Error_flag) != 0:
                raise RobotCommandRejected(
                    f"电机 {active_id} error_flag=0x{int(state.Error_flag):04x}"
                )
            starts[active_id] = _finite_float(
                state.Position_Actual,
                f"motor[{active_id}].Position_Actual",
            )
        start = starts[motor_id]
        targets = dict(starts)
        targets[motor_id] = target
        self._cancel_event.clear()
        self._record_event(
            "joint_started",
            motor_id=motor_id,
            start=start,
            target=target,
        )
        if spec.interpolate:
            self._interpolate_targets(
                lease_id,
                starts,
                targets,
                duration_s,
                smooth=smooth,
            )
            self._hold_targets.update(targets)
        else:
            self._require_live_lease(lease_id)
            self._send_position(motor_id, target)
            self._support_targets[motor_id] = target
        final_state = self._read_motor(motor_id)
        if int(final_state.Error_flag) != 0:
            raise RobotCommandRejected(
                f"电机 {motor_id} 在轨迹后 error_flag=0x{int(final_state.Error_flag):04x}"
            )
        self._poll_state(force=True)
        self._record_event("joint_completed", motor_id=motor_id, target=target)
        return {
            "ok": True,
            "motor_id": motor_id,
            "start": start,
            "target": target,
            "duration_s": duration_s,
            "feedback": float(final_state.Position_Actual),
            "unit": spec.unit,
        }

    def _joints_impl(
        self,
        lease_id: str,
        requested_targets: dict[int, float],
        duration_s: float,
        smooth: bool,
    ) -> dict[str, Any]:
        self._require_ready(lease_id)
        self._check_power_for_motion(chassis=False)
        requested_ids = set(requested_targets)
        active_ids: set[int] = set()
        if requested_ids.intersection(LEFT_ARM_MOTOR_IDS):
            active_ids.update(LEFT_ARM_MOTOR_IDS)
        if requested_ids.intersection(RIGHT_ARM_MOTOR_IDS):
            active_ids.update(RIGHT_ARM_MOTOR_IDS)

        starts: dict[int, float] = {}
        for motor_id in sorted(active_ids):
            state = self._read_motor(motor_id)
            if int(state.Error_flag) != 0:
                raise RobotCommandRejected(
                    f"电机 {motor_id} error_flag=0x{int(state.Error_flag):04x}"
                )
            starts[motor_id] = _finite_float(
                state.Position_Actual,
                f"motor[{motor_id}].Position_Actual",
            )
        targets = dict(starts)
        targets.update(requested_targets)
        self._cancel_event.clear()
        self._record_event(
            "joints_started",
            targets={str(key): value for key, value in sorted(requested_targets.items())},
        )
        self._interpolate_targets(
            lease_id,
            starts,
            targets,
            duration_s,
            smooth=smooth,
        )
        self._hold_targets.update(targets)
        feedback: dict[str, float] = {}
        for motor_id in sorted(requested_targets):
            state = self._read_motor(motor_id)
            if int(state.Error_flag) != 0:
                raise RobotCommandRejected(
                    f"电机 {motor_id} 在轨迹后 error_flag=0x{int(state.Error_flag):04x}"
                )
            feedback[str(motor_id)] = float(state.Position_Actual)
        self._poll_state(force=True)
        self._record_event("joints_completed", targets=feedback)
        return {
            "ok": True,
            "targets": {str(key): value for key, value in sorted(requested_targets.items())},
            "duration_s": duration_s,
            "feedback": feedback,
        }

    def _home_impl(self, lease_id: str, duration_s: float) -> dict[str, Any]:
        self._require_ready(lease_id)
        self._check_power_for_motion(chassis=False)
        robot, _ = self._require_robot()
        start: dict[int, float] = {}
        for motor_id in HOME_TARGETS:
            state = self._read_motor(motor_id)
            if int(state.Error_flag) != 0:
                raise RobotCommandRejected(
                    f"电机 {motor_id} error_flag=0x{int(state.Error_flag):04x}"
                )
            start[motor_id] = _finite_float(state.Position_Actual, f"motor[{motor_id}]")
        self._cancel_event.clear()
        self._record_event("home_started")

        # Match the existing CLI: lift first, both arms over 8 s, then grippers.
        lift_target = HOME_TARGETS[2]
        lift_deadline = time.monotonic() + self._home_lift_timeout_s
        try:
            while True:
                self._require_live_lease(lease_id)
                self._raise_if_cancelled()
                if not robot.isRobotConnected():
                    raise RobotUnavailable("升降过程中机器人断开")
                self._send_position(2, lift_target)
                lift_state = self._read_motor(2)
                if abs(float(lift_state.Position_Actual) - lift_target) <= 0.01:
                    break
                if time.monotonic() >= lift_deadline:
                    raise RobotCommandRejected(
                        f"升降柱未在 {self._home_lift_timeout_s:g}s 内到达 0.40±0.01m"
                    )
                self._poll_state(force=True)
                time.sleep(0.1)
        except BaseException:
            try:
                current_lift = float(self._read_motor(2).Position_Actual)
                self._send_position(2, current_lift)
                self._support_targets[2] = current_lift
            except BaseException:
                pass
            raise
        self._support_targets[2] = lift_target

        arm_ids = list(range(7, 14)) + list(range(15, 22))
        self._interpolate_targets(
            lease_id,
            {motor_id: start[motor_id] for motor_id in arm_ids},
            {motor_id: HOME_TARGETS[motor_id] for motor_id in arm_ids},
            duration_s,
        )
        self._send_position(14, HOME_TARGETS[14])
        self._send_position(22, HOME_TARGETS[22])

        self._hold_targets.update({motor_id: HOME_TARGETS[motor_id] for motor_id in arm_ids})
        self._support_targets.update(
            {2: lift_target, 14: HOME_TARGETS[14], 22: HOME_TARGETS[22]}
        )
        self._poll_state(force=True)
        self._record_event("home_completed")
        return {
            "ok": True,
            "targets": {str(key): value for key, value in HOME_TARGETS.items()},
            "holding": True,
            "duration_s": duration_s,
            "note": "保持到单独执行反初始化；网页不会把浏览器断开当作反初始化授权",
            "state": self.state(),
        }

    @staticmethod
    def _parse_motion_speed_scale(value: Any) -> float:
        speed_scale = _finite_float(value, "speed_scale")
        if not MIN_MOTION_SPEED_SCALE <= speed_scale <= MAX_MOTION_SPEED_SCALE:
            raise RobotCommandRejected(
                "speed_scale 必须在 "
                f"{MIN_MOTION_SPEED_SCALE:g}..{MAX_MOTION_SPEED_SCALE:g} 之间"
            )
        return speed_scale

    def _chassis_impl(
        self,
        lease_id: str,
        left_speed: float,
        right_speed: float,
    ) -> dict[str, Any]:
        self._require_ready(lease_id)
        self._check_power_for_motion(chassis=True)
        for motor_id in (0, 1):
            state = self._read_motor(motor_id)
            if int(state.Error_flag) != 0:
                raise RobotCommandRejected(
                    f"轮毂电机 {motor_id} error_flag=0x{int(state.Error_flag):04x}"
                )
        if self._chassis_stop_pending and (left_speed != 0.0 or right_speed != 0.0):
            raise RobotConflict("底盘零速尚未确认，后台正在重试，拒绝新的非零轮速")
        self._send_chassis(left_speed, right_speed)
        self._chassis_target = (left_speed, right_speed)
        self._chassis_deadline = time.monotonic() + self._chassis_watchdog_seconds
        self._chassis_was_moving = bool(left_speed or right_speed)
        self._chassis_stop_pending = False
        with self._snapshot_lock:
            self._snapshot["chassis"]["command_left_rad_s"] = left_speed
            self._snapshot["chassis"]["command_right_rad_s"] = right_speed
            self._snapshot["chassis"]["stop_pending"] = False
        return {"ok": True, "left_speed": left_speed, "right_speed": right_speed}

    def _stop_impl(self, lease_id: str) -> dict[str, Any]:
        self._require_live_lease(lease_id)
        with self._policy_lock:
            active_session_id = self._policy_session_id
        if active_session_id is not None:
            result = self._terminate_policy_session(
                "software_stop",
                expected_session_id=active_session_id,
            )
            if not result["ok"]:
                raise RobotCommandRejected(
                    "policy 已停止，但部分安全保持/底盘零速命令失败；"
                    "请使用实体急停: " + "; ".join(result["failures"])
                )
            self._record_event("software_stop", policy_session=True)
            result["note"] = (
                "policy session 已终止、底盘已发零速、21 个位置电机保持最新反馈；"
                "这不是实体急停"
            )
            return result
        stopped = self._send_chassis(0.0, 0.0, require_success=False)
        self._hold_current_feedback()
        self._cancel_event.clear()
        self._policy_abort_event.clear()
        if not stopped:
            raise RobotCommandRejected("底盘零速尚未确认；后台将继续重试，请使用实体急停")
        self._record_event("software_stop")
        return {
            "ok": True,
            "note": "轨迹已取消、底盘已发零速、位置电机保持当前反馈；这不是实体急停",
        }

    def _shutdown_impl(self) -> dict[str, Any]:
        with self._policy_lock:
            active_session_id = self._policy_session_id
        if active_session_id is not None:
            self._terminate_policy_session(
                "service_shutdown",
                expected_session_id=active_session_id,
            )
        if (
            self._chassis_was_moving or self._chassis_stop_pending
        ) and self._ready_without_lease():
            self._send_chassis(0.0, 0.0, require_success=False)
        self._stop_event.set()
        return {"ok": True}

    def _tick(self) -> None:
        if self._robot is None:
            return
        now = time.monotonic()
        lease_valid = self._lease_is_valid()
        with self._policy_lock:
            active_session_id = self._policy_session_id
        if active_session_id is not None:
            policy_end_reason: str | None = None
            if self._policy_abort_event.is_set():
                policy_end_reason = "abort_requested"
            elif not lease_valid:
                policy_end_reason = "lease_expired"
            elif not self._ready_without_lease():
                policy_end_reason = "robot_not_ready"
            if policy_end_reason is not None:
                self._terminate_policy_session(
                    policy_end_reason,
                    expected_session_id=active_session_id,
                )
                now = time.monotonic()
                lease_valid = self._lease_is_valid()
        if not lease_valid:
            self._cancel_event.set()
            if self._chassis_was_moving:
                self._send_chassis(0.0, 0.0, require_success=False)
                self._record_event("chassis_watchdog_stop", reason="lease_expired")
        if self._chassis_was_moving and now >= self._chassis_deadline:
            self._send_chassis(0.0, 0.0, require_success=False)
            self._record_event("chassis_watchdog_stop", reason="command_timeout")
        if (
            self._chassis_stop_pending
            and now >= self._chassis_deadline
            and self._ready_without_lease()
        ):
            if self._send_chassis(0.0, 0.0, require_success=False):
                self._record_event("chassis_zero_retry_succeeded")

        if now >= self._next_hold_tick:
            self._next_hold_tick = now + 1.0 / self._trajectory_rate_hz
            if self._ready_without_lease():
                for motor_id, target in tuple(self._hold_targets.items()):
                    self._send_position(motor_id, target)
        if now >= self._next_support_tick:
            self._next_support_tick = now + 1.0
            if self._ready_without_lease():
                for motor_id, target in tuple(self._support_targets.items()):
                    self._send_position(motor_id, target)
        if now >= self._next_state_poll:
            self._poll_state(force=True)

    def _interpolate_targets(
        self,
        lease_id: str,
        starts: dict[int, float],
        targets: dict[int, float],
        duration_s: float,
        *,
        smooth: bool = False,
    ) -> None:
        steps = max(1, int(round(duration_s * self._trajectory_rate_hz)))
        period = 1.0 / self._trajectory_rate_hz
        next_tick = time.perf_counter()
        robot, _ = self._require_robot()
        try:
            for step in range(1, steps + 1):
                self._require_live_lease(lease_id)
                self._raise_if_cancelled()
                health_interval = max(1, int(self._trajectory_rate_hz / 10.0))
                if step % health_interval == 0 or step == steps:
                    if not robot.isRobotConnected():
                        raise RobotUnavailable("插值过程中机器人断开")
                    for motor_id in targets:
                        state = self._read_motor(motor_id)
                        if int(state.Error_flag) != 0:
                            raise RobotCommandRejected(
                                f"电机 {motor_id} 在轨迹中 "
                                f"error_flag=0x{int(state.Error_flag):04x}"
                            )
                    self._poll_state(force=True)
                progress = step / steps
                # Gesture trajectories use a quintic smoothstep so velocity
                # and acceleration taper to zero at both endpoints.  Normal
                # web joint commands retain their existing linear profile.
                alpha = (
                    progress * progress * progress
                    * (progress * (progress * 6.0 - 15.0) + 10.0)
                    if smooth
                    else progress
                )
                # A trajectory blocks the worker's normal hold tick.  Keep
                # refreshing previously controlled interpolated joints (for
                # example the opposite arm) in the same 100 Hz cycle.
                for motor_id, target in tuple(self._hold_targets.items()):
                    if motor_id not in targets:
                        self._send_position(motor_id, target)
                for motor_id in targets:
                    position = starts[motor_id] + alpha * (targets[motor_id] - starts[motor_id])
                    self._send_position(motor_id, position)
                next_tick += period
                delay = next_tick - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
        except BaseException:
            # A browser disconnect/STOP cancels the unfinished interpolation.
            # Do not let the last in-flight trajectory sample remain the only
            # target: explicitly capture and hold the latest feedback instead.
            # A motor error is different: continuing position commands can add
            # heat/torque, so drop the whole active trajectory from background
            # holding and do not send another sample to any of its motors.
            has_motor_error = False
            for motor_id in targets:
                try:
                    has_motor_error = has_motor_error or bool(
                        int(self._read_motor(motor_id).Error_flag)
                    )
                except BaseException:
                    pass
            if has_motor_error:
                for motor_id in targets:
                    self._hold_targets.pop(motor_id, None)
            else:
                for motor_id in targets:
                    try:
                        current = float(self._read_motor(motor_id).Position_Actual)
                        self._send_position(motor_id, current)
                        self._hold_targets[motor_id] = current
                    except BaseException:
                        pass
            raise

    def _send_position(self, motor_id: int, position: float) -> None:
        robot, sdk = self._require_robot()
        control = sdk.Motor_Control()
        control.Position = float(position)
        control.Speed = 0.0
        control.Torque = 0.0
        control.KP = -1.0
        control.KD = -1.0
        index = motor_index(sdk, motor_id)
        spec = MOTOR_SPEC_BY_ID[motor_id]
        if spec.command_kind == "arm":
            ok = robot.setArm_low(index, control)
        elif spec.command_kind == "gripper":
            ok = robot.setGripper_low(index, control, True)
        elif spec.command_kind == "waist":
            ok = robot.setWaist_low(index, control)
        elif spec.command_kind == "head":
            ok = robot.setHead_low(index, control)
        else:  # pragma: no cover - all constants are audited above
            raise RobotCommandRejected(f"未知控制种类: {spec.command_kind}")
        if not ok:
            raise RobotCommandRejected(f"{spec.command_kind} setter({motor_id}) 返回 false")

    def _send_chassis(
        self,
        left_speed: float,
        right_speed: float,
        *,
        require_success: bool = True,
    ) -> bool:
        if self._robot is None or self._sdk is None:
            return False
        failures: list[str] = []
        for motor_id, speed in ((0, left_speed), (1, right_speed)):
            control = self._sdk.Motor_Control()
            control.Position = 0.0
            control.Speed = float(speed)
            control.Torque = 0.0
            control.KP = -1.0
            control.KD = -1.0
            try:
                ok = self._robot.setChassis_low(
                    motor_index(self._sdk, motor_id),
                    control,
                )
                if not ok:
                    failures.append(f"motor {motor_id}: returned false")
            except BaseException as exc:
                failures.append(f"motor {motor_id}: {type(exc).__name__}: {exc}")
        if not failures:
            if left_speed == 0.0 and right_speed == 0.0:
                self._reset_chassis_command_state()
            return True

        # The SDK has no atomic two-wheel command.  If either sequential
        # setter fails, the other wheel may already be moving.  Always attempt
        # an explicit zero on *both* wheels before reporting the failure.
        compensation_failures: list[str] = []
        for motor_id in (0, 1):
            control = self._sdk.Motor_Control()
            control.Position = 0.0
            control.Speed = 0.0
            control.Torque = 0.0
            control.KP = -1.0
            control.KD = -1.0
            try:
                if not self._robot.setChassis_low(
                    motor_index(self._sdk, motor_id),
                    control,
                ):
                    compensation_failures.append(
                        f"motor {motor_id}: zero returned false"
                    )
            except BaseException as exc:
                compensation_failures.append(
                    f"motor {motor_id}: zero {type(exc).__name__}: {exc}"
                )
        stopped = not compensation_failures
        if stopped:
            self._reset_chassis_command_state()
        else:
            self._mark_chassis_stop_pending()
        detail = "; ".join(failures)
        if compensation_failures:
            detail += "; 回零异常: " + "; ".join(compensation_failures)
        self._record_event("chassis_set_failed", detail=detail)
        if require_success:
            raise RobotCommandRejected("setChassis_low() 失败；已尝试双轮回零: " + detail)
        return stopped

    def _read_motor(self, motor_id: int) -> Any:
        robot, sdk = self._require_robot()
        index = motor_index(sdk, motor_id)
        if motor_id in (0, 1):
            ok, state = robot.getChassisState(index)
        elif motor_id in (2, 3, 4):
            ok, state = robot.getWaistState(index)
        elif motor_id in (5, 6):
            ok, state = robot.getHeadState(index)
        elif motor_id in (14, 22):
            ok, state = robot.getGripperState(index)
        else:
            ok, state = robot.getArmState(index)
        if not ok:
            raise RobotUnavailable(f"读取电机 {motor_id} 状态失败")
        return state

    def _poll_state(self, *, force: bool = False) -> None:
        if self._robot is None:
            return
        now = time.monotonic()
        if not force and now < self._next_state_poll:
            return
        self._next_state_poll = now + self._state_period
        robot = self._robot
        connected = bool(robot.isRobotConnected())
        mode = enum_int(robot.getCurrentMode())
        init_state = enum_int(robot.getInitState())
        motors: dict[str, Any] = {}
        if connected:
            for motor_id in range(23):
                try:
                    state = self._read_motor(motor_id)
                    motors[str(motor_id)] = {
                        "ok": True,
                        "name": MOTOR_NAMES[motor_id],
                        "position": float(state.Position_Actual),
                        "speed": float(state.Speed_Actual),
                        "torque": float(state.Torque_Actual),
                        "kp": float(state.KP_Actual),
                        "kd": float(state.KD_Actual),
                        "error": int(state.Error_flag),
                    }
                except BaseException as exc:
                    motors[str(motor_id)] = {
                        "ok": False,
                        "name": MOTOR_NAMES[motor_id],
                        "error_message": str(exc),
                    }
        chassis = {
            "left_wheel_rad_s": None,
            "right_wheel_rad_s": None,
            "linear_m_s": None,
            "angular_rad_s": None,
            "command_left_rad_s": self._chassis_target[0],
            "command_right_rad_s": self._chassis_target[1],
            "stop_pending": self._chassis_stop_pending,
        }
        if connected:
            try:
                ok, actual, algorithm = robot.getChassisSpeedState()
                if ok:
                    chassis.update(
                        {
                            "left_wheel_rad_s": float(actual[0]),
                            "right_wheel_rad_s": float(actual[1]),
                            "linear_m_s": float(algorithm[0]),
                            "angular_rad_s": float(algorithm[1]),
                        }
                    )
            except BaseException:
                pass
        power = None
        if connected:
            try:
                ok, value = robot.getPowerChargeState()
                if ok:
                    power = {
                        "soc": int(value.soc),
                        "temperature": int(value.temperature),
                        "status": int(value.status),
                    }
            except BaseException:
                pass
        with self._snapshot_lock:
            self._snapshot.update(
                {
                    "sdk_loaded": self._sdk is not None,
                    "server_owned": True,
                    "connected": connected,
                    "control_mode": mode,
                    "control_mode_name": MODE_NAMES.get(mode, f"unknown({mode})"),
                    "init_state": init_state,
                    "init_state_name": INIT_STATE_NAMES.get(
                        init_state, f"unknown({init_state})"
                    ),
                    "motors": motors,
                    "chassis": chassis,
                    "power": power,
                    "state_monotonic": now,
                }
            )

    def _hold_current_feedback(self) -> None:
        if not self._ready_without_lease():
            self._hold_targets.clear()
            self._support_targets.clear()
            return
        ids = set(self._hold_targets) | set(self._support_targets)
        new_holds: dict[int, float] = {}
        new_support: dict[int, float] = {}
        for motor_id in ids:
            try:
                position = float(self._read_motor(motor_id).Position_Actual)
                if MOTOR_SPEC_BY_ID[motor_id].interpolate:
                    new_holds[motor_id] = position
                else:
                    new_support[motor_id] = position
            except BaseException:
                continue
        self._hold_targets = new_holds
        self._support_targets = new_support

    def _assert_operation_allowed_during_policy(self, operation: str) -> None:
        allowed = {
            "policy_state",
            "policy_step",
            "policy_end",
            "stop",
            "shutdown",
        }
        with self._policy_lock:
            active = self._policy_session_id is not None
        if active and operation not in allowed:
            raise RobotConflict(
                "Pi0.5 policy session 正在运行；普通运动/初始化/释放均已锁定，"
                "只能读取 policy state、发送 policy step、结束 session 或 STOP"
            )

    def _require_policy_readable(self, lease_id: str) -> None:
        self._require_live_lease(lease_id)
        robot, _ = self._require_robot()
        if not robot.isRobotConnected():
            raise RobotUnavailable("机器人心跳已断开")

    def _require_policy_session(
        self,
        lease_id: str,
        session_id: str | None,
    ) -> str:
        self._require_live_lease(lease_id)
        with self._policy_lock:
            active_session_id = self._policy_session_id
            owner_lease_id = self._policy_session_lease_id
        if active_session_id is None:
            raise RobotConflict(
                "Pi0.5 policy session 已结束，请操作员重新确认后重新开始"
            )
        if owner_lease_id != lease_id:
            raise RobotConflict("Pi0.5 policy session 属于其他控制租约")
        if session_id is not None and session_id != active_session_id:
            raise RobotConflict(
                "Pi0.5 policy session token 已失效，请操作员重新确认"
            )
        return active_session_id

    def _sample_policy_state(self) -> tuple[list[float], float]:
        positions: list[float] = []
        for index, motor_id in enumerate(POLICY_WIRE_MOTOR_IDS):
            motor_state = self._read_motor(motor_id)
            error_flag = int(motor_state.Error_flag)
            if error_flag != 0:
                raise RobotCommandRejected(
                    f"policy state[{index}] motor {motor_id} "
                    f"error_flag=0x{error_flag:04x}"
                )
            position = _finite_float(
                motor_state.Position_Actual,
                f"policy state[{index}] motor {motor_id} feedback",
            )
            positions.append(position)
        sampled_at = time.monotonic()
        state = positions + [0.0, 0.0]
        if len(state) != POLICY_STATE_DIM or not all(map(math.isfinite, state)):
            raise RobotCommandRejected("policy state 必须恰好为 23 个有限数")
        return state, sampled_at

    @staticmethod
    def _parse_policy_action(action: Any) -> list[float]:
        if isinstance(action, (str, bytes, bytearray, dict)):
            raise RobotCommandRejected("policy action 必须是长度 23 的数值序列")
        try:
            raw_values = list(action)
        except TypeError as exc:
            raise RobotCommandRejected(
                "policy action 必须是长度 23 的数值序列"
            ) from exc
        if len(raw_values) != POLICY_STATE_DIM:
            raise RobotCommandRejected(
                f"policy action 必须恰好 23 维，实际为 {len(raw_values)}"
            )
        values: list[float] = []
        for index, value in enumerate(raw_values):
            if isinstance(value, bool):
                raise RobotCommandRejected(
                    f"policy action[{index}] 不接受布尔值"
                )
            values.append(_finite_float(value, f"policy action[{index}]"))
        return values

    @staticmethod
    def _validate_policy_targets(
        effective_action: list[float],
        latest_state: list[float],
    ) -> None:
        if (
            len(effective_action) != POLICY_STATE_DIM
            or len(latest_state) != POLICY_STATE_DIM
        ):
            raise RobotCommandRejected("policy state/action 必须恰好为 23 维")
        if not all(map(math.isfinite, effective_action)) or not all(
            map(math.isfinite, latest_state)
        ):
            raise RobotCommandRejected("policy state/action 必须全部为有限数")

        for index in POLICY_GRIPPER_WIRE_INDICES:
            if effective_action[index] not in POLICY_GRIPPER_VALUES:
                raise RobotCommandRejected(
                    f"policy gripper action[{index}] 必须严格为 0 或 1.5"
                )

        # Model-controlled targets (arms, grippers and lift) still have to be
        # values accepted by the corresponding SDK setters.  Waist/head are
        # not model targets: indices 17..20 are overwritten from the latest
        # feedback immediately before every step.  Preserve those finite
        # feedback values exactly, including encoder drift a few ticks beyond
        # a nominal zero, instead of turning a hold command into a range fault.
        for index, (motor_id, target) in enumerate(
            zip(POLICY_WIRE_MOTOR_IDS[:17], effective_action[:17])
        ):
            spec = MOTOR_SPEC_BY_ID[motor_id]
            if not spec.minimum <= target <= spec.maximum:
                raise RobotCommandRejected(
                    f"policy action[{index}] motor {motor_id} target {target:g} "
                    f"超出 SDK 范围 [{spec.minimum:g}, {spec.maximum:g}] {spec.unit}"
                )
        if effective_action[21:] != [0.0, 0.0]:
            raise RobotCommandRejected("policy 底盘 action[21:23] 必须为零")

    def _terminate_policy_session(
        self,
        reason: str,
        *,
        expected_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Fail closed without deinit; this method never raises."""

        with self._policy_lock:
            active_session_id = self._policy_session_id
            if active_session_id is None:
                return {
                    "ok": True,
                    "already_inactive": True,
                    "reason": reason,
                    "failures": [],
                }
            if (
                expected_session_id is not None
                and active_session_id != expected_session_id
            ):
                return {
                    "ok": False,
                    "session_mismatch": True,
                    "reason": reason,
                    "failures": ["policy session changed before safe stop"],
                }
        self._cancel_event.set()
        self._policy_abort_event.set()
        failures: list[str] = []
        chassis_zero = False
        try:
            chassis_zero = self._send_chassis(
                0.0,
                0.0,
                require_success=False,
            )
        except BaseException as exc:
            failures.append(
                f"chassis zero {type(exc).__name__}: {exc}"
            )
        if not chassis_zero:
            failures.append("chassis zero not confirmed")

        new_holds: dict[int, float] = {}
        new_support: dict[int, float] = {}
        latest_positions: list[float] = []
        complete_state = True
        held_motor_ids: list[int] = []
        for motor_id in POLICY_WIRE_MOTOR_IDS:
            try:
                motor_state = self._read_motor(motor_id)
                error_flag = int(motor_state.Error_flag)
                if error_flag != 0:
                    raise RobotCommandRejected(
                        f"error_flag=0x{error_flag:04x}"
                    )
                position = _finite_float(
                    motor_state.Position_Actual,
                    f"motor[{motor_id}].Position_Actual feedback",
                )
                spec = MOTOR_SPEC_BY_ID[motor_id]
                latest_positions.append(position)
                self._send_position(motor_id, position)
                held_motor_ids.append(motor_id)
                if spec.interpolate:
                    new_holds[motor_id] = position
                else:
                    new_support[motor_id] = position
            except BaseException as exc:
                complete_state = False
                failures.append(
                    f"motor {motor_id} hold {type(exc).__name__}: {exc}"
                )

        self._hold_targets = new_holds
        self._support_targets = new_support
        with self._policy_lock:
            if self._policy_session_id == active_session_id:
                self._policy_session_id = None
                self._policy_session_lease_id = None
                self._policy_session_started_monotonic = 0.0
                self._policy_last_end_reason = str(reason)[:160]
        try:
            self._poll_state(force=True)
        except BaseException as exc:
            failures.append(f"state refresh {type(exc).__name__}: {exc}")
        self._cancel_event.clear()
        self._policy_abort_event.clear()
        self._record_event(
            "policy_session_ended",
            reason=str(reason)[:160],
            chassis_zero=chassis_zero,
            held_motor_count=len(held_motor_ids),
            failure_count=len(failures),
        )
        latest_state = (
            latest_positions + [0.0, 0.0]
            if complete_state and len(latest_positions) == POLICY_POSITION_DIM
            else None
        )
        return {
            "ok": chassis_zero and not failures,
            "session_id": active_session_id,
            "reason": reason,
            "chassis_zero": chassis_zero,
            "held_motor_ids": held_motor_ids,
            "latest_state": latest_state,
            "state": latest_state,
            "failures": failures,
        }

    def _require_robot(self) -> tuple[Any, Any]:
        if self._robot is None or self._sdk is None:
            raise RobotUnavailable("尚未接管机器人 SDK")
        return self._robot, self._sdk

    def _require_ready(self, lease_id: str) -> None:
        self._require_live_lease(lease_id)
        robot, sdk = self._require_robot()
        if not robot.isRobotConnected():
            raise RobotUnavailable("机器人心跳已断开")
        if enum_int(robot.getInitState()) != 2:
            raise RobotConflict("请先执行初始化")
        if enum_int(robot.getCurrentMode()) != enum_int(sdk.MotorControlMode.LOW_LEVEL):
            raise RobotConflict("当前不是 LOW_LEVEL，网页逐关节控制不可用")

    def _check_power_for_motion(self, *, chassis: bool) -> None:
        robot, _ = self._require_robot()
        try:
            ok, power = robot.getPowerChargeState()
        except BaseException as exc:
            raise RobotUnavailable(f"读取电池状态失败: {exc}") from exc
        if not ok:
            raise RobotUnavailable("getPowerChargeState() 返回 false")
        status = int(power.status)
        soc = int(power.soc)
        if soc < 10 and status != 1:
            raise RobotCommandRejected("电量低于 10% 且未充电，SDK 禁止运动控制")
        if chassis and status == 1:
            raise RobotCommandRejected("机器人正在充电，SDK 禁止底盘运动")

    def _ready_without_lease(self) -> bool:
        if self._robot is None or self._sdk is None:
            return False
        try:
            return (
                self._robot.isRobotConnected()
                and enum_int(self._robot.getInitState()) == 2
                and enum_int(self._robot.getCurrentMode())
                == enum_int(self._sdk.MotorControlMode.LOW_LEVEL)
            )
        except BaseException:
            return False

    def _require_lease(self, lease_id: str, *, renew: bool) -> None:
        now = time.monotonic()
        with self._lease_lock:
            if not lease_id or lease_id != self._lease_id or now >= self._lease_deadline:
                raise RobotConflict("控制权已失效，请重新接管")
            if renew:
                self._lease_deadline = now + self._lease_seconds

    def _require_live_lease(self, lease_id: str) -> None:
        self._require_lease(lease_id, renew=False)

    def _release_admission(self, token: object | None) -> None:
        if token is None:
            return
        with self._admission_lock:
            if self._admission_token is token:
                self._admission_token = None
                self._admitted_operation = None

    def _reset_chassis_command_state(self) -> None:
        self._chassis_target = (0.0, 0.0)
        self._chassis_deadline = 0.0
        self._chassis_was_moving = False
        self._chassis_stop_pending = False
        with self._snapshot_lock:
            self._snapshot["chassis"]["command_left_rad_s"] = 0.0
            self._snapshot["chassis"]["command_right_rad_s"] = 0.0
            self._snapshot["chassis"]["stop_pending"] = False

    def _mark_chassis_stop_pending(self) -> None:
        self._chassis_target = (0.0, 0.0)
        self._chassis_deadline = time.monotonic() + 0.05
        self._chassis_was_moving = False
        self._chassis_stop_pending = True
        with self._snapshot_lock:
            self._snapshot["chassis"]["command_left_rad_s"] = 0.0
            self._snapshot["chassis"]["command_right_rad_s"] = 0.0
            self._snapshot["chassis"]["stop_pending"] = True

    def _lease_is_valid(self) -> bool:
        with self._lease_lock:
            return bool(self._lease_id and time.monotonic() < self._lease_deadline)

    def _raise_if_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise RobotCommandRejected("动作已由软件停止请求取消")

    def _set_busy(self, busy: bool, operation: str | None) -> None:
        with self._snapshot_lock:
            self._snapshot["busy"] = busy
            self._snapshot["active_operation"] = operation

    def _set_error(self, message: str) -> None:
        with self._snapshot_lock:
            self._snapshot["last_error"] = message

    def _record_event(self, event: str, **fields: Any) -> None:
        with self._events_lock:
            self._events.append(
                {
                    "time_monotonic": round(time.monotonic(), 6),
                    "event": event,
                    **fields,
                }
            )

    def _deep_copy_snapshot(self) -> dict[str, Any]:
        # The snapshot contains only JSON-compatible dictionaries/scalars.
        result = dict(self._snapshot)
        result["motors"] = {
            key: dict(value) for key, value in self._snapshot["motors"].items()
        }
        result["chassis"] = dict(self._snapshot["chassis"])
        if isinstance(self._snapshot.get("power"), dict):
            result["power"] = dict(self._snapshot["power"])
        return result


__all__ = [
    "HOME_TARGETS",
    "INIT_STATE_NAMES",
    "MODE_NAMES",
    "MOTOR_SPECS",
    "POLICY_POSITION_DIM",
    "POLICY_STATE_DIM",
    "POLICY_WIRE_MOTOR_IDS",
    "RobotCallTimeout",
    "RobotCommandRejected",
    "RobotConflict",
    "RobotService",
    "RobotServiceError",
    "RobotUnavailable",
]
