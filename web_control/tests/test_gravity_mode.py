from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest
from types import SimpleNamespace

from control.web_control.control_mode import CONTROL_MODE_ID, CONTROL_MODE_NAME, sdk_control_mode
from control.web_control.fake_sdk import FakeH1Robot, FakeSdk
from control.web_control.robot_service import (
    POLICY_WIRE_MOTOR_IDS, RobotCommandRejected, RobotConflict, RobotService, RobotUnavailable, RobotServiceError,
)
from control.web_control.voice_motion import VoiceMotionController


class AuditedRobot(FakeH1Robot):
    """Reject writes outside mode 3 and record commands, including gains."""

    def __init__(self):
        super().__init__()
        self.writes = []
        self.flip_after_motor = None

    def audit(self, kind, motor, command):
        if self.mode != CONTROL_MODE_ID or self.init_state != 2:
            raise AssertionError("SDK write outside initialized gravity mode")
        self.writes.append((threading.get_ident(), kind, int(motor), command.Position,
                            command.Speed, command.Torque, command.KP, command.KD))

    def _set_position(self, kind, motor, command):
        self.audit(kind, motor, command)
        result = super()._set_position(kind, motor, command)
        if motor == self.flip_after_motor:
            self.mode = 0
        return result

    def setChassis_low(self, motor, command):
        self.audit("setChassis_low", motor, command)
        result = super().setChassis_low(motor, command)
        if motor == self.flip_after_motor:
            self.mode = 0
        return result


