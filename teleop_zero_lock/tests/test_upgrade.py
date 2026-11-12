import contextlib
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import copy
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import upgrade_trial


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'runtime').mkdir();(self.root/'candidate').mkdir()
        self.binary=self.root/'runtime/teleop_zero_lock';self.binary.write_bytes(b'old')
        self.candidate=self.root/'candidate/teleop_zero_lock_candidate';self.candidate.write_bytes(b'new')
        self.old=hashlib.sha256(b'old').hexdigest();self.new=hashlib.sha256(b'new').hexdigest()
        for directory,digest in [(self.binary.parent,self.old),(self.candidate.parent,self.new)]:
            (directory/'validated.json').write_text(json.dumps({'passed':True,'sha256':digest}))
            (directory/'build.json').write_text(json.dumps({'sha256':digest,'original_sha256':'factory','source_commit':'source'}))
        self.boot=self.root/'autostart.json'
        self.boot.write_text(json.dumps({'teleop':{'binary':str(self.binary),'sha256':self.old}}))
        self.state={'device':{'vr_connected':True,'checks':[
            {'key':k,'ok':True} for k in ('service','motors','communication','mode')]+[{'key':'init','detail':'反初始化完成'}]},
            'collection':{'phase':'idle','current':None}}
        self.calls=[]
    def tearDown(self):self.temp.cleanup()
    def run_upgrade(self,check=False):
        def tmux(*args):
            self.calls.append(args)
            return '%2' if args[0]=='list-panes' else ''
        with contextlib.ExitStack() as stack:
            for name,value in [('ROOT',self.root),('AUTOSTART_CONFIG',self.boot)]:stack.enter_context(patch.object(upgrade_trial,name,value))
            stack.enter_context(patch('os.geteuid',return_value=0))
            stack.enter_context(patch.object(sys,'argv',['upgrade','--candidate',str(self.candidate)]+(['--check'] if check else [])))
            stack.enter_context(patch.object(upgrade_trial,'snapshot',return_value=self.state))
            stack.enter_context(patch.object(upgrade_trial,'teleop_pids',return_value=[]))
            stack.enter_context(patch.object(upgrade_trial,'trial_pids',side_effect=[[123],[]]))
            stack.enter_context(patch.object(upgrade_trial,'wait_started',return_value=456))
            stack.enter_context(patch.object(upgrade_trial,'tmux',side_effect=tmux))
            upgrade_trial.main()
    def test_checked_bundle_installs_matching_markers_and_boot_hash(self):
        self.run_upgrade()
        self.assertEqual(self.binary.read_bytes(),b'new')
        self.assertEqual((self.root/'runtime/teleop_zero_lock.previous').read_bytes(),b'old')
        for file in ('build.json','validated.json'):
            self.assertEqual(json.loads((self.binary.parent/file).read_text())['sha256'],self.new)
        self.assertEqual(json.loads(self.boot.read_text())['teleop']['sha256'],self.new)
        previous=json.loads((self.binary.parent/'previous-release.json').read_text())
        self.assertEqual(previous['manifest']['sha256'],self.old)
        self.assertEqual(previous['autostart']['teleop']['sha256'],self.old)
    def test_plan_leaves_controller_and_files_untouched(self):
        self.run_upgrade(check=True)
        self.assertEqual(self.binary.read_bytes(),b'old');self.assertEqual(len(self.calls),1)
        self.assertTrue(self.candidate.exists())
    def test_corrupt_candidate_never_stops_controller(self):
        self.candidate.write_bytes(b'corrupt')
        with self.assertRaisesRegex(RuntimeError,'self-test'):self.run_upgrade()
        self.assertEqual(self.calls,[]);self.assertEqual(self.binary.read_bytes(),b'old')
    def test_initialized_robot_never_stops_controller(self):
        self.state['device']['checks'][-1]['detail']='初始化完成'
        with self.assertRaisesRegex(RuntimeError,'反初始化'):self.run_upgrade()
        self.assertEqual(self.calls,[])

    def idle_state(self):
        self.state['device']['vr_connected']=False
        self.state['device']['checks'][1]['ok']=False
        policy={'timestamp':100.,'active':False,'initializing':False,'operator':{
            'initialized':False,'calibrated':False,'state':'STOP_TELEOP','motor_control_active':False,'control_mode':0}}
        return policy
    def test_idle_boot_allows_only_program_restart_in_known_inactive_states(self):
        policy=self.idle_state()
        for value in (0,4,5):
            self.assertEqual(upgrade_trial.idle_boot_check(self.state,policy,{'fresh':True,'system_init':value},100.5),[])
        for value in (1,2,3,None,99):
            self.assertTrue(upgrade_trial.idle_boot_check(self.state,policy,{'fresh':True,'system_init':value},100.5))
    def test_idle_boot_rejects_active_stale_calibrating_or_motor_control(self):
        policy=self.idle_state();init={'fresh':True,'system_init':5}
        for field,value in [('active',True),('initializing',True),('timestamp',90.)]:
            changed=copy.deepcopy(policy);changed[field]=value
            self.assertTrue(upgrade_trial.idle_boot_check(self.state,changed,init,100.5))
        for field,value in [('initialized',True),('calibrated',True),('state','DECOUPLED'),('motor_control_active',True),('control_mode',3)]:
            changed=copy.deepcopy(policy);changed['operator'][field]=value
            self.assertTrue(upgrade_trial.idle_boot_check(self.state,changed,init,100.5))
        self.assertTrue(upgrade_trial.idle_boot_check(self.state,policy,{'fresh':False,'system_init':5},100.5))
        self.state['collection']['phase']='waiting'
        self.assertTrue(upgrade_trial.idle_boot_check(self.state,policy,init,100.5))
