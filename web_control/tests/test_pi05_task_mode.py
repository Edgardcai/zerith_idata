"""Optional task-mode wire format; legacy requests remain byte-schema compatible."""
import json
import unittest
from unittest import mock

import numpy as np
from control.web_control import pi05_protocol as protocol
from control.web_control.pi05_executor import Pi05Executor, REQUIRED_CONFIRMATION
from control.web_control.tests.test_pi05_executor import FakeRobot, FakeCamera, FakePolicyClient
from control.web_control.tests.test_pi05_protocol import _images, _FakeWebSocket, _action_response


class TaskModeProtocolTests(unittest.TestCase):
    def test_optional_fields_and_validation(self):
        for mode in (None, 'lefthand', 'righthand', 'twohands'):
            request = protocol.build_observation_request(np.zeros(23), _images(), 'task', task_mode=mode)
            if mode is None:
                self.assertEqual(set(request), {'type', 'prompt', 'observation'})
            else:
                self.assertEqual(request['task_mode'], mode)
        for mode in ('single', '', [], 1):
            with self.assertRaises(protocol.ProtocolValidationError):
                protocol.build_observation_request(np.zeros(23), _images(), 'task', task_mode=mode)

    def test_phase_entry_action_at_top_level_and_validated(self):
        entry = np.linspace(-0.25, 0.5, 23)
        request = protocol.build_observation_request(
            np.zeros(23), _images(), 'task', task_mode='lefthand', phase_entry_action=entry)
        self.assertEqual(request['phase_entry_action'], entry.tolist())
        self.assertNotIn('phase_entry_action', request['observation'])
        for invalid in ([0] * 22, [[0] * 23], [float('nan')] * 23, [float('inf')] * 23, ['bad'] * 23):
            with self.assertRaises(protocol.ProtocolValidationError):
                protocol.build_observation_request(np.zeros(23), _images(), 'task', phase_entry_action=invalid)

    def test_client_transmits_optional_mode(self):
        ws = _FakeWebSocket([json.dumps(_action_response())])
        with mock.patch.object(protocol, '_websocket_connect', return_value=ws):
            client = protocol.ZerithJsonPolicyClient('localhost')
            try:
                client.infer(np.zeros(23), _images(), 'task', task_mode='lefthand', phase_entry_action=[0.25] * 23)
                self.assertEqual(json.loads(ws.sent[0])['phase_entry_action'], [0.25] * 23)
                self.assertEqual(json.loads(ws.sent[0])['task_mode'], 'lefthand')
            finally:
                client.close()


class TaskModeExecutorTests(unittest.TestCase):
    def test_dry_run_modes_and_legacy(self):
        for fmt in ('legacy', 'task_mode'):
            for mode, hand, expected in (
                ('single', 'left', ['lefthand']),
                ('single', 'right', ['righthand']),
                ('dual_continuous', None, ['twohands']),
                ('dual_separate', None, ['lefthand', 'righthand']),
            ):
                with self.subTest(fmt=fmt, mode=mode, hand=hand):
                    policy = FakePolicyClient()
                    sent = []
                    original = policy.infer
                    def infer(*args, **kwargs):
                        sent.append(kwargs.pop('task_mode', None))
                        return original(*args, **kwargs)
                    policy.infer = infer
                    executor = Pi05Executor(FakeRobot(), FakeCamera(), policy_factory=lambda *_: policy)
                    try:
                        executor.reconnect('localhost', 9977, request_format=fmt)
                        executor.dry_run('right task' if hand == 'right' else 'left task', 'lease', inference_mode=mode, active_hand=hand,
                                         right_prompt='right task' if mode == 'dual_separate' else None)
                        self.assertEqual(sent, expected if fmt == 'task_mode' else [None] * len(expected))
                        # Changing format must invalidate the validated dry-run.
                        executor._policy_factory = lambda *_: FakePolicyClient()
                        executor.reconnect('localhost', 9977, request_format='legacy')
                        self.assertFalse(executor.status()['dry_run_ok'])
                    finally:
                        executor.close()

    def test_execution_chunks_keep_mode(self):
        import time
        policy = FakePolicyClient()
        sent = []
        original = policy.infer
        def infer(*args, **kwargs):
            sent.append(kwargs.pop('task_mode', None))
            return original(*args, **kwargs)
        policy.infer = infer
        executor = Pi05Executor(FakeRobot(), FakeCamera(), policy_factory=lambda *_: policy)
        try:
            executor.reconnect('localhost', 9977, request_format='task_mode')
            executor.dry_run('left task', 'lease', inference_mode='single', active_hand='left')
            executor.start('left task', 'lease', inference_mode='single', active_hand='left',
                           confirmation=REQUIRED_CONFIRMATION, steps_per_chunk=1, control_rate_hz=50)
            deadline = time.monotonic() + 3
            while len(sent) < 3 and time.monotonic() < deadline:
                time.sleep(.01)
            executor.stop()
            self.assertGreaterEqual(len(sent), 3)
            self.assertEqual(set(sent), {'lefthand'})
        finally:
            executor.close()

