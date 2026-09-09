from __future__ import annotations

import math
from types import SimpleNamespace
import threading
import time
import unittest
from unittest import mock

import numpy as np

from control.web_control.pi05_executor import (
    PHASE_DRY_RUN_READY,
    PHASE_FAULT,
    PHASE_IDLE,
    Pi05Executor,
    Pi05SafetyError,
    Pi05StateError,
    REQUIRED_CONFIRMATION,
    _slew_limit_arm_action,
)
from control.web_control.pi05_protocol import ACTION_ORDER, STATE_ORDER


def valid_metadata() -> dict:
    return {
        "robot": "zerith_h1_pro",
        "wire_state_dim": 23,
        "wire_action_dim": 23,
        "model_policy_dim": 17,
        "input_gripper_binary": False,
        "output_gripper_binary": True,
        "status_mode": "none",
        "state_order": list(STATE_ORDER),
        "action_order": list(ACTION_ORDER),
    }


def valid_chunk() -> np.ndarray:
    actions = np.zeros((50, 23), dtype=np.float64)
    actions[:, :7] = 0.01
    actions[:, 7] = 0.4
    actions[:, 8:15] = -0.01
    actions[:, 15] = 0.8
    actions[:, 16] = 0.25
    actions[:, 17:21] = np.asarray((9.0, 8.0, 7.0, 6.0))
    actions[:, 21:23] = np.asarray((4.0, -4.0))
    return actions


class FakePolicyClient:
    def __init__(self) -> None:
        self.usable = True
        self.closed = False
        self.metadata_calls = 0
        self.infer_calls = 0
        self.infer_inputs: list[tuple[np.ndarray, dict[str, np.ndarray], str]] = []
        self.mode = "valid"
        self.metadata_value = valid_metadata()
        self.raw_factory = None
        self.delay_s = 0.0
        self.block = False
        self.block_on_infer_call: int | None = None
        self.fail_on_infer_call: int | None = None
        self.chunk_factory = None
        self.on_infer = None
        self.entered_infer = threading.Event()
        self._closed_event = threading.Event()
        self._active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def metadata(self, *, timeout: float = 5.0) -> dict:
        del timeout
        self.metadata_calls += 1
        return dict(self.metadata_value)

    def infer(self, state, images, prompt, *, jpeg_quality=90, timeout=None):
        del jpeg_quality, timeout
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        self.entered_infer.set()
        try:
            self.infer_calls += 1
            call_number = self.infer_calls
            if self.on_infer is not None:
                self.on_infer(call_number)
            self.infer_inputs.append(
                (
                    np.asarray(state).copy(),
                    {name: np.asarray(image).copy() for name, image in images.items()},
                    prompt,
                )
            )
            if self.block or call_number == self.block_on_infer_call:
                self._closed_event.wait(5.0)
                raise RuntimeError("policy connection closed while blocked")
            if call_number == self.fail_on_infer_call:
                raise RuntimeError("next chunk failed deterministically")
            if self.delay_s:
                time.sleep(self.delay_s)
            actions = (
                self.chunk_factory(call_number)
                if self.chunk_factory is not None
                else valid_chunk()
            )
            if self.mode == "nan":
                actions[0, 0] = np.nan
            elif self.mode == "wrong_shape":
                actions = actions[:-1]
            raw = (
                self.raw_factory(call_number)
                if self.raw_factory is not None
                else {"type": "action_chunk", "actions": [object()] * 50}
            )
            return actions, raw
        finally:
            with self._lock:
                self._active -= 1

    def close(self) -> None:
        self.closed = True
        self.usable = False
        self._closed_event.set()


class FakeRobot:
    def __init__(self) -> None:
        self.live = True
        self.state = np.zeros(23, dtype=np.float64)
        self.state[7] = 0.4  # continuous observed grippers are valid input
        self.state[15] = 0.8
        self.state[17:21] = np.asarray((0.11, -0.12, 0.13, -0.14))
        self.read_calls = 0
        self.begin_calls = 0
        self.steps: list[np.ndarray] = []
        self.hold_calls: list[tuple[str | None, str]] = []
        self.end_calls: list[tuple[str | None, str]] = []
        self.stop_calls = 0
        self.deinit_calls = 0
        self.lose_lease_after_steps: int | None = None
        self.fail_on_step: int | None = None
        self.track_feedback = False
        self.session_body_hold: np.ndarray | None = None

    def has_live_lease(self, lease_id: str) -> bool:
        return bool(self.live and lease_id == "lease")

    def read_policy_state(self, lease_id: str, *, session_id=None) -> dict:
        del session_id
        if not self.has_live_lease(lease_id):
            raise RuntimeError("lease expired")
        self.read_calls += 1
        return {"state": self.state.tolist(), "state_monotonic": time.monotonic()}

    def begin_policy_session(self, lease_id: str) -> dict:
        if not self.has_live_lease(lease_id):
            raise RuntimeError("lease expired")
        self.begin_calls += 1
        self.session_body_hold = self.state[17:21].copy()
        return {
            "session_id": "policy-session",
            "state": self.state.tolist(),
            "body_hold_reference": "session_start_feedback",
            "body_hold_target": self.session_body_hold.tolist(),
        }

    def policy_step(
        self,
        lease_id: str,
        action,
        *,
        session_id=None,
    ) -> dict:
        if not self.has_live_lease(lease_id) or session_id != "policy-session":
            raise RuntimeError("invalid policy session")
        if self.fail_on_step is not None and len(self.steps) + 1 == self.fail_on_step:
            raise RuntimeError("policy step failed deterministically")
        value = np.asarray(action, dtype=np.float64)
        self.steps.append(value.copy())
        effective = value.copy()
        if self.session_body_hold is not None:
            effective[17:21] = self.session_body_hold
        effective[21:23] = 0.0
        latest_state = self.state.copy()
        if self.lose_lease_after_steps is not None and len(self.steps) >= self.lose_lease_after_steps:
            self.live = False
        result = {
            "latest_state": latest_state.tolist(),
            "requested_action": value.tolist(),
            "effective_action": effective.tolist(),
            "sent_action": effective[:21].tolist(),
            "body_hold_reference": "session_start_feedback",
            "body_hold_target": effective[17:21].tolist(),
            "state_monotonic": time.monotonic(),
        }
        if self.track_feedback:
            self.state[:21] = effective[:21]
        return result

    def policy_hold_and_zero(self, lease_id: str, *, session_id=None, reason="operator_stop") -> dict:
        del lease_id
        self.hold_calls.append((session_id, reason))
        return {"ok": True}

    def end_policy_session(self, lease_id: str, *, session_id=None, reason="operator_end") -> dict:
        del lease_id
        self.end_calls.append((session_id, reason))
        self.session_body_hold = None
        return {"ok": True}

    def stop_motion(self, lease_id: str, *, renew_lease=False) -> dict:
        del lease_id, renew_lease
        self.stop_calls += 1
        return {"ok": True}


