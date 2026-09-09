import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import switch_trial


class StartupTests(unittest.TestCase):
    def test_live_status_confirms_matching_process(self):
        started = time.time() - 1
        state = {'pid': 123, 'timestamp': time.time(), 'version': 'collection-1.0'}
        with patch.object(switch_trial, 'trial_pids', return_value=[123]), \
                patch.object(Path, 'read_text', return_value=json.dumps(state)):
            self.assertEqual(switch_trial.wait_started(started, timeout=1), 123)

    def test_exited_process_with_old_status_reports_pane_error(self):
        state = {'pid': 123, 'timestamp': time.time() - 100, 'version': 'collection-1.0'}
        with patch.object(switch_trial, 'trial_pids', return_value=[]), \
                patch.object(Path, 'read_text', return_value=json.dumps(state)), \
                patch.object(switch_trial, 'tmux', return_value='startup rejected'):
            with self.assertRaisesRegex(RuntimeError, 'startup rejected'):
                switch_trial.wait_started(time.time() - 10, timeout=1)

    def test_embedded_live_gate_honors_pending_save_flag(self):
        source = (Path(__file__).resolve().parents[1] / 'tools/bootstrap.py').read_text()
        gate = source.split("os.environ['JAX_PLATFORMS']")[0]
        preflight = '''from preflight import check
def snapshot():
    return {'device': {'checks': [
        {'key': k, 'ok': True} for k in ('service', 'motors', 'communication', 'mode')
    ] + [{'key': 'init', 'detail': '反初始化完成'}], 'vr_connected': True},
    'collection': {'phase': 'waiting', 'current': {'state': 'saving'}}}
def teleop_pids(): return []
'''
        binary = sys.executable
        import hashlib
        digest = hashlib.sha256(Path(binary).read_bytes()).hexdigest()
        marker = json.dumps({'sha256': digest, 'passed': True})
        for allow in (False, True):
            with self.subTest(allow=allow), \
                    patch.object(sys, 'argv', [binary, '--live'] + (['--allow-pending-save'] if allow else [])), \
                    patch('os.geteuid', return_value=0), \
                    patch.object(Path, 'read_text', return_value=marker):
                namespace = {'__PREFLIGHT_SOURCE__': preflight}
                if allow:
                    exec(compile(gate, '<bootstrap-gate-test>', 'exec'), namespace)
                else:
                    with self.assertRaisesRegex(SystemExit, '请先结束采集会话'):
                        exec(compile(gate, '<bootstrap-gate-test>', 'exec'), namespace)
