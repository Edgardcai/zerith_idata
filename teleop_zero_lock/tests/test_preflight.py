import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from preflight import check

class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.state={'device':{'checks':[{'key':k,'ok':True} for k in ('service','motors','communication','mode')]+[{'key':'init','ok':False,'detail':'反初始化完成'}], 'vr_connected':True,'battery':30},'collection':{'phase':'idle','current':None}}
    def test_ready_requires_deinit_and_no_collection(self):
        self.assertEqual(check(self.state),[])
        self.state['device']['checks'][-1]['detail']='初始化完成'
        self.assertTrue(check(self.state))
        self.state['device']['checks'][-1]['detail']='反初始化完成'
        self.state['collection']['phase']='recording'
        self.assertTrue(check(self.state))
    def test_battery_is_reported_without_extra_motion_threshold(self):
        self.state['device'].update(vr_connected=False,battery=19)
        self.assertEqual(len(check(self.state)),1)
        self.state['device']['vr_connected']=True
        self.assertEqual(check(self.state),[])
    def test_restore_does_not_require_vr_or_charged_battery(self):
        self.state['device'].update(vr_connected=False,battery=6)
        self.assertEqual(check(self.state,starting=False),[])
        self.state['device']['checks'][0]['ok']=False
        self.assertTrue(check(self.state,starting=False))
