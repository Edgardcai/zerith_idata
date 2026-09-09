from __future__ import annotations

import threading
import time
import unittest

from control.web_control.fake_sdk import FakeH1Robot, FakeSdk
from control.web_control.robot_service import (
    ARM_HOME_TARGETS,
    HOME_TARGETS,
    MOTOR_SPEC_BY_ID,
    POLICY_WIRE_MOTOR_IDS,
    RobotCallTimeout,
    RobotCommandRejected,
    RobotConflict,
    RobotService,
)


class RobotServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sdk = FakeSdk()
        self.robot = FakeH1Robot()
        self.loads = 0

        def loader():
            self.loads += 1
            return self.sdk

        self.service = RobotService(
            sdk_loader=loader,
            robot_factory=lambda _sdk: self.robot,
            lease_seconds=0.4,
            chassis_watchdog_seconds=0.05,
            trajectory_rate_hz=250.0,
            state_rate_hz=25.0,
            home_duration_s=0.025,
            home_lift_timeout_s=0.2,
        )

    def tearDown(self) -> None:
        self.service.close()

    def acquire_and_init(self) -> str:
        lease = self.service.acquire()["lease_id"]
        self.service.initialize(lease)
        return lease

    def restart_fake_service(
        self,
        *,
        robot: FakeH1Robot | None = None,
        lease_seconds: float = 5.0,
        trajectory_rate_hz: float = 1.0,
    ) -> None:
        self.service.close()
        self.robot = robot or FakeH1Robot()
        self.service = RobotService(
            sdk_loader=lambda: self.sdk,
            robot_factory=lambda _sdk: self.robot,
            lease_seconds=lease_seconds,
            chassis_watchdog_seconds=0.05,
            trajectory_rate_hz=trajectory_rate_hz,
            state_rate_hz=25.0,
            home_duration_s=0.025,
            home_lift_timeout_s=0.2,
        )

    def policy_action_from_feedback(self) -> list[float]:
        action = [
            float(self.robot.states[motor_id].Position_Actual)
            for motor_id in POLICY_WIRE_MOTOR_IDS
        ] + [0.0, 0.0]
        for index in (*range(0, 7), *range(8, 15)):
            action[index] += 0.1
        action[7] = 1.5
        action[15] = 0.0
        action[16] += 0.01
        action[17:21] = [99.0, -99.0, 88.0, -88.0]
        action[21:23] = [7.0, -7.0]
        return action

    def test_sdk_is_lazy_and_takeover_defaults_off(self) -> None:
        state = self.service.state()
        self.assertFalse(state["takeover"])
        self.assertFalse(state["sdk_loaded"])
        self.assertEqual(self.loads, 0)
        lease = self.service.acquire()["lease_id"]
        self.assertTrue(lease)
        self.assertEqual(self.loads, 1)

    def test_only_one_page_can_hold_takeover(self) -> None:
        first = self.service.acquire("page-a")
        self.assertEqual(first["client_id"], "page-a")
        self.assertEqual(self.service.state()["takeover_client_id"], "page-a")
        with self.assertRaises(RobotConflict):
            self.service.acquire("page-b")
        self.service.release(first["lease_id"])
        second = self.service.acquire("page-b")
        self.assertEqual(second["client_id"], "page-b")

    def test_config_uses_exact_sdk_soft_limits_without_margin(self) -> None:
        config = self.service.config()
        motors = {item["id"]: item for item in config["motors"]}
        self.assertEqual((motors[7]["min"], motors[7]["max"]), (-2.7, 1.5))
        self.assertEqual((motors[14]["min"], motors[14]["max"]), (0.0, 1.5))
        self.assertEqual((motors[3]["min"], motors[3]["max"]), (0.0, 1.3))
        self.assertEqual((motors[6]["min"], motors[6]["max"]), (-0.5, 0.75))
        self.assertIsNone(config["chassis"]["wheel_speed"]["min"])
        self.assertIsNone(config["chassis"]["wheel_speed"]["max"])
        self.assertEqual(config["motion_speed"]["default"], 1.0)
        self.assertEqual(
            (config["motion_speed"]["min"], config["motion_speed"]["max"]),
            (0.2, 2.0),
        )
        self.assertEqual(
            config["policy"]["wire_motor_ids"],
            [*range(7, 15), *range(15, 23), 2, 3, 4, 5, 6],
        )
        self.assertEqual(config["policy"]["state_dim"], 23)
        self.assertEqual(config["policy"]["position_dim"], 21)
        self.assertFalse(
            any(
                "delta" in key or "speed_limit" in key
                for key in config["policy"]
            )
        )
        self.assertNotIn("watchdog_ms", config["policy"])
        self.assertNotIn("first_step_grace_ms", config["policy"])
        self.assertNotIn("feedback_limit_tolerance", config["policy"])

    def test_policy_state_is_read_only_23d_and_uses_wire_order(self) -> None:
        lease = self.acquire_and_init()
        expected = []
        for index, motor_id in enumerate(POLICY_WIRE_MOTOR_IDS):
            spec = MOTOR_SPEC_BY_ID[motor_id]
            position = spec.minimum + 0.25 * (spec.maximum - spec.minimum)
            self.robot.states[motor_id].Position_Actual = position
            expected.append(position)
        self.robot.calls.clear()

        result = self.service.read_policy_state(lease)

        self.assertEqual(result["state"], expected + [0.0, 0.0])
        self.assertEqual(len(result["state"]), 23)
        setters = [call for call in self.robot.calls if call[0].startswith("set")]
        self.assertEqual(setters, [])

    def test_policy_feedback_preserves_finite_values_without_range_threshold(self) -> None:
        lease = self.service.acquire()["lease_id"]
        self.robot.states[2].Position_Actual = -0.25
        self.robot.states[3].Position_Actual = -2.396844865870662e-05
        result = self.service.read_policy_state(lease)
        self.assertEqual(result["state"][16], -0.25)
        self.assertEqual(result["state"][17], -2.396844865870662e-05)

        self.robot.states[2].Position_Actual = float("nan")
        with self.assertRaisesRegex(RobotCommandRejected, "必须是有限数"):
            self.service.read_policy_state(lease)

    def test_policy_session_blocks_normal_motion_but_stop_is_always_allowed(self) -> None:
        lease = self.acquire_and_init()
        session = self.service.begin_policy_session(lease)
        self.assertTrue(self.service.state()["policy_session"]["active"])
        with self.assertRaises(RobotConflict):
            self.service.move_joint(lease, 7, 0.1, duration_s=0.005)
        with self.assertRaises(RobotConflict):
            self.service.command_chassis(lease, 1.0, 1.0)
        with self.assertRaises(RobotConflict):
            self.service.deinitialize(lease)
        with self.assertRaises(RobotConflict):
            self.service.move_arm_home(lease)
        observation = self.service.read_policy_state(
            lease,
            session_id=session["session_id"],
        )
        self.assertEqual(len(observation["state"]), 23)

        stopped = self.service.stop_motion(lease, renew_lease=False)

        self.assertTrue(stopped["ok"])
        self.assertFalse(self.service.state()["policy_session"]["active"])
        self.assertNotIn(("robot_deinit",), self.robot.calls)
        self.assertEqual(self.robot.states[0].Speed_Actual, 0.0)
        self.assertEqual(self.robot.states[1].Speed_Actual, 0.0)

    def test_policy_step_masks_body_and_base_and_sends_exactly_21_positions(self) -> None:
        self.restart_fake_service(trajectory_rate_hz=1.0)
        lease = self.acquire_and_init()
        self.robot.states[2].Position_Actual = 0.4
        self.robot.states[3].Position_Actual = -2.396844865870662e-05
        self.robot.states[4].Position_Actual = -0.2
        self.robot.states[5].Position_Actual = 0.3
        self.robot.states[6].Position_Actual = -0.1
        begin = self.service.begin_policy_session(lease)
        time.sleep(0.03)  # let the worker's immediate hold tick finish
        self.robot.calls.clear()
        action = self.policy_action_from_feedback()
        action[7] = 0.42
        action[15] = 1.23

        result = self.service.policy_step(
            lease,
            action,
            session_id=begin["session_id"],
        )

        self.assertEqual(len(result["latest_state"]), 23)
        self.assertEqual(len(result["requested_action"]), 23)
        self.assertEqual(len(result["effective_action"]), 23)
        self.assertEqual(len(result["sent_action"]), 21)
        self.assertEqual(result["sent_motor_ids"], list(POLICY_WIRE_MOTOR_IDS))
        self.assertEqual(
            result["effective_action"][17:21],
            begin["body_hold_target"],
        )
        self.assertEqual(result["effective_action"][17], -2.396844865870662e-05)
        self.assertEqual(result["effective_action"][21:23], [0.0, 0.0])
        self.assertEqual(result["effective_action"][7], 0.42)
        self.assertEqual(result["effective_action"][15], 1.23)
        self.assertEqual(result["sent_action"][7], 0.42)
        self.assertEqual(result["sent_action"][15], 1.23)
        self.assertNotIn("max_arm_delta_rad", result)
        self.assertNotIn("joint_speed_deg_s", result)
        self.assertNotIn("max_joint_step_rad", result)
        position_names = {
            "setArm_low",
            "setGripper_low",
            "setWaist_low",
            "setHead_low",
        }
        position_calls = [
            call for call in self.robot.calls if call[0] in position_names
        ]
        self.assertEqual(
            [call[1] for call in position_calls[:21]],
            list(POLICY_WIRE_MOTOR_IDS),
        )
        self.assertEqual(
            [call[2] for call in position_calls[:21]],
            result["sent_action"],
        )
        self.assertFalse(
            any(call[0] == "setChassis_low" and call[2] != 0.0 for call in self.robot.calls)
        )

    def test_policy_body_hold_stays_at_session_start_when_feedback_drifts(self) -> None:
        self.restart_fake_service(trajectory_rate_hz=1.0)
        lease = self.acquire_and_init()
        initial_body = [-2.396844865870662e-05, -0.2, 0.0032, 0.0242]
        for motor_id, target in zip((3, 4, 5, 6), initial_body):
            self.robot.states[motor_id].Position_Actual = target
        begin = self.service.begin_policy_session(lease)
        self.assertEqual(begin["body_hold_target"], initial_body)
        self.assertEqual(begin["body_hold_reference"], "session_start_feedback")

        observed_drift = [0.08, -0.05, 0.12, 0.24]
        for motor_id, target in zip((3, 4, 5, 6), observed_drift):
            self.robot.states[motor_id].Position_Actual = target
        first = self.service.policy_step(
            lease,
            self.policy_action_from_feedback(),
            session_id=begin["session_id"],
        )
        self.assertEqual(first["latest_state"][17:21], observed_drift)
        self.assertEqual(first["effective_action"][17:21], initial_body)
        self.assertEqual(first["body_hold_target"], initial_body)
        self.assertEqual(
            [self.robot.states[motor_id].Position_Actual for motor_id in (3, 4, 5, 6)],
            initial_body,
        )

        second_drift = [0.16, 0.02, 0.25, 0.48]
        for motor_id, target in zip((3, 4, 5, 6), second_drift):
            self.robot.states[motor_id].Position_Actual = target
        second = self.service.policy_step(
            lease,
            self.policy_action_from_feedback(),
            session_id=begin["session_id"],
        )
        self.assertEqual(second["latest_state"][17:21], second_drift)
        self.assertEqual(second["effective_action"][17:21], initial_body)

        policy_state = self.service.state()["policy_session"]
        self.assertEqual(policy_state["body_hold_target"], initial_body)
        self.assertEqual(
            policy_state["last_body_diagnostics"],
            {
                "feedback": second_drift,
                "requested": [99.0, -99.0, 88.0, -88.0],
                "effective": initial_body,
            },
        )
        self.service.end_policy_session(
            lease,
            session_id=begin["session_id"],
        )
        self.assertIsNone(
            self.service.state()["policy_session"]["body_hold_target"]
        )

    def test_policy_calls_validate_but_do_not_renew_lease(self) -> None:
        self.restart_fake_service()
        lease = self.acquire_and_init()
        with self.service._lease_lock:
            deadline = self.service._lease_deadline
        self.service.read_policy_state(lease)
        begin = self.service.begin_policy_session(lease)
        result = self.service.policy_step(
            lease,
            self.policy_action_from_feedback(),
            session_id=begin["session_id"],
        )
        self.service.end_policy_session(
            lease,
            session_id=result["session_id"],
        )
        with self.service._lease_lock:
            self.assertEqual(self.service._lease_deadline, deadline)

    def test_invalid_policy_actions_fail_closed_without_deinit(self) -> None:
        self.restart_fake_service()
        lease = self.acquire_and_init()
        invalid_actions = []
        valid = self.policy_action_from_feedback()
        invalid_actions.append(valid[:-1])
        not_finite = list(valid)
        not_finite[0] = float("nan")
        invalid_actions.append(not_finite)
        out_of_range_gripper = list(valid)
        out_of_range_gripper[7] = 1.6
        invalid_actions.append(out_of_range_gripper)

        for action in invalid_actions:
            with self.subTest(action=action):
                begin = self.service.begin_policy_session(lease)
                with self.assertRaises(RobotCommandRejected):
                    self.service.policy_step(
                        lease,
                        action,
                        session_id=begin["session_id"],
                    )
                self.assertFalse(
                    self.service.state()["policy_session"]["active"]
                )
                self.assertNotIn(("robot_deinit",), self.robot.calls)
                self.assertEqual(self.robot.states[0].Speed_Actual, 0.0)
                self.assertEqual(self.robot.states[1].Speed_Actual, 0.0)

    def test_policy_step_sends_large_arm_and_lift_changes_without_clamping(self) -> None:
        self.restart_fake_service()
        lease = self.acquire_and_init()
        session_id = self.service.begin_policy_session(lease)["session_id"]
        action = [
            float(self.robot.states[motor_id].Position_Actual)
            for motor_id in POLICY_WIRE_MOTOR_IDS
        ] + [0.0, 0.0]
        action[0] = 1.2
        action[8] = -1.2
        action[16] = 0.7

        result = self.service.policy_step(
            lease,
            action,
            session_id=session_id,
        )
        self.assertTrue(self.service.state()["policy_session"]["active"])
        self.assertEqual(result["requested_action"][0], 1.2)
        self.assertEqual(result["effective_action"][0], 1.2)
        self.assertEqual(result["sent_action"][0], 1.2)
        self.assertEqual(result["effective_action"][8], -1.2)
        self.assertEqual(result["effective_action"][16], 0.7)
        self.assertEqual(self.robot.states[7].Position_Actual, 1.2)
        self.assertEqual(self.robot.states[15].Position_Actual, -1.2)
        self.assertEqual(self.robot.states[2].Position_Actual, 0.7)
        self.assertNotIn(("robot_deinit",), self.robot.calls)

    def test_policy_step_still_rejects_targets_outside_sdk_soft_limits(self) -> None:
        self.restart_fake_service()
        lease = self.acquire_and_init()
        cases = ((0, 1.500001), (16, 0.800001))

        for index, target in cases:
            with self.subTest(index=index, target=target):
                action = [
                    float(self.robot.states[motor_id].Position_Actual)
                    for motor_id in POLICY_WIRE_MOTOR_IDS
                ] + [0.0, 0.0]
                action[index] = target
                session_id = self.service.begin_policy_session(lease)[
                    "session_id"
                ]
                with self.assertRaisesRegex(RobotCommandRejected, "超出 SDK 范围"):
                    self.service.policy_step(
                        lease,
                        action,
                        session_id=session_id,
                    )
                self.assertFalse(self.service.state()["policy_session"]["active"])
                self.assertNotIn(("robot_deinit",), self.robot.calls)

    def test_motor_error_or_partial_setter_failure_ends_policy_safely(self) -> None:
        self.restart_fake_service()
        lease = self.acquire_and_init()
        begin = self.service.begin_policy_session(lease)
        self.robot.states[9].Error_flag = 0x0008
        with self.assertRaisesRegex(RobotCommandRejected, "0x0008"):
            self.service.policy_step(
                lease,
                self.policy_action_from_feedback(),
                session_id=begin["session_id"],
            )
        self.assertFalse(self.service.state()["policy_session"]["active"])
        self.assertNotIn(("robot_deinit",), self.robot.calls)

        self.robot.states[9].Error_flag = 0
        begin = self.service.begin_policy_session(lease)
        self.robot.fail_next_set = True
        with self.assertRaisesRegex(RobotCommandRejected, "setter"):
            self.service.policy_step(
                lease,
                self.policy_action_from_feedback(),
                session_id=begin["session_id"],
            )
        self.assertFalse(self.service.state()["policy_session"]["active"])
        self.assertEqual(self.robot.states[0].Speed_Actual, 0.0)
        self.assertEqual(self.robot.states[1].Speed_Actual, 0.0)
        self.assertNotIn(("robot_deinit",), self.robot.calls)

    def test_policy_session_has_no_first_step_or_step_timing_threshold(self) -> None:
        self.restart_fake_service(
            lease_seconds=1.0,
            trajectory_rate_hz=20.0,
        )
        lease = self.acquire_and_init()
        begin = self.service.begin_policy_session(lease)
        self.assertNotIn("first_step_grace_seconds", begin)
        self.assertNotIn("step_watchdog_seconds", begin)
        time.sleep(0.4)
        policy_state = self.service.state()["policy_session"]
        self.assertTrue(policy_state["active"])
        self.assertNotIn("awaiting_first_step", policy_state)
        self.assertNotIn("watchdog_remaining_ms", policy_state)

        self.service.policy_step(
            lease,
            self.policy_action_from_feedback(),
            session_id=begin["session_id"],
        )
        time.sleep(0.4)
        self.assertTrue(self.service.state()["policy_session"]["active"])
        ended = self.service.end_policy_session(
            lease,
            session_id=begin["session_id"],
        )
        self.assertTrue(ended["ok"])

    def test_all_position_endpoints_are_accepted_and_outside_rejected(self) -> None:
        lease = self.acquire_and_init()
        for motor_id, spec in MOTOR_SPEC_BY_ID.items():
            result = self.service.move_joint(
                lease,
                motor_id,
                spec.minimum,
                duration_s=0.005,
            )
            self.assertEqual(result["target"], spec.minimum)
            result = self.service.move_joint(
                lease,
                motor_id,
                spec.maximum,
                duration_s=0.005,
            )
            self.assertEqual(result["target"], spec.maximum)
            with self.assertRaises(RobotCommandRejected):
                self.service.move_joint(
                    lease,
                    motor_id,
                    spec.maximum + 1e-6,
                    duration_s=0.005,
                )

    def test_release_requires_explicit_deinit(self) -> None:
        lease = self.acquire_and_init()
        with self.assertRaises(RobotConflict):
            self.service.release(lease)
        self.service.deinitialize(lease)
        released = self.service.release(lease)
        self.assertFalse(released["takeover"])
        self.assertFalse(released["connected"])
        self.assertEqual(released["motors"], {})
        self.assertIsNone(released["power"])
        self.assertFalse(self.service.state()["sdk_loaded"])

    def test_home_matches_requested_targets_and_holds(self) -> None:
        lease = self.acquire_and_init()
        result = self.service.move_home(lease, speed_scale=2.0)
        self.assertTrue(result["holding"])
        self.assertAlmostEqual(result["duration_s"], 0.0125)
        self.assertEqual(set(HOME_TARGETS), set(range(2, 23)))
        self.assertEqual(HOME_TARGETS[2], 0.4)
        for motor_id, target in HOME_TARGETS.items():
            self.assertAlmostEqual(self.robot.states[motor_id].Position_Actual, target)
        self.assertTrue(all(HOME_TARGETS[motor_id] == 0.0 for motor_id in range(3, 23)))
        self.assertEqual(self.robot.states[0].Speed_Actual, 0.0)
        self.assertEqual(self.robot.states[1].Speed_Actual, 0.0)
        hold_calls_before = len(self.robot.calls)
        time.sleep(0.03)
        self.assertGreater(len(self.robot.calls), hold_calls_before)

    def test_arm_home_zeros_body_arms_and_grippers_but_preserves_lift(self) -> None:
        lease = self.acquire_and_init()
        lift_before = 0.63
        self.robot.states[2].Position_Actual = lift_before
        for motor_id in ARM_HOME_TARGETS:
            self.robot.states[motor_id].Position_Actual = (
                1.2 if motor_id in (14, 22) else 0.1
            )
        self.robot.states[0].Speed_Actual = 2.0
        self.robot.states[1].Speed_Actual = -2.0
        self.robot.calls.clear()

        result = self.service.move_arm_home(lease, speed_scale=2.0)

        self.assertTrue(result["holding"])
        self.assertAlmostEqual(result["duration_s"], 0.0125)
        self.assertAlmostEqual(result["lift_held"], lift_before)
        self.assertAlmostEqual(self.robot.states[2].Position_Actual, lift_before)
        for motor_id, target in ARM_HOME_TARGETS.items():
            self.assertEqual(target, 0.0)
            self.assertAlmostEqual(self.robot.states[motor_id].Position_Actual, 0.0)
        self.assertEqual(self.robot.states[0].Speed_Actual, 0.0)
        self.assertEqual(self.robot.states[1].Speed_Actual, 0.0)
        chassis_calls = [
            call for call in self.robot.calls if call[0] == "setChassis_low"
        ]
        self.assertTrue(chassis_calls)
        self.assertTrue(all(call[2] == 0.0 for call in chassis_calls))

    def test_motion_speed_scale_is_bounded(self) -> None:
        self.assertEqual(self.service._parse_motion_speed_scale(1.0), 1.0)
        for value in (0.19, 2.01, float("inf")):
            with self.assertRaises(RobotCommandRejected):
                self.service._parse_motion_speed_scale(value)

    def test_single_arm_joint_refreshes_all_seven_axes_per_cycle(self) -> None:
        lease = self.acquire_and_init()
        initial = {motor_id: motor_id / 100.0 for motor_id in range(7, 14)}
        for motor_id, position in initial.items():
            self.robot.states[motor_id].Position_Actual = position
        self.robot.calls.clear()

        self.service.move_joint(lease, 9, 0.5, duration_s=0.012)

        arm_calls = [
            call for call in self.robot.calls if call[0] == "setArm_low"
        ]
        self.assertTrue(arm_calls)
        self.assertEqual(
            {call[1] for call in arm_calls[:7]},
            set(range(7, 14)),
        )
        for motor_id, position in initial.items():
            expected = 0.5 if motor_id == 9 else position
            self.assertAlmostEqual(
                self.robot.states[motor_id].Position_Actual,
                expected,
            )

    def test_multi_arm_trajectory_is_synchronized_and_arm_only(self) -> None:
        lease = self.acquire_and_init()
        initial = {
            motor_id: motor_id / 100.0
            for motor_id in (*range(7, 14), *range(15, 22))
        }
        for motor_id, position in initial.items():
            self.robot.states[motor_id].Position_Actual = position
        self.robot.calls.clear()

        result = self.service.move_joints(
            lease,
            {7: -0.3, 10: -0.9, 15: -0.3, 18: -0.9},
            duration_s=0.012,
        )

        self.assertEqual(
            result["targets"],
            {"7": -0.3, "10": -0.9, "15": -0.3, "18": -0.9},
        )
        for motor_id, position in initial.items():
            expected = {
                7: -0.3,
                10: -0.9,
                15: -0.3,
                18: -0.9,
            }.get(motor_id, position)
            self.assertAlmostEqual(
                self.robot.states[motor_id].Position_Actual,
                expected,
            )

        for forbidden_id in (3, 5, 14, 22):
            with self.subTest(forbidden_id=forbidden_id):
                with self.assertRaises(RobotCommandRejected):
                    self.service.move_joints(
                        lease,
                        {forbidden_id: 0.0},
                        duration_s=0.005,
                    )

    def test_gesture_smoothing_tapers_the_trajectory_endpoints(self) -> None:
        lease = self.acquire_and_init()
        self.robot.states[9].Position_Actual = 0.0
        self.robot.calls.clear()

        self.service.move_joint(
            lease,
            9,
            0.5,
            duration_s=0.04,
            smooth=True,
        )

        targets = [
            call[2]
            for call in self.robot.calls
            if call[0] == "setArm_low" and call[1] == 9
        ]
        self.assertGreaterEqual(len(targets), 10)
        self.assertGreater(targets[0], 0.0)
        self.assertLess(targets[0], 0.01)
        self.assertAlmostEqual(targets[-1], 0.5)

    def test_joint_error_during_trajectory_aborts_without_holding_faulted_arm(self) -> None:
        class FaultDuringMotionRobot(FakeH1Robot):
            def setArm_low(self, motor_id, control):
                result = super().setArm_low(motor_id, control)
                if int(motor_id) == 7 and float(control.Position) < -0.1:
                    self.states[7].Error_flag = 0x0008
                return result

        self.service.close()
        self.robot = FaultDuringMotionRobot()
        self.service = RobotService(
            sdk_loader=lambda: self.sdk,
            robot_factory=lambda _sdk: self.robot,
            lease_seconds=0.4,
            trajectory_rate_hz=250.0,
            state_rate_hz=25.0,
        )
        lease = self.acquire_and_init()

        with self.assertRaisesRegex(RobotCommandRejected, "0x0008"):
            self.service.move_joint(
                lease,
                7,
                -0.3,
                duration_s=0.04,
                smooth=True,
            )

        calls_after_abort = len(self.robot.calls)
        time.sleep(0.03)
        self.assertEqual(len(self.robot.calls), calls_after_abort)

    def test_chassis_has_short_watchdog_and_no_numeric_limit(self) -> None:
        lease = self.acquire_and_init()
        self.service.command_chassis(lease, 123.456, -987.5)
        self.assertEqual(self.robot.states[0].Speed_Actual, 123.456)
        self.assertEqual(self.robot.states[1].Speed_Actual, -987.5)
        time.sleep(0.09)
        self.assertEqual(self.robot.states[0].Speed_Actual, 0.0)
        self.assertEqual(self.robot.states[1].Speed_Actual, 0.0)

    def test_partial_chassis_failure_compensates_both_wheels_to_zero(self) -> None:
        class RightWheelFailsOnce(FakeH1Robot):
            def __init__(self) -> None:
                super().__init__()
                self.failed = False

            def setChassis_low(self, motor_id, control):
                if int(motor_id) == 1 and float(control.Speed) != 0.0 and not self.failed:
                    self.failed = True
                    self.calls.append(
                        ("setChassis_low", int(motor_id), float(control.Speed))
                    )
                    return False
                return super().setChassis_low(motor_id, control)

        self.service.close()
        self.robot = RightWheelFailsOnce()
        self.service = RobotService(
            sdk_loader=lambda: self.sdk,
            robot_factory=lambda _sdk: self.robot,
            chassis_watchdog_seconds=0.05,
            trajectory_rate_hz=250,
        )
        lease = self.acquire_and_init()
        with self.assertRaises(RobotCommandRejected):
            self.service.command_chassis(lease, 2.0, 2.0)
        self.assertEqual(self.robot.states[0].Speed_Actual, 0.0)
        self.assertEqual(self.robot.states[1].Speed_Actual, 0.0)
        chassis = self.service.state()["chassis"]
        self.assertEqual(chassis["command_left_rad_s"], 0.0)
        self.assertEqual(chassis["command_right_rad_s"], 0.0)
        self.assertFalse(chassis["stop_pending"])

    def test_failed_zero_compensation_stays_pending_until_retry_succeeds(self) -> None:
        class InitialAndCompensationFailure(FakeH1Robot):
            def __init__(self) -> None:
                super().__init__()
                self.right_nonzero_failed = False
                self.left_zero_failed = False

            def setChassis_low(self, motor_id, control):
                motor_id = int(motor_id)
                speed = float(control.Speed)
                if motor_id == 1 and speed != 0.0 and not self.right_nonzero_failed:
                    self.right_nonzero_failed = True
                    self.calls.append(("setChassis_low", motor_id, speed))
                    return False
                if (
                    motor_id == 0
                    and speed == 0.0
                    and self.right_nonzero_failed
                    and not self.left_zero_failed
                ):
                    self.left_zero_failed = True
                    self.calls.append(("setChassis_low", motor_id, speed))
                    return False
                return super().setChassis_low(motor_id, control)

        self.service.close()
        self.robot = InitialAndCompensationFailure()
        self.service = RobotService(
            sdk_loader=lambda: self.sdk,
            robot_factory=lambda _sdk: self.robot,
            chassis_watchdog_seconds=0.05,
            trajectory_rate_hz=250,
        )
        lease = self.acquire_and_init()
        with self.assertRaises(RobotCommandRejected):
            self.service.command_chassis(lease, 2.0, 2.0)

        self.assertEqual(self.robot.states[0].Speed_Actual, 2.0)
        self.assertTrue(self.service.state()["chassis"]["stop_pending"])
        deadline = time.monotonic() + 0.4
        while self.service.state()["chassis"]["stop_pending"]:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.assertEqual(self.robot.states[0].Speed_Actual, 0.0)
        self.assertEqual(self.robot.states[1].Speed_Actual, 0.0)

    def test_busy_motion_is_rejected_and_expired_queue_item_never_executes(self) -> None:
        lease = self.acquire_and_init()
        move_errors = []

        def move() -> None:
            try:
                self.service.move_joint(lease, 7, 0.8, duration_s=0.2)
            except Exception as exc:  # surfaced below with its exact value
                move_errors.append(exc)

        thread = threading.Thread(target=move)
        thread.start()
        deadline = time.monotonic() + 0.5
        while self.service.state()["active_operation"] != "joint":
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.002)

        started = time.monotonic()
        with self.assertRaises(RobotConflict):
            self.service.command_chassis(lease, 4.0, 4.0)
        self.assertLess(time.monotonic() - started, 0.1)

        # Exercise the queue-expiry guard directly: even if a future caller
        # bypassed admission, a timed-out item must be skipped by the worker.
        with self.assertRaises(RobotCallTimeout):
            self.service._call(
                "chassis",
                lease,
                4.0,
                4.0,
                timeout=0.01,
            )
        thread.join(1.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(move_errors, [])
        time.sleep(0.03)
        nonzero_chassis = [
            call
            for call in self.robot.calls
            if call[0] == "setChassis_low" and call[2] != 0.0
        ]
        self.assertEqual(nonzero_chassis, [])

    def test_stop_cancels_trajectory_and_holds_latest_feedback(self) -> None:
        lease = self.acquire_and_init()
        errors = []

        def move() -> None:
            try:
                self.service.move_joint(lease, 7, 1.0, duration_s=0.25)
            except RobotCommandRejected as exc:
                errors.append(str(exc))

        thread = threading.Thread(target=move)
        thread.start()
        time.sleep(0.04)
        self.service.stop_motion(lease)
        thread.join(1.0)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        held = self.robot.states[7].Position_Actual
        time.sleep(0.025)
        self.assertAlmostEqual(self.robot.states[7].Position_Actual, held)

    def test_charging_rejects_chassis_but_not_joint(self) -> None:
        class ChargingRobot(FakeH1Robot):
            def getPowerChargeState(self):
                ok, power = super().getPowerChargeState()
                power.status = 1
                return ok, power

        self.service.close()
        self.robot = ChargingRobot()
        self.service = RobotService(
            sdk_loader=lambda: self.sdk,
            robot_factory=lambda _sdk: self.robot,
            trajectory_rate_hz=250,
            home_duration_s=0.01,
        )
        lease = self.acquire_and_init()
        self.service.move_joint(lease, 13, 0.1, duration_s=0.005)
        with self.assertRaises(RobotCommandRejected):
            self.service.command_chassis(lease, 1.0, 1.0)


if __name__ == "__main__":
    unittest.main()
