"""One network worker + independent control clock for the 8080 RTC executor."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
import queue
import threading
import time
import uuid

import numpy as np

from .pi05_rtc import RtcBuffer, DelayEstimator, envelope, validate_rtc_metadata
from .pi05_protocol import ProtocolValidationError
from .pi05_action_modes import apply_execution_mode

logger = logging.getLogger(__name__)


class OneWorker:
    """One owner of send/recv. One job/result slot, never an observation backlog."""

    def __init__(self):
        self.jobs = queue.Queue(maxsize=1)
        self.done = threading.Event()
        self.closed = threading.Event()
        self.pending = False
        self.result = None
        self.error = None
        self.thread = threading.Thread(
            target=self._loop, name="pi05-rtc-network", daemon=True
        )
        self.thread.start()

    def _loop(self):
        while not self.closed.is_set():
            try:
                job = self.jobs.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                self.result = job()
            except BaseException as exc:
                self.error = exc
            finally:
                self.done.set()

    def submit(self, job):
        if self.pending or self.closed.is_set():
            raise RuntimeError("RTC permits only one request/result in flight")
        self.result = None
        self.error = None
        self.done.clear()
        self.pending = True
        self.jobs.put_nowait(job)

    def take(self):
        if not self.pending or not self.done.is_set():
            raise RuntimeError("RTC result is not ready")
        self.pending = False
        if self.error:
            raise self.error
        return self.result

    def close(self):
        self.closed.set()
        self.thread.join(timeout=0.2)


@dataclass
class Reply:
    actions: np.ndarray
    model: np.ndarray
    raw: dict
    observation: object
    observation_time: float
    observation_tick: int
    generation: int
    s: int
    d: int
    identifiers: dict
    ready_time: float


class RtcRunner:
    def __init__(
        self,
        executor,
        plan,
        lease_id,
        session_id,
        initial_state,
        *,
        dry=False,
        stage=None,
        prompt=None,
    ):
        self.ex = executor
        self.plan = plan
        self.lease = lease_id
        self.robot_session = session_id
        self.config = executor._rtc_config
        self.dry = dry
        self.stage = stage or (
            "single_" + plan.active_hand
            if plan.mode == "single"
            else "dual_separate_left" if plan.mode == "dual_separate" else plan.mode
        )
        self.prompt = prompt or plan.prompt
        self.initial = np.asarray(initial_state, dtype=np.float64).copy()
        self.previous = self.initial.copy()
        self.buffer = RtcBuffer()
        self.delay = DelayEstimator(self.config)
        self.lock = threading.Lock()
        self.worker = OneWorker()
        self.session = uuid.uuid4().hex
        self.request_id = 0
        self.episode_epoch = 0
        self.phase_epoch = 0
        self.base = executor._clock()
        self.hz = 30.0
        self.metadata = None
        self.handoffs = []
        self.missed = 0
        self.underruns = 0
        self.stale = 0
        self.left_closed = 0
        self.first_capture = True
        self.network_started = None

    def tick(self, now=None):
        return max(
            0,
            int(
                math.floor(
                    ((self.ex._clock() if now is None else now) - self.base) * self.hz
                    + 1e-7
                )
            ),
        )

    def status(self, phase):
        return dict(
            state=phase,
            session_id=self.session,
            episode_epoch=self.episode_epoch,
            phase_epoch=self.phase_epoch,
            generation=self.buffer.generation,
            cursor=self.buffer.cursor,
            estimated_delay_steps=self.delay.steps,
            delay_samples=list(self.delay.samples),
            metadata=self.metadata,
            deadline_misses=self.missed,
            buffer_underruns=self.underruns,
            stale_responses=self.stale,
            chunks=list(self.handoffs[-20:]),
        )

    def publish(self, phase):
        with self.ex._lock:
            self.ex._rtc_status = self.status(phase)

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        while not self.worker.done.wait(0.01):
            if self.ex._stop_event.is_set():
                raise RuntimeError("RTC stopped")
            self.ex._require_live_lease(self.lease)
            if time.monotonic() > deadline:
                raise TimeoutError("RTC request timed out")
        if self.ex._stop_event.is_set():
            raise RuntimeError("RTC stopped")
        return self.worker.take()

    def handshake(self):
        self.worker.submit(
            lambda: validate_rtc_metadata(
                self.ex._policy.metadata(timeout=self.ex.inference_timeout_s)
            )
        )
        self.metadata = self.wait(self.ex.inference_timeout_s)
        previous_metadata = self.ex._rtc_status.get("metadata")
        if not self.dry and previous_metadata is not None:
            for key in (
                "rtc_action_space_id",
                "input_gripper_binary",
                "output_gripper_binary",
            ):
                if self.metadata[key] != previous_metadata[key]:
                    raise ProtocolValidationError(
                        f"RTC metadata {key} changed after dry-run; reconnect and revalidate"
                    )
        self.publish("WARMUP" if self.dry else "BOOTSTRAP")

    def capture(self, *, bootstrap):
        # Capture off the control thread. Only accept a guided observation if
        # the cursor AND the wall-clock tick stayed stable throughout capture.
        # No buffer lock is held across SDK/camera access or image copies.
        deadline = time.monotonic() + self.config.max_observation_age_s
        while True:
            self.ex._require_live_lease(self.lease)
            if self.ex._stop_event.is_set():
                raise RuntimeError("RTC stopped")
            with self.lock:
                tick = self.tick()
                generation = self.buffer.generation
                s = 0 if bootstrap else self.buffer.cursor
                old = None if bootstrap else self.buffer.actions.copy()
            if self.first_capture:
                obs = self.ex._capture_initial_observation(
                    self.lease, session_id=self.robot_session
                )
                self.first_capture = False
            else:
                obs = self.ex._capture_observation(
                    self.lease, session_id=self.robot_session
                )
            now = self.ex._clock()
            stamp = obs.monotonic_time
            if (
                now - stamp > self.config.max_observation_age_s
                or max(obs.camera_ages_ms.values())
                > self.config.max_observation_age_s * 1000
            ):
                raise ProtocolValidationError("RTC observation/camera frame is stale")
            with self.lock:
                stable = generation == self.buffer.generation and (
                    bootstrap
                    or (
                        s == self.buffer.cursor
                        and tick == self.tick()
                        and self.tick(stamp) == tick
                    )
                )
            if stable:
                return obs, stamp, self.tick(stamp), generation, s, old
            if time.monotonic() > deadline:
                raise ProtocolValidationError(
                    "RTC could not capture a coherent observation/cursor within the age limit"
                )

    def request(self, *, bootstrap=False, warmup=False, warmup_history=None):
        obs, stamp, tick, generation, s, old = self.capture(
            bootstrap=bootstrap or warmup
        )
        d = self.delay.steps
        history = None if old is None else old[s:]
        if warmup_history is not None:
            s = self.config.trigger_index
            history = warmup_history[s:]
        ids = envelope(
            self.config,
            self.session,
            self.request_id,
            self.episode_epoch,
            self.phase_epoch,
            s=s,
            d=d,
            previous_actions=history,
        )
        self.request_id += 1
        options = {}
        if self.ex._request_format != "legacy":
            options["task_mode"] = (
                "righthand"
                if self.stage.endswith("right")
                else "lefthand" if self.stage.endswith("left") else "twohands"
            )
            if self.ex._phase_entry_action is not None:
                options["phase_entry_action"] = list(self.ex._phase_entry_action)
        if not self.dry:
            self.ex.trace.record("request", chunk=ids["request_id"],
                                 observation_state=obs.state.tolist(),
                                 observation_monotonic=obs.sdk_state_monotonic)
        actions, model, raw = self.ex._policy.infer_rtc(
            obs.state,
            obs.images,
            self.prompt,
            identifiers=ids,
            metadata=self.metadata,
            timeout=(
                self.config.warmup_timeout_s if warmup else self.ex.inference_timeout_s
            ),
            **options,
        )
        if not self.dry:
            self.ex._trace_chunk(actions, raw, obs, ids["request_id"])
        return Reply(
            actions,
            model,
            raw,
            obs,
            stamp,
            tick,
            generation,
            s,
            d,
            ids,
            self.ex._clock(),
        )

    def warmup(self):
        self.publish("WARMUP")
        started = self.ex._clock()
        self.worker.submit(lambda: self.request(bootstrap=True, warmup=True))
        first = self.wait(self.config.warmup_timeout_s + self.ex.camera_start_timeout_s)
        self.worker.submit(
            lambda: self.request(warmup=True, warmup_history=first.actions)
        )
        self.wait(self.config.warmup_timeout_s)
        logger.info(
            "RTC bootstrap/guided warmup completed in %.3fs (excluded from latency estimator)",
            self.ex._clock() - started,
        )
        self.session = uuid.uuid4().hex
        self.request_id = 0
        self.buffer = RtcBuffer()
        self.delay = DelayEstimator(self.config)
        self.base = self.ex._clock()

    def prepare(self, reply, k):
        from .pi05_executor import _slew_limit_arm_action

        effective = reply.actions.copy()
        prev = self.previous.copy()
        limit = math.radians(self.ex._joint_speed_deg_s) / self.hz
        for i in range(k, 50):
            action = self.ex._apply_plan_action_mask(
                effective[i],
                plan=self.plan,
                task_stage=self.stage,
                run_initial_state=self.initial,
            )
            action = np.asarray(
                apply_execution_mode(action, self.initial, self.plan.execution_mode),
                dtype=np.float64,
            )
            if self.plan.execution_mode != "full":
                action, _ = _slew_limit_arm_action(action, prev, limit)
            effective[i] = action
            prev = action
        return effective

    def activate(self, reply, tick):
        if self.ex._stop_event.is_set():
            raise RuntimeError("RTC stopped before handoff")
        if (
            reply.identifiers["session_id"] != self.session
            or reply.identifiers["rtc"]["phase_epoch"] != self.phase_epoch
        ):
            self.stale += 1
            raise ProtocolValidationError("RTC stale phase/session result")
        # Re-evaluate at the control boundary, including any local preparation
        # time. An already received response is never timestamped on arrival.
        for _ in range(3):
            tick = max(tick, self.tick())
            k = tick - reply.observation_tick
            if not 0 <= k < 50:
                raise ProtocolValidationError(
                    f"RTC elapsed prefix exhausted chunk: k={k}"
                )
            actions = self.prepare(reply, k)
            if self.tick() == tick:
                break
        else:
            raise ProtocolValidationError(
                "RTC chunk preparation exceeded a control period"
            )
        self.delay.observe(k)
        d = self.delay.steps
        self.config.check_schedule(max(self.config.trigger_index, d, k), d)
        with self.lock:
            self.buffer.handoff(
                actions,
                reply.model,
                observation_tick=reply.observation_tick,
                handoff_tick=tick,
                expected_generation=reply.generation,
            )
        record = dict(
            session_id=self.session,
            request_id=reply.identifiers["request_id"],
            episode_epoch=self.episode_epoch,
            phase_epoch=self.phase_epoch,
            generation=self.buffer.generation,
            observation_tick=reply.observation_tick,
            handoff_tick=tick,
            observation_monotonic_time=reply.observation_time,
            s=reply.s,
            estimated_d=reply.d,
            actual_k=k,
            history_type="previous_actions" if reply.s else "none",
            history_shape=[50 - reply.s, 23] if reply.s else None,
            observation_to_ready_ms=(reply.ready_time - reply.observation_time) * 1000,
            observation_to_handoff_ms=(self.ex._clock() - reply.observation_time)
            * 1000,
            camera_ages_ms=reply.observation.camera_ages_ms,
            remaining_frames=50 - k,
            policy_timing=reply.raw.get("policy_timing"),
            server_timing=reply.raw.get("server_timing"),
        )
        self.handoffs.append(record)
        with self.ex._lock:
            self.ex._chunk_sequence += 1
            self.ex._chunk_length = 50
            self.ex._inference_latency_ms = record["observation_to_ready_ms"]
            self.ex._camera_ages_ms = dict(reply.observation.camera_ages_ms)
        logger.info("RTC chunk %s", record)
        self.publish("RUNNING_RTC")
        return tick

    def transition(self):
        from .pi05_executor import LEFT_GRIPPER_CLOSE_REQUIRED_STEPS

        if (
            self.stage != "dual_separate_left"
            or self.left_closed < LEFT_GRIPPER_CLOSE_REQUIRED_STEPS
        ):
            return
        self.publish("HOLD")
        if self.worker.pending:
            # Drain and discard the old result before issuing a new request.
            self.wait(self.ex.inference_timeout_s)
            self.stale += 1
        self.phase_epoch += 1
        with self.lock:
            generation = self.buffer.generation + 1
            self.buffer = RtcBuffer()
            self.buffer.generation = generation
        if not self.dry:
            self.previous = self.ex._home_dual_separate_arms(
                self.lease,
                self.robot_session,
                self.previous,
                max_arm_step_rad=math.radians(self.ex._joint_speed_deg_s) / self.hz,
                control_rate_hz=self.hz,
            )
        self.stage = "dual_separate_right"
        self.prompt = self.plan.right_prompt
        with self.ex._lock:
            self.ex._task_stage = self.stage
            self.ex._active_prompt = self.prompt
        self.worker.submit(lambda: self.request(bootstrap=True))
        self.network_started = time.monotonic()
        self.publish("BOOTSTRAP")

    def run(self):
        self.handshake()
        if self.dry:
            self.warmup()
        self.worker.submit(lambda: self.request(bootstrap=True))
        self.network_started = time.monotonic()
        next_tick = self.tick()
        last_reply = None
        while not self.ex._stop_event.is_set():
            self.ex._require_live_lease(self.lease)
            deadline = self.base + next_tick / self.hz
            if self.ex._stop_event.wait(max(0, deadline - self.ex._clock())):
                break
            tick = self.tick()
            next_tick = tick + 1
            if (
                self.worker.pending
                and not self.worker.done.is_set()
                and time.monotonic() - self.network_started
                > self.ex.inference_timeout_s
                + (self.ex.camera_start_timeout_s if self.buffer.actions is None else 0)
            ):
                raise TimeoutError("RTC inference timeout")
            if self.buffer.actions is not None:
                with self.lock:
                    missed = self.buffer.advance_clock(tick)
                self.missed += missed
                if missed:
                    raise ProtocolValidationError(
                        f"RTC missed {missed} control periods; hold instead of replaying overdue commands"
                    )
            if self.worker.pending and self.worker.done.is_set():
                reply = self.worker.take()
                last_reply = reply
                tick = self.activate(reply, tick)
                next_tick = tick + 1
                if not self.dry and self.ex._response_is_success(reply.raw):
                    self.ex._mark_server_success(self.prompt)
                    return "server_success", reply
                if self.dry and len(self.handoffs) >= self.config.dry_run_chunks:
                    return "dry_run_complete", reply
            if self.buffer.actions is None:
                continue
            with self.lock:
                cursor = self.buffer.cursor
            if cursor >= 50:
                self.underruns += 1
                raise ProtocolValidationError(
                    "RTC buffer exhausted; fresh bootstrap required"
                )
            if not self.worker.pending and cursor >= max(
                self.config.trigger_index, self.delay.steps
            ):
                self.config.check_schedule(cursor, self.delay.steps)
                self.worker.submit(self.request)
                self.network_started = time.monotonic()
            # Commands are preprocessed in the entire future buffer. Never run
            # a second dynamic filter that would invalidate physical history.
            with self.ex._step_stop_barrier:
                if self.ex._stop_event.is_set():
                    break
                action = self.buffer.actions[cursor].copy()
                if not self.dry:
                    step = self.ex.robot.policy_step(
                        self.lease, action.tolist(), session_id=self.robot_session
                    )
                    if (
                        isinstance(step, dict)
                        and "effective_action" in step
                        and not np.allclose(
                            step["effective_action"], action, rtol=0, atol=1e-6
                        )
                    ):
                        raise ProtocolValidationError(
                            "Robot adapter changed an RTC target after buffering; history is invalid"
                        )
                    self.ex._trace_step(last_reply.actions[cursor], step, cursor,
                                        last_reply.identifiers["request_id"])
                    self.ex._record_lift_step(action, step, cursor)
                self.previous = action.copy()
                with self.lock:
                    self.buffer.cursor += 1
            if not self.dry:
                with self.ex._lock:
                    self.ex._executed_steps += 1
                self.left_closed = self.left_closed + 1 if action[7] > 0.2 else 0
                self.transition()
            self.publish("RUNNING_RTC")
        return "execution_stopped", last_reply

    def close(self):
        # Interrupt an unfinished request before retiring its sole receiver.
        # Completed dry-run keeps the healthy socket for explicit start.
        if self.worker.pending and not self.worker.done.is_set():
            self.ex._close_policy(invalidate=True)
        self.worker.close()
        self.publish("HOLD")
