from pathlib import Path
import numpy as np
import jax
import jax.numpy as jnp
sys.path.insert(0,'/home/robot/teleop_zero_lock/patch')
from zero_lock import freeze_model, project, install
p=Path(sys._MEIPASS)/'urdf/ZR_H1PRO-1.2.00.H.V4.3_URDF_2025.12.02/urdf/ZR_H1PRO-1.2.00.H.V4.3_URDF_2025.12.02.urdf'
ZerithWBC._warmup_jax=lambda self:None
wbc=ZerithWBC(str(p),2,1.85)
fixed=freeze_model(wbc.robot)
fixed_dummy=freeze_model(wbc.dummy_robot)
rng=np.random.default_rng(28)
max_fk=0.0
for i in range(20):
    q=rng.uniform(-.2,.2,19);q[0]=.4
    for robot,locked in [(wbc.robot,fixed),(wbc.dummy_robot,fixed_dummy)]:
        expected=np.asarray(robot.forward_kinematics(jnp.asarray(project(q))))
        got=np.asarray(locked.forward_kinematics(jnp.asarray(q)))
        max_fk=max(max_fk,float(np.max(np.abs(expected-got))))
        np.testing.assert_allclose(got,expected,atol=1e-6)
print('FK_40_POSES_PASS max_error',max_fk,flush=True)
q=jnp.asarray(wbc.home_cfg)
jac=jax.jacfwd(fixed.forward_kinematics)(q)
assert np.max(np.abs(np.asarray(jac)[...,[1,2,17,18]])) < 1e-7
print('LOCKED_FK_JACOBIAN_ZERO_PASS',flush=True)
# A nonzero fixed lift must be baked into FK, not merely clipped at output.
for height in (0.0, .4, .8):
    for robot in (wbc.robot, wbc.dummy_robot):
        lifted=freeze_model(robot,height)
        for i in range(4):
            q=rng.uniform(-.2,.2,19)
            expected=np.asarray(robot.forward_kinematics(jnp.asarray(project(q,height))))
            actual=np.asarray(lifted.forward_kinematics(jnp.asarray(q)))
            np.testing.assert_allclose(actual,expected,atol=1e-6)
        jac=jax.jacfwd(lifted.forward_kinematics)(jnp.asarray(q))
        assert np.max(np.abs(np.asarray(jac)[...,[0,1,2,17,18]])) < 1e-7
print('FIXED_LIFT_24_FK_AND_JACOBIAN_PASS',flush=True)
# Verify all link transforms, including the neck and both hands, against the
# unfrozen vendor model at the selected nonzero angles. Derivatives must agree.
custom_fk_count=0
for angles in ([.5,0,0,0],[.35,-.2,.15,-.12],[.7,.3,-.4,.2]):
    for height in (None,0.0,.4,.8):
        for robot in (wbc.robot,wbc.dummy_robot):
            frozen=freeze_model(robot,height,angles)
            for _ in range(3):
                q=rng.uniform(-.2,.2,19)
                expected=np.asarray(robot.forward_kinematics(jnp.asarray(project(q,height,angles=angles))))
                got=np.asarray(frozen.forward_kinematics(jnp.asarray(q)))
                np.testing.assert_allclose(got,expected,atol=1e-6)
                custom_fk_count+=1
            jac=jax.jacfwd(frozen.forward_kinematics)(jnp.asarray(q))
            locked=[1,2,17,18]+([0] if height is not None else [])
            assert np.max(np.abs(np.asarray(jac)[...,locked]))<1e-7
