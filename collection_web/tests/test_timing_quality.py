import json
from pathlib import Path
import tempfile
import unittest
import h5py
import numpy as np
from test_core import episode
from episodes import EpisodeStore, validate_finished


def timing_metadata(p):
    with h5py.File(p/'episode.hdf5','a') as f:
        n=len(f['timestamp/t']);t=f['timestamp/t'][:]
        g=f.require_group('timing');g.attrs.update(version=1,sample_stats='{"accepted":8,"candidates":8}',source_events='{}')
        ref=1_000_000_000+np.arange(n,dtype='int64')*33_333_333
        g.create_dataset('reference_receive_ns',data=ref)
        for camera in ('cam_high','cam_left_wrist','cam_right_wrist'):
            pfx='cameras/rs/'+camera+'/color/'
            for name,values in [('device_timestamp_ms',t),('receive_ns',ref),('frame_number',np.arange(n)+1),('epoch',np.zeros(n,dtype='int64'))]:
                g.create_dataset(pfx+name,data=values)
        for field in ('upper_joint','gripper','chassis','waist','head'):
            for kind in ('state','control'):
                pfx='messages/'+field+'_'+kind+'/'
                g.create_dataset(pfx+'receive_ns',data=ref-1_000_000)
                g.create_dataset(pfx+'seq',data=np.arange(n)+1)


class TimingQualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'data';self.root.mkdir()
        self.dataset=self.root/'test';self.dataset.mkdir()
    def tearDown(self):self.tmp.cleanup()
    def test_legacy_duplicate_and_gap_are_failed_not_pending(self):
        p=episode(self.dataset)
        with h5py.File(p/'episode.hdf5','a') as f:f['timestamp/t'][3]=f['timestamp/t'][2]
        qc=validate_finished(p)['timing_qc']
        self.assertEqual(qc['status'],'failed');self.assertEqual(qc['duplicate_count'],1)
        store=EpisodeStore(Path(self.tmp.name)/'runtime',self.root)
        try:
            store.new_session('s',{}, {},self.dataset)
            store.observe({'id':'s','dataset':str(self.dataset),'baseline':[]})
            row=store.list()[0]
            self.assertEqual(row['state'],'completed');self.assertEqual(row['grade'],'A')
            self.assertEqual(json.loads((Path(row['path'])/'timing_quality.json').read_text())['status'],'failed')
            for grade in ('F','B','A'):
                store.rate(row['id'],grade)
                self.assertEqual(store.list()[0]['grade'],grade)
                self.assertEqual(json.loads((Path(row['path'])/'review.json').read_text())['grade'],grade)
            store.close();store=EpisodeStore(Path(self.tmp.name)/'runtime',self.root)
            self.assertEqual(store.list()[0]['grade'],'A')
            self.assertEqual(json.loads((Path(row['path'])/'review.json').read_text())['timing_qc']['status'],'failed')
        finally:store.close()
    def test_backwards_is_terminal_quality_failure(self):
        p=episode(self.dataset)
        with h5py.File(p/'episode.hdf5','a') as f:f['timestamp/t'][3]=f['timestamp/t'][2]-1
        self.assertEqual(validate_finished(p)['timing_qc']['backward_count'],1)
    def test_verified_data_passes(self):
        p=episode(self.dataset);timing_metadata(p)
        self.assertEqual(validate_finished(p)['timing_qc']['status'],'passed')
    def test_each_failure_is_detected(self):
        for failure in ('missing','reuse','skew','future_action','stale_state','clock_reset','rejected','master_mismatch'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(dir=self.root) as temp:
                p=episode(Path(temp));timing_metadata(p)
                with h5py.File(p/'episode.hdf5','a') as f:
                    g=f['timing']
                    if failure=='missing':del g['reference_receive_ns']
                    if failure=='reuse':g['cameras/rs/cam_left_wrist/color/frame_number'][2]=2
                    if failure=='skew':g['cameras/rs/cam_left_wrist/color/receive_ns'][2]+=50_000_000
                    if failure=='future_action':g['messages/upper_joint_control/receive_ns'][2]+=50_000_000
                    if failure=='stale_state':g['messages/waist_state/receive_ns'][2]-=100_000_000
                    if failure=='clock_reset':g['cameras/rs/cam_high/color/epoch'][2]=1
                    if failure=='rejected':g.attrs['sample_stats']='{"rejected":1}'
                    if failure=='master_mismatch':g['cameras/rs/cam_high/color/device_timestamp_ms'][2]+=1
                self.assertEqual(validate_finished(p)['timing_qc']['status'],'failed')


if __name__=='__main__':unittest.main()