class GravityModeTests(unittest.TestCase):
    def setUp(self):
        self.robot = AuditedRobot()
        self.service = RobotService(sdk_loader=FakeSdk, robot_factory=lambda _: self.robot,
                                    lease_seconds=10, state_rate_hz=100)
        self.lease = self.service.acquire("gravity-test")["lease_id"]
        self.service.initialize(self.lease)

    def tearDown(self):
        self.service.close()

    def action(self, target):
        action = [self.robot.states[i].Position_Actual for i in POLICY_WIRE_MOTOR_IDS] + [0., 0.]
        action[0] = target
        return action

    def wait_until(self, predicate):
        deadline = time.monotonic() + 1
        while not predicate():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.005)

    def test_config_and_sdk_feedback_use_same_mode(self):
        config = self.service.config()
        self.assertEqual(config["control_mode_id"], CONTROL_MODE_ID)
        self.assertEqual(config["control_mode"], CONTROL_MODE_NAME)
        self.assertEqual(self.service.state()["control_mode"], CONTROL_MODE_ID)
        self.assertEqual(self.service.state()["control_mode_name"], CONTROL_MODE_NAME)

    def test_lifecycle_switches_only_before_initialization(self):
        self.service.initialize(self.lease)
        self.service.deinitialize(self.lease)
        self.service.initialize(self.lease)
        lifecycle = [c for c in self.robot.calls if c[0] in ("switchControlMode", "robot_init", "robot_deinit")]
        self.assertEqual(lifecycle, [("switchControlMode", 3), ("robot_init",),
                                    ("robot_deinit",), ("switchControlMode", 3), ("robot_init",)])

    def test_blocking_lifecycle_preserves_owner_and_resumes_normal_lease(self):
        self.service.deinitialize(self.lease)
        self.service._lease_seconds = .15
        for method, public in (("robot_init", self.service.initialize),
                               ("robot_deinit", self.service.deinitialize)):
            with self.subTest(method=method):
                self.service.heartbeat(self.lease)
                original = getattr(self.robot, method)
                entered = threading.Event()
                def slow():
                    entered.set()
                    time.sleep(.3)
                    return original()
                setattr(self.robot, method, slow)
                with ThreadPoolExecutor(max_workers=3) as pool:
                    pending = pool.submit(public, self.lease)
                    self.assertTrue(entered.wait(1))
                    time.sleep(.18)  # the original lease has expired inside SDK
                    heartbeat = pool.submit(self.service.heartbeat, self.lease)
                    takeover = pool.submit(self.service.acquire, "competing-client")
                    self.assertTrue(pending.result(timeout=2)["ok"])
                    self.assertTrue(heartbeat.result(timeout=2)["ok"])
                    with self.assertRaises(RobotConflict):
                        takeover.result(timeout=2)
                self.assertTrue(self.service.has_live_lease(self.lease))
                setattr(self.robot, method, original)
        time.sleep(.2)
        self.assertFalse(self.service.has_live_lease(self.lease))
        with self.assertRaises(RobotConflict):
            self.service.heartbeat(self.lease)

    def test_failed_blocking_lifecycle_allows_owner_to_receive_error_and_retry(self):
        self.service._lease_seconds = .15
        self.service.heartbeat(self.lease)
        def fail():
            time.sleep(.3)
            raise RuntimeError("vendor deinit failure")
        self.robot.robot_deinit = fail
        with self.assertRaisesRegex(RobotServiceError, "vendor deinit failure"):
            self.service.deinitialize(self.lease)
        self.assertTrue(self.service.heartbeat(self.lease)["ok"])

    def test_lifecycle_grace_cannot_revive_an_expired_lease(self):
        self.service._lease_deadline = time.monotonic() - 1
        calls = []
        with self.assertRaises(RobotConflict):
            self.service._sdk_lifecycle_call(self.lease, lambda: calls.append(True))
        self.assertEqual(calls, [])
        self.assertFalse(self.service.has_live_lease(self.lease))

    def test_initialized_foreign_modes_are_not_taken_over(self):
        for mode in (0, 1, 2):
            with self.subTest(mode=mode):
                self.robot.mode = mode
                count = len(self.robot.writes)
                with self.assertRaises(RobotConflict):
                    self.service.initialize(self.lease)
                with self.assertRaises(RobotConflict):
                    self.service.validate_motion_ready(self.lease)
                with self.assertRaises(RobotConflict):
                    self.service.deinitialize(self.lease)
                self.assertEqual(count, len(self.robot.writes))
        self.robot.mode = CONTROL_MODE_ID

    def test_actions_and_holds_keep_latest_command_on_one_sdk_thread(self):
        session = self.service.begin_policy_session(self.lease)["session_id"]
        for target in (.1, .2, .3):
            self.service.policy_step(self.lease, self.action(target), session_id=session)
            self.wait_until(lambda: sum(w[2] == 7 and w[3] == target for w in self.robot.writes) >= 3)
        positions = [w[3] for w in self.robot.writes if w[1] == "setArm_low" and w[2] == 7]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(len({w[0] for w in self.robot.writes}), 1)
        for write in self.robot.writes:
            self.assertEqual(write[5:], (0., -1., -1.))
        self.service.end_policy_session(self.lease, session_id=session)
        self.assertEqual(self.robot.mode, CONTROL_MODE_ID)

    def test_policy_excludes_other_sources_and_rejects_late_action(self):
        voice = VoiceMotionController(self.service)
        voice.set_enabled(self.lease, True)
        session = self.service.begin_policy_session(self.lease)["session_id"]
        with self.assertRaises(RobotConflict):
            self.service.command_chassis(self.lease, .1, .1)
        with self.assertRaises(RobotConflict):
            voice.execute("forward")
        with self.assertRaises(RobotConflict):
            self.service.replay_trajectory(self.lease, [self.action(.5)], mode="arms",
                                           rate_hz=30, alignment_duration_s=0)
        self.service.end_policy_session(self.lease, session_id=session)
        with self.assertRaises(RobotConflict):
            self.service.policy_step(self.lease, self.action(.5), session_id=session)
        self.assertFalse(any(w[2] == 7 and w[3] == .5 for w in self.robot.writes))

    def test_voice_motion_is_ready_in_gravity_mode(self):
        voice = VoiceMotionController(self.service)
        voice.set_enabled(self.lease, True)
        self.assertTrue(voice.status()["ready"])
        voice.disable(reason="test_complete")

    def test_mode_changes_mid_action_block_remaining_writes_and_stop_holds(self):
        session = self.service.begin_policy_session(self.lease)["session_id"]
        self.robot.writes.clear()
        self.robot.flip_after_motor = 7
        with self.assertRaises(RobotConflict):
            self.service.policy_step(self.lease, self.action(.2), session_id=session)
        self.assertEqual([(w[1], w[2]) for w in self.robot.writes], [("setArm_low", 7)])
        self.assertFalse(self.service.state()["policy_session"]["active"])
        self.assertFalse(self.service._hold_targets)
        self.assertFalse(self.service._support_targets)
        count = len(self.robot.writes)
        self.service.emergency_stop_motion()
        self.assertEqual(len(self.robot.writes), count)

    def test_mode_changes_between_wheels_do_not_write_into_new_mode(self):
        self.robot.flip_after_motor = 0
        with self.assertRaises(RobotCommandRejected):
            self.service.command_chassis(self.lease, .1, .1)
        self.assertEqual([(w[1], w[2]) for w in self.robot.writes], [("setChassis_low", 0)])

    def test_lost_mode_discards_holds_before_reentering_gravity_mode(self):
        session = self.service.begin_policy_session(self.lease)["session_id"]
        self.service.policy_step(self.lease, self.action(.3), session_id=session)
        self.service.end_policy_session(self.lease, session_id=session)
        self.robot.mode = 0
        self.wait_until(lambda: not self.service._hold_targets and not self.service._support_targets)
        count = len(self.robot.writes)
        self.robot.mode = CONTROL_MODE_ID
        time.sleep(.04)
        self.assertEqual(len(self.robot.writes), count)


class UnsupportedSdkTests(unittest.TestCase):
    def test_missing_or_wrong_enum_does_not_fall_back(self):
        for modes in [SimpleNamespace(LOW_LEVEL=1), SimpleNamespace(GRAVITY_COMPENSATION_LEVEL=1)]:
            with self.assertRaises(RuntimeError):
                sdk_control_mode(SimpleNamespace(MotorControlMode=modes))

    def test_unsupported_sdk_fails_before_robot_construction(self):
        factories = []
        service = RobotService(
            sdk_loader=lambda: SimpleNamespace(MotorControlMode=SimpleNamespace(LOW_LEVEL=1)),
            robot_factory=lambda sdk: factories.append(sdk),
        )
        try:
            with self.assertRaises(RobotUnavailable):
                service.acquire("unsupported-sdk")
            self.assertEqual(factories, [])
        finally:
            service.close()