print('CUSTOM_POSE_FK_AND_JACOBIAN_PASS',custom_fk_count,flush=True)
policy, originals=install(namespace)
patched=ZerithWBC(str(p),2,1.85)
assert not np.asarray(patched.robot.joints.twists)[[1,2,25,26]].any()
patched.update_dummy(1.75)
assert not np.asarray(patched.dummy_robot.joints.twists)[[1,2,25,26]].any()
print('MODEL_RECALIBRATION_LOCK_PRESERVED_PASS',flush=True)
# Real vendor IK with its normal cost, limit and self-collision pipeline.
poses=patched.get_home_pose(is_dummy=True)
names=[patched.mapping_vr_to_command[name] for name in poses]
commands=list(poses.values())
print('IK_BEGIN',names,flush=True)
for i in range(3):
    patched.move_to_cartesian(names,commands)
    assert np.isfinite(patched.cfg).all()
    assert not patched.cfg[[1,2,17,18]].any()
print('REAL_VENDOR_IK_FINITE_ZERO_OUTPUT_PASS',patched.cfg.tolist(),flush=True)
assert not blocked
print('OFFLINE_MODEL_TESTS_PASS NO_ZCM_CONSTRUCTION',flush=True)
# Exercise actual factory controller serialization with an in-memory bus.
import copy
from types import SimpleNamespace
class FakeBus:
    def __init__(self): self.messages=[];self.subscriptions={}
    def subscribe(self,channel,message_type,callback): self.subscriptions[channel]=callback
    def publish(self,channel,msg): self.messages.append((channel,copy.deepcopy(msg)))
class NoPlot:
    def __init__(self,*a,**kw): pass
    def __getattr__(self,name): return lambda *a,**kw:None
originals[('ZerithCtrl','__init__')].__globals__['PlotJugglerPublisher']=NoPlot
bus=FakeBus()
ctrl=ZerithCtrl(str(p),2,zcm=bus,enable_feedback=False)
ctrl._waist_state_handler('waist_state',SimpleNamespace(position_actual=[.4,0,0],error_flag=[0,0,0]))
ctrl._head_state_handler('head_state',SimpleNamespace(position_actual=[0,0],error_flag=[0,0]))
ctrl.init_state=2
ctrl.init_home_transition_duration=.02
assert ctrl.init() is True and policy.active
q=np.arange(19,dtype=float)/100;q[0]=.4
ctrl.waist_locked=True;ctrl.waist_last_published=np.array([.4,.12,-.1])
ctrl.send_control_cmd(q,np.ones(19),np.zeros(19),.05)
ctrl.send_head_cmd(.2,.3)
waist=next(m for ch,m in reversed(bus.messages) if ch=='waist_control')
head=next(m for ch,m in reversed(bus.messages) if ch=='head_control')
assert waist.position[1:] == [0,0],waist.position
assert head.position == [0,0],head.position
assert head.KP == [6,6] and head.KD == [1,1]
arm=next(m for ch,m in reversed(bus.messages) if ch=='upper_joint_control')
np.testing.assert_allclose(arm.position,q[3:17])
ctrl.publish_motor_control(q,np.zeros(19))
waist=next(m for ch,m in reversed(bus.messages) if ch=='waist_control')
head=next(m for ch,m in reversed(bus.messages) if ch=='head_control')
assert waist.position[1:]==[0,0] and head.position==[0,0]
ctrl.deinit()
assert not policy.active and bus.messages[-1][0]=='system_init_cmd_remote' and bus.messages[-1][1].system_init==2
assert ('TeleopController','_check_error_state') not in originals
assert ('TeleopController','_trigger_safety_stop') not in originals
assert not blocked
print('REAL_VENDOR_INIT_COMMAND_PATHS_DEINIT_PASS NO_ZCM_CONSTRUCTION',flush=True)

