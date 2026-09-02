"""Safety-latched Pi0.5 execution state machine for the web console.

This module deliberately owns neither :class:`H1Robot` nor ``CameraClient``.
It coordinates the already shared ``RobotService`` and ``CameraService`` and
uses their narrow policy/camera adapters.  In particular, importing this file
never imports the vendor robot SDK or the legacy ``Real_Env`` wrapper.

The state machine is intentionally one-shot after any transport or safety
failure.  A fault closes the policy connection and requires an explicit
``reset_fault()`` followed by a new probe and dry-run; it never reconnects or
resumes motion automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
import threading
import time
from typing import Any, Callable, Mapping, Protocol, Sequence

import numpy as np

from .pi05_protocol import (
    ACTION_DIM,
    ACTION_HORIZON,
    CAMERA_NAMES,
    GRIPPER_CLOSED_VALUE,
    GRIPPER_INDICES,
    GRIPPER_OPEN_VALUE,
    GRIPPER_VALUE_TOLERANCE,
    STATE_DIM,
    ZerithJsonPolicyClient,
    probe_healthz,
    validate_metadata,
)


PHASE_IDLE = "idle"
PHASE_PROBING = "probing"
PHASE_DRY_RUN_READY = "dry_run_ready"
PHASE_RUNNING = "running"
PHASE_STOPPING = "stopping"
PHASE_FAULT = "fault"
VALID_PHASES = frozenset(
    (
        PHASE_IDLE,
        PHASE_PROBING,
        PHASE_DRY_RUN_READY,
        PHASE_RUNNING,
        PHASE_STOPPING,
        PHASE_FAULT,
    )
)

REQUIRED_CONFIRMATION = "我确认实体急停可用并启动PI0.5真机执行"
DEFAULT_HOST = "192.168.1.154"
DEFAULT_PORT = 9973
DEFAULT_CONTROL_RATE_HZ = 30.0
DEFAULT_STEPS_PER_CHUNK = 30
DEFAULT_JOINT_SPEED_DEG_S = 30.0
# Backwards-compatible public name used by deployment documentation.
CONTROL_RATE_HZ = DEFAULT_CONTROL_RATE_HZ
ARM_JOINT_INDICES = (*range(0, 7), *range(8, 15))

logger = logging.getLogger(__name__)

# Policy wire names map to CameraService's stable logical aliases.
CAMERA_SERVICE_NAMES: Mapping[str, str] = {
    "cam_high": "head",
    "cam_left_wrist": "left_wrist",
    "cam_right_wrist": "right_wrist",
}


class Pi05ExecutorError(RuntimeError):
    """Base error raised by the Pi0.5 execution state machine."""


class Pi05StateError(Pi05ExecutorError):
    """The requested transition is not allowed in the current phase."""


class Pi05SafetyError(Pi05ExecutorError):
    """A local state, image, action, or lease safety check failed."""


class Pi05CameraNotReadyError(Pi05SafetyError):
    """A newly started shared CameraService has not published all first frames."""


class PolicyClientProtocol(Protocol):
    @property
    def usable(self) -> bool: ...

    def metadata(self, *, timeout: float = 5.0) -> dict[str, Any]: ...

    def infer(
        self,
        state: Any,
        images: Mapping[str, np.ndarray],
        prompt: str,
        *,
        jpeg_quality: int = 90,
        timeout: float | None = None,
    ) -> tuple[np.ndarray, dict[str, Any]]: ...

    def close(self) -> None: ...


PolicyFactory = Callable[[str, int], PolicyClientProtocol]
HealthProbe = Callable[[str, int], str]
CameraLifecycleCallback = Callable[[], Any]


@dataclass(frozen=True)
class _Observation:
    state: np.ndarray
    images: dict[str, np.ndarray]
    camera_ages_ms: dict[str, float]


@dataclass(frozen=True)
class _Chunk:
    actions: np.ndarray
    raw_length: int
    latency_ms: float
    camera_ages_ms: dict[str, float]
    observation_state: np.ndarray


def _default_policy_factory(host: str, port: int) -> PolicyClientProtocol:
    return ZerithJsonPolicyClient(host, port)


def _default_health_probe(host: str, port: int) -> str:
    return probe_healthz(host, port, timeout=3.0)


def _normalise_prompt(prompt: Any) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise Pi05SafetyError("prompt must be a non-empty string")
    value = prompt.strip()
    if len(value) > 1000:
        raise Pi05SafetyError("prompt must not exceed 1000 characters")
    return value


def _normalise_host(host: Any) -> str:
    if not isinstance(host, str) or not host.strip():
        raise Pi05SafetyError("policy host must be a non-empty hostname or IP address")
    value = host.strip()
    if (
        "://" in value
        or any(character in value for character in "/?#@")
        or any(character.isspace() for character in value)
    ):
        raise Pi05SafetyError(
            "policy host must not contain a URL scheme, path, credentials, or whitespace"
        )
    return value


def _normalise_port(port: Any) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise Pi05SafetyError("policy port must be an integer in 1..65535")
    return port


def _positive_execution_value(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or float(value) <= 0.0
    ):
        raise Pi05SafetyError(f"{label} must be a positive finite number")
    return float(value)


def _finite_vector(value: Any, dimension: int, label: str) -> np.ndarray:
    try:
        result = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise Pi05SafetyError(f"{label} must be numeric") from exc
    if result.shape != (dimension,):
        raise Pi05SafetyError(f"{label} must have shape ({dimension},), got {result.shape}")
    if not np.isfinite(result).all():
        raise Pi05SafetyError(f"{label} contains NaN or Inf")
    return result


def _slew_limit_arm_action(
    action: np.ndarray,
    previous_command: np.ndarray,
    max_step_rad: float,
) -> tuple[np.ndarray, tuple[int, ...]]:
    """Limit arm targets from the last successful command, never feedback.

    Using a persistent commanded target preserves accumulated position error
    when hardware feedback lags.  Re-basing on feedback every tick would make
    the target follow a drooping arm and starve the position controller of the
    error it needs to support and move the load.
    """

    limited = np.asarray(action, dtype=np.float64).copy()
    reference = np.asarray(previous_command, dtype=np.float64)
    if limited.shape != (ACTION_DIM,) or reference.shape != (ACTION_DIM,):
        raise Pi05SafetyError("action and previous command must both be 23-D")
    if not np.isfinite(limited).all() or not np.isfinite(reference).all():
        raise Pi05SafetyError("action and previous command must be finite")
    if not math.isfinite(max_step_rad) or max_step_rad <= 0.0:
        raise Pi05SafetyError("max arm step must be positive and finite")

    limited_indices: list[int] = []
    for index in ARM_JOINT_INDICES:
        delta = limited[index] - reference[index]
        if abs(delta) > max_step_rad:
            limited[index] = reference[index] + math.copysign(max_step_rad, delta)
            limited_indices.append(index)
    return limited, tuple(limited_indices)


def _strict_chunk(value: Any) -> np.ndarray:
    try:
        chunk = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise Pi05SafetyError("action chunk must be numeric") from exc
    expected = (ACTION_HORIZON, ACTION_DIM)
    if chunk.shape != expected:
        raise Pi05SafetyError(f"action chunk must have shape {expected}, got {chunk.shape}")
    if not np.isfinite(chunk).all():
        raise Pi05SafetyError("action chunk contains NaN or Inf")
    for index in GRIPPER_INDICES:
        values = chunk[:, index]
        opened = np.isclose(
            values,
            GRIPPER_OPEN_VALUE,
            rtol=0.0,
            atol=GRIPPER_VALUE_TOLERANCE,
        )
        closed = np.isclose(
            values,
            GRIPPER_CLOSED_VALUE,
            rtol=0.0,
            atol=GRIPPER_VALUE_TOLERANCE,
        )
        if not np.logical_or(opened, closed).all():
            invalid = float(values[np.flatnonzero(~np.logical_or(opened, closed))[0]])
            raise Pi05SafetyError(
                f"action gripper index {index} must contain only 0 or 1.5, got {invalid!r}"
            )
        # Canonicalise values accepted within the wire tolerance.
        chunk[:, index] = np.where(opened, GRIPPER_OPEN_VALUE, GRIPPER_CLOSED_VALUE)
    return chunk


def _apply_hold_and_zero(chunk: np.ndarray, state: np.ndarray) -> np.ndarray:
    """Apply the executor-side copy of the mandatory final action transform.

    ``RobotService.policy_step`` repeats this transform against a newly read
    state immediately before each hardware write.  Keeping it here makes a
    dry-run capable of proving the wire-to-hardware conversion without calling
    a setter and prevents unsafe fields from entering the action buffer.
    """

    effective = np.asarray(chunk, dtype=np.float64).copy()
    effective[:, 17:21] = state[17:21]
    effective[:, 21:23] = 0.0
    if not np.isfinite(effective).all():
        raise Pi05SafetyError("effective action chunk contains NaN or Inf")
    return effective


class Pi05Executor:
    """Coordinate read-only probing, dry-run validation, and execution."""

    def __init__(
        self,
        robot: Any,
        camera: Any,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        policy_factory: PolicyFactory = _default_policy_factory,
        health_probe: HealthProbe = _default_health_probe,
        camera_acquire: CameraLifecycleCallback | None = None,
        camera_release: CameraLifecycleCallback | None = None,
        camera_start_timeout_s: float = 3.0,
        inference_timeout_s: float = 10.0,
        stop_timeout_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        try:
            normalised_host = _normalise_host(host)
            normalised_port = _normalise_port(port)
        except Pi05SafetyError as exc:
            raise ValueError(str(exc)) from exc
        for value, label in (
            (camera_start_timeout_s, "camera_start_timeout_s"),
            (inference_timeout_s, "inference_timeout_s"),
            (stop_timeout_s, "stop_timeout_s"),
        ):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{label} must be a positive finite number")
        if (camera_acquire is None) != (camera_release is None):
            raise ValueError("camera_acquire and camera_release must be supplied together")

        self.robot = robot
        self.camera = camera
        self.host = normalised_host
        self.port = normalised_port
        self._policy_factory = policy_factory
        self._health_probe = health_probe
        self._camera_acquire = camera_acquire
        self._camera_release = camera_release
        self.camera_start_timeout_s = float(camera_start_timeout_s)
        self.inference_timeout_s = float(inference_timeout_s)
        self.stop_timeout_s = float(stop_timeout_s)
        self._clock = clock

        self._lock = threading.RLock()
        self._operation_lock = threading.Lock()
        # STOP closes admission through _stop_event, then crosses this barrier
        # before returning.  A policy step holds the barrier from its final
        # STOP check through the synchronous RobotService call.  Consequently
        # an already admitted step may finish, but no newly admitted setter can
        # race behind a successful stop.
        self._step_stop_barrier = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._policy: PolicyClientProtocol | None = None
        self._closed = False
        self._phase = PHASE_IDLE
        self._fault: str | None = None
        self._metadata: dict[str, Any] | None = None
        self._metadata_ok = False
        self._dry_run_ok = False
        self._dry_run_monotonic: float | None = None
        self._dry_run_prompt: str | None = None
        self._prompt = ""
        self._lease_id: str | None = None
        self._session_id: str | None = None
        self._steps_per_chunk = DEFAULT_STEPS_PER_CHUNK
        self._control_rate_hz = DEFAULT_CONTROL_RATE_HZ
        self._joint_speed_deg_s = DEFAULT_JOINT_SPEED_DEG_S
        self._max_arm_step_rad = math.radians(DEFAULT_JOINT_SPEED_DEG_S) / DEFAULT_CONTROL_RATE_HZ
        self._last_arm_slew_limited_indices: tuple[int, ...] = ()
        self._arm_slew_limited_steps = 0
        self._inference_latency_ms: float | None = None
        self._chunk_length: int | None = None
        self._executed_steps = 0
        self._camera_ages_ms: dict[str, float] = {}
        self._chunk_sequence = 0
        self._last_chunk_first_arm_delta_from_observation_rad: float | None = None
        self._last_chunk_first_arm_delta_from_feedback_rad: float | None = None
        self._max_chunk_first_arm_delta_from_feedback_rad: float | None = None

    # ------------------------------------------------------------------
    # Public status and transitions
    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self._lock:
            phase = self._phase
            if phase not in VALID_PHASES:  # defensive invariant
                phase = PHASE_FAULT
            dry_run_age_ms = (
                None
                if self._dry_run_monotonic is None
                else max(0.0, (self._clock() - self._dry_run_monotonic) * 1000.0)
            )
            try:
                connected = bool(
                    self._metadata_ok
                    and self._policy is not None
                    and getattr(self._policy, "usable", True)
                )
            except BaseException:
                connected = False
            return {
                "endpoint": f"{self.host}:{self.port}",
                "host": self.host,
                "port": self.port,
                "connected": connected,
                "phase": phase,
                "active": phase in (PHASE_RUNNING, PHASE_STOPPING),
                "fault": self._fault,
                "metadata_ok": self._metadata_ok,
                "dry_run_ok": self._dry_run_ok,
                "dry_run_age_ms": dry_run_age_ms,
                "prompt": self._prompt,
                "inference_latency_ms": self._inference_latency_ms,
                "chunk_length": self._chunk_length,
                "executed_steps": self._executed_steps,
                "camera_ages_ms": dict(self._camera_ages_ms),
                "camera_mapping": dict(CAMERA_SERVICE_NAMES),
                "camera_start_timeout_ms": self.camera_start_timeout_s * 1000.0,
                "steps_per_chunk": self._steps_per_chunk,
                "control_rate_hz": self._control_rate_hz,
                "joint_speed_deg_s": self._joint_speed_deg_s,
                "max_arm_step_rad": self._max_arm_step_rad,
                "arm_slew_reference": "previous_successful_command",
                "last_arm_slew_limited_indices": list(
                    self._last_arm_slew_limited_indices
                ),
                "arm_slew_limited_steps": self._arm_slew_limited_steps,
                "chunk_request_mode": "after_chunk_sync",
                "chunk_sequence": self._chunk_sequence,
                "last_chunk_first_arm_delta_from_observation_rad": (
                    self._last_chunk_first_arm_delta_from_observation_rad
                ),
                "last_chunk_first_arm_delta_from_feedback_rad": (
                    self._last_chunk_first_arm_delta_from_feedback_rad
                ),
                "max_chunk_first_arm_delta_from_feedback_rad": (
                    self._max_chunk_first_arm_delta_from_feedback_rad
                ),
                "default_steps_per_chunk": DEFAULT_STEPS_PER_CHUNK,
                "default_control_rate_hz": DEFAULT_CONTROL_RATE_HZ,
                "default_joint_speed_deg_s": DEFAULT_JOINT_SPEED_DEG_S,
                "required_confirmation": REQUIRED_CONFIRMATION,
            }

    def probe(self) -> dict[str, Any]:
        """Probe health and metadata on the currently selected endpoint."""

        with self._lock:
            host, port = self.host, self.port
        return self.reconnect(host, port)

    def reconnect(self, host: str, port: int) -> dict[str, Any]:
        """Explicitly replace the one-shot connection and validate metadata.

        This is the only endpoint-changing operation.  It is never called by
        the inference or recovery paths, and a failed request remains a
        latched fault until the operator explicitly resets it.
        """

        selected_host = _normalise_host(host)
        selected_port = _normalise_port(port)
        with self._operation_lock:
            self._require_open()
            with self._lock:
                if self._phase not in (PHASE_IDLE, PHASE_DRY_RUN_READY):
                    raise Pi05StateError(
                        f"reconnect is not allowed while phase={self._phase}"
                    )
                self._phase = PHASE_PROBING

            # The prior connection is closed before the endpoint and its
            # validation state are replaced.  There is never an overlap of two
            # policy WebSockets owned by this executor.
            self._close_policy(invalidate=True)
            with self._lock:
                self.host = selected_host
                self.port = selected_port
                self._fault = None
                self._clear_connection_readiness_locked()

            client: PolicyClientProtocol | None = None
            try:
                health = self._health_probe(selected_host, selected_port)
                if str(health).strip() != "OK":
                    raise Pi05SafetyError(f"unexpected health response: {health!r}")
                client = self._policy_factory(selected_host, selected_port)
                metadata = validate_metadata(client.metadata(timeout=5.0))
                if not bool(getattr(client, "usable", True)):
                    raise Pi05SafetyError(
                        "policy client became unusable during metadata probe"
                    )
                with self._lock:
                    self._policy = client
                    self._metadata = metadata
                    self._metadata_ok = True
                    self._phase = PHASE_IDLE
                client = None
                return {
                    "health": "OK",
                    "metadata": dict(metadata),
                    "status": self.status(),
                }
            except BaseException as exc:
                if client is not None:
                    try:
                        client.close()
                    except BaseException:
                        pass
                self._latch_fault(exc)
                raise

    def disconnect(
        self,
        *,
        timeout: float | None = None,
        reason: str = "operator_disconnect",
    ) -> dict[str, Any]:
        """Safely stop and explicitly close policy transport without deinit.

        A normal connection returns to ``idle``.  A latched fault deliberately
        remains in ``fault`` after transport teardown so disconnect cannot be
        used to bypass the required operator reset/probe/dry-run sequence.
        """

        self.stop(timeout=timeout, reason=reason)
        with self._operation_lock:
            self._close_policy(invalidate=True)
            with self._lock:
                self._clear_connection_readiness_locked()
                if self._phase != PHASE_FAULT:
                    self._finish_idle_locked()
        return self.status()

    def dry_run(self, prompt: str, lease_id: str) -> dict[str, Any]:
        """Request and validate exactly one chunk without any hardware setter."""

        prompt = _normalise_prompt(prompt)
        if not isinstance(lease_id, str) or not lease_id:
            raise Pi05SafetyError("dry-run requires a non-empty control lease")
        with self._operation_lock:
            self._require_open()
            with self._lock:
                if self._phase not in (PHASE_IDLE, PHASE_DRY_RUN_READY):
                    raise Pi05StateError(f"dry-run is not allowed while phase={self._phase}")
                if not self._metadata_ok or self._policy is None:
                    raise Pi05StateError("probe metadata successfully before dry-run")
                self._dry_run_ok = False
                self._prompt = prompt

            camera_acquired = False
            try:
                self._require_live_lease(lease_id)
                camera_acquired = self._acquire_camera()
                observation = self._capture_initial_observation(
                    lease_id,
                    session_id=None,
                )
                request_started = self._clock()
                actions, _raw = self._infer(observation, prompt)
                response_time = self._clock()
                latency_ms = max(0.0, (response_time - request_started) * 1000.0)
                effective = _apply_hold_and_zero(actions, observation.state)
                first_arm_delta = float(
                    np.max(
                        np.abs(
                            effective[0, list(ARM_JOINT_INDICES)]
                            - observation.state[list(ARM_JOINT_INDICES)]
                        )
                    )
                )
                hold_ok = bool(
                    np.array_equal(effective[:, 17:21], np.broadcast_to(observation.state[17:21], (ACTION_HORIZON, 4)))
                )
                base_zero_ok = bool(np.count_nonzero(effective[:, 21:23]) == 0)
                if not hold_ok or not base_zero_ok:
                    raise Pi05SafetyError("dry-run hold/zero transformation did not verify")
                with self._lock:
                    self._phase = PHASE_DRY_RUN_READY
                    self._dry_run_ok = True
                    self._dry_run_monotonic = response_time
                    self._dry_run_prompt = prompt
                    self._inference_latency_ms = latency_ms
                    self._chunk_length = int(actions.shape[0])
                    self._camera_ages_ms = dict(observation.camera_ages_ms)
                return {
                    "ok": True,
                    "state_dim": int(observation.state.shape[0]),
                    "chunk_length": int(actions.shape[0]),
                    "action_dim": int(actions.shape[1]),
                    "hold_verified": hold_ok,
                    "base_zero_verified": base_zero_ok,
                    "gripper_values": {
                        str(index): sorted(float(value) for value in np.unique(actions[:, index]))
                        for index in GRIPPER_INDICES
                    },
                    "first_effective_action": effective[0].tolist(),
                    "first_arm_delta_from_observation_rad": first_arm_delta,
                    "camera_ages_ms": dict(observation.camera_ages_ms),
                    "inference_latency_ms": latency_ms,
                }
            except BaseException as exc:
                self._latch_fault(exc)
                raise
            finally:
                if camera_acquired:
                    self._release_camera_best_effort()

    def start(
        self,
        prompt: str,
        lease_id: str,
        *,
        confirmation: str,
        steps_per_chunk: int = DEFAULT_STEPS_PER_CHUNK,
        control_rate_hz: float = DEFAULT_CONTROL_RATE_HZ,
        joint_speed_deg_s: float = DEFAULT_JOINT_SPEED_DEG_S,
    ) -> dict[str, Any]:
        """Start continuous background execution after all operator gates pass.

        The policy protocol always returns 50 actions.  ``steps_per_chunk``
        selects how many leading actions to consume from every response;
        it is not a total run limit.  Execution continues across chunks until
        an explicit stop, a fault, or lease loss.
        """

        prompt = _normalise_prompt(prompt)
        if confirmation != REQUIRED_CONFIRMATION:
            raise Pi05SafetyError("exact Pi0.5 motion confirmation phrase is required")
        if (
            isinstance(steps_per_chunk, bool)
            or not isinstance(steps_per_chunk, int)
            or not 1 <= steps_per_chunk <= ACTION_HORIZON
        ):
            raise Pi05SafetyError(
                f"steps_per_chunk must be an integer in 1..{ACTION_HORIZON}"
            )
        if not isinstance(lease_id, str) or not lease_id:
            raise Pi05SafetyError("start requires a non-empty control lease")
        selected_rate_hz = _positive_execution_value(
            control_rate_hz,
            "control_rate_hz",
        )
        selected_joint_speed_deg_s = _positive_execution_value(
            joint_speed_deg_s,
            "joint_speed_deg_s",
        )
        max_arm_step_rad = (
            math.radians(selected_joint_speed_deg_s) / selected_rate_hz
        )
        with self._operation_lock:
            self._require_open()
            with self._lock:
                if self._phase != PHASE_DRY_RUN_READY:
                    raise Pi05StateError("a successful dry-run is required before start")
                if not self._metadata_ok or not self._dry_run_ok:
                    raise Pi05StateError("probe and dry-run must both be successful")
                if prompt != self._dry_run_prompt:
                    raise Pi05SafetyError("start prompt must exactly match the validated dry-run prompt")
                if self._policy is None or not bool(getattr(self._policy, "usable", True)):
                    raise Pi05StateError("policy connection is not usable; probe again")
                # Register this start attempt with a fresh stop token before
                # the potentially slow begin_policy_session() call.  A
                # concurrent stop sets this exact Event; a STOP from a prior
                # completed run must not permanently poison future,
                # explicitly re-probed attempts.
                start_stop_event = threading.Event()
                self._stop_event = start_stop_event
            self._require_live_lease(lease_id)

            # Establish RobotService's backend-wide policy exclusion before
            # returning HTTP success.  Starting this only inside the new
            # thread would leave a scheduling window in which a manual or
            # voice command could be admitted after the operator pressed
            # "start" but before the policy mutex became active.
            session_id: str | None = None
            try:
                session = self.robot.begin_policy_session(lease_id)
                if (
                    not isinstance(session, Mapping)
                    or not isinstance(session.get("session_id"), str)
                    or not session["session_id"]
                ):
                    raise Pi05SafetyError(
                        "begin_policy_session did not return a valid session_id"
                    )
                session_id = str(session["session_id"])
                with self._lock:
                    # stop() deliberately does not wait on _operation_lock:
                    # it must remain able to interrupt a slow SDK call.  It
                    # may therefore run while begin_policy_session() is in
                    # flight.  Never replace the Event that stop() set or
                    # resurrect a phase that stop() already returned to idle.
                    if (
                        self._stop_event is not start_stop_event
                        or start_stop_event.is_set()
                        or self._phase != PHASE_DRY_RUN_READY
                        or self._policy is None
                        or not bool(getattr(self._policy, "usable", True))
                    ):
                        start_cancelled = True
                    else:
                        start_cancelled = False
                        self._phase = PHASE_RUNNING
                        self._fault = None
                        self._prompt = prompt
                        self._lease_id = lease_id
                        self._session_id = session_id
                        self._steps_per_chunk = steps_per_chunk
                        self._control_rate_hz = selected_rate_hz
                        self._joint_speed_deg_s = selected_joint_speed_deg_s
                        self._max_arm_step_rad = max_arm_step_rad
                        self._last_arm_slew_limited_indices = ()
                        self._arm_slew_limited_steps = 0
                        self._executed_steps = 0
                        self._chunk_sequence = 0
                        self._last_chunk_first_arm_delta_from_observation_rad = None
                        self._last_chunk_first_arm_delta_from_feedback_rad = None
                        self._max_chunk_first_arm_delta_from_feedback_rad = None
                        thread = threading.Thread(
                            target=self._run,
                            args=(
                                prompt,
                                lease_id,
                                steps_per_chunk,
                                session_id,
                                selected_rate_hz,
                                selected_joint_speed_deg_s,
                            ),
                            name="pi05-executor",
                            daemon=True,
                        )
                        self._thread = thread
                        # Keep the state lock through Thread.start(): stop()
                        # must never observe an assigned-but-not-started
                        # Thread and attempt to join it.
                        thread.start()
                if start_cancelled:
                    self._best_effort_hold_zero(
                        lease_id,
                        session_id,
                        "concurrent_operator_stop",
                    )
                    self._best_effort_end_session(
                        lease_id,
                        session_id,
                        "concurrent_operator_stop",
                    )
                    self._close_policy(invalidate=True)
                    with self._lock:
                        if self._phase != PHASE_FAULT:
                            self._finish_idle_locked()
                    raise Pi05StateError(
                        "Pi0.5 start was cancelled by a concurrent stop"
                    )
                return self.status()
            except BaseException as exc:
                if session_id is not None:
                    self._best_effort_hold_zero(
                        lease_id,
                        session_id,
                        "executor_start_failed",
                    )
                    self._best_effort_end_session(
                        lease_id,
                        session_id,
                        "executor_start_failed",
                    )
                with self._lock:
                    cancelled_by_stop = (
                        self._stop_event.is_set()
                        and self._phase in (PHASE_IDLE, PHASE_STOPPING)
                    )
                if cancelled_by_stop:
                    self._close_policy(invalidate=True)
                    with self._lock:
                        if self._phase != PHASE_FAULT:
                            self._finish_idle_locked()
                    if isinstance(exc, Pi05StateError):
                        raise
                    raise Pi05StateError(
                        "Pi0.5 start was cancelled by a concurrent stop"
                    ) from exc
                self._latch_fault(exc)
                raise

    def stop(self, *, timeout: float | None = None, reason: str = "operator_stop") -> dict[str, Any]:
        """Interrupt execution, hold/zero best-effort, and never deinitialize."""

        selected_timeout = self.stop_timeout_s if timeout is None else float(timeout)
        if not math.isfinite(selected_timeout) or selected_timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        with self._lock:
            thread = self._thread
            if self._phase in (PHASE_RUNNING, PHASE_STOPPING):
                self._phase = PHASE_STOPPING
            self._stop_event.set()
            lease_id = self._lease_id
            session_id = self._session_id

        # Drain the one policy step that may already have crossed its final
        # STOP check.  Do not hold _lock while waiting: the in-flight executor
        # thread updates status under that lock after RobotService returns.
        # Every later step sees the Event while holding this same barrier and
        # exits without calling policy_step().
        with self._step_stop_barrier:
            pass

        # Closing the one-shot connection is how a blocking recv is interrupted;
        # no replacement connection is created here.
        self._close_policy(invalidate=True)
        if lease_id:
            self._best_effort_hold_zero(lease_id, session_id, reason)

        if thread is not None and thread is not threading.current_thread():
            thread.join(selected_timeout)
            if thread.is_alive():
                self._latch_fault(Pi05ExecutorError("executor thread did not stop before timeout"))
        with self._lock:
            if self._phase != PHASE_FAULT and (thread is None or not thread.is_alive()):
                self._finish_idle_locked()
        return self.status()

    def reset_fault(self) -> dict[str, Any]:
        """Explicitly clear a latched fault without probing or restarting."""

        with self._operation_lock:
            with self._lock:
                if self._closed:
                    raise Pi05StateError("executor is closed")
                if self._phase != PHASE_FAULT:
                    raise Pi05StateError("reset_fault is only allowed in fault phase")
                if self._thread is not None and self._thread.is_alive():
                    raise Pi05StateError("executor thread is still stopping")
                self._fault = None
                self._finish_idle_locked()
            self._close_policy(invalidate=True)
            return self.status()

    def wait(self, timeout: float | None = None) -> dict[str, Any]:
        """Wait for execution; Ctrl+C requests the same stop path."""

        with self._lock:
            thread = self._thread
        if thread is None:
            return self.status()
        try:
            thread.join(timeout)
        except KeyboardInterrupt:
            self.stop(reason="keyboard_interrupt")
            raise
        return self.status()

    def close(self) -> None:
        """Stop threads and policy I/O without ever calling robot_deinit()."""

        with self._lock:
            if self._closed:
                return
        try:
            self.stop(reason="executor_close")
        finally:
            self._close_policy(invalidate=True)
            with self._lock:
                self._closed = True

    # ------------------------------------------------------------------
    # Execution loop and inference pipeline
    # ------------------------------------------------------------------
    def _run(
        self,
        prompt: str,
        lease_id: str,
        steps_per_chunk: int,
        session_id: str,
        control_rate_hz: float,
        joint_speed_deg_s: float,
    ) -> None:
        camera_acquired = False
        normal_stop = False
        try:
            camera_acquired = self._acquire_camera()
            self._require_live_lease(lease_id)

            # Deliberately request synchronously from the state observed after
            # the preceding selected chunk has finished.  Starting inference
            # several control ticks early and then consuming action[0] made
            # the next plan stale at activation and caused large boundary
            # target jumps.
            chunk = self._request_chunk(
                lease_id,
                session_id,
                prompt,
                True,
            )
            active_actions = self._activate_chunk(
                chunk,
                steps_per_chunk,
            )
            previous_command = chunk.observation_state.copy()
            max_arm_step_rad = (
                math.radians(joint_speed_deg_s) / control_rate_hz
            )
            index = 0

            while True:
                if self._stop_event.is_set():
                    normal_stop = True
                    break
                if not self.robot.has_live_lease(lease_id):
                    raise Pi05SafetyError("control lease expired during Pi0.5 execution")

                remaining = len(active_actions) - index
                if remaining <= 0:
                    # Capture state/images only after the previous selected
                    # actions have all completed.  The policy's action[0]
                    # therefore belongs to the current robot state rather
                    # than to a state observed eight ticks in the past.
                    chunk = self._request_chunk(
                        lease_id,
                        session_id,
                        prompt,
                    )
                    active_actions = self._activate_chunk(
                        chunk,
                        steps_per_chunk,
                    )
                    index = 0
                    remaining = len(active_actions)

                # Re-check immediately before handing the action to the
                # serialized hardware owner.  The checks at the top of the
                # loop intentionally remain too: this second gate narrows the
                # stop/lease-expiry race after chunk bookkeeping.
                with self._step_stop_barrier:
                    if self._stop_event.is_set():
                        normal_stop = True
                        break
                    self._require_live_lease(lease_id)
                    tick_started = self._clock()
                    raw_action = active_actions[index]
                    action, limited_indices = _slew_limit_arm_action(
                        raw_action,
                        previous_command,
                        max_arm_step_rad,
                    )
                    # RobotService re-reads the latest 23-D state, reapplies
                    # hold/zero, and only then sends action[:21].  Keeping the
                    # synchronous call inside the barrier gives STOP a clear
                    # drain point without interrupting an already in-flight
                    # SDK operation.
                    step_result = self.robot.policy_step(
                        lease_id,
                        action.tolist(),
                        session_id=session_id,
                    )
                    if index == 0:
                        self._record_chunk_first_feedback_delta(step_result)
                    previous_command[list(ARM_JOINT_INDICES)] = action[
                        list(ARM_JOINT_INDICES)
                    ]
                    with self._lock:
                        self._last_arm_slew_limited_indices = limited_indices
                        if limited_indices:
                            self._arm_slew_limited_steps += 1
                index += 1
                with self._lock:
                    self._executed_steps += 1
                elapsed = max(0.0, self._clock() - tick_started)
                delay = max(0.0, 1.0 / control_rate_hz - elapsed)
                if self._stop_event.wait(delay):
                    normal_stop = True
                    break
        except BaseException as exc:
            if self._stop_event.is_set() and self._phase_is_stopping():
                normal_stop = True
            else:
                self._latch_fault(exc)
        finally:
            # Interrupt any in-flight recv.  Closing is one-shot and never
            # constructs a replacement connection.
            self._close_policy(invalidate=True)
            with self._lock:
                session_id = self._session_id
            self._best_effort_hold_zero(
                lease_id,
                session_id,
                "execution_stopped" if normal_stop else "executor_fault",
            )
            self._best_effort_end_session(
                lease_id,
                session_id,
                "execution_stopped" if normal_stop else "executor_fault",
            )
            if camera_acquired:
                self._release_camera_best_effort()
            with self._lock:
                self._session_id = None
                self._lease_id = None
                self._thread = None
                if normal_stop and self._phase != PHASE_FAULT:
                    self._finish_idle_locked()

    def _request_chunk(
        self,
        lease_id: str,
        session_id: str,
        prompt: str,
        allow_camera_startup: bool = False,
    ) -> _Chunk:
        if self._stop_event.is_set():
            raise Pi05StateError("execution is stopping")
        self._require_live_lease(lease_id)
        observation = (
            self._capture_initial_observation(lease_id, session_id=session_id)
            if allow_camera_startup
            else self._capture_observation(lease_id, session_id=session_id)
        )
        request_started = self._clock()
        actions, _raw = self._infer(observation, prompt)
        response_monotonic = self._clock()
        latency_ms = max(0.0, (response_monotonic - request_started) * 1000.0)
        effective = _apply_hold_and_zero(actions, observation.state)
        with self._lock:
            self._inference_latency_ms = latency_ms
            self._chunk_length = int(actions.shape[0])
            self._camera_ages_ms = dict(observation.camera_ages_ms)
        return _Chunk(
            actions=effective.copy(),
            raw_length=int(actions.shape[0]),
            latency_ms=latency_ms,
            camera_ages_ms=dict(observation.camera_ages_ms),
            observation_state=observation.state.copy(),
        )

    def _activate_chunk(
        self,
        chunk: _Chunk,
        steps_per_chunk: int,
    ) -> np.ndarray:
        """Select the configured leading actions from a 50-step chunk."""

        stop = min(chunk.raw_length, steps_per_chunk)
        selected = chunk.actions[:stop].copy()
        if selected.ndim != 2 or selected.shape[1:] != (ACTION_DIM,) or not len(selected):
            raise Pi05SafetyError("action chunk has no executable steps")
        if not np.isfinite(selected).all():
            raise Pi05SafetyError("activated action chunk contains NaN or Inf")
        first_delta = float(
            np.max(
                np.abs(
                    selected[0, list(ARM_JOINT_INDICES)]
                    - chunk.observation_state[list(ARM_JOINT_INDICES)]
                )
            )
        )
        with self._lock:
            self._chunk_sequence += 1
            sequence = self._chunk_sequence
            self._last_chunk_first_arm_delta_from_observation_rad = first_delta
        logger.info(
            "Pi0.5 chunk %d activated synchronously: first_arm_delta_from_observation=%.6f rad",
            sequence,
            first_delta,
        )
        return selected

    def _record_chunk_first_feedback_delta(self, step_result: Any) -> None:
        """Record boundary continuity without rejecting or rewriting motion."""

        if not isinstance(step_result, Mapping):
            return
        try:
            latest = np.asarray(step_result.get("latest_state"), dtype=np.float64)
            effective = np.asarray(step_result.get("effective_action"), dtype=np.float64)
        except (TypeError, ValueError):
            return
        if latest.shape != (STATE_DIM,) or effective.shape != (ACTION_DIM,):
            return
        if not np.isfinite(latest).all() or not np.isfinite(effective).all():
            return
        delta = float(
            np.max(
                np.abs(
                    effective[list(ARM_JOINT_INDICES)]
                    - latest[list(ARM_JOINT_INDICES)]
                )
            )
        )
        with self._lock:
            self._last_chunk_first_arm_delta_from_feedback_rad = delta
            current_max = self._max_chunk_first_arm_delta_from_feedback_rad
            if current_max is None or delta > current_max:
                self._max_chunk_first_arm_delta_from_feedback_rad = delta
            sequence = self._chunk_sequence
        logger.info(
            "Pi0.5 chunk %d first command: max_arm_target_feedback_delta=%.6f rad",
            sequence,
            delta,
        )

    def _infer(self, observation: _Observation, prompt: str) -> tuple[np.ndarray, dict[str, Any]]:
        with self._lock:
            client = self._policy
        if client is None or not bool(getattr(client, "usable", True)):
            raise Pi05StateError("policy client is not usable")
        response = client.infer(
            observation.state,
            observation.images,
            prompt,
            timeout=self.inference_timeout_s,
        )
        if not isinstance(response, tuple) or len(response) != 2:
            raise Pi05SafetyError("policy infer must return (actions, raw_response)")
        actions, raw = response
        if not isinstance(raw, Mapping):
            raise Pi05SafetyError("policy raw response must be a JSON object")
        return _strict_chunk(actions), dict(raw)

    # ------------------------------------------------------------------
    # Shared robot and camera adapters
    # ------------------------------------------------------------------
    def _capture_initial_observation(
        self,
        lease_id: str,
        *,
        session_id: str | None,
    ) -> _Observation:
        """Wait only for a newly started CameraService's first frame set.

        CameraClient.start() returns before its polling thread necessarily
        publishes a frame.  This bounded wait is used only before dry-run and
        before the first execution inference, when no action has been sent.
        Missing frames later in a run remain immediate terminal faults.
        """

        waiter = getattr(self.camera, "wait_for_frame", None)
        if not callable(waiter):
            # Test/minimal camera adapters publish synchronously and need no
            # lifecycle warm-up.  Preserve their strict one-shot semantics.
            return self._capture_observation(lease_id, session_id=session_id)

        deadline = time.monotonic() + self.camera_start_timeout_s
        while True:
            self._require_live_lease(lease_id)
            missing_wire_name: str | None = None
            for wire_name in CAMERA_NAMES:
                logical_name = CAMERA_SERVICE_NAMES[wire_name]
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    raise Pi05CameraNotReadyError(
                        f"missing camera frame {missing_wire_name or wire_name} "
                        f"after {self.camera_start_timeout_s:g}s startup wait"
                    )
                snapshot = waiter(
                    logical_name,
                    "rgb",
                    timeout=min(0.05, remaining),
                    copy=False,
                )
                if snapshot is None:
                    missing_wire_name = wire_name
                    break
                if session_id is not None and self._stop_event.is_set():
                    raise Pi05StateError("execution stopped during camera startup")
            if missing_wire_name is not None:
                continue
            try:
                return self._capture_observation(
                    lease_id,
                    session_id=session_id,
                )
            except Pi05CameraNotReadyError:
                # A lifecycle race may invalidate a frame between the wait and
                # the strict snapshot.  Retry only within this initial bounded
                # window; later observations never use this path.
                if time.monotonic() >= deadline:
                    raise

    def _capture_observation(self, lease_id: str, *, session_id: str | None) -> _Observation:
        result = self.robot.read_policy_state(lease_id, session_id=session_id)
        if not isinstance(result, Mapping):
            raise Pi05SafetyError("read_policy_state must return an object")
        state = _finite_vector(result.get("state"), STATE_DIM, "robot state")
        now = self._clock()

        images: dict[str, np.ndarray] = {}
        ages_ms: dict[str, float] = {}
        for wire_name in CAMERA_NAMES:
            logical_name = CAMERA_SERVICE_NAMES[wire_name]
            snapshot = self.camera.get_latest(logical_name, "rgb", copy=True)
            if snapshot is None:
                raise Pi05CameraNotReadyError(f"missing camera frame {wire_name}")
            image = np.asarray(getattr(snapshot, "image", None))
            if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
                raise Pi05SafetyError(
                    f"camera {wire_name} must be HxWx3 uint8 BGR, got {image.shape} {image.dtype}"
                )
            if image.shape[0] <= 0 or image.shape[1] <= 0:
                raise Pi05SafetyError(f"camera {wire_name} has an empty frame")
            received_ns = getattr(snapshot, "host_getter_monotonic_ns", None)
            if isinstance(received_ns, bool) or not isinstance(received_ns, (int, float)) or not math.isfinite(received_ns):
                raise Pi05SafetyError(f"camera {wire_name} has no valid host timestamp")
            received_s = float(received_ns) / 1_000_000_000.0
            age_s = now - received_s
            images[wire_name] = image.copy()
            ages_ms[wire_name] = max(0.0, age_s * 1000.0)
        with self._lock:
            self._camera_ages_ms = dict(ages_ms)
        return _Observation(
            state=state,
            images=images,
            camera_ages_ms=ages_ms,
        )

    def _require_live_lease(self, lease_id: str) -> None:
        if not bool(self.robot.has_live_lease(lease_id)):
            raise Pi05SafetyError("control lease is not live")

    def _acquire_camera(self) -> bool:
        if self._camera_acquire is None:
            return False
        self._camera_acquire()
        return True

    def _release_camera_best_effort(self) -> None:
        if self._camera_release is None:
            return
        try:
            self._camera_release()
        except BaseException as exc:
            # During motion this is a cleanup fault worth exposing, but it must
            # never prevent the robot session cleanup below/above it.
            with self._lock:
                if self._phase not in (PHASE_IDLE, PHASE_FAULT):
                    self._fault = self._fault or f"camera release failed: {type(exc).__name__}: {exc}"
                    self._phase = PHASE_FAULT

    def _best_effort_hold_zero(self, lease_id: str, session_id: str | None, reason: str) -> None:
        try:
            self.robot.policy_hold_and_zero(lease_id, session_id=session_id, reason=reason)
            return
        except BaseException:
            pass
        # Retain compatibility with the pre-policy RobotService signature as
        # a final best effort.  The RobotService watchdog independently holds
        # the robot and zeros the base if the browser lease has already
        # expired, even when this public fallback rejects that stale lease.
        try:
            self.robot.stop_motion(lease_id, renew_lease=False)
        except TypeError:
            try:
                self.robot.stop_motion(lease_id)
            except BaseException:
                pass
        except BaseException:
            pass

    def _best_effort_end_session(self, lease_id: str, session_id: str | None, reason: str) -> None:
        try:
            self.robot.end_policy_session(lease_id, session_id=session_id, reason=reason)
        except BaseException:
            pass

    # ------------------------------------------------------------------
    # Internal state helpers
    # ------------------------------------------------------------------
    def _phase_is_stopping(self) -> bool:
        with self._lock:
            return self._phase == PHASE_STOPPING

    def _require_open(self) -> None:
        with self._lock:
            if self._closed:
                raise Pi05StateError("executor is closed")
            if self._phase == PHASE_FAULT:
                raise Pi05StateError("fault is latched; reset it explicitly before continuing")

    def _latch_fault(self, exc: BaseException) -> None:
        detail = f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._fault = detail
            self._phase = PHASE_FAULT
            self._clear_connection_readiness_locked()
            self._stop_event.set()
        self._close_policy(invalidate=True)

    def _clear_connection_readiness_locked(self) -> None:
        self._metadata = None
        self._metadata_ok = False
        self._dry_run_ok = False
        self._dry_run_monotonic = None
        self._dry_run_prompt = None

    def _finish_idle_locked(self) -> None:
        self._phase = PHASE_IDLE
        self._clear_connection_readiness_locked()
        self._lease_id = None
        self._session_id = None

    def _close_policy(self, *, invalidate: bool = False) -> None:
        with self._lock:
            client = self._policy
            self._policy = None
            if invalidate:
                self._clear_connection_readiness_locked()
        if client is not None:
            try:
                client.close()
            except BaseException:
                pass


Pi05ExecutorService = Pi05Executor


__all__ = [
    "CAMERA_SERVICE_NAMES",
    "CONTROL_RATE_HZ",
    "DEFAULT_CONTROL_RATE_HZ",
    "DEFAULT_JOINT_SPEED_DEG_S",
    "DEFAULT_STEPS_PER_CHUNK",
    "PHASE_DRY_RUN_READY",
    "PHASE_FAULT",
    "PHASE_IDLE",
    "PHASE_PROBING",
    "PHASE_RUNNING",
    "PHASE_STOPPING",
    "Pi05CameraNotReadyError",
    "Pi05Executor",
    "Pi05ExecutorError",
    "Pi05ExecutorService",
    "Pi05SafetyError",
    "Pi05StateError",
    "REQUIRED_CONFIRMATION",
]
