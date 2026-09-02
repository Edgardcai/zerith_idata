from __future__ import annotations

import threading
import time
import unittest

from control.web_control.fake_sdk import FakeH1Robot, FakeSdk
from control.web_control.pi05_executor import (
    PHASE_IDLE,
    PHASE_RUNNING,
    Pi05Executor,
    REQUIRED_CONFIRMATION,
)
from control.web_control.robot_service import POLICY_WIRE_MOTOR_IDS, RobotService
from control.web_control.tests.test_pi05_executor import (
    FakeCamera,
    FakePolicyClient,
    FakeRobot,
)


class _GatedFeedbackRobot(FakeH1Robot):
    """Pause exactly one feedback read after the test enables the gate."""

    def __init__(self) -> None:
        super().__init__()
        self.gate_enabled = False
        self.read_entered = threading.Event()
        self.release_read = threading.Event()
        self._gate_lock = threading.Lock()
        self._gate_used = False

    def _gated_get(self, motor_id):
        should_wait = False
        with self._gate_lock:
            if self.gate_enabled and not self._gate_used:
                self._gate_used = True
                should_wait = True
        if should_wait:
            self.read_entered.set()
            if not self.release_read.wait(2.0):
                raise RuntimeError("test did not release gated feedback read")
        return True, self.states[int(motor_id)]

    getChassisState = _gated_get
    getWaistState = _gated_get
    getHeadState = _gated_get
    getArmState = _gated_get
    getGripperState = _gated_get


class _BlockingBeginRobot(FakeRobot):
    """Hold begin_policy_session open to deterministically race STOP."""

    def __init__(self) -> None:
        super().__init__()
        self.begin_entered = threading.Event()
        self.release_begin = threading.Event()

    def begin_policy_session(self, lease_id: str) -> dict:
        self.begin_entered.set()
        if not self.release_begin.wait(2.0):
            raise RuntimeError("test did not release begin_policy_session")
        return super().begin_policy_session(lease_id)


class _BlockingPolicyStepRobot(FakeRobot):
    """Hold one admitted policy_step inside the executor's STOP barrier."""

    def __init__(self) -> None:
        super().__init__()
        self.step_entered = threading.Event()
        self.release_step = threading.Event()

    def policy_step(
        self,
        lease_id: str,
        action,
        *,
        session_id=None,
    ) -> dict:
        self.step_entered.set()
        if not self.release_step.wait(2.0):
            raise RuntimeError("test did not release policy_step")
        return super().policy_step(
            lease_id,
            action,
            session_id=session_id,
        )


