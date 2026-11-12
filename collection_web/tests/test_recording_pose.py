import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import h5py
from test_core import episode

_spec=importlib.util.spec_from_file_location('pose_recording_audit',
    Path(__file__).resolve().parents[2]/'teleop_zero_lock/tools/analyze_recording.py')
_audit=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(_audit)


class RecordingPoseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=episode(Path(self.temp.name))
        with h5py.File(self.path/'episode.hdf5','a') as f:f.create_dataset('subtask_transitions',data=[4,8])
    def tearDown(self):self.temp.cleanup()
    def nonzero(self):
        target=[.5,-.2,.15,-.1]
        (self.path/'collection_task.json').write_text(json.dumps({'targets':{'fixed_pose':{'target_rad':target}}}))
        with h5py.File(self.path/'episode.hdf5','a') as f:
            for prefix in ('action','observation/state'):
                f[prefix+'/waist/position'][:]=[.8,*target[:2]]
                f[prefix+'/head/position'][:]=target[2:]
            f['observation/state/head/position'][:]=[.151,-.08]
        return target
    def test_custom_pose_uses_recorded_target_without_rewriting_data(self):
        target=self.nonzero();before=(self.path/'episode.hdf5').read_bytes()
        report=_audit.analyze(self.path)
        self.assertEqual(report['target_rad'],target)
        self.assertTrue(report['commands_match_target_within_1e_minus_6'])
        self.assertTrue(report['actual_within_limits']);self.assertEqual(report['actual_outside_frames'],[0,0,0,0])
        self.assertFalse(report['commands_zero_within_1e_minus_6'])
        self.assertEqual(before,(self.path/'episode.hdf5').read_bytes())
    def test_drift_and_wrong_command_are_detected(self):
        self.nonzero()
        with h5py.File(self.path/'episode.hdf5','a') as f:
            f['action/waist/position'][3,1]=0
            f['observation/state/waist/position'][4,2]=-.18
        report=_audit.analyze(self.path)
        self.assertFalse(report['commands_match_target_within_1e_minus_6'])
        self.assertFalse(report['actual_within_limits']);self.assertEqual(report['actual_outside_frames'],[0,1,0,0])
    def test_old_zero_pose_episodes_remain_supported(self):
        report=_audit.analyze(self.path)
        self.assertEqual(report['target_rad'],[0,0,0,0]);self.assertTrue(report['actual_within_limits'])
    def test_bad_pose_metadata_is_rejected(self):
        (self.path/'collection_task.json').write_text(json.dumps({'targets':{'fixed_pose':{'target_rad':[.5]}}}))
        with self.assertRaises(ValueError):_audit.analyze(self.path)
