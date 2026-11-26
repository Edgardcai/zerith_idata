import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import boot_guard
from direct_boot import render


class DirectBootTests(unittest.TestCase):
    def test_factory_windows_preserved_and_no_original_collection_launch(self):
        source = (Path(__file__).resolve().parent / 'fixtures/start_all_h1_tmux.sh').read_text()
        config = {'teleop': {'binary': '/home/robot/teleop_zero_lock/runtime/teleop_zero_lock'},
                  'collection': {'binary': '/home/robot/collection_depth_toggle/runtime/server_fast'}}
        result = render(source, config)
        subprocess.run(['sh', '-n'], input=result, text=True, check=True)
        self.assertNotIn('&& ./teleop;', result)
        self.assertNotIn('&& ./server;', result)
        self.assertEqual(result.count(' --boot;'), 2)
        for window in ('Motion_Control', 'SDKService', 'robot_state'):
            self.assertIn('&& ./' + window + '; exec sh', result)
        self.assertIn('&& /usr/local/lib/robot-wifi-client-only/wifi_config;', result)
        self.assertIn('ROBOT_BIN_PATH="/opt/robot"', result)
        with self.assertRaises(RuntimeError):
            render(source.replace('./teleop;', './teleop_new;'), config)

    def test_boot_mode_rejects_outside_robotd(self):
        with patch.dict(os.environ, {'INVOCATION_ID': 'test'}), patch('os.geteuid', return_value=0):
            with self.assertRaisesRegex(RuntimeError, 'inside robotd'):
                boot_guard.claim('teleop')

    def test_boot_authorization_and_single_use_claim(self):
        original_read = Path.read_text
        original_stat = Path.stat
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            marker = runtime / 'direct-boot.json'
            data = {'invocation_id': 'test', 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
            marker.write_text(json.dumps(data))

            def read(path, *args, **kwargs):
                if str(path) == '/proc/self/cgroup':
                    return '0::/system.slice/robotd.service\n'
                return original_read(path, *args, **kwargs)

            def stat(path, *args, **kwargs):
                value = original_stat(path, *args, **kwargs)
                if path == marker:
                    from types import SimpleNamespace
                    return SimpleNamespace(st_uid=0, st_mode=0o100644)
                return value

            # Real live originals must not be interrupted; exclude enumeration
            # only in the successful startup simulation.
            with patch.object(boot_guard, 'RUNTIME', runtime), patch.dict(os.environ, {'INVOCATION_ID': 'test'}), \
                    patch('os.geteuid', return_value=0), patch.object(Path, 'read_text', read), \
                    patch.object(Path, 'stat', stat), patch.object(Path, 'iterdir', return_value=iter([])):
                boot_guard.claim('teleop')
                with self.assertRaises(FileExistsError):
                    boot_guard.claim('teleop')
                data['invocation_id'] = 'stale'
                marker.write_text(json.dumps(data))
                with self.assertRaisesRegex(RuntimeError, 'another service start'):
                    boot_guard.claim('collection')

    def test_running_factory_process_rejects_boot_claim(self):
        from types import SimpleNamespace
        fake = SimpleNamespace(name='99999999')
        class Process:
            name = fake.name
            def __truediv__(self, name):
                return SimpleNamespace(read_bytes=lambda: b'./teleop\0')
        original_read = Path.read_text
        def read(path, *args, **kwargs):
            if str(path) == '/proc/self/cgroup':
                return '0::/system.slice/robotd.service\n'
            if path.name == 'direct-boot.json':
                return json.dumps({'invocation_id': 'test', 'boot_id': original_read(Path('/proc/sys/kernel/random/boot_id')).strip()})
            return original_read(path, *args, **kwargs)
        with patch.dict(os.environ, {'INVOCATION_ID': 'test'}), patch('os.geteuid', return_value=0), \
                patch.object(Path, 'read_text', read), patch.object(Path, 'stat', return_value=SimpleNamespace(st_uid=0, st_mode=0o100644)), \
                patch.object(Path, 'iterdir', return_value=iter([Process()])):
            with self.assertRaisesRegex(RuntimeError, 'already running'):
                boot_guard.claim('teleop')