class PolicyQueueRegressionTests(unittest.TestCase):
    def test_session_observation_and_step_serialize_without_conflict(self) -> None:
        sdk = FakeSdk()
        fake_robot = _GatedFeedbackRobot()
        service = RobotService(
            sdk_loader=lambda: sdk,
            robot_factory=lambda _sdk: fake_robot,
            lease_seconds=5.0,
            trajectory_rate_hz=20.0,
        )
        try:
            lease_id = service.acquire("policy-queue-regression")["lease_id"]
            service.initialize(lease_id)
            session_id = service.begin_policy_session(lease_id)["session_id"]
            fake_robot.gate_enabled = True

            results: dict[str, dict] = {}
            errors: list[BaseException] = []

            def read_observation() -> None:
                try:
                    results["observation"] = service.read_policy_state(
                        lease_id,
                        session_id=session_id,
                    )
                except BaseException as exc:
                    errors.append(exc)

            def send_step() -> None:
                try:
                    results["step"] = service.policy_step(
                        lease_id,
                        [0.0] * 23,
                        session_id=session_id,
                    )
                except BaseException as exc:
                    errors.append(exc)

            observation_thread = threading.Thread(target=read_observation)
            observation_thread.start()
            self.assertTrue(fake_robot.read_entered.wait(1.0))

            step_thread = threading.Thread(target=send_step)
            step_thread.start()
            time.sleep(0.03)
            self.assertTrue(
                step_thread.is_alive(),
                "policy_step should wait behind the owner-thread observation",
            )
            fake_robot.release_read.set()
            observation_thread.join(2.0)
            step_thread.join(2.0)

            self.assertFalse(observation_thread.is_alive())
            self.assertFalse(step_thread.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(results["observation"]["ok"])
            self.assertTrue(results["step"]["ok"])
            self.assertEqual(
                results["step"]["sent_motor_ids"],
                list(POLICY_WIRE_MOTOR_IDS),
            )
        finally:
            fake_robot.release_read.set()
            service.close()


class StartStopRegressionTests(unittest.TestCase):
    def test_stop_closes_admission_before_a_waiting_policy_step(self) -> None:
        robot = FakeRobot()
        camera = FakeCamera()
        policy = FakePolicyClient()
        executor = Pi05Executor(
            robot,
            camera,
            policy_factory=lambda _host, _port: policy,
            health_probe=lambda _host, _port: "OK",
            stop_timeout_s=1.0,
        )
        prompt = "stop before step admission"
        barrier_held = False
        stop_results: list[dict] = []
        stop_errors: list[BaseException] = []
        try:
            executor.probe()
            executor.dry_run(prompt, "lease")

            executor._step_stop_barrier.acquire()
            barrier_held = True
            executor.start(
                prompt,
                "lease",
                confirmation=REQUIRED_CONFIRMATION,
                steps_per_chunk=30,
                control_rate_hz=120,
            )

            deadline = time.monotonic() + 1.0
            while policy.infer_calls < 2:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.002)

            def stop() -> None:
                try:
                    stop_results.append(executor.stop(reason="admission_gate_stop"))
                except BaseException as exc:
                    stop_errors.append(exc)

            stop_thread = threading.Thread(target=stop)
            stop_thread.start()
            self.assertTrue(executor._stop_event.wait(1.0))
            self.assertTrue(stop_thread.is_alive())

            executor._step_stop_barrier.release()
            barrier_held = False
            stop_thread.join(2.0)

            self.assertFalse(stop_thread.is_alive())
            self.assertEqual(stop_errors, [])
            self.assertEqual(stop_results[-1]["phase"], PHASE_IDLE)
            self.assertEqual(robot.steps, [])
        finally:
            if barrier_held:
                executor._step_stop_barrier.release()
            executor.close()

    def test_stop_waits_for_one_inflight_step_and_no_setter_follows_return(self) -> None:
        robot = _BlockingPolicyStepRobot()
        camera = FakeCamera()
        policy = FakePolicyClient()
        executor = Pi05Executor(
            robot,
            camera,
            policy_factory=lambda _host, _port: policy,
            health_probe=lambda _host, _port: "OK",
            stop_timeout_s=1.0,
        )
        prompt = "drain inflight step"
        stop_results: list[dict] = []
        stop_errors: list[BaseException] = []
        try:
            executor.probe()
            executor.dry_run(prompt, "lease")
            executor.start(
                prompt,
                "lease",
                confirmation=REQUIRED_CONFIRMATION,
                steps_per_chunk=30,
                control_rate_hz=120,
            )
            self.assertTrue(robot.step_entered.wait(1.0))

            def stop() -> None:
                try:
                    stop_results.append(executor.stop(reason="inflight_step_stop"))
                except BaseException as exc:
                    stop_errors.append(exc)

            stop_thread = threading.Thread(target=stop)
            stop_thread.start()
            self.assertTrue(executor._stop_event.wait(1.0))
            self.assertTrue(
                stop_thread.is_alive(),
                "STOP must wait for the already admitted policy_step",
            )

            robot.release_step.set()
            stop_thread.join(2.0)

            self.assertFalse(stop_thread.is_alive())
            self.assertEqual(stop_errors, [])
            self.assertEqual(stop_results[-1]["phase"], PHASE_IDLE)
            self.assertEqual(len(robot.steps), 1)
            setters_at_stop_return = len(robot.steps)
            time.sleep(0.05)
            self.assertEqual(len(robot.steps), setters_at_stop_return)
        finally:
            robot.release_step.set()
            executor.close()

    def test_stop_during_blocked_begin_prevents_running_result(self) -> None:
        robot = _BlockingBeginRobot()
        camera = FakeCamera()
        policy = FakePolicyClient()
        executor = Pi05Executor(
            robot,
            camera,
            policy_factory=lambda _host, _port: policy,
            health_probe=lambda _host, _port: "OK",
            stop_timeout_s=1.0,
        )
        prompt = "concurrent start stop regression"
        start_results: list[dict] = []
        stop_results: list[dict] = []
        start_errors: list[BaseException] = []
        stop_errors: list[BaseException] = []
        try:
            executor.probe()
            executor.dry_run(prompt, "lease")

            def start() -> None:
                try:
                    start_results.append(
                        executor.start(
                            prompt,
                            "lease",
                            confirmation=REQUIRED_CONFIRMATION,
                            steps_per_chunk=1,
                        )
                    )
                except BaseException as exc:
                    start_errors.append(exc)

            def stop() -> None:
                try:
                    stop_results.append(executor.stop(reason="concurrent_stop"))
                except BaseException as exc:
                    stop_errors.append(exc)

            start_thread = threading.Thread(target=start)
            start_thread.start()
            self.assertTrue(robot.begin_entered.wait(1.0))
            stop_thread = threading.Thread(target=stop)
            stop_thread.start()
            time.sleep(0.03)
            robot.release_begin.set()
            start_thread.join(2.0)
            stop_thread.join(2.0)

            self.assertFalse(start_thread.is_alive())
            self.assertFalse(stop_thread.is_alive())
            self.assertEqual(stop_errors, [])
            self.assertTrue(stop_results)
            self.assertEqual(stop_results[-1]["phase"], PHASE_IDLE)
            self.assertEqual(executor.status()["phase"], PHASE_IDLE)
            self.assertFalse(
                any(result.get("phase") == PHASE_RUNNING for result in start_results),
                "a start overtaken by STOP must not report running",
            )
            self.assertEqual(robot.steps, [])
            # The expected implementation may reject the overtaken start; the
            # exact exception type is deliberately not coupled to this test.
            self.assertLessEqual(len(start_errors), 1)
        finally:
            robot.release_begin.set()
            executor.close()

    def test_explicit_stop_does_not_poison_a_later_confirmed_run(self) -> None:
        robot = FakeRobot()
        camera = FakeCamera()
        policies: list[FakePolicyClient] = []

        def policy_factory(_host: str, _port: int) -> FakePolicyClient:
            policy = FakePolicyClient()
            policies.append(policy)
            return policy

        executor = Pi05Executor(
            robot,
            camera,
            policy_factory=policy_factory,
            health_probe=lambda _host, _port: "OK",
            stop_timeout_s=1.0,
        )
        prompt = "restart only after a new confirmation"
        try:
            executor.probe()
            executor.dry_run(prompt, "lease")
            policies[-1].block = True
            policies[-1].entered_infer.clear()
            executor.start(
                prompt,
                "lease",
                confirmation=REQUIRED_CONFIRMATION,
                steps_per_chunk=5,
            )
            self.assertTrue(policies[-1].entered_infer.wait(1.0))
            self.assertEqual(executor.stop(reason="first_run_stop")["phase"], PHASE_IDLE)

            # A new probe, dry-run and confirmation are the explicit operator
            # re-arm.  The Event set by the previous STOP must not cancel this
            # separately confirmed session.
            executor.probe()
            executor.dry_run(prompt, "lease")
            started = executor.start(
                prompt,
                "lease",
                confirmation=REQUIRED_CONFIRMATION,
                steps_per_chunk=1,
            )
            self.assertEqual(started["phase"], PHASE_RUNNING)
            deadline = time.monotonic() + 2.0
            while not robot.steps:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.005)
            self.assertEqual(executor.stop(reason="second_run_stop")["phase"], PHASE_IDLE)
            self.assertGreaterEqual(len(robot.steps), 1)
        finally:
            executor.close()


if __name__ == "__main__":
    unittest.main()
