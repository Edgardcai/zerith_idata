from __future__ import annotations

import json
from pathlib import Path
import time
import unittest

import numpy as np

from control.web_control.pi05_action_modes import ChassisKinematics
from control.web_control.pi05_executor import Pi05SafetyError, REQUIRED_CONFIRMATION
from control.web_control.pi05_executor import Pi05Executor
from control.web_control.pi05_protocol import parse_action_chunk
from control.web_control.fake_sdk import FakeH1Robot, FakeSdk
from control.web_control.robot_service import RobotService
from control.web_control.tests.test_pi05_json_integration import observation_server
from control.web_control.tests.test_pi05_protocol import _action_response
from control.web_control.robot_service import POLICY_WIRE_MOTOR_IDS, RobotCommandRejected
from control.web_control.tests import test_pi05_executor as executor_tests
from control.web_control.tests.test_pi05_executor import valid_chunk
from control.web_control.tests import test_robot_service as robot_tests


class ExecutorExecutionModeTests(unittest.TestCase):
    setUp = executor_tests.Pi05ExecutorTests.setUp
    tearDown = executor_tests.Pi05ExecutorTests.tearDown
    wait_for_executed_steps = executor_tests.Pi05ExecutorTests.wait_for_executed_steps

    def test_default_holds_lift_at_start_across_chunks_and_feedback_drift(self):
        self.robot.state[16:21] = [0.4, 0.2, 0.1, 0.3, 0.1]
        self.executor.probe()
        self.executor.dry_run("task", "lease")
        # The start snapshot, not the earlier dry-run observation, owns holds.
        self.robot.state[16] = 0.45
        initial = self.robot.state.copy()

        def infer_hook(call):
            if call >= 2:
                self.robot.state[16:21] += 0.01

        self.policy.on_infer = infer_hook
        self.executor.start("task", "lease", confirmation=REQUIRED_CONFIRMATION,
                            steps_per_chunk=2, control_rate_hz=100)
        self.wait_for_executed_steps(6)
        self.executor.stop()
        self.assertGreaterEqual(self.policy.infer_calls, 4)
        for action in self.robot.steps:
            np.testing.assert_array_equal(action[16:21], initial[16:21])
            np.testing.assert_array_equal(action[21:], [0, 0])
        self.assertEqual(self.executor.status()["execution_mode"], "arms")
        self.assertTrue(all(s["sent_height"] == 0.45 for s in self.executor.lift_diagnostics()["steps"]))

    def test_17_dimensions_pass_lift_but_hold_waist_head_and_base(self):
        self.robot.state[16:21] = [0.4, 0.2, 0.1, 0.3, 0.1]
        initial = self.robot.state.copy()
        self.executor.probe()
        preview = self.executor.dry_run("task", "lease", execution_mode="arms_lift")
        self.assertEqual(preview["first_effective_action"][16], 0.25)
        self.executor.start("task", "lease", confirmation=REQUIRED_CONFIRMATION,
                            execution_mode="arms_lift")
        self.wait_for_executed_steps(3)
        self.executor.stop()
        for action in self.robot.steps:
            self.assertEqual(action[16], 0.25)
            np.testing.assert_array_equal(action[17:21], initial[17:21])
            np.testing.assert_array_equal(action[21:], [0, 0])

    def test_full_mode_passes_all_values_without_arm_slew_or_single_hand_mask(self):
        source = valid_chunk()
        source[:, :7] = 0.6
        source[:, 8:15] = -0.5
        self.policy.chunk_factory = lambda call: source.copy()
        self.executor.probe()
        prompt = "Grasp object with the left hand"
        preview = self.executor.dry_run(prompt, "lease", execution_mode="full",
                                       inference_mode="single", active_hand="left")
        np.testing.assert_array_equal(preview["first_effective_action"], source[0])
        self.executor.start(prompt, "lease", confirmation=REQUIRED_CONFIRMATION,
                            execution_mode="full", inference_mode="single", active_hand="left",
                            joint_speed_deg_s=0.01)
        self.wait_for_executed_steps(3)
        self.executor.stop()
        for index, action in enumerate(self.robot.steps):
            np.testing.assert_array_equal(action, source[index])
        self.assertEqual(self.executor.status()["arm_slew_limited_steps"], 0)

    def test_changing_mode_requires_matching_dry_run(self):
        self.executor.probe()
        self.executor.dry_run("task", "lease")
        for mode in ("arms_lift", "full"):
            with self.subTest(mode=mode), self.assertRaisesRegex(Pi05SafetyError, "exactly match"):
                self.executor.start("task", "lease", confirmation=REQUIRED_CONFIRMATION, execution_mode=mode)
        self.assertEqual(self.robot.begin_calls, 0)

    def test_unknown_mode_rejected_before_camera_or_sdk_access(self):
        self.executor.probe()
        for mode in (None, "", "17", True):
            with self.subTest(mode=mode), self.assertRaises(Pi05SafetyError):
                self.executor.dry_run("task", "lease", execution_mode=mode)
        self.assertEqual(self.robot.read_calls, 0)
        self.assertEqual(self.policy.infer_calls, 0)


