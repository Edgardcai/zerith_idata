import json
from pathlib import Path
import tempfile
import time
import unittest
from teleop_status import TeleopStatus


class TeleopStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        (self.root/'runtime').mkdir();self.status=TeleopStatus(self.root)
        self.state={'timestamp':time.time(),'version':'collection-1.0','active':True,'ready':False,
                    'operator':{'initialized':True,'calibrated':True,'state':'DECOUPLED'},
                    'warnings':['头 yaw 偏差 0.00600 rad'],'calibration_seq':1}
        self.write()
    def tearDown(self):self.tmp.cleanup()
    def write(self): (self.root/'runtime/status.json').write_text(json.dumps(self.state))
    def test_warning_does_not_hide_running_or_calibrated_status(self):
        self.assertEqual(self.status.snapshot()['phase'],'running')
        self.assertTrue(self.status.snapshot()['warnings'])
        self.state['operator']['state']='STOP_TELEOP';self.write()
        self.assertEqual(self.status.snapshot()['phase'],'calibrated')
        self.state['operator']['calibrated']=False;self.write()
        self.assertEqual(self.status.snapshot()['phase'],'uncalibrated')
    def test_stale_state_cannot_claim_ready(self):
        self.state.update(timestamp=time.time()-10,ready=True);self.write()
        r=self.status.snapshot();self.assertFalse(r['available']);self.assertFalse(r['ready'])
        self.assertEqual(r['phase'],'offline')
    def test_save_only_when_deinitialized_and_no_recording(self):
        config=TeleopStatus.validate({'lift_enabled':True,'lift_height_m':.6})
        device={'checks':[{'key':'init','detail':'初始化完成'}]}
        with self.assertRaises(ValueError):self.status.save(config,device,{'current':None})
        self.state.update(active=False);self.state['operator']['initialized']=False;self.write()
        device['checks'][0]['detail']='反初始化完成'
        with self.assertRaises(ValueError):self.status.save(config,device,{'current':{'id':1}})
        self.status.save(config,device,{'current':None})
        self.assertEqual(self.status.config(),config)
        for value in (None,-1,.81,float('nan'),True):
            with self.assertRaises(ValueError):self.status.save({'lift_enabled':True,'lift_height_m':value},device,{'current':None})
        self.assertEqual(self.status.config(),config)

    def test_nonzero_pose_capability_limits_and_session_guard(self):
        config={'lift_enabled':True,'lift_height_m':.8,'waist_pitch_rad':.5,'head_yaw_rad':.1,'head_pitch_rad':-.2}
        device={'checks':[{'key':'init','detail':'反初始化完成'}]}
        self.state.update(active=False);self.state['operator']['initialized']=False;self.write()
        with self.assertRaisesRegex(ValueError,'固定角度'):self.status.save(config,device,{'current':None})
        self.state.update(version='collection-1.1-fixed-pose',pose_config_supported=True);self.write()
        for phase in ('waiting','recording','saving','disconnected'):
            with self.assertRaises(ValueError):self.status.save(config,device,{'current':None,'phase':phase})
        saved=self.status.save(config,device,{'current':None,'phase':'idle'})
        self.assertEqual(saved['waist_pitch_rad'],.5);self.assertEqual(saved['waist_yaw_rad'],0)
        self.assertEqual(self.status.config(),saved)
        for field,value in [('waist_pitch_rad',-0.01),('waist_yaw_rad',1),('head_yaw_rad',2),('head_pitch_rad',.6)]:
            with self.assertRaises(ValueError):self.status.save({**config,field:value},device,{'current':None})
        self.assertEqual(self.status.config(),saved)

    def test_current_and_saved_pose_are_distinct_and_feedback_is_raw(self):
        self.state.update(version='collection-1.1-fixed-pose',pose_config_supported=True,
            config=TeleopStatus.validate({'waist_pitch_rad':.5}),target_rad=[.5,0,0,0],actual_rad=[.499,0,.1,-.2])
        self.write();state=self.status.snapshot()
        self.assertTrue(state['pose_config_supported']);self.assertEqual(state['applied_config']['waist_pitch_rad'],.5)
        self.assertEqual(state['config']['waist_pitch_rad'],0);self.assertEqual(state['actual_rad'],[.499,0,.1,-.2])