# Feedback FK must still reflect measured nonzero waist/head angles.
FakeBus.start=lambda self:None
originals[('ZerithCtrl','__init__')].__globals__['ZCM']=lambda *a,**kw:FakeBus()
feedback_ctrl=ZerithCtrl(str(p),2,zcm=FakeBus(),enable_feedback=True)
assert feedback_ctrl.wbc._zero_lock_ik is False
q=np.zeros(19);q[0]=.4;q[1]=.12;q[18]=.08
expected=np.asarray(wbc.robot.forward_kinematics(jnp.asarray(q)))
actual=np.asarray(feedback_ctrl.wbc.robot.forward_kinematics(jnp.asarray(q)))
np.testing.assert_allclose(actual,expected,atol=1e-6)
assert np.max(np.abs(actual-np.asarray(patched.robot.forward_kinematics(jnp.asarray(q))))) > .01
print('REAL_FEEDBACK_FK_UNMODIFIED_PASS',flush=True)

# The factory main constructor also creates a 16-axis HIGH_LEVEL model.
# That unrelated model must remain usable and unchanged.
high_path=p.with_name(p.stem+'_high_level.urdf')
high_wbc=ZerithWBC(str(high_path),2,1.85)
assert high_wbc._zero_lock_ik is False
assert high_wbc.robot.joints.num_actuated_joints==16
assert np.asarray(high_wbc.robot.joints.twists)[25:27].any()
print('FACTORY_HIGH_LEVEL_MODEL_COMPATIBILITY_PASS',flush=True)

# Run actual factory transition and steady teleop methods through the patched
# head interface and actual controller, with an in-memory bus. WBC output here
# is the configuration already solved by the real IK checks above.
import time
head=object.__new__(HeadCtrl)
head.enable=False;head.manual_head_angles=np.zeros(2);head.last_head_angles=np.zeros(2)
node=object.__new__(namespace['TeleopController'])
noop=lambda *a,**kw:None
node.ctrl=ctrl;node.motor_ctrl=None;node.head_ctrl=head
node.is_calibrate=True;node.is_initialized=True;node.is_transitioning=True
node.transition_duration=2.5;node.transition_start_time=time.time()
node.paused_pose=ctrl.get_current_pose();node.teleop_height_offset=0.0
node._publish_vr_visual=noop;node._build_latency_trace_info=lambda *a:None
node._suppress_teleop_start=False;node.feedback=SimpleNamespace(emit=noop)
node.processor=SimpleNamespace(get_vr_command=lambda:{},plotjuggler=NoPlot())
node.wbc=SimpleNamespace(step=lambda *a,**kw:(patched.cfg.copy(),np.zeros(2),0.0))
node.planner=SimpleNamespace(sync_for_transition=noop,update=lambda cfg:(np.array([cfg]),np.zeros((1,19)),np.zeros((1,19))))
node.button_ctrl=SimpleNamespace(right_trigger_hand=0,left_trigger_hand=0,
    left_thumbstick=np.zeros(2),right_thumbstick=np.zeros(2),is_button_pressed=lambda *a:False,
    left_trigger_index=0,right_trigger_index=0)
node.chassis_ctrl=SimpleNamespace(prev_vel_root_x=0.,prev_vel_root_rotz=0.,
    update_height_offset=lambda a,b,c:c,filter_root_pose=lambda xy,yaw:(xy,yaw),
    smooth=lambda a,b:a,reset_velocity_state=noop,reset_root_tracking=noop,
    update_fixed_rod_state=lambda *a:{'can_move':False})
node.safe_ctrl=SimpleNamespace(last_joint_position=None,paused_head_angles=np.zeros(2),
    set_paused_head_angles=noop,check_joint_safety=lambda q:(q,True,0.),
    get_transition_head_angles=lambda q,t:q,is_safety_stopped=False,check_runtime_safety=lambda *a:False)
def fresh():
    ctrl._waist_state_handler('waist_state',SimpleNamespace(position_actual=[.4,0,0],error_flag=[0,0,0]))
    ctrl._head_state_handler('head_state',SimpleNamespace(position_actual=[0,0],error_flag=[0,0]))
