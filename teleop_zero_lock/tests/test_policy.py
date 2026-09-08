import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'patch'))
from zero_lock import ZeroPolicy, GuardedBus, project

class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.t = 10.0
        self.p = ZeroPolicy(clock=lambda: self.t)
    def sample(self, waist=(0.4,0,0), head=(0,0), errors=None):
        self.p.observe('waist', SimpleNamespace(position_actual=waist, error_flag=errors or [0]*3))
        self.p.observe('head', SimpleNamespace(position_actual=head, error_flag=[0]*2))
    def test_requires_fresh_valid_feedback(self):
        with self.assertRaises(RuntimeError): self.p.arm()
        self.sample();self.t += .3
        with self.assertRaises(RuntimeError): self.p.arm()
        self.sample(errors=[0,1,0])
        with self.assertRaises(RuntimeError): self.p.arm()
    def test_refuses_nonzero_waist_after_init(self):
        self.sample(waist=(.4,.1,0))
        with self.assertRaises(RuntimeError): self.p.arm()
    def test_zero_ramp_bounded_and_no_initial_step(self):
        self.sample(head=(.1,-.2)); self.p.arm()
        np.testing.assert_array_equal(self.p.head_target(), [.1,-.2])
        previous=self.p.head_target()
        for t in np.arange(.01,self.p.duration+0.02,.01):
            self.t = 10+t
            self.sample()
            current=self.p.head_target()
            self.assertLessEqual(float(np.max(np.abs(current-previous)))/.01, .030001)
            previous=current
        np.testing.assert_array_equal(previous,[0,0])
    def test_requires_one_second_and_loses_readiness_on_stale_or_drift(self):
        self.sample();self.p.arm();self.t+=3
        self.sample();self.assertFalse(self.p.ready())
        for i in range(11):
            self.t+=.1;self.sample();self.p.ready()
        self.assertTrue(self.p.ready())
        self.t+=.3;self.assertFalse(self.p.ready())
        self.sample(head=(.02,0));self.assertFalse(self.p.ready())
    def test_projection_preserves_arms_lift_and_input(self):
        q=np.arange(38,dtype=float).reshape(2,19)
        result=project(q)
        np.testing.assert_array_equal(result[:,3:17],q[:,3:17])
        np.testing.assert_array_equal(result[:,0],q[:,0])
        self.assertEqual(q[0,1],1)
        self.assertFalse(result[:,[1,2,17,18]].any())
        with self.assertRaises(ValueError):project(np.zeros(16))
        q[0,0]=np.nan
        with self.assertRaises(ValueError):project(q)
    def test_final_bus_guard_and_safety_passthrough(self):
        sent=[]
        bus=GuardedBus(SimpleNamespace(publish=lambda *v:sent.append(v)),self.p)
        self.sample();self.p.arm();self.t+=3;self.sample()
        m=SimpleNamespace(position=[.8,.2,-.1],speed=[.1,.2,.3])
        bus.publish('waist_control',m)
        self.assertEqual(m.position,[.8,0,0]);self.assertEqual(m.speed,[.1,0,0])
        m=SimpleNamespace(position=[.2,.5],speed=[1,2],torque=[0,0],KP=[6,6],KD=[1,1])
        bus.publish('head_control',m)
        self.assertEqual(m.position,[0,0]);self.assertEqual(m.KP,[6,6])
        stop=SimpleNamespace(system_init=3)
        bus.publish('system_init_cmd_remote',stop)
        self.assertIs(sent[-1][1],stop)
        self.p.disarm()
        m=SimpleNamespace(position=[.4,.2,.1],speed=[0,0,0])
        bus.publish('waist_control',m)
        self.assertEqual(m.position,[.4,.2,.1])