class SdkExecutionModeTests(unittest.TestCase):
    setUp = robot_tests.RobotServiceTests.setUp
    tearDown = robot_tests.RobotServiceTests.tearDown
    acquire_and_init = robot_tests.RobotServiceTests.acquire_and_init
    restart_fake_service = robot_tests.RobotServiceTests.restart_fake_service

    def test_h1pro_config_drives_sdk_forward_reverse_and_turns(self):
        config = Path(__file__).resolve().parents[2] / "config/pi05_chassis_h1pro_v4_10.json"
        self.service._chassis_kinematics = ChassisKinematics(**json.loads(config.read_text()))
        lease = self.acquire_and_init()
        begin = self.service.begin_policy_session(lease, execution_mode="full")
        target = list(begin["state"])
        for v, w, expected in (
            (0.0835, 0, (1, 1)),
            (-0.0835, 0, (-1, -1)),
            (0, 0.167 / 0.379, (-1, 1)),
            (0, -0.167 / 0.379, (1, -1)),
            (0.0835, 0.167 / 0.379, (0, 2)),
        ):
            with self.subTest(linear=v, angular=w):
                target[21:] = [v, w]
                result = self.service.policy_step(lease, target, session_id=begin["session_id"])
                self.assertEqual(result["effective_action"], target)
                np.testing.assert_allclose(result["sent_chassis_wheel_rad_s"], expected, atol=1e-12)
                self.assertAlmostEqual(self.robot.states[0].Speed_Actual, expected[0])
                self.assertAlmostEqual(self.robot.states[1].Speed_Actual, expected[1])
        self.service.end_policy_session(lease, session_id=begin["session_id"])
        self.assertEqual(self.robot.states[0].Speed_Actual, 0)
        self.assertEqual(self.robot.states[1].Speed_Actual, 0)

    def test_sdk_holds_disabled_axes_at_session_start_even_if_feedback_changes(self):
        for mode, first_held in (("arms", 16), ("arms_lift", 17)):
            with self.subTest(mode=mode):
                self.restart_fake_service()
                lease = self.acquire_and_init()
                for motor, value in zip((2, 3, 4, 5, 6), (0.4, 0.2, 0.1, 0.3, 0.1)):
                    self.robot.states[motor].Position_Actual = value
                begin = self.service.begin_policy_session(lease, execution_mode=mode)
                target = list(begin["state"])
                target[16:21] = [0.65, 0.4, 0.2, 0.2, 0.2]
                target[21:] = [0.5, 0.2]
                for offset in (0.01, 0.02):
                    for motor in (2, 3, 4, 5, 6):
                        self.robot.states[motor].Position_Actual += offset
                    result = self.service.policy_step(lease, target, session_id=begin["session_id"])
                    self.assertEqual(result["effective_action"][first_held:21], begin["state"][first_held:21])
                    self.assertEqual(result["effective_action"][:first_held], target[:first_held])
                    self.assertEqual(result["effective_action"][21:], [0, 0])
                    for index, motor in enumerate(POLICY_WIRE_MOTOR_IDS):
                        self.assertEqual(self.robot.states[motor].Position_Actual, result["sent_action"][index])

    def test_full_sdk_preserves_21_positions_and_converts_both_base_velocities(self):
        self.restart_fake_service()
        # Fixture geometry only: deliberately unlike an assumed real robot.
        self.service._chassis_kinematics = ChassisKinematics(0.1, 0.5, -1, 1)
        lease = self.acquire_and_init()
        begin = self.service.begin_policy_session(lease, execution_mode="full")
        target = list(begin["state"])
        target[:7] = [0.1] * 7
        target[8:15] = [-0.1] * 7
        target[16:21] = [0.6, 0.3, -0.2, 0.4, -0.2]
        target[21:] = [0.2, 0.4]
        result = self.service.policy_step(lease, target, session_id=begin["session_id"])
        self.assertEqual(result["effective_action"], target)
        self.assertEqual(result["sent_action"], target[:21])
        np.testing.assert_allclose(result["sent_chassis_wheel_rad_s"], [-1, 3])
        self.assertAlmostEqual(self.robot.states[0].Speed_Actual, -1)
        self.assertAlmostEqual(self.robot.states[1].Speed_Actual, 3)
        self.service.end_policy_session(lease, session_id=begin["session_id"])
        self.assertEqual(self.robot.states[0].Speed_Actual, 0)
        self.assertEqual(self.robot.states[1].Speed_Actual, 0)

    def test_unconfigured_nonzero_base_is_rejected_before_position_writes(self):
        self.restart_fake_service()
        lease = self.acquire_and_init()
        begin = self.service.begin_policy_session(lease, execution_mode="full")
        target = list(begin["state"])
        target[0] = 0.7
        target[21] = 0.2
        with self.assertRaisesRegex(RobotCommandRejected, "未配置"):
            self.service.validate_policy_chassis([target])
        with self.assertRaisesRegex(RobotCommandRejected, "未配置"):
            self.service.policy_step(lease, target, session_id=begin["session_id"])
        self.assertNotEqual(self.robot.states[7].Position_Actual, 0.7)
        self.assertFalse(self.service.state()["policy_session"]["active"])