fresh();policy.arm()
head_command=head.get_head_command(patched.cfg)
assert head_command['head_yaw']==0 and head_command['head_pitch']==0 and isinstance(head_command['source'],str)
assert node._handle_transition_mode(0)==1
node.transition_start_time=time.time()-2.6
fresh();assert node._handle_transition_mode(1)==2
assert not node.is_transitioning
fresh();assert node._handle_decoupled_mode(2)==3
waist=next(m for ch,m in reversed(bus.messages) if ch=='waist_control')
head_message=next(m for ch,m in reversed(bus.messages) if ch=='head_control')
assert waist.position[1:]==[0,0] and head_message.position==[0,0]
assert not blocked
print('REAL_FACTORY_TRANSITION_AND_STEADY_TELEOP_PASS',flush=True)

ctrl.deinit();policy.config={'lift_enabled':True,'lift_height_m':.6}
# Use an integral number of the factory 120 Hz interpolation steps.
# The earlier .02 s smoke-test shortcut overshoots its np.arange endpoint.
ctrl.init_home_transition_duration=.1
fresh();bus.messages.clear();assert ctrl.init()
waists=[m for ch,m in bus.messages if ch=='waist_control']
assert len(waists)>1
assert abs(waists[0].position[0]-.4)<1e-5
assert abs(waists[-1].position[0]-.6)<1e-5, [(m.position,m.speed) for m in waists]
assert any(.4 < m.position[0] < .6 for m in waists)
q=np.arange(19,dtype=float)/100;q[0]=.2
ctrl.waist_locked=True;ctrl.waist_last_published=np.array([.3,.1,-.1])
fresh();ctrl.send_control_cmd(q,np.ones(19),np.ones(19),.15)
waist=next(m for ch,m in reversed(bus.messages) if ch=='waist_control')
np.testing.assert_allclose(waist.position,[.6,0,0]);assert waist.speed==[0,0,0]
ctrl.publish_motor_control(q,np.ones(19))
waist=next(m for ch,m in reversed(bus.messages) if ch=='waist_control')
np.testing.assert_allclose(waist.position,[.6,0,0]);assert waist.speed==[0,0,0]
patched.update_dummy(1.65);first=np.asarray(patched.dummy_robot.joints.parent_transforms).copy()
patched.update_dummy(1.65)
np.testing.assert_array_equal(first,np.asarray(patched.dummy_robot.joints.parent_transforms))
poses=patched.get_home_pose(is_dummy=True)
patched.move_to_cartesian([patched.mapping_vr_to_command[n] for n in poses],list(poses.values()))
assert abs(patched.cfg[0]-.6)<1e-6 and not patched.cfg[[1,2,17,18]].any()
ctrl.deinit();policy.config={'lift_enabled':False,'lift_height_m':.6}
fresh();assert ctrl.init();assert ctrl.vr_home_pose[0]==.4
ctrl.deinit();assert not blocked
print('FIXED_LIFT_INIT_INTERPOLATION_OUTPUT_IK_RECALIBRATION_DISABLE_PASS',flush=True)

# Real vendor interpolation/serialization, with simulated motor feedback only.
# Asymmetric head angles catch the factory's otherwise invisible yaw/pitch swap.
from pose_config import ANGLE_FIELDS,validate_config
custom_clock=[time.monotonic()]
policy.clock=lambda:custom_clock[0]
angles=np.array([.5,-.2,.15,-.1])
policy.config=validate_config({'lift_enabled':True,'lift_height_m':.6,**dict(zip(ANGLE_FIELDS,angles))})
head_feedback=[np.array([.03,-.02])]
waist_feedback=[np.array([.4,0,0])]
ctrl._waist_state_handler('waist_state',SimpleNamespace(position_actual=[.4,0,0],error_flag=[0,0,0]))
ctrl._head_state_handler('head_state',SimpleNamespace(position_actual=head_feedback[0],error_flag=[0,0]))
np.testing.assert_allclose(ctrl.get_current_pose()[17:19],head_feedback[0])
original_publish=bus.publish
def simulated_publish(channel,message):
    original_publish(channel,message)
    if channel=='waist_control':
        waist_feedback[0]=np.asarray(message.position)
    if channel=='head_control':head_feedback[0]=np.asarray(message.position)
    ctrl._waist_state_handler('waist_state',SimpleNamespace(position_actual=waist_feedback[0],error_flag=[0,0,0]))
    ctrl._head_state_handler('head_state',SimpleNamespace(position_actual=head_feedback[0],error_flag=[0,0]))
