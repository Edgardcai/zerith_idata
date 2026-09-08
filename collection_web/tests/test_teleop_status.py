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
        config={'lift_enabled':True,'lift_height_m':.6}
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