class StateGateTests(unittest.TestCase):
    setUp=PolicyTests.setUp
    sample=PolicyTests.sample
    def node(self):
        self.sent=[]
        State=SimpleNamespace(STOP_TELEOP=0,DECOUPLED=1,TELEOP=2,ERROR=3)
        no_op=lambda *a:None
        node=SimpleNamespace(state=1,is_initialized=True,is_transitioning=True,
            ctrl=SimpleNamespace(stop_chassis=lambda:self.sent.append('stop'),
                get_current_pose=lambda:np.arange(19,dtype=float)/100,
                send_head_cmd=lambda *a:self.sent.append('head')),
            chassis_ctrl=SimpleNamespace(reset_velocity_state=no_op),
            safe_ctrl=SimpleNamespace(set_paused_pose=no_op,set_paused_head_angles=no_op,mark_user_paused=no_op),
            processor=SimpleNamespace(get_vr_command=lambda:{}))
        return node,State
    def test_settling_and_deviation_never_pause_teleop(self):
        from zero_lock import after_joystick
        self.sample(head=(.02,0));self.p.arm()
        node,State=self.node();after_joystick(node,self.p,State)
        self.assertEqual(node.state,State.DECOUPLED)
        self.assertTrue(node.is_transitioning)
        self.assertNotIn('stop',self.sent)
        self.sample(head=(.025,.03));after_joystick(node,self.p,State)
        self.assertEqual(node.state,State.DECOUPLED)
        self.assertTrue(self.p.warnings())

    def test_feedback_fault_still_pauses_and_preserves_arm_pose(self):
        from zero_lock import after_joystick
        self.sample();self.p.arm();self.t+=.4
        node,State=self.node();after_joystick(node,self.p,State)
        self.assertEqual(node.state,State.STOP_TELEOP)
        np.testing.assert_allclose(node.paused_pose[3:17],np.arange(19)[3:17]/100)
    def test_disarmed_policy_blocks_teleop_without_moving_head(self):
        from zero_lock import after_joystick
        node,State=self.node();after_joystick(node,self.p,State)
        self.assertEqual(node.state,State.STOP_TELEOP)
        self.assertNotIn('head',self.sent)
    def test_error_and_deinit_never_send_head(self):
        from zero_lock import after_joystick
        self.sample();self.p.arm()
        node,State=self.node();node.state=State.ERROR
        after_joystick(node,self.p,State);self.assertFalse(self.sent)
        node.state=State.DECOUPLED;node.is_initialized=False
        after_joystick(node,self.p,State);self.assertFalse(self.sent)
    def test_stale_feedback_latches_until_reinitialize(self):
        self.sample(head=(.1,0));self.p.arm();before=self.p.head_target()
        self.t+=.4
        np.testing.assert_array_equal(before,self.p.head_target())
        self.sample();self.assertFalse(self.p.ready())
        self.assertEqual(self.p.reason,'feedback_lost_reinitialize')
    def test_user_accepted_pitch_offset_passes_but_larger_offset_does_not(self):
        self.sample();self.p.arm()
        for i in range(12):
            self.t+=.1;self.sample(head=(0,.020027));self.p.ready()
        self.assertTrue(self.p.ready())
        self.sample(head=(0,.022));self.assertFalse(self.p.ready())
        self.sample(waist=(.4,.006,0));self.assertFalse(self.p.ready())
    def test_sample_gap_cannot_count_as_continuous_stability(self):
        self.sample();self.p.arm();self.p.ready()
        self.t+=2;self.sample();self.assertFalse(self.p.ready())

class GravityTests(unittest.TestCase):
    setUp=PolicyTests.setUp
    sample=PolicyTests.sample
    def test_feedforward_limits_rate_and_integral_with_fixed_zero_angle(self):
        self.sample();self.p.arm();self.p.gravity=lambda waist,head:np.array([0,-.4])
        previous=np.zeros(2)
        for i in range(3000):
            self.t+=.01;self.sample(head=(0,.02));self.p.head_target()
            ff=self.p.head_feedforward()
            self.assertLessEqual(float(np.max(np.abs(ff-previous))), .00050001)
            self.assertLessEqual(float(np.max(np.abs(ff))), .60000001)
            self.assertLessEqual(float(np.max(np.abs(self.p.integral))), .15000001)
            np.testing.assert_array_equal(self.p.head_target(),[0,0])
            previous=ff
        self.assertLess(ff[1],-.4)
    def test_feedforward_freezes_on_feedback_loss(self):
        self.sample();self.p.arm();self.p.gravity=lambda waist,head:np.array([0,-.4])
        self.t+=.01;self.sample();self.p.head_target();before=self.p.head_feedforward()
        self.t+=1
        np.testing.assert_array_equal(before,self.p.head_feedforward())
        self.assertEqual(self.p.fault,'feedback_lost_reinitialize')

class LiftTests(unittest.TestCase):
    def test_lift_configuration_range_and_projection(self):
        from zero_lock import validate_config
        for value in (-.01,.81,float('nan'),True):
            with self.assertRaises(ValueError): validate_config({'lift_enabled':True,'lift_height_m':value})
        self.assertEqual(validate_config({'lift_enabled':True,'lift_height_m':.8})['lift_height_m'],.8)
        q=np.arange(19,dtype=float)
        self.assertEqual(project(q,.6)[0],.6)
        self.assertEqual(project(q,.6,True)[0],0)
        np.testing.assert_array_equal(project(q,.6)[3:17],q[3:17])

if __name__ == '__main__':unittest.main()