class WireToSdkExecutionModeTests(unittest.TestCase):
    def test_three_modes_from_real_websocket_json_through_sdk_setters(self):
        for mode, controlled_positions in (("arms", 16), ("arms_lift", 17), ("full", 21)):
            with self.subTest(mode=mode):
                sdk, hardware = FakeSdk(), FakeH1Robot()
                results = []

                class RecordingRobotService(RobotService):
                    def policy_step(self, *args, **kwargs):
                        result = super().policy_step(*args, **kwargs)
                        # Capture what the SDK received, not just executor output.
                        result["sdk_positions"] = [hardware.states[m].Position_Actual for m in POLICY_WIRE_MOTOR_IDS]
                        result["sdk_wheels"] = [hardware.states[m].Speed_Actual for m in (0, 1)]
                        results.append(result)
                        return result

                robot = RecordingRobotService(
                    sdk_loader=lambda: sdk, robot_factory=lambda _: hardware,
                    chassis_kinematics=ChassisKinematics(0.1, 0.5, -1, 1),
                    lease_seconds=20, trajectory_rate_hz=1,
                )
                executor = None
                try:
                    lease = robot.acquire()["lease_id"]
                    robot.initialize(lease)
                    for motor, value in zip((2, 3, 4, 5, 6), (0.45, 0.2, 0.1, 0.3, 0.1)):
                        hardware.states[motor].Position_Actual = value
                    initial = robot.read_policy_state(lease)["state"]
                    response = _action_response()
                    response.pop("is_success")  # Optional on the actual wire.
                    for index, action in enumerate(response["actions"]):
                        action["lift"]["height"] = 0.1 if index % 2 else 0.6
                        action["speed"] = {"linear": 0.2, "angular": 0.4}
                    source = parse_action_chunk(response)
                    with observation_server(response) as (port, requests, paths):
                        executor = Pi05Executor(robot, executor_tests.FakeCamera(), host="127.0.0.1", port=port)
                        executor.probe()
                        executor.dry_run("task", lease, execution_mode=mode)
                        executor.start("task", lease, confirmation=REQUIRED_CONFIRMATION,
                                       execution_mode=mode, steps_per_chunk=3, control_rate_hz=50)
                        deadline = time.monotonic() + 5
                        while len(results) < 7 and time.monotonic() < deadline:
                            if executor.status()["phase"] == "fault":
                                self.fail(executor.status()["fault"])
                            time.sleep(0.01)
                        executor.stop()
                        self.assertGreaterEqual(len(results), 7)
                        self.assertGreaterEqual(len(requests), 4)
                        self.assertTrue(all(r["type"] == "observation" for r in requests))
                        self.assertTrue(all(len(r["observation"]["state"]) == 23 for r in requests))
                        for index, result in enumerate(results):
                            row = source[index % 3]
                            self.assertEqual(result["sdk_positions"], result["effective_action"][:21])
                            self.assertEqual(result["sdk_positions"][controlled_positions:], initial[controlled_positions:21])
                            self.assertEqual(result["sdk_positions"][7], row[7])
                            self.assertEqual(result["sdk_positions"][15], row[15])
                            if mode != "arms":
                                self.assertEqual(result["sdk_positions"][16], row[16])
                            if mode == "full":
                                np.testing.assert_array_equal(result["effective_action"], row)
                                np.testing.assert_allclose(result["sdk_wheels"], [-1, 3])
                            else:
                                self.assertEqual(result["sdk_wheels"], [0, 0])
                finally:
                    if executor is not None:
                        executor.close()
                    robot.close()


if __name__ == "__main__":
    unittest.main()