class A90ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.policy = FakePolicyClient()
        self.sent = []
        original = self.policy.infer
        def infer(*args, **kwargs):
            self.sent.append((kwargs.pop('task_mode', None), kwargs.pop('phase_entry_action', None)))
            return original(*args, **kwargs)
        self.policy.infer = infer
        self.executor = Pi05Executor(FakeRobot(), FakeCamera(), policy_factory=lambda *_: self.policy)
        self.executor.reconnect('localhost', 9977, request_format='a90')

    def tearDown(self):
        self.executor.close()

    def test_rejects_right_stages_before_inference(self):
        from control.web_control.pi05_executor import Pi05SafetyError
        for args in (
            dict(inference_mode='single', active_hand='right', phase_entry_action=[0] * 23),
            dict(inference_mode='dual_separate', right_prompt='right task', phase_entry_action=[0] * 23),
        ):
            prompt = 'right task' if args.get('active_hand') == 'right' else 'left task'
            with self.subTest(args=args), self.assertRaises(Pi05SafetyError):
                self.executor.dry_run(prompt, 'lease', **args)
        self.assertEqual(self.sent, [])

    def test_copies_entry_and_sends_every_execution_chunk(self):
        import time
        from control.web_control.pi05_executor import Pi05SafetyError
        entry = [0.25] * 23
        args = dict(inference_mode='single', active_hand='left', phase_entry_action=entry)
        self.executor.dry_run('left task', 'lease', **args)
        entry[0] = 0.75
        with self.assertRaises(Pi05SafetyError):
            self.executor.start('left task', 'lease', confirmation=REQUIRED_CONFIRMATION, **args)
        self.assertEqual(self.executor.status()['phase_entry_action'], [0.25] * 23)
        entry[0] = 0.25
        self.executor.start('left task', 'lease', confirmation=REQUIRED_CONFIRMATION,
                            steps_per_chunk=1, control_rate_hz=50, **args)
        entry[0] = 0.9
        deadline = time.monotonic() + 3
        while len(self.sent) < 3 and time.monotonic() < deadline:
            time.sleep(.01)
        self.executor.stop()
        self.assertGreaterEqual(len(self.sent), 3)
        self.assertTrue(all(item == ('lefthand', [0.25] * 23) for item in self.sent))

    def test_twohands_and_legacy_opt_out(self):
        self.executor.dry_run('left then right task', 'lease', inference_mode='dual_continuous',
                              phase_entry_action=[0.1] * 23)
        self.assertEqual(self.sent, [('twohands', [0.1] * 23)])
        self.executor._policy_factory = lambda *_: FakePolicyClient()
        self.executor.reconnect('localhost', 9977)
        self.assertEqual(self.executor.status()['request_format'], 'legacy')
        self.assertIsNone(self.executor.status()['phase_entry_action'])
        self.assertFalse(self.executor.status()['dry_run_ok'])

    def test_auto_entry_refreshes_at_start_and_stays_fixed_across_chunks(self):
        import time
        robot = self.executor.robot
        robot.state[:] = 0.12
        self.executor.dry_run('left task', 'lease', inference_mode='single', active_hand='left')
        self.assertTrue(np.allclose(self.sent[0][1], [0.12] * 23))
        self.assertIsNone(self.executor.status()['phase_entry_action'])
        robot.state[:] = 0.24
        self.executor.start('left task', 'lease', confirmation=REQUIRED_CONFIRMATION,
                            inference_mode='single', active_hand='left',
                            steps_per_chunk=1, control_rate_hz=50)
        robot.state[:] = 0.36
        deadline = time.monotonic() + 3
        while len(self.sent) < 3 and time.monotonic() < deadline:
            time.sleep(.01)
        self.executor.stop()
        self.assertGreaterEqual(len(self.sent), 3)
        for mode, entry in self.sent[1:]:
            self.assertEqual(mode, 'lefthand')
            self.assertTrue(np.allclose(entry, [0.24] * 23))
        # A new dry-run must not reuse the prior task's reference.
        robot.state[:] = 0.48
        replacement = FakePolicyClient()
        original = replacement.infer
        def infer(*args, **kwargs):
            self.sent.append((kwargs.pop("task_mode", None), kwargs.pop("phase_entry_action", None)))
            return original(*args, **kwargs)
        replacement.infer = infer
        self.executor._policy_factory = lambda *_: replacement
        self.executor.reconnect("localhost", 9977, request_format="a90")
        self.executor.dry_run('left task', 'lease', inference_mode='single', active_hand='left')
        self.assertTrue(np.allclose(self.sent[-1][1], [0.48] * 23))