class FakeCamera:
    def __init__(self) -> None:
        self.stale = False
        self.calls: list[str] = []
        self.sequence = 0

    def get_latest(self, camera: str, stream: str, *, copy: bool = True):
        self.calls.append(camera)
        if stream != "rgb" or not copy:
            raise AssertionError((stream, copy))
        self.sequence += 1
        age_s = 2.0 if self.stale else 0.001
        colours = {"head": 10, "left_wrist": 20, "right_wrist": 30}
        return SimpleNamespace(
            image=np.full((12, 16, 3), colours[camera], dtype=np.uint8),
            host_getter_monotonic_ns=int((time.monotonic() - age_s) * 1e9),
            sequence=self.sequence,
        )


class DelayedFirstFrameCamera(FakeCamera):
    def __init__(self) -> None:
        super().__init__()
        self.wait_calls = 0

    def wait_for_frame(
        self,
        camera: str,
        stream: str,
        *,
        timeout: float,
        copy: bool,
    ):
        del timeout, copy
        self.wait_calls += 1
        if self.wait_calls == 1:
            return None
        return self.get_latest(camera, stream, copy=True)


class CameraLease:
    def __init__(self) -> None:
        self.acquired = 0
        self.released = 0

    def acquire(self) -> None:
        self.acquired += 1

    def release(self) -> None:
        self.released += 1


class FakeClock:
    def __init__(self, initial: float = 100.0) -> None:
        self.value = initial
        self.lock = threading.Lock()

    def __call__(self) -> float:
        with self.lock:
            return self.value

    def advance(self, seconds: float) -> None:
        with self.lock:
            self.value += seconds


class ClockedRobot(FakeRobot):
    def __init__(self, clock: FakeClock, *, step_seconds: float) -> None:
        super().__init__()
        self.clock = clock
        self.step_seconds = step_seconds

    def read_policy_state(self, lease_id: str, *, session_id=None) -> dict:
        del session_id
        if not self.has_live_lease(lease_id):
            raise RuntimeError("lease expired")
        self.read_calls += 1
        return {"state": self.state.tolist(), "state_monotonic": self.clock()}

    def policy_step(
        self,
        lease_id: str,
        action,
        *,
        session_id=None,
    ) -> dict:
        result = super().policy_step(
            lease_id,
            action,
            session_id=session_id,
        )
        self.clock.advance(self.step_seconds)
        result["state_monotonic"] = self.clock()
        return result


class ClockedCamera(FakeCamera):
    def __init__(self, clock: FakeClock) -> None:
        super().__init__()
        self.clock = clock

    def get_latest(self, camera: str, stream: str, *, copy: bool = True):
        self.calls.append(camera)
        if stream != "rgb" or not copy:
            raise AssertionError((stream, copy))
        self.sequence += 1
        colours = {"head": 10, "left_wrist": 20, "right_wrist": 30}
        return SimpleNamespace(
            image=np.full((12, 16, 3), colours[camera], dtype=np.uint8),
            host_getter_monotonic_ns=int(self.clock() * 1e9),
            sequence=self.sequence,
        )


