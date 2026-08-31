from __future__ import annotations

import time
import unittest

from control.voice_assistant.robot_control import (
    RobotCommandIntent,
    RobotControlError,
    WebRobotControl,
)
from control.web_control.fake_sdk import FakeH1Robot, FakeSdk
from control.web_control.robot_service import RobotConflict, RobotService
from control.web_control.voice_motion import (
    CHASSIS_PRESETS,
    VoiceMotionController,
    VoiceMotionInternalServer,
)
import threading


class VoiceMotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sdk = FakeSdk()
        self.robot = FakeH1Robot()
        self.service = RobotService(
            sdk_loader=lambda: self.sdk,
            robot_factory=lambda _sdk: self.robot,
            lease_seconds=2.0,
            chassis_watchdog_seconds=0.05,
            trajectory_rate_hz=250.0,
            state_rate_hz=50.0,
        )
        self.controller = VoiceMotionController(
            self.service,
            chassis_refresh_s=0.015,
            wave_setup_segment_s=0.006,
            wave_segment_s=0.006,
            wave_endpoint_pause_s=0.002,
            wave_neutral_s=0.008,
            handshake_segment_s=0.006,
            handshake_hold_s=0.002,
        )

    def tearDown(self) -> None:
        self.controller.close()
        self.service.close()

    def ready_lease(self) -> str:
        lease = self.service.acquire("voice-test")["lease_id"]
        self.service.initialize(lease)
        return lease

    def wait_idle(self, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while self.controller.status()["active_action"]:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.005)

    def test_default_off_and_enable_requires_initialized_live_lease(self) -> None:
        self.assertFalse(self.controller.status()["enabled"])
        with self.assertRaises(RobotConflict):
            self.controller.execute("forward")
        lease = self.service.acquire("voice-test")["lease_id"]
        with self.assertRaises(RobotConflict):
            self.controller.set_enabled(lease, True)
        self.service.initialize(lease)
        self.assertTrue(self.controller.set_enabled(lease, True)["enabled"])

    def test_forward_refreshes_watchdog_then_stops_at_bounded_duration(self) -> None:
        lease = self.ready_lease()
        self.controller.set_enabled(lease, True)
        result = self.controller.execute("forward")
        self.assertTrue(result["accepted"])
        self.assertEqual(self.robot.states[0].Speed_Actual, 1.5)
        time.sleep(0.12)
        self.assertEqual(self.robot.states[0].Speed_Actual, 1.5)
        self.wait_idle()
        self.assertEqual(self.robot.states[0].Speed_Actual, 0.0)
        self.assertEqual(self.robot.states[1].Speed_Actual, 0.0)

    def test_chassis_presets_match_voice_contract(self) -> None:
        expected = {
            "forward": (1.5, 1.5, 1.0),
            "backward": (-1.5, -1.5, 1.0),
            "turn_left": (-1.5, 1.5, 3.5),
            "turn_right": (1.5, -1.5, 3.0),
            "turn_around": (1.5, -1.5, 8.0),
        }
        actual = {
            name: (preset.left_speed, preset.right_speed, preset.duration_s)
            for name, preset in CHASSIS_PRESETS.items()
        }
        self.assertEqual(actual, expected)

    def test_wave_runs_left_then_right_five_times_and_returns_arms_to_zero(self) -> None:
        lease = self.ready_lease()
        self.robot.states[8].Position_Actual = 0.2
        self.robot.states[16].Position_Actual = -0.2
        self.controller.set_enabled(lease, True)
        call_start = len(self.robot.calls)
        self.controller.execute("wave")
        self.wait_idle()
        gesture_calls = self.robot.calls[call_start:]

        for motor_id in (*range(7, 14), *range(15, 22)):
            self.assertAlmostEqual(self.robot.states[motor_id].Position_Actual, 0.0)

        def arm_targets(motor_id):
            return [
                call[2]
                for call in gesture_calls
                if call[0] == "setArm_low" and call[1] == motor_id
            ]

        left_yaw = arm_targets(9)
        right_yaw = arm_targets(17)
        self.assertGreaterEqual(sum(abs(value - 0.4) < 1e-6 for value in left_yaw), 5)
        self.assertGreaterEqual(sum(abs(value + 0.4) < 1e-6 for value in left_yaw), 5)
        self.assertGreaterEqual(sum(abs(value - 0.4) < 1e-6 for value in right_yaw), 5)
        self.assertGreaterEqual(sum(abs(value + 0.4) < 1e-6 for value in right_yaw), 5)
        self.assertTrue(any(abs(value + 0.3) < 1e-6 for value in arm_targets(7)))
        self.assertTrue(any(abs(value + 0.9) < 1e-6 for value in arm_targets(10)))
        self.assertTrue(any(abs(value + 0.3) < 1e-6 for value in arm_targets(15)))
        self.assertTrue(any(abs(value + 0.9) < 1e-6 for value in arm_targets(18)))

        left_first = next(
            index
            for index, call in enumerate(gesture_calls)
            if call[0] == "setArm_low" and call[1] == 9 and abs(call[2]) > 1e-6
        )
        right_first = next(
            index
            for index, call in enumerate(gesture_calls)
            if call[0] == "setArm_low" and call[1] == 17 and abs(call[2]) > 1e-6
        )
        self.assertLess(left_first, right_first)
        self.assertFalse(
            any(call[0] in {"setWaist_low", "setHead_low", "setChassis_low"} for call in gesture_calls)
        )

    def test_wave_reasserts_fixed_pitch_instead_of_accumulating_feedback_error(self) -> None:
        class LaggyArmRobot(FakeH1Robot):
            def setArm_low(self, motor_id, control):
                motor_id = int(motor_id)
                target = float(control.Position)
                with self._lock:
                    self.calls.append(("setArm_low", motor_id, target))
                    state = self.states[motor_id]
                    state.Position_Actual += 0.1 * (target - state.Position_Actual)
                    state.Speed_Actual = float(control.Speed)
                    state.Torque_Actual = float(control.Torque)
                    state.KP_Actual = float(control.KP)
                    state.KD_Actual = float(control.KD)
                return True

        self.controller.close()
        self.service.close()
        self.robot = LaggyArmRobot()
        self.service = RobotService(
            sdk_loader=lambda: self.sdk,
            robot_factory=lambda _sdk: self.robot,
            lease_seconds=2.0,
            trajectory_rate_hz=100.0,
            state_rate_hz=50.0,
        )
        self.controller = VoiceMotionController(
            self.service,
            wave_cycles=2,
            wave_setup_segment_s=0.01,
            wave_segment_s=0.01,
            wave_endpoint_pause_s=0.0,
            wave_neutral_s=0.01,
            handshake_segment_s=0.01,
            handshake_hold_s=0.0,
        )
        lease = self.ready_lease()
        self.controller.set_enabled(lease, True)
        call_start = len(self.robot.calls)

        self.controller.execute("wave")
        self.wait_idle()
        gesture_calls = self.robot.calls[call_start:]
        left_pitch_targets = [
            call[2]
            for call in gesture_calls
            if call[0] == "setArm_low" and call[1] == 7
        ]

        # Setup has three poses and two cycles have four yaw endpoints.  Each
        # must explicitly keep shoulder pitch at -0.3 instead of adopting the
        # lagging Position_Actual as the next target.
        exact_pitch_holds = sum(
            abs(target + 0.3) < 1e-9 for target in left_pitch_targets
        )
        self.assertGreaterEqual(exact_pitch_holds, 7)

    def test_handshake_uses_right_arm_sequence_then_returns_to_neutral(self) -> None:
        lease = self.ready_lease()
        self.robot.states[8].Position_Actual = 0.2
        self.robot.states[14].Position_Actual = 0.12
        self.robot.states[16].Position_Actual = -0.2
        self.robot.states[22].Position_Actual = 0.13
        self.controller.set_enabled(lease, True)
        call_start = len(self.robot.calls)

        result = self.controller.execute("handshake")
        self.assertEqual(result["message"], "正在握手")
        self.wait_idle()
        gesture_calls = self.robot.calls[call_start:]

        for motor_id in range(7, 14):
            self.assertAlmostEqual(self.robot.states[motor_id].Position_Actual, 0.0)
        for motor_id in range(15, 22):
            self.assertAlmostEqual(self.robot.states[motor_id].Position_Actual, 0.0)
        self.assertAlmostEqual(self.robot.states[14].Position_Actual, 0.12)
        self.assertAlmostEqual(self.robot.states[22].Position_Actual, 0.13)

        def endpoint_index(motor_id, target):
            return next(
                index
                for index, call in enumerate(gesture_calls)
                if call[0] == "setArm_low"
                and call[1] == motor_id
                and abs(call[2] - target) < 1e-6
            )

        shoulder_first = endpoint_index(15, -0.4)
        elbow = endpoint_index(18, 0.6)
        shoulder_final = endpoint_index(15, -0.85)
        self.assertLess(shoulder_first, elbow)
        self.assertLess(elbow, shoulder_final)
        self.assertFalse(
            any(call[0] in {"setWaist_low", "setHead_low", "setChassis_low"} for call in gesture_calls)
        )

    def test_handshake_fault_during_hold_drops_right_arm_refreshes(self) -> None:
        class FaultDuringHoldRobot(FakeH1Robot):
            def __init__(self):
                super().__init__()
                self._fault_timer = None

            def setArm_low(self, motor_id, control):
                result = super().setArm_low(motor_id, control)
                if (
                    int(motor_id) == 15
                    and abs(float(control.Position) + 0.85) < 1e-9
                    and self._fault_timer is None
                ):
                    self._fault_timer = threading.Timer(0.03, self._set_fault)
                    self._fault_timer.start()
                return result

            def _set_fault(self):
                with self._lock:
                    self.states[15].Error_flag = 0x0008

        self.controller.close()
        self.service.close()
        self.robot = FaultDuringHoldRobot()
        self.service = RobotService(
            sdk_loader=lambda: self.sdk,
            robot_factory=lambda _sdk: self.robot,
            lease_seconds=2.0,
            trajectory_rate_hz=250.0,
            state_rate_hz=100.0,
        )
        self.controller = VoiceMotionController(
            self.service,
            wave_neutral_s=0.008,
            handshake_segment_s=0.006,
            handshake_hold_s=0.2,
        )
        lease = self.ready_lease()
        self.controller.set_enabled(lease, True)

        self.controller.execute("handshake")
        self.wait_idle()

        self.assertIn("0x0008", self.controller.status()["last_error"])
        right_calls = sum(
            call[0] == "setArm_low" and 15 <= call[1] <= 21
            for call in self.robot.calls
        )
        time.sleep(0.04)
        self.assertEqual(
            right_calls,
            sum(
                call[0] == "setArm_low" and 15 <= call[1] <= 21
                for call in self.robot.calls
            ),
        )

    def test_action_refresh_does_not_keep_browser_lease_alive(self) -> None:
        self.controller.close()
        self.service.close()
        self.robot = FakeH1Robot()
        self.service = RobotService(
            sdk_loader=lambda: self.sdk,
            robot_factory=lambda _sdk: self.robot,
            lease_seconds=0.18,
            chassis_watchdog_seconds=0.05,
            trajectory_rate_hz=250.0,
            state_rate_hz=50.0,
        )
        self.controller = VoiceMotionController(
            self.service,
            chassis_refresh_s=0.015,
        )
        lease = self.ready_lease()
        self.controller.set_enabled(lease, True)
        self.controller.execute("forward")
        time.sleep(0.32)
        self.assertFalse(self.controller.status()["enabled"])
        self.assertEqual(self.robot.states[0].Speed_Actual, 0.0)
        self.assertEqual(self.robot.states[1].Speed_Actual, 0.0)

    def test_internal_loopback_adapter_cannot_execute_until_opted_in(self) -> None:
        server = VoiceMotionInternalServer(("127.0.0.1", 0), self.controller)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = WebRobotControl("127.0.0.1", server.server_port)
        try:
            self.assertFalse(client.available)
            with self.assertRaises(RobotControlError):
                client.execute(RobotCommandIntent("turn_left", {}))
            lease = self.ready_lease()
            self.controller.set_enabled(lease, True)
            self.assertTrue(client.available)
            result = client.execute(RobotCommandIntent("turn_left", {}))
            self.assertTrue(result["accepted"])
            self.controller.execute("stop")
            self.wait_idle()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(1.0)


if __name__ == "__main__":
    unittest.main()