bus.publish=simulated_publish
bus.messages.clear();assert ctrl.init()
waists=[m for ch,m in bus.messages if ch=='waist_control']
np.testing.assert_allclose(waists[0].position,[.4,0,0],atol=1e-6)
np.testing.assert_allclose(waists[-1].position,[.6,*angles[:2]],atol=1e-6)
assert max(max(abs(v) for v in m.speed[1:]) for m in waists)<=.150001
assert any(.05<m.position[1]<.45 for m in waists)
np.testing.assert_allclose(ctrl.vr_home_pose[[1,2,17,18]],angles)
np.testing.assert_allclose(policy.head_target(),[.03,-.02])
previous=policy.head_target()
for _ in range(int(policy.duration/.02)+3):
    custom_clock[0]+=.02
    ctrl.send_head_cmd(.4,.6)
    current=next(m for ch,m in reversed(bus.messages) if ch=='head_control')
    assert max(abs(np.asarray(current.position)-previous))/.02<=.030001
    previous=np.array(current.position)
np.testing.assert_allclose(previous,angles[2:],atol=1e-10)
q=np.arange(19,dtype=float)/100
for locked in (False,True):
    ctrl.waist_locked=locked;ctrl.waist_last_published=np.array([.2,-.1,.1])
    ctrl.send_control_cmd(q,np.ones(19),np.ones(19),.2)
    ctrl.publish_motor_control(q,np.ones(19))
    waist=next(m for ch,m in reversed(bus.messages) if ch=='waist_control')
    head_message=next(m for ch,m in reversed(bus.messages) if ch=='head_control')
    np.testing.assert_allclose(waist.position,[.6,*angles[:2]])
    assert waist.speed==[0,0,0]
    np.testing.assert_allclose(head_message.position,angles[2:])
    arm=next(m for ch,m in reversed(bus.messages) if ch=='upper_joint_control')
    np.testing.assert_allclose(arm.position,q[3:17])
for _ in range(2):
    patched.update_dummy(1.7)
    current=np.asarray(patched.dummy_robot.joints.parent_transforms).copy()
    if _==0:first=current
    else:np.testing.assert_array_equal(current,first)
    poses=patched.get_home_pose(is_dummy=True)
    patched.move_to_cartesian([patched.mapping_vr_to_command[n] for n in poses],list(poses.values()))
    assert np.isfinite(patched.cfg).all()
    np.testing.assert_allclose(patched.cfg[[1,2,17,18]],angles)
    assert abs(patched.cfg[0]-.6)<1e-6
command=head.get_head_command(patched.cfg)
assert command['head_yaw']==angles[2] and command['head_pitch']==angles[3]
node.safe_ctrl.set_paused_pose=noop;node.safe_ctrl.mark_user_paused=noop
from zero_lock import pause_node
pause_node(node,namespace['TeleopState'],policy)
np.testing.assert_allclose(node.paused_pose[[1,2,17,18]],angles)
# The current measured head pose is still truthful, with no factory axis swap.
np.testing.assert_allclose(ctrl.get_current_pose()[17:19],head_feedback[0])
ctrl.deinit();assert not policy.active
policy.config=validate_config({'lift_enabled':False})
bus.messages.clear();assert ctrl.init()
waists=[m for ch,m in bus.messages if ch=='waist_control']
np.testing.assert_allclose(waists[0].position[1:],angles[:2],atol=1e-6)
np.testing.assert_allclose(waists[-1].position[1:],[0,0],atol=1e-6)
assert ctrl.vr_home_pose[0]==.4
ctrl.deinit();bus.publish=original_publish;policy.clock=time.monotonic
assert not blocked
print('CUSTOM_POSE_REAL_INIT_RAW_HEAD_IK_XLOCK_PAUSE_RECALIBRATION_RESET_PASS NO_DEVICE_COMMANDS',flush=True)