class Pi05ExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.robot = FakeRobot()
        self.camera = FakeCamera()
        self.policy = FakePolicyClient()
        self.camera_lease = CameraLease()
        self.health_calls: list[tuple[str, int]] = []
        self.factory_calls: list[tuple[str, int]] = []

        def health(host: str, port: int) -> str:
            self.health_calls.append((host, port))
            return "OK"

        def factory(host: str, port: int) -> FakePolicyClient:
            self.factory_calls.append((host, port))
            return self.policy

        self.executor = Pi05Executor(
            self.robot,
            self.camera,
            policy_factory=factory,
            health_probe=health,
            camera_acquire=self.camera_lease.acquire,
            camera_release=self.camera_lease.release,
            stop_timeout_s=1.0,
        )

    def tearDown(self) -> None:
        self.executor.close()

    def probe_and_dry_run(self, prompt: str = "把物体放进盒子") -> dict:
        self.executor.probe()
        return self.executor.dry_run(prompt, "lease")

    def wait_for_phase(self, phase: str, timeout: float = 3.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.executor.status()
            if status["phase"] == phase:
                return status
            time.sleep(0.005)
        self.fail(f"phase did not become {phase}: {self.executor.status()}")

    def wait_for_executed_steps(self, minimum: int, timeout: float = 3.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.executor.status()
            if status["executed_steps"] >= minimum:
                return status
            if status["phase"] == PHASE_FAULT:
                self.fail(f"execution faulted before step {minimum}: {status}")
            time.sleep(0.002)
        self.fail(f"execution did not reach step {minimum}: {self.executor.status()}")

    def wait_for_task_stage(self, stage: str, timeout: float = 3.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.executor.status()
            if status["task_stage"] == stage:
                return status
            if status["phase"] == PHASE_FAULT:
                self.fail(f"execution faulted before task stage {stage}: {status}")
            time.sleep(0.002)
        self.fail(f"task stage did not become {stage}: {self.executor.status()}")

    def test_probe_only_health_and_metadata(self) -> None:
        result = self.executor.probe()
        self.assertEqual(result["health"], "OK")
        self.assertTrue(result["status"]["metadata_ok"])
        self.assertEqual(self.health_calls, [("192.168.1.154", 9973)])
        self.assertEqual(self.factory_calls, [("192.168.1.154", 9973)])
        self.assertEqual(self.policy.metadata_calls, 1)
        self.assertEqual(self.policy.infer_calls, 0)
        self.assertEqual(self.robot.read_calls, 0)
        self.assertEqual(self.robot.begin_calls, 0)
        self.assertEqual(self.robot.steps, [])
        self.assertEqual(self.camera.calls, [])
        self.assertEqual(self.camera_lease.acquired, 0)
        self.assertTrue(result["status"]["connected"])
        self.assertEqual(result["status"]["metadata_status_mode"], "none")

    def test_reconnect_closes_old_connection_and_replaces_endpoint_state(self) -> None:
        self.probe_and_dry_run("旧任务")
        old_policy = self.policy
        replacement = FakePolicyClient()
        events: list[tuple[str, str, int, bool]] = []

        def health(host: str, port: int) -> str:
            events.append(("health", host, port, old_policy.closed))
            return "OK"

        def factory(host: str, port: int) -> FakePolicyClient:
            events.append(("factory", host, port, old_policy.closed))
            return replacement

        self.executor._health_probe = health
        self.executor._policy_factory = factory
        result = self.executor.reconnect("10.1.1.135", 9988)

        self.assertTrue(old_policy.closed)
        self.assertEqual(
            events,
            [
                ("health", "10.1.1.135", 9988, True),
                ("factory", "10.1.1.135", 9988, True),
            ],
        )
        self.assertEqual(result["status"]["endpoint"], "10.1.1.135:9988")
        self.assertEqual(result["status"]["host"], "10.1.1.135")
        self.assertEqual(result["status"]["port"], 9988)
        self.assertTrue(result["status"]["connected"])
        self.assertTrue(result["status"]["metadata_ok"])
        self.assertFalse(result["status"]["dry_run_ok"])
        self.assertIsNone(result["status"]["dry_run_age_ms"])

    def test_reconnect_rejects_invalid_endpoint_without_closing_current_connection(self) -> None:
        self.executor.probe()
        invalid_endpoints = (
            ("", 9973),
            ("http://192.168.1.154", 9973),
            ("host/path", 9973),
            ("bad host", 9973),
            ("host", True),
            ("host", 0),
            ("host", 65536),
            ("host", 9973.0),
        )
        for host, port in invalid_endpoints:
            with self.subTest(host=host, port=port), self.assertRaises(Pi05SafetyError):
                self.executor.reconnect(host, port)
        self.assertFalse(self.policy.closed)
        self.assertTrue(self.executor.status()["connected"])

    def test_failed_reconnect_faults_once_without_retry_or_endpoint_rollback(self) -> None:
        self.executor.probe()
        old_policy = self.policy
        attempts: list[tuple[str, int]] = []

        def failing_health(host: str, port: int) -> str:
            attempts.append((host, port))
            raise RuntimeError("health unavailable")

        self.executor._health_probe = failing_health
        with self.assertRaisesRegex(RuntimeError, "health unavailable"):
            self.executor.reconnect("10.1.1.135", 9973)

        self.assertTrue(old_policy.closed)
        self.assertEqual(attempts, [("10.1.1.135", 9973)])
        time.sleep(0.05)
        self.assertEqual(attempts, [("10.1.1.135", 9973)])
        status = self.executor.status()
        self.assertEqual(status["phase"], PHASE_FAULT)
        self.assertEqual(status["endpoint"], "10.1.1.135:9973")
        self.assertFalse(status["connected"])

    def test_probe_reuses_the_endpoint_selected_by_reconnect(self) -> None:
        clients = [FakePolicyClient(), FakePolicyClient()]
        calls: list[tuple[str, str, int]] = []

        def health(host: str, port: int) -> str:
            calls.append(("health", host, port))
            return "OK"

        def factory(host: str, port: int) -> FakePolicyClient:
            calls.append(("factory", host, port))
            return clients.pop(0)

        self.executor._health_probe = health
        self.executor._policy_factory = factory
        self.executor.reconnect("100.93.20.55", 9974)
        self.executor.probe()

        self.assertEqual(
            calls,
            [
                ("health", "100.93.20.55", 9974),
                ("factory", "100.93.20.55", 9974),
                ("health", "100.93.20.55", 9974),
                ("factory", "100.93.20.55", 9974),
            ],
        )
        self.assertEqual(self.executor.status()["endpoint"], "100.93.20.55:9974")

    def test_reconnect_is_rejected_while_execution_is_running(self) -> None:
        prompt = "运行中的任务"
        self.probe_and_dry_run(prompt)
        self.policy.block = True
        self.policy.entered_infer.clear()
        self.executor.start(
            prompt,
            "lease",
            confirmation=REQUIRED_CONFIRMATION,
            steps_per_chunk=2,
        )
        self.assertTrue(self.policy.entered_infer.wait(1.0))
        with self.assertRaises(Pi05StateError):
            self.executor.reconnect("10.1.1.135", 9973)
        self.assertEqual(self.executor.status()["endpoint"], "192.168.1.154:9973")
        self.executor.stop(timeout=1.0)

    def test_disconnect_returns_idle_and_never_deinitializes(self) -> None:
        self.probe_and_dry_run()
        status = self.executor.disconnect()
        self.assertEqual(status["phase"], PHASE_IDLE)
        self.assertFalse(status["connected"])
        self.assertFalse(status["metadata_ok"])
        self.assertFalse(status["dry_run_ok"])
        self.assertTrue(self.policy.closed)
        self.assertEqual(self.robot.deinit_calls, 0)

    def test_disconnect_interrupts_running_inference_and_holds_without_deinit(self) -> None:
        prompt = "断开运行连接"
        self.probe_and_dry_run(prompt)
        self.policy.block = True
        self.policy.entered_infer.clear()
        self.executor.start(
            prompt,
            "lease",
            confirmation=REQUIRED_CONFIRMATION,
            steps_per_chunk=5,
        )
        self.assertTrue(self.policy.entered_infer.wait(1.0))

        status = self.executor.disconnect(timeout=1.0)
        self.assertEqual(status["phase"], PHASE_IDLE)
        self.assertFalse(status["connected"])
        self.assertEqual(self.robot.steps, [])
        self.assertGreaterEqual(len(self.robot.hold_calls), 1)
        self.assertEqual(self.robot.deinit_calls, 0)

    def test_disconnect_preserves_a_latched_fault_until_explicit_reset(self) -> None:
        self.executor.probe()
        self.policy.mode = "nan"
        with self.assertRaises(Pi05SafetyError):
            self.executor.dry_run("故障任务", "lease")
        fault_before = self.executor.status()["fault"]

        disconnected = self.executor.disconnect()
        self.assertEqual(disconnected["phase"], PHASE_FAULT)
        self.assertEqual(disconnected["fault"], fault_before)
        self.assertFalse(disconnected["connected"])
        self.assertEqual(self.executor.reset_fault()["phase"], PHASE_IDLE)

    def test_dry_run_validates_images_and_hold_zero_without_setter(self) -> None:
        result = self.probe_and_dry_run()
        self.assertEqual(self.executor.status()["phase"], PHASE_DRY_RUN_READY)
        self.assertEqual(result["state_dim"], 23)
        self.assertEqual((result["chunk_length"], result["action_dim"]), (50, 23))
        self.assertTrue(result["hold_verified"])
        self.assertTrue(result["base_zero_verified"])
        self.assertEqual(result["gripper_values"], {"7": [0.4], "15": [0.8]})
        effective = np.asarray(result["first_effective_action"])
        np.testing.assert_array_equal(effective[17:21], self.robot.state[17:21])
        np.testing.assert_array_equal(effective[21:23], np.zeros(2))
        self.assertAlmostEqual(
            result["first_arm_delta_from_observation_rad"],
            0.01,
        )
        self.assertEqual(set(self.policy.infer_inputs[0][1]), {"cam_high", "cam_left_wrist", "cam_right_wrist"})
        self.assertEqual(float(self.policy.infer_inputs[0][0][7]), 0.4)
        self.assertEqual(float(self.policy.infer_inputs[0][0][15]), 0.8)
        self.assertEqual(
            self.camera.calls,
            ["head", "left_wrist", "right_wrist"],
        )
        self.assertEqual(self.robot.steps, [])
        self.assertEqual(self.robot.begin_calls, 0)
        self.assertEqual(self.robot.hold_calls, [])
        self.assertEqual(self.robot.end_calls, [])
        self.assertEqual((self.camera_lease.acquired, self.camera_lease.released), (1, 1))

    def test_dry_run_waits_for_camera_services_first_published_frame(self) -> None:
        self.camera = DelayedFirstFrameCamera()
        self.executor.camera = self.camera
        result = self.probe_and_dry_run()
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(self.camera.wait_calls, 4)
        self.assertEqual(self.robot.steps, [])
        self.assertEqual(self.robot.read_calls, 1)
        self.assertEqual(self.policy.infer_calls, 1)

    def test_start_requires_exact_confirmation_matching_recent_dry_run_and_live_lease(self) -> None:
        self.probe_and_dry_run("叠衣服")
        with self.assertRaises(Pi05SafetyError):
            self.executor.start("叠衣服", "lease", confirmation="确认", steps_per_chunk=1)
        with self.assertRaises(Pi05SafetyError):
            self.executor.start(
                "不同任务",
                "lease",
                confirmation=REQUIRED_CONFIRMATION,
                steps_per_chunk=1,
            )
        with self.assertRaises(Pi05SafetyError):
            self.executor.start(
                "叠衣服",
                "lease",
                confirmation=REQUIRED_CONFIRMATION,
                steps_per_chunk=0,
            )
        with self.assertRaises(Pi05SafetyError):
            self.executor.start(
                "叠衣服",
                "lease",
                confirmation=REQUIRED_CONFIRMATION,
                steps_per_chunk=51,
            )
        self.robot.live = False
        with self.assertRaises(Pi05SafetyError):
            self.executor.start(
                "叠衣服",
                "lease",
                confirmation=REQUIRED_CONFIRMATION,
                steps_per_chunk=1,
            )
        self.assertEqual(self.robot.begin_calls, 0)

    def test_start_accepts_chunk_boundaries_and_rejects_only_outside_protocol_horizon(self) -> None:
        for steps_per_chunk in (1, 50):
            with self.subTest(steps_per_chunk=steps_per_chunk):
                self.executor.close()
                self.setUp()
                prompt = f"边界 {steps_per_chunk}"
                self.probe_and_dry_run(prompt)
                status = self.executor.start(
                    prompt,
                    "lease",
                    confirmation=REQUIRED_CONFIRMATION,
                    steps_per_chunk=steps_per_chunk,
                )
                self.assertEqual(status["steps_per_chunk"], steps_per_chunk)
                self.wait_for_executed_steps(1)
                self.executor.stop(timeout=1.0)

        self.executor.close()
        self.setUp()
        prompt = "叠衣服"
        self.probe_and_dry_run(prompt)
        for steps_per_chunk in (0, 51, True, 1.0):
            with self.subTest(steps_per_chunk=steps_per_chunk), self.assertRaises(Pi05SafetyError):
                self.executor.start(
                    prompt,
                    "lease",
                    confirmation=REQUIRED_CONFIRMATION,
                    steps_per_chunk=steps_per_chunk,
                )
        self.assertEqual(self.robot.begin_calls, 0)
        self.assertEqual(self.executor.status()["phase"], PHASE_DRY_RUN_READY)

    def test_control_rate_has_no_artificial_range_but_must_be_positive_finite(self) -> None:
        for rate in (0.25, 120.0):
            with self.subTest(rate=rate):
                self.executor.close()
                self.setUp()
                prompt = f"频率 {rate}"
                self.probe_and_dry_run(prompt)
                status = self.executor.start(
                    prompt,
                    "lease",
                    confirmation=REQUIRED_CONFIRMATION,
                    steps_per_chunk=1,
                    control_rate_hz=rate,
                )
                self.assertEqual(status["control_rate_hz"], rate)
                self.executor.stop(timeout=1.0)

        self.executor.close()
        self.setUp()
        self.probe_and_dry_run("无效频率")
        for rate in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(rate=rate), self.assertRaises(Pi05SafetyError):
                self.executor.start(
                    "无效频率",
                    "lease",
                    confirmation=REQUIRED_CONFIRMATION,
                    control_rate_hz=rate,
                )
        self.assertEqual(self.robot.begin_calls, 0)

    def test_joint_speed_has_no_artificial_upper_bound_but_must_be_positive_finite(self) -> None:
        for speed in (0.25, 720.0):
            with self.subTest(speed=speed):
                self.executor.close()
                self.setUp()
                prompt = f"关节速度 {speed}"
                self.probe_and_dry_run(prompt)
                status = self.executor.start(
                    prompt,
                    "lease",
                    confirmation=REQUIRED_CONFIRMATION,
                    steps_per_chunk=1,
                    joint_speed_deg_s=speed,
                )
                self.assertEqual(status["joint_speed_deg_s"], speed)
                self.executor.stop(timeout=1.0)

        self.executor.close()
        self.setUp()
        self.probe_and_dry_run("无效关节速度")
        for speed in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(speed=speed), self.assertRaises(Pi05SafetyError):
                self.executor.start(
                    "无效关节速度",
                    "lease",
                    confirmation=REQUIRED_CONFIRMATION,
                    joint_speed_deg_s=speed,
                )
        self.assertEqual(self.robot.begin_calls, 0)

    def test_arm_slew_limit_advances_from_previous_command_not_lagging_feedback(self) -> None:
        previous = np.zeros(23, dtype=np.float64)
        requested = np.zeros(23, dtype=np.float64)
        requested[0] = 1.0
        requested[8] = -1.0
        requested[16] = 0.7

        first, first_limited = _slew_limit_arm_action(requested, previous, 0.1)
        second, second_limited = _slew_limit_arm_action(requested, first, 0.1)

        # Hardware feedback may still be the all-zero vector.  The second
        # command nevertheless advances from the first successful command,
        # instead of being re-based to feedback and getting stuck at 0.1.
        self.assertAlmostEqual(first[0], 0.1)
        self.assertAlmostEqual(second[0], 0.2)
        self.assertAlmostEqual(first[8], -0.1)
        self.assertAlmostEqual(second[8], -0.2)
        self.assertEqual(first[16], 0.7)  # lift is intentionally not limited
        self.assertEqual(second[16], 0.7)
        self.assertEqual(set(first_limited), {0, 8})
        self.assertEqual(set(second_limited), {0, 8})

    def test_stop_from_dry_run_ready_invalidates_readiness_and_returns_idle(self) -> None:
        self.probe_and_dry_run()
        before = self.executor.status()
        self.assertTrue(before["dry_run_ok"])
        self.assertIsNotNone(before["dry_run_age_ms"])
        self.assertFalse(before["active"])
        stopped = self.executor.stop()
        self.assertEqual(stopped["phase"], PHASE_IDLE)
        self.assertFalse(stopped["metadata_ok"])
        self.assertFalse(stopped["dry_run_ok"])
        self.assertIsNone(stopped["dry_run_age_ms"])
        self.assertNotIn("dry_run_ttl_ms", stopped)
        self.assertNotIn("max_camera_age_ms", stopped)
        self.assertNotIn("max_state_age_ms", stopped)

    def test_run_continues_past_steps_per_chunk_until_explicit_stop(self) -> None:
        prompt = "抓起红色方块"
        self.probe_and_dry_run(prompt)
        start_status = self.executor.start(
            prompt,
            "lease",
            confirmation=REQUIRED_CONFIRMATION,
            steps_per_chunk=3,
            control_rate_hz=60,
        )
        # Backend-wide manual/voice exclusion must exist before start() tells
        # the web layer that execution was accepted.
        self.assertEqual(self.robot.begin_calls, 1)
        self.assertTrue(start_status["active"])
        self.assertEqual(start_status["steps_per_chunk"], 3)
        self.assertEqual(start_status["control_rate_hz"], 60.0)
        self.assertEqual(start_status["joint_speed_deg_s"], 30.0)
        self.assertAlmostEqual(start_status["max_arm_step_rad"], math.radians(30) / 60)
        self.assertEqual(start_status["arm_slew_reference"], "previous_successful_command")
        self.assertEqual(start_status["default_steps_per_chunk"], 30)
        self.assertEqual(start_status["default_control_rate_hz"], 30.0)
        self.assertEqual(start_status["default_joint_speed_deg_s"], 30.0)

        running = self.wait_for_executed_steps(7)
        self.assertEqual(running["phase"], "running")
        status = self.executor.stop(timeout=1.0)
        self.assertEqual(status["phase"], PHASE_IDLE)
        self.assertGreaterEqual(status["executed_steps"], 7)
        self.assertGreaterEqual(len(self.robot.steps), 7)
        for action in self.robot.steps:
            self.assertEqual(action.shape, (23,))
            np.testing.assert_array_equal(action[17:21], self.robot.state[17:21])
            np.testing.assert_array_equal(action[21:23], np.zeros(2))
        self.assertEqual(self.robot.begin_calls, 1)
        self.assertGreaterEqual(len(self.robot.hold_calls), 1)
        self.assertEqual(len(self.robot.end_calls), 1)
        self.assertEqual(self.robot.deinit_calls, 0)
        self.assertEqual((self.camera_lease.acquired, self.camera_lease.released), (2, 2))

    def test_next_chunk_is_requested_only_after_selected_actions_finish(self) -> None:
        prompt = "整理桌面"
        self.probe_and_dry_run(prompt)
        infer_step_counts: list[tuple[int, int]] = []
        self.policy.on_infer = lambda call: infer_step_counts.append(
            (call, len(self.robot.steps))
        )
        self.executor.start(
            prompt,
            "lease",
            confirmation=REQUIRED_CONFIRMATION,
            steps_per_chunk=20,
            control_rate_hz=60,
        )
        self.wait_for_executed_steps(25, timeout=4.0)
        status = self.executor.stop(timeout=1.0)
        self.assertGreaterEqual(status["executed_steps"], 25)
        self.assertEqual(self.factory_calls, [("192.168.1.154", 9973)])
        self.assertGreaterEqual(self.policy.infer_calls, 3)
        self.assertEqual(self.policy.max_active, 1)
        # Infer #2 starts the run.  Infer #3 must not begin until all twenty
        # selected actions from chunk #1 have been sent.
        self.assertGreaterEqual(len(infer_step_counts), 2)
        self.assertEqual(infer_step_counts[0], (2, 0))
        self.assertEqual(infer_step_counts[1], (3, 20))
        self.assertEqual(status["chunk_request_mode"], "after_chunk_sync")

    def test_next_chunk_failure_faults_after_finishing_current_selection(self) -> None:
        prompt = "整理桌面"
        self.probe_and_dry_run(prompt)
        # Infer #1 is dry-run, #2 is the first execution chunk, and #3 is the
        # synchronous request made after the selected thirty actions finish.
        self.policy.fail_on_infer_call = 3
        self.executor.start(
            prompt,
            "lease",
            confirmation=REQUIRED_CONFIRMATION,
            steps_per_chunk=30,
        )
        status = self.wait_for_phase(PHASE_FAULT)

        self.assertIn("next chunk failed deterministically", status["fault"])
        self.assertEqual(len(self.robot.steps), 30)
        time.sleep(0.05)
        self.assertEqual(len(self.robot.steps), 30)

    def test_default_executes_first_30_actions_of_every_50_step_chunk(self) -> None:
        def indexed_chunk(call_number: int) -> np.ndarray:
            actions = valid_chunk()
            actions[:, 16] = float(call_number)
            return actions

        self.policy.chunk_factory = indexed_chunk
        prompt = "把物体放进盒子"
        self.probe_and_dry_run(prompt)  # infer call 1
        started = self.executor.start(
            prompt,
            "lease",
            confirmation=REQUIRED_CONFIRMATION,
            control_rate_hz=120,
        )
        self.assertEqual(started["steps_per_chunk"], 30)
        self.wait_for_executed_steps(31, timeout=2.0)
        self.executor.stop(timeout=1.0)

        self.assertGreaterEqual(len(self.robot.steps), 31)
        self.assertAlmostEqual(self.robot.steps[0][0], math.radians(30) / 120)
        self.assertTrue(all(action[16] == 2.0 for action in self.robot.steps[:30]))
        self.assertEqual(self.robot.steps[30][16], 3.0)
        status = self.executor.status()
        self.assertEqual(status["chunk_request_mode"], "after_chunk_sync")
        self.assertGreaterEqual(status["chunk_sequence"], 2)
        self.assertAlmostEqual(
            status["last_chunk_first_arm_delta_from_observation_rad"],
            0.01,
        )
        self.assertAlmostEqual(
            status["last_chunk_first_arm_delta_from_feedback_rad"],
            0.01,
        )
        expected_body = self.robot.state[17:21]
        self.assertEqual(
            status["body_order"],
            ["waist.pitch", "waist.yaw", "head.yaw", "head.pitch"],
        )
        self.assertEqual(
            status["body_hold_reference"],
            "policy_session_start_feedback",
        )
        np.testing.assert_array_equal(
            status["last_chunk_observation_body"],
            expected_body,
        )
        np.testing.assert_array_equal(
            status["last_chunk_server_body"],
            np.asarray((9.0, 8.0, 7.0, 6.0)),
        )
        np.testing.assert_array_equal(
            status["last_chunk_requested_body"],
            expected_body,
        )
        np.testing.assert_array_equal(
            status["last_chunk_effective_body"],
            expected_body,
        )
        np.testing.assert_array_equal(
            status["last_chunk_feedback_body"],
            expected_body,
        )
        np.testing.assert_array_equal(
            status["last_chunk_body_hold_target"],
            expected_body,
        )

    def test_execution_slew_target_keeps_advancing_while_feedback_lags(self) -> None:
        def large_jump_chunk(_call_number: int) -> np.ndarray:
            actions = valid_chunk()
            actions[:, :7] = 1.0
            actions[:, 8:15] = -1.0
            return actions

        self.policy.chunk_factory = large_jump_chunk
        prompt = "大目标连续限幅"
        self.probe_and_dry_run(prompt)
        self.executor.start(
            prompt,
            "lease",
            confirmation=REQUIRED_CONFIRMATION,
            steps_per_chunk=5,
            control_rate_hz=30,
            joint_speed_deg_s=30,
        )
        self.wait_for_executed_steps(3)
        status = self.executor.stop(timeout=1.0)

        step = math.radians(30) / 30
        self.assertGreaterEqual(len(self.robot.steps), 3)
        # FakeRobot deliberately leaves feedback at zero after every setter.
        # The commanded target must still ramp 1°, 2°, 3° rather than repeat 1°.
        for number, action in enumerate(self.robot.steps[:3], start=1):
            self.assertAlmostEqual(action[0], number * step)
            self.assertAlmostEqual(action[8], -number * step)
        self.assertGreaterEqual(status["arm_slew_limited_steps"], 3)

    def test_dry_run_has_no_artificial_expiry_threshold(self) -> None:
        clock = FakeClock()
        self.executor._clock = clock
        prompt = "持续任务"
        self.probe_and_dry_run(prompt)
        clock.advance(1_000_000.0)
        status = self.executor.start(
            prompt,
            "lease",
            confirmation=REQUIRED_CONFIRMATION,
            steps_per_chunk=1,
        )
        self.assertTrue(status["active"])
        self.executor.stop(timeout=1.0)

    def test_bad_chunk_latches_fault_holds_and_never_auto_recovers(self) -> None:
        prompt = "递给我杯子"
        self.probe_and_dry_run(prompt)
        self.policy.mode = "nan"
        self.executor.start(
            prompt,
            "lease",
            confirmation=REQUIRED_CONFIRMATION,
            steps_per_chunk=2,
        )
        status = self.wait_for_phase(PHASE_FAULT)
        self.assertIn("NaN or Inf", status["fault"])
        self.assertEqual(self.robot.steps, [])
        self.assertGreaterEqual(len(self.robot.hold_calls), 1)
        self.assertEqual(len(self.robot.end_calls), 1)
        self.assertTrue(self.policy.closed)
        time.sleep(0.05)
        self.assertEqual(self.executor.status()["phase"], PHASE_FAULT)
        with self.assertRaises(Pi05StateError):
            self.executor.start(
                prompt,
                "lease",
                confirmation=REQUIRED_CONFIRMATION,
                steps_per_chunk=1,
            )
        reset = self.executor.reset_fault()
        self.assertEqual(reset["phase"], PHASE_IDLE)
        self.assertFalse(reset["metadata_ok"])

    def test_camera_age_is_reported_but_not_rejected_by_an_executor_threshold(self) -> None:
        self.executor.probe()
        self.camera.stale = True
        result = self.executor.dry_run("拿起毛巾", "lease")
        self.assertTrue(result["ok"])
        self.assertEqual(self.executor.status()["phase"], PHASE_DRY_RUN_READY)
        self.assertTrue(all(age >= 1_900 for age in result["camera_ages_ms"].values()))
        self.assertEqual(self.policy.infer_calls, 1)
        self.assertEqual(self.robot.steps, [])
        self.assertEqual(self.robot.begin_calls, 0)
        self.assertEqual(self.robot.deinit_calls, 0)
        self.assertEqual((self.camera_lease.acquired, self.camera_lease.released), (1, 1))

    def test_lease_loss_latches_fault_and_stops_new_actions(self) -> None:
        prompt = "移动积木"
        self.probe_and_dry_run(prompt)
        self.robot.lose_lease_after_steps = 1
        self.executor.start(
            prompt,
            "lease",
            confirmation=REQUIRED_CONFIRMATION,
            steps_per_chunk=10,
        )
        status = self.wait_for_phase(PHASE_FAULT)
        self.assertIn("lease", status["fault"])
        self.assertEqual(len(self.robot.steps), 1)
        time.sleep(0.08)
        self.assertEqual(len(self.robot.steps), 1)
        self.assertGreaterEqual(len(self.robot.hold_calls), 1)
        self.assertEqual(len(self.robot.end_calls), 1)

    def test_stop_interrupts_blocked_inference_and_never_deinitializes(self) -> None:
        prompt = "装入盒子"
        self.probe_and_dry_run(prompt)
        self.policy.block = True
        self.policy.entered_infer.clear()
        self.executor.start(
            prompt,
            "lease",
            confirmation=REQUIRED_CONFIRMATION,
            steps_per_chunk=10,
        )
        self.assertTrue(self.policy.entered_infer.wait(1.0))
        status = self.executor.stop(timeout=1.0)
        self.assertEqual(status["phase"], PHASE_IDLE)
        self.assertEqual(self.robot.steps, [])
        self.assertGreaterEqual(len(self.robot.hold_calls), 1)
        self.assertEqual(len(self.robot.end_calls), 1)
        self.assertEqual(self.robot.deinit_calls, 0)
        self.executor.close()
        self.assertEqual(self.robot.deinit_calls, 0)

    def test_dual_separate_dry_run_validates_both_prompts_and_gates_full_plan(self) -> None:
        left_prompt = "Grasp Coca-Cola with the left hand"
        right_prompt = "Grasp Vita Coconut with the right hand"
        self.executor.probe()
        result = self.executor.dry_run(
            left_prompt,
            "lease",
            inference_mode="dual_separate",
            right_prompt=right_prompt,
        )

        self.assertEqual(result["inference_mode"], "dual_separate")
        self.assertEqual(result["right_prompt"], right_prompt)
        self.assertEqual(len(result["prompt_results"]), 2)
        left_preview = np.asarray(
            result["prompt_results"][0]["first_effective_action"]
        )
        right_preview = np.asarray(
            result["prompt_results"][1]["first_effective_action"]
        )
        np.testing.assert_array_equal(left_preview[8:16], self.robot.state[8:16])
        np.testing.assert_array_equal(right_preview[:7], np.zeros(7))
        self.assertEqual(right_preview[7], 1.5)
        self.assertAlmostEqual(
            result["prompt_results"][1]["server_first_action"][0],
            0.01,
        )
        self.assertEqual(
            [entry[2] for entry in self.policy.infer_inputs],
            [left_prompt, right_prompt],
        )
        status = self.executor.status()
        self.assertEqual(status["task_stage"], "ready")
        self.assertEqual(status["active_prompt"], left_prompt)
        self.assertEqual(status["right_prompt"], right_prompt)

        with self.assertRaisesRegex(Pi05SafetyError, "exactly match"):
            self.executor.start(
                left_prompt,
                "lease",
                confirmation=REQUIRED_CONFIRMATION,
                inference_mode="dual_separate",
                right_prompt="Grasp Pepsi with the right hand",
            )
        self.assertEqual(self.robot.begin_calls, 0)

    def test_prompt_status_rejects_ambiguous_plans_before_camera_or_inference(self) -> None:
        self.policy.metadata_value["status_mode"] = "prompt"
        self.executor.probe()

        with self.assertRaisesRegex(Pi05SafetyError, "dual_continuous"):
            self.executor.dry_run(
                "Grasp Coca-Cola with the left hand and Pepsi with the right hand",
                "lease",
                inference_mode="dual_continuous",
            )
        with self.assertRaisesRegex(Pi05SafetyError, "exactly one"):
            self.executor.dry_run(
                "Grasp Coca-Cola",
                "lease",
                inference_mode="custom",
            )
        with self.assertRaisesRegex(Pi05SafetyError, "match active_hand"):
            self.executor.dry_run(
                "Grasp Coca-Cola with the left hand",
                "lease",
                inference_mode="single",
                active_hand="right",
            )

        self.assertEqual(self.policy.infer_calls, 0)
        self.assertEqual(self.camera_lease.acquired, 0)
        self.assertEqual(self.executor.status()["phase"], PHASE_IDLE)

    def test_non_none_status_requires_is_success_and_true_stops_before_chunk(self) -> None:
        self.policy.metadata_value["status_mode"] = "left"
        self.executor.probe()
        with self.assertRaisesRegex(Pi05SafetyError, "missing is_success"):
            self.executor.dry_run(
                "Grasp Coca-Cola with the left hand",
                "lease",
                inference_mode="single",
                active_hand="left",
            )
        self.assertEqual(self.executor.status()["phase"], PHASE_FAULT)

        self.executor.close()
        self.setUp()
        self.policy.raw_factory = lambda call: {
            "type": "action_chunk",
            "actions": [object()] * 50,
            "is_success": call >= 2,
        }
        prompt = "custom task"
        self.probe_and_dry_run(prompt)
        self.executor.start(
            prompt,
            "lease",
            confirmation=REQUIRED_CONFIRMATION,
        )
        status = self.wait_for_phase(PHASE_IDLE)
        self.assertEqual(self.robot.steps, [])
        self.assertTrue(status["last_is_success"])
        self.assertEqual(status["completion_reason"], "server_success")
        self.assertEqual(status["task_stage"], "completed")
        self.assertEqual(self.robot.hold_calls[-1][1], "server_success")

    def test_single_mode_freezes_the_inactive_side_at_run_initial_state(self) -> None:
        self.robot.state[8:15] = np.linspace(0.11, 0.17, 7)
        self.robot.state[15] = 0.63
        prompt = "Grasp Coca-Cola with the left hand"
        self.executor.probe()
        self.executor.dry_run(
            prompt,
            "lease",
            inference_mode="single",
            active_hand="left",
        )
        self.executor.start(
            prompt,
            "lease",
            confirmation=REQUIRED_CONFIRMATION,
            inference_mode="single",
            active_hand="left",
            steps_per_chunk=50,
            control_rate_hz=120,
        )
        self.wait_for_executed_steps(2)
        status = self.executor.stop(timeout=1.0)

        self.assertGreaterEqual(len(self.robot.steps), 2)
        for action in self.robot.steps:
            np.testing.assert_array_equal(action[8:16], self.robot.state[8:16])
        self.assertEqual(status["inference_mode"], "single")
        self.assertEqual(status["active_hand"], "left")

    def test_dual_separate_counts_executed_left_gripper_homes_and_stops_on_right_success(self) -> None:
        left_prompt = "Grasp Coca-Cola with the left hand"
        right_prompt = "Grasp Vita Coconut with the right hand"
        self.robot.state[8:15] = np.linspace(0.11, 0.17, 7)
        self.robot.state[15] = 0.63
        self.robot.state[16] = 0.41
        initial_right = self.robot.state[8:16].copy()
        self.robot.track_feedback = True
        self.policy.raw_factory = lambda call: {
            "type": "action_chunk",
            "actions": [object()] * 50,
            "is_success": call == 7,
        }
        self.executor.probe()
        self.executor.dry_run(
            left_prompt,
            "lease",
            inference_mode="dual_separate",
            right_prompt=right_prompt,
        )
        self.executor.start(
            left_prompt,
            "lease",
            confirmation=REQUIRED_CONFIRMATION,
            inference_mode="dual_separate",
            right_prompt=right_prompt,
            steps_per_chunk=50,
            control_rate_hz=1000,
            joint_speed_deg_s=100000,
        )
        status = self.wait_for_phase(PHASE_IDLE, timeout=3.0)

        self.assertEqual(status["completion_reason"], "server_success")
        self.assertTrue(status["last_is_success"])
        self.assertEqual(status["task_stage"], "completed")
        self.assertEqual(status["active_prompt"], right_prompt)
        self.assertEqual(status["left_gripper_consecutive"], 150)
        self.assertEqual(status["executed_steps"], 200)
        self.assertGreaterEqual(len(self.robot.steps), 206)
        for action in self.robot.steps[:150]:
            np.testing.assert_array_equal(action[8:16], initial_right)
        home = self.robot.steps[150]
        np.testing.assert_array_equal(home[list(range(0, 7))], np.zeros(7))
        np.testing.assert_array_equal(home[list(range(8, 15))], np.zeros(7))
        self.assertEqual(home[7], 1.5)
        self.assertEqual(home[15], 0.0)
        self.assertEqual(home[16], 0.25)
        for action in self.robot.steps[150:]:
            np.testing.assert_array_equal(action[:7], np.zeros(7))
            self.assertEqual(action[7], 1.5)
        self.assertLessEqual(
            status["home_feedback_error_rad"],
            status["home_feedback_tolerance_rad"],
        )
        self.assertGreaterEqual(
            status["home_feedback_stable_steps"],
            status["home_feedback_required_stable_steps"],
        )
        self.assertEqual(
            [entry[2] for entry in self.policy.infer_inputs],
            [
                left_prompt,
                right_prompt,
                left_prompt,
                left_prompt,
                left_prompt,
                right_prompt,
                right_prompt,
            ],
        )

    def test_dual_separate_counter_advances_only_after_successful_policy_step(self) -> None:
        left_prompt = "Grasp Coca-Cola with the left hand"
        right_prompt = "Grasp Vita Coconut with the right hand"
        self.robot.fail_on_step = 150
        self.executor.probe()
        self.executor.dry_run(
            left_prompt,
            "lease",
            inference_mode="dual_separate",
            right_prompt=right_prompt,
        )
        self.executor.start(
            left_prompt,
            "lease",
            confirmation=REQUIRED_CONFIRMATION,
            inference_mode="dual_separate",
            right_prompt=right_prompt,
            steps_per_chunk=50,
            control_rate_hz=1000,
        )
        status = self.wait_for_phase(PHASE_FAULT, timeout=3.0)
        self.assertIn("policy step failed deterministically", status["fault"])
        self.assertEqual(status["executed_steps"], 149)
        self.assertEqual(status["left_gripper_consecutive"], 149)
        self.assertEqual(len(self.robot.steps), 149)

    def test_direction_semantics_and_fixed_status_side_are_checked_before_inference(self) -> None:
        self.executor.probe()
        with self.assertRaisesRegex(Pi05SafetyError, "match active_hand"):
            self.executor.dry_run(
                "Grasp Coca-Cola with the left hand",
                "lease",
                inference_mode="single",
                active_hand="right",
            )
        self.assertEqual(self.policy.infer_calls, 0)

        self.executor.close()
        self.setUp()
        self.policy.metadata_value["status_mode"] = "left"
        self.executor.probe()
        with self.assertRaisesRegex(Pi05SafetyError, "incompatible"):
            self.executor.dry_run(
                "Grasp Coca-Cola with the right hand",
                "lease",
                inference_mode="single",
                active_hand="right",
            )
        with self.assertRaisesRegex(Pi05SafetyError, "fixed-side"):
            self.executor.dry_run(
                "Grasp Coca-Cola with the left hand",
                "lease",
                inference_mode="dual_separate",
                right_prompt="Grasp Vita Coconut with the right hand",
            )
        self.assertEqual(self.policy.infer_calls, 0)

    def test_dual_separate_never_starts_right_stage_before_home_feedback_converges(self) -> None:
        left_prompt = "Grasp Coca-Cola with the left hand"
        right_prompt = "Grasp Vita Coconut with the right hand"
        self.robot.state[0] = 0.5
        self.executor.probe()
        self.executor.dry_run(
            left_prompt,
            "lease",
            inference_mode="dual_separate",
            right_prompt=right_prompt,
        )
        with mock.patch(
            "control.web_control.pi05_executor.DUAL_SEPARATE_HOME_TIMEOUT_S",
            0.03,
        ):
            self.executor.start(
                left_prompt,
                "lease",
                confirmation=REQUIRED_CONFIRMATION,
                inference_mode="dual_separate",
                right_prompt=right_prompt,
                steps_per_chunk=50,
                control_rate_hz=1000,
                joint_speed_deg_s=100000,
            )
            status = self.wait_for_phase(PHASE_FAULT, timeout=3.0)

        self.assertIn("home feedback did not converge", status["fault"])
        # The only right prompt was the read-only second dry-run validation;
        # execution itself never requested a right-stage chunk.
        self.assertEqual(
            [entry[2] for entry in self.policy.infer_inputs].count(right_prompt),
            1,
        )
        self.assertGreaterEqual(len(self.robot.steps), 151)
        for action in self.robot.steps[150:]:
            np.testing.assert_array_equal(action[list(range(0, 7))], np.zeros(7))
            self.assertEqual(action[7], 1.5)


if __name__ == "__main__":
    unittest.main()
