from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from control.web_control.inference_trace import InferenceTrace
from control.web_control.fake_sdk import FakeSdk, FakeH1Robot
from control.web_control.robot_service import RobotService
from control.web_control.pi05_executor import Pi05Executor, REQUIRED_CONFIRMATION
from control.web_control.tests.test_pi05_executor import FakeCamera, FakePolicyClient


class LaggingRobot(FakeH1Robot):
    def setArm_low(self, motor_id, control):
        result = super().setArm_low(motor_id, control)
        if motor_id == 7:
            self.states[motor_id].Position_Actual -= 0.03
        return result


class InferenceTraceTests(unittest.TestCase):
    def test_waiting_for_policy_keeps_target_and_records_measured_error(self):
        with tempfile.TemporaryDirectory() as directory:
            sdk_robot = LaggingRobot()
            robot = RobotService(sdk_loader=FakeSdk, robot_factory=lambda _: sdk_robot,
                                 lease_seconds=5, home_duration_s=0.01)
            policy = FakePolicyClient()
            executor = Pi05Executor(robot, FakeCamera(), policy_factory=lambda *_: policy,
                                    trace_directory=directory)
            try:
                lease = robot.acquire()['lease_id']
                robot.initialize(lease)
                executor.reconnect('127.0.0.1', 9999)
                executor.dry_run('test', lease)
                self.assertEqual(list(Path(directory).iterdir()), [])
                policy.block_on_infer_call = 3  # dry-run, first chunk, blocked next chunk
                executor.start('test', lease, confirmation=REQUIRED_CONFIRMATION, steps_per_chunk=2)
                deadline = time.monotonic() + 2
                while policy.infer_calls < 3 and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(policy.infer_calls, 3)
                started_wait = time.monotonic()
                calls_before = len(sdk_robot.calls)
                time.sleep(.25)
                during_wait = [e for e in executor.trace.data()['events']
                               if e['kind'] == 'state' and e['monotonic'] >= started_wait]
                self.assertGreaterEqual(len(during_wait), 4)
                held = [e['held_action'][0] for e in during_wait]
                self.assertLess(max(held) - min(held), 1e-10)
                for event in during_wait:
                    self.assertAlmostEqual(event['error'][0], .03)
                    self.assertEqual(len(event['kp']), 21)
                    self.assertFalse(event['base_feedback_measured'])
                arm_refreshes = [c for c in sdk_robot.calls[calls_before:] if c[:2] == ('setArm_low', 7)]
                self.assertGreaterEqual(len(arm_refreshes), 10)
                self.assertTrue(all(abs(c[2] - held[0]) < 1e-10 for c in arm_refreshes))
                executor.stop()
                run_id = executor.trace.run_id
                folder = Path(directory) / run_id
                events = [json.loads(line) for line in (folder / 'events.jsonl').read_text().splitlines()]
                chunks = [e for e in events if e['kind'] == 'chunk']
                steps = [e for e in events if e['kind'] == 'step']
                self.assertEqual(len(chunks[0]['actions']), 50)
                self.assertEqual(len(steps), 2)
                self.assertNotEqual(steps[0]['server_action'][16], steps[0]['sent_action'][16])
                self.assertIn('request', [e['kind'] for e in events])
                self.assertEqual(events[-1]['kind'], 'end')
                summary = json.loads((folder / 'summary.json').read_text())
                self.assertGreaterEqual(summary['max_abs_error'][0], .029)
                self.assertEqual(summary['dropped_events'], 0)
                self.assertIsNone(summary['error'])
                with (folder / 'samples.csv').open() as stream:
                    rows = list(csv.DictReader(stream))
                self.assertTrue(any(r['state.left_j1'] for r in rows))
                reloaded = InferenceTrace(directory).data()
                self.assertEqual(reloaded['run_id'], run_id)
                self.assertTrue(reloaded['events'])
            finally:
                executor.close()
                robot.close()

    def test_writer_failure_visible_without_raising_on_control_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = InferenceTrace(directory)
            trace.start({})
            trace.record('bad_test_event', value=object())
            deadline = time.monotonic() + 1
            while trace.status()['error'] is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIn('TypeError', trace.status()['error'])
            trace.record('state', state=[0.] * 23)
            trace.finish('test')
            trace.thread.join(1)
            self.assertFalse(trace.active)

    def test_unwritable_directory_fails_before_starting_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'file'
            file.write_text('not a directory')
            trace = InferenceTrace(file)
            with self.assertRaises(OSError):
                trace.start({})
            self.assertFalse(trace.active)

    def test_paths_cannot_escape_trace_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = InferenceTrace(directory)
            for value in ('../secret', '/etc/passwd', '', None):
                with self.assertRaises(ValueError):
                    trace.folder(value)

    def test_hold_clock_does_not_accumulate_late_wakeup_delay_or_burst(self):
        robot = RobotService()
        robot.close()  # deterministic calls below; no SDK/thread involved
        robot._robot = object()
        robot._next_hold_tick = 100.01
        robot._next_support_tick = 200
        robot._next_state_poll = 200
        with patch.object(robot, '_lease_is_valid', return_value=True), \
             patch.object(robot, '_ready_without_lease', return_value=False), \
             patch('control.web_control.robot_service.time.monotonic', return_value=100.013):
            robot._tick()
            self.assertAlmostEqual(robot._next_hold_tick, 100.02)
        with patch.object(robot, '_lease_is_valid', return_value=True), \
             patch.object(robot, '_ready_without_lease', return_value=False), \
             patch('control.web_control.robot_service.time.monotonic', return_value=100.058):
            robot._tick()
            self.assertAlmostEqual(robot._next_hold_tick, 100.06)


if __name__ == '__main__':
    unittest.main()