# Exercise the exact factory calibration wait, with no VR/device constructor.
# A stale operator initialized flag used to trap its caller after a mode change.
from processors.CalibrationManager import CalibrationManager
factory_phase=originals[('CalibrationManager','run_calibration_phase')]
calibration_mode_checks=0
calibration_bus_count=len(bus.messages)
def calibration_fixture():
    phase=object.__new__(CalibrationManager)
    phase.ctrl=SimpleNamespace(control_mode=0,init_state=2,
        waist_pos=np.zeros(3),arm_pos=np.zeros(14),head_pos=np.zeros(2))
    phase._is_current_pose_near_home=lambda:(True,0.0)
    phase.run_calibration=lambda **kw:{'offline':True}
    phase.recalibration_required_samples=1
    phase.recalibration_transition_duration=.1
    phase.safe_ctrl=SimpleNamespace(reset_all=noop,set_calibration_pose=noop,
        set_paused_head_angles=noop)
    phase.processor=SimpleNamespace(get_vr_command=lambda **kw:{})
    return phase

class OriginalWaitBlocked(Exception): pass
phase=calibration_fixture();polls=[0]
def old_mode_switch():
    polls[0]+=1;phase.ctrl.control_mode=3
    if polls[0]>=3:raise OriginalWaitBlocked()
try:
    factory_phase(phase,old_mode_switch,lambda:True)
except OriginalWaitBlocked:
    assert polls[0]==3
else:
    raise AssertionError('Factory regression reproduction unexpectedly exited')
calibration_mode_checks+=1

for field,values in [('control_mode',(1,2,3)),('init_state',(0,1,3,4))]:
    for value in values:
        phase=calibration_fixture();polls=[0]
        def change_feedback():
            polls[0]+=1;setattr(phase.ctrl,field,value)
            if polls[0]>1:raise AssertionError('Calibration did not release its caller')
            return 'calibrate'  # The switch wins over a simultaneous A press.
        assert phase.run_calibration_phase(change_feedback,lambda:True) is None
        assert polls[0]==1
        calibration_mode_checks+=1

for mode in (None,3):
    phase=calibration_fixture();phase.ctrl.control_mode=mode
    def forbidden_button_poll():raise AssertionError('Consumed a button outside VR mode')
    assert phase.run_calibration_phase(forbidden_button_poll,lambda:True) is None
    calibration_mode_checks+=1

phase=calibration_fixture();initialized=[True]
def cancel_initialization():initialized[0]=False
assert phase.run_calibration_phase(cancel_initialization,lambda:initialized[0]) is None
calibration_mode_checks+=1

# VR mode still waits for its explicit calibration event and keeps the factory
# completion contract. These callbacks perform no motion or actual calibration.
phase=calibration_fixture();polls=[0]
def normal_calibration_button():
    polls[0]+=1
    return 'calibrate' if polls[0]==3 else None
calibration_result=phase.run_calibration_phase(normal_calibration_button,lambda:True)
assert polls[0]==3 and calibration_result['imu_state']=={'offline':True}
assert calibration_result['is_transitioning'] is True
assert len(calibration_result['paused_pose'])==19
calibration_mode_checks+=1
assert len(bus.messages)==calibration_bus_count and not blocked
print('REAL_FACTORY_CALIBRATION_MODE_EXIT_PASS',calibration_mode_checks,
      'NO_DEVICE_COMMANDS',flush=True)
