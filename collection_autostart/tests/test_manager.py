import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from manager import decide, validate, Host


def fixture():
    inventory = {n: {'original': [10 if n == 'collection' else 20], 'new': [], 'unknown': []}
                 for n in ('collection', 'teleop')}
    state = {'device': {'vr_connected': True, 'checks': [
        {'key': k, 'ok': True} for k in ('service', 'motors', 'communication', 'mode')
    ] + [{'key': 'init', 'detail': '反初始化完成'}]},
             'collection': {'phase': 'idle', 'current': None}, 'teleop': {'available': False}}
    return inventory, state


class Decisions(unittest.TestCase):
    def test_collection_before_teleop(self):
        inv, state = fixture()
        self.assertEqual(decide({}, inv, state), ('switch', 'collection'))
        inv['collection'] = {'original': [], 'new': [30], 'unknown': []}
        self.assertEqual(decide({}, inv, state), ('switch', 'teleop'))

    def test_no_switch_when_initialized(self):
        inv, state = fixture()
        state['device']['checks'][-1]['detail'] = '初始化完成'
        self.assertEqual(decide({}, inv, state)[0], 'wait')

    def test_no_switch_during_recording_or_saving_or_waiting(self):
        inv, state = fixture()
        for phase, current in [('recording', None), ('waiting', None),
                               ('idle', {'state': 'saving'}), ('closed', {'state': 'recording'})]:
            with self.subTest(phase=phase, current=current):
                state['collection'] = {'phase': phase, 'current': current}
                self.assertEqual(decide({}, inv, state)[0], 'wait')

    def test_unknown_status_does_not_switch(self):
        inv, _ = fixture()
        self.assertEqual(decide({}, inv, {})[0], 'wait')

    def test_vr_only_gates_teleop(self):
        inv, state = fixture()
        state['device']['vr_connected'] = False
        self.assertEqual(decide({}, inv, state), ('switch', 'collection'))
        inv['collection'] = {'original': [], 'new': [30], 'unknown': []}
        self.assertEqual(decide({}, inv, state)[0], 'wait')

    def test_ready_requires_both_hash_classified_processes_and_live_pid(self):
        inv, state = fixture()
        for n, pid in [('collection', 30), ('teleop', 40)]:
            inv[n] = {'original': [], 'new': [pid], 'unknown': []}
        state['teleop'] = {'available': True, 'pid': 999}
        self.assertEqual(decide({}, inv, state)[0], 'wait')
        state['teleop']['pid'] = 40
        self.assertEqual(decide({}, inv, state)[0], 'ready')
        state['teleop']['available'] = False
        self.assertEqual(decide({}, inv, state)[0], 'wait')

    def test_already_ready_while_recording_is_not_interrupted(self):
        inv, state = fixture()
        for n, pid in [('collection', 30), ('teleop', 40)]:
            inv[n] = {'original': [], 'new': [pid], 'unknown': []}
        state['teleop'] = {'available': True, 'pid': 40}
        state['collection'] = {'phase': 'recording', 'current': {'state': 'recording'}}
        state['device']['checks'][-1]['detail'] = '初始化完成'
        self.assertEqual(decide({}, inv, state)[0], 'ready')

    def test_unknown_or_concurrent_versions_are_not_replaced(self):
        inv, state = fixture()
        for n in inv:
            for field in ('unknown', 'new'):
                test = copy.deepcopy(inv)
                test[n][field] = [999]
                self.assertEqual(decide({}, test, state)[0], 'wait')

    def test_unhealthy_new_process_does_not_spawn_duplicate(self):
        inv, state = fixture()
        inv['collection'] = {'original': [], 'new': [30], 'unknown': []}
        state['device']['checks'][0]['ok'] = False
        self.assertEqual(decide({}, inv, state)[0], 'wait')

    def test_motor_communication_mode_faults_wait_before_teleop(self):
        inv, state = fixture()
        inv['collection'] = {'original': [], 'new': [30], 'unknown': []}
        for key in ('motors', 'communication', 'mode'):
            test = copy.deepcopy(state)
            next(x for x in test['device']['checks'] if x['key'] == key)['ok'] = False
            self.assertEqual(decide({}, inv, test)[0], 'wait')

    def test_retry_does_not_expire_after_120_seconds(self):
        inv, state = fixture()
        state['collection']['phase'] = 'waiting'
        for _ in range(100):
            self.assertEqual(decide({}, inv, state)[0], 'wait')
        state['collection']['phase'] = 'idle'
        self.assertEqual(decide({}, inv, state), ('switch', 'collection'))


class Validation(unittest.TestCase):
    def test_exact_validated_binary_and_pinned_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / 'binary'
            binary.write_bytes(b'version1')
            sha = hashlib.sha256(b'version1').hexdigest()
            marker = {'passed': True, 'sha256': sha}
            (root / 'validated.json').write_text(json.dumps(marker))
            (root / 'build.json').write_text(json.dumps({'sha256': sha, 'original_sha256': 'original'}))
            cfg = {'binary': str(binary), 'sha256': sha, 'original_sha256': 'original'}
            validate(cfg)
            binary.write_bytes(b'version2')
            with self.assertRaises(RuntimeError):
                validate(cfg)
            binary.write_bytes(b'version1')
            marker['passed'] = False
            (root / 'validated.json').write_text(json.dumps(marker))
            with self.assertRaises(RuntimeError):
                validate(cfg)

    def test_switch_uses_existing_final_gates_and_recovery_flag_only_when_stopped(self):
        config = {'teleop': {'switch': '/example/switch_trial.py'}}
        host = Host(config)
        with patch('manager.validate') as validation, patch('manager.subprocess.run') as run:
            run.return_value.returncode = 0
            host.switch('teleop', {'teleop': {'original': [1]}})
            self.assertEqual(run.call_args.args[0][-2:], ['start', '--operator-ready'])
            host.switch('teleop', {'teleop': {'original': []}})
            self.assertEqual(run.call_args.args[0][-1], '--recover-stopped')
            self.assertEqual(validation.call_count, 2)


if __name__ == '__main__':
    unittest.main()
