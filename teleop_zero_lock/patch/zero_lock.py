"""Opt-in H1 1.3.9 experiment; installed inside the sole vendor teleop process.

Joint feedback is never changed. Init/deinit and the vendor safety handlers remain
in control. No SDK publisher or independent command thread is created.
"""
import functools
import json
import math
import os
import threading
import time
from pathlib import Path

import numpy as np

LOCK_NAMES = ('body_pitch_joint', 'body_yaw_joint', 'neck_yaw_joint', 'neck_pitch_joint')
LOCK_INDICES = (1, 2, 17, 18)


def validate_config(value):
    if not isinstance(value, dict) or not isinstance(value.get('lift_enabled', False), bool):
        raise ValueError('升降柱配置格式错误')
    height = value.get('lift_height_m', 0.4)
    if isinstance(height, bool):
        raise ValueError('升降柱高度必须是 0–0.8 m')
    height = float(height)
    if not math.isfinite(height) or not 0 <= height <= .8:
        raise ValueError('升降柱高度必须是 0–0.8 m')
    return {'lift_enabled': value.get('lift_enabled', False), 'lift_height_m': height}


def project(value, lift_height=None, derivative=False):
    q = np.array(value, dtype=float, copy=True)
    if q.ndim not in (1, 2) or q.shape[-1] != 19 or not np.isfinite(q).all():
        raise ValueError('zero lock requires a finite 19-joint configuration')
    q[..., list(LOCK_INDICES)] = 0.0
    if lift_height is not None:
        q[..., 0] = 0.0 if derivative else lift_height
    return q


def freeze_model(robot, lift_height=None):
    """Preserve the vendor's 19-variable layout; remove motion of four axes.

    Zero twists make FK and its analytic Jacobian use the same fixed transforms.
    Projecting the unused variables then cannot change either hand's pose.
    """
    import jax.numpy as jnp
    import jax_dataclasses as jdc
    import jaxlie
    if robot.joints.num_actuated_joints != 19:
        raise ValueError('Unsupported model: expected 19 actuated joints')
    ids = [robot.joints.actuated_names.index(name) for name in LOCK_NAMES]
    if tuple(ids) != LOCK_INDICES:
        raise ValueError('Vendor joint ordering changed')
    rows = jnp.array([robot.joints.names.index(name) for name in LOCK_NAMES])
    with jdc.copy_and_mutate(robot) as fixed:
        fixed.joints.twists = fixed.joints.twists.at[rows].set(0.0)
        if lift_height is not None:
            if robot.joints.actuated_names[0] != 'daogui_joint':
                raise ValueError('Lift model ordering changed')
            row = robot.joints.names.index('daogui_joint')
            # Bake the selected displacement into the parent transform before
            # removing its degree of freedom. FK and IK now share that height.
            transform = (jaxlie.SE3(robot.joints.parent_transforms[row]) @
                         jaxlie.SE3.exp(robot.joints.twists[row] * lift_height)).wxyz_xyz
            fixed.joints.parent_transforms = fixed.joints.parent_transforms.at[row].set(transform)
            fixed.joints.twists = fixed.joints.twists.at[row].set(0.0)
    return fixed


class HeadGravity:
    """URDF gravity at measured waist/head joints, in raw motor joint order."""
    def __init__(self, model):
        import pinocchio as pin
        self.pin, self.model, self.data = pin, model, model.createData()
        self.names = ('daogui_joint','body_pitch_joint','body_yaw_joint','neck_yaw_joint','neck_pitch_joint')
        if any(not model.existJointName(name) for name in self.names):
            raise ValueError('Head gravity model joint names do not match')
        self.q_indices = [model.joints[model.getJointId(name)].idx_q for name in self.names]
        self.v_indices = [model.joints[model.getJointId(name)].idx_v for name in self.names[-2:]]
    def __call__(self, waist, head):
        q = self.pin.neutral(self.model)
        q[self.q_indices] = np.r_[waist, head]
        # Arm coordinates do not affect gravity torques of the neck branch.
        g = self.pin.computeGeneralizedGravity(self.model, self.data, q)
        return np.asarray(g)[self.v_indices].copy()


class ZeroPolicy:
    def __init__(self, clock=time.monotonic, status_path=None):
        self.clock = clock
        self.status_path = Path(status_path) if status_path else None
        self.mutex = threading.RLock()
        self.active = False
        self.samples = {}
        self.started = None
        self.stable_since = None
        self.seed_head = np.zeros(2)
        self.duration = 3.0
        self.tolerance = 0.005
        self.head_pitch_tolerance = 0.021  # User accepts the observed ~0.020 rad pitch offset.
        self.last_write = -math.inf
        self.reason = 'waiting_for_initialization'
        self.fault = None
        self.last_head_target = np.zeros(2)
        self.gravity = None
        self.feedforward = np.zeros(2)
        self.integral = np.zeros(2)
        self.comp_ready = False
        self.config = {'lift_enabled': False, 'lift_height_m': .4}
        self.initializing = False
        self.operator = {'initialized': False, 'calibrated': False, 'state': 'STOP_TELEOP'}
        self.calibration_seq = 0

    @property
    def lift_height(self):
        return self.config['lift_height_m'] if self.config['lift_enabled'] else None

    def warnings(self):
        actual = self.feedback(self.clock())
        if not self.active or actual is None:
            return []
        names = ['腰 pitch', '腰 yaw', '头 yaw', '头 pitch']
        limits = [self.tolerance, self.tolerance, self.tolerance, self.head_pitch_tolerance]
        result = [f'{name} 偏差 {v:.5f} rad' for name, v, limit in zip(names, actual, limits) if abs(v) > limit]
        if self.lift_height is not None:
            lift = float(self.samples['waist'][1][0])
            if abs(lift - self.lift_height) > .005:
                result.append(f'升降柱偏差 {lift - self.lift_height:+.4f} m')
        return result

    def observe(self, kind, msg):
        n = 3 if kind == 'waist' else 2
        values = np.asarray(msg.position_actual, dtype=float)
        errors = list(msg.error_flag)
        valid = values.shape == (n,) and np.isfinite(values).all() and len(errors) == n and all(v == 0 for v in errors)
        with self.mutex:
            now = self.clock()
            previous = self.samples.get(kind)
            locked = values[1:3] if kind == 'waist' else values
            limits = self.tolerance if kind == 'waist' else np.array([self.tolerance, self.head_pitch_tolerance])
            if (not valid or np.any(np.abs(locked) > limits) or
                    previous is None or now - previous[0] > 0.25):
                self.stable_since = None
            self.samples[kind] = (now, values.copy(), bool(valid))

    def feedback(self, now):
        if any(k not in self.samples for k in ('waist', 'head')):
            return None
        waist, head = self.samples['waist'], self.samples['head']
        if any(not sample[2] or not 0 <= now - sample[0] <= 0.25 for sample in (waist, head)):
            return None
        return np.r_[waist[1][1:3], head[1]]

    def arm(self):
        with self.mutex:
            now = self.clock()
            actual = self.feedback(now)
            if actual is None:
                raise RuntimeError('Head/waist feedback stale or motor error; zero lock not armed')
            # The factory init has already smoothly commanded the waist to zero.
            # Refuse a large remaining error instead of snapping the waist to zero.
            if np.max(np.abs(actual[:2])) > 0.03:
                raise RuntimeError('Waist did not reach the factory zero home; zero lock not armed')
            if np.max(np.abs(actual[2:])) > 0.35:
                raise RuntimeError('Head outside the first-trial start envelope (0.35 rad)')
            self.fault = None
            self.seed_head = actual[2:].copy()  # raw wire order, no vendor head_pos swap
            self.duration = max(3.0, 1.875 * float(np.max(np.abs(self.seed_head))) / 0.03) if np.any(self.seed_head) else 0.0
            self.ramp_elapsed = 0.0
            self.last_target_at = now
            self.last_head_target = self.seed_head.copy()
            self.started = now
            self.last_ff_at = now
            self.feedforward[:] = 0
            self.integral[:] = 0
            self.comp_ready = False
            self.stable_since = None
            self.active = True
            self.reason = 'moving_head_to_zero'

    def disarm(self):
        with self.mutex:
            self.active = False
            self.stable_since = None
            self.started = None
            self.reason = 'waiting_for_initialization'
            self.operator.update(initialized=False, calibrated=False, state='STOP_TELEOP')

    def head_target(self):
        with self.mutex:
            if not self.active:
                raise RuntimeError('Zero lock is not armed')
            if self.feedback(self.clock()) is None:
                self.fault = 'feedback_lost_reinitialize'
            if self.fault:
                return self.last_head_target.copy()
            now = self.clock()
            self.ramp_elapsed += min(0.05, max(0.0, now - self.last_target_at))
            self.last_target_at = now
            t = min(1.0, self.ramp_elapsed / self.duration) if self.duration else 1.0
            s = 10*t**3 - 15*t**4 + 6*t**5
            self.last_head_target = self.seed_head * (1.0 - s)
            return self.last_head_target.copy()

    def head_feedforward(self):
        with self.mutex:
            now = self.clock()
            dt = min(.05, max(0.0, now - self.last_ff_at))
            self.last_ff_at = now
            if self.gravity is None or not self.active:
                return np.zeros(2)
            actual = self.feedback(now)
            if actual is None:
                self.fault = 'feedback_lost_reinitialize'
            if self.fault or self.ramp_elapsed < self.duration:
                self.comp_ready = False
                return self.feedforward.copy()
            g = np.asarray(self.gravity(self.samples['waist'][1], self.samples['head'][1]), dtype=float)
            if g.shape != (2,) or not np.isfinite(g).all():
                self.fault = 'gravity_model_invalid'
                self.comp_ready = False
                return self.feedforward.copy()
            target = np.clip(g + self.integral, -.6, .6)
            self.comp_ready = bool(np.max(np.abs(target - self.feedforward)) < .01)
            if self.comp_ready:
                error = np.where(np.abs(actual[2:]) > .0015, -actual[2:], 0.0)
                # Slow integral trim for small URDF mass/friction errors; bounded at 0.15 Nm.
                self.integral = np.clip(self.integral + .4 * error * dt, -.15, .15)
                target = np.clip(g + self.integral, -.6, .6)
            self.feedforward += np.clip(target - self.feedforward, -.05 * dt, .05 * dt)
            return self.feedforward.copy()

    def ready(self):
        with self.mutex:
            now = self.clock()
            actual = self.feedback(now)
            if not self.active:
                self.reason = 'waiting_for_initialization'
            elif self.fault:
                self.reason = self.fault
            elif actual is None:
                self.fault = 'feedback_lost_reinitialize'
                self.reason = self.fault
            elif self.ramp_elapsed < self.duration:
                self.reason = 'moving_head_to_zero'
            elif self.gravity is not None and not self.comp_ready:
                self.reason = 'settling_gravity_compensation'
            elif np.any(np.abs(actual) > [self.tolerance, self.tolerance, self.tolerance, self.head_pitch_tolerance]):
                self.reason = 'actual_joint_not_zero'
            else:
                if self.stable_since is None:
                    self.stable_since = now
                self.reason = 'ready' if now - self.stable_since >= 1.0 else 'checking_stability'
                return self.reason == 'ready'
            self.stable_since = None
            return False

    def write_status(self):
        if not self.status_path:
            return
        with self.mutex:
            now = self.clock()
            if now - self.last_write < 0.2:
                return
            ready = self.ready()
            actual = self.feedback(now)
            payload = {'timestamp': time.time(), 'pid': os.getpid(), 'active': self.active,
                       'ready': ready, 'reason': self.reason, 'tolerance_rad': self.tolerance,
                       'head_pitch_tolerance_rad': self.head_pitch_tolerance,
                       'actual_rad': None if actual is None else actual.tolist(),
                       'head_target_rad': self.head_target().tolist() if self.active else None,
                       'head_feedforward_nm': self.feedforward.tolist(),
                       'head_integral_nm': self.integral.tolist(),
                       'warnings': self.warnings(), 'fault': self.fault,
                       'config': self.config.copy(), 'initializing': self.initializing,
                       'operator': self.operator.copy(), 'calibration_seq': self.calibration_seq,
                       'lift_actual_m': float(self.samples['waist'][1][0]) if actual is not None else None,
                       'lift_target_m': self.lift_height if self.active else None,
                       'deviation_policy': 'warn_only', 'version': 'collection-1.0'}
            tmp = self.status_path.with_suffix('.tmp')
            tmp.write_text(json.dumps(payload, allow_nan=False) + '\n')
            tmp.replace(self.status_path)
            self.last_write = now


class GuardedBus:
    """Final guard for *all* waist/head commands from the vendor controller."""
    def __init__(self, original, policy):
        self.original, self.policy = original, policy

    def __getattr__(self, name):
        return getattr(self.original, name)

    def publish(self, channel, msg):
        if self.policy.active:
            if channel == 'waist_control':
                if len(msg.position) != 3:
                    raise ValueError('Unexpected waist command shape')
                msg.position = [msg.position[0], 0.0, 0.0]
                msg.speed = [msg.speed[0], 0.0, 0.0]
                if self.policy.lift_height is not None:
                    msg.position[0] = self.policy.lift_height
                    msg.speed[0] = 0.0
            elif channel == 'head_control':
                msg.position = self.policy.head_target().tolist()
                msg.speed = [0.0, 0.0]
                if np.max(np.abs(msg.torque)) > 1e-6:
                    raise ValueError('Unexpected factory head feed-forward; refusing double compensation')
                msg.torque = self.policy.head_feedforward().tolist()
                # Factory KP/KD stay unchanged; bounded gravity feed-forward holds zero.
        return self.original.publish(channel, msg)



def pause_node(self, State):
    self.state = State.STOP_TELEOP
    self.is_transitioning = False
    self.ctrl.stop_chassis()
    self.chassis_ctrl.reset_velocity_state()
    self.paused_pose = project(self.ctrl.get_current_pose())
    self.safe_ctrl.set_paused_pose(self.processor.get_vr_command())
    self.safe_ctrl.set_paused_head_angles(np.zeros(2))
    self.safe_ctrl.mark_user_paused()


def after_joystick(self, policy, State):
    if self.is_initialized and self.state != State.ERROR:
        ready = policy.ready()
        # Position deviations and settling only warn. Genuine feedback faults
        # retain the stop behavior; all vendor safety handlers still run first.
        if (policy.fault or not policy.active) and self.state in (State.DECOUPLED, State.TELEOP):
            pause_node(self, State)
        if policy.active:
            self.ctrl.send_head_cmd(0.0, 0.0)  # bus applies the bounded initial ramp
    policy.operator = {'initialized': bool(self.is_initialized),
                       'calibrated': bool(getattr(self, 'is_calibrate', False)),
                       'state': getattr(self.state, 'name', str(self.state))}
    policy.write_status()

def install(namespace, status_path=None, initialization_check=None, config_loader=None):
    """Patch known class methods before constructing any live vendor controller."""
    from controllers.ZerithCtrl import ZerithCtrl
    from controllers.HeadCtrl import HeadCtrl
    from processors.ZerithWBC import ZerithWBC
    Teleop = namespace['TeleopController']
    State = namespace['TeleopState']
    policy = ZeroPolicy(status_path=status_path)
    original = {}
    context = threading.local()
    planners = []
    if config_loader is not None:
        policy.config = validate_config(config_loader())

    def constrain_planner(planner):
        height = policy.lift_height
        planner.robot = freeze_model(planner._zero_base_robot, height)
        planner.dummy_robot = freeze_model(planner._zero_base_dummy, height)
        planner.cfg, planner.home_cfg = project(planner.cfg, height), project(planner.home_cfg, height)

    def wrap(cls, name, builder):
        original[(cls.__name__, name)] = getattr(cls, name)
        setattr(cls, name, functools.wraps(getattr(cls, name))(builder(getattr(cls, name))))

    def ctrl_constructor(old):
        def init(self, *args, **kwargs):
            previous = getattr(context, 'feedback', False)
            context.feedback = True
            try:
                old(self, *args, **kwargs)
            finally:
                context.feedback = previous
            self.zcm = GuardedBus(self.zcm, policy)
            policy.gravity = HeadGravity(self.model)
            self._zero_original_vr_home = self.vr_home_pose.copy()
        return init
    wrap(ZerithCtrl, '__init__', ctrl_constructor)

    for method, kind in [('_waist_state_handler', 'waist'), ('_head_state_handler', 'head')]:
        def observer(old, kind=kind):
            def call(self, channel, msg):
                policy.observe(kind, msg)
                return old(self, channel, msg)
            return call
        wrap(ZerithCtrl, method, observer)

    def initialize(old):
        def call(self, *args, **kwargs):
            policy.disarm()
            if initialization_check is not None:
                try:
                    initialization_check()
                except Exception as exc:
                    namespace['logger'].warning('锁零试验尚不能初始化：%s', exc)
                    return False
            if config_loader is not None:
                policy.config = validate_config(config_loader())
            self.vr_home_pose = self._zero_original_vr_home.copy()
            self.waist_locked = False
            if policy.lift_height is not None:
                joint = self.model.joints[self.model.getJointId('daogui_joint')]
                if not self.model.lowerPositionLimit[joint.idx_q] <= policy.lift_height <= self.model.upperPositionLimit[joint.idx_q]:
                    raise ValueError('升降柱高度超出机器人模型范围')
                self.vr_home_pose[0] = policy.lift_height
            for planner in planners:
                constrain_planner(planner)
            policy.initializing = True
            policy.last_write = -math.inf
            policy.write_status()
            try:
                # No output lock is armed until the original interpolation ends.
                result = old(self, *args, **kwargs)
            finally:
                policy.initializing = False
            if result:
                try:
                    policy.arm()
                    self.waist_locked = False
                except Exception:
                    self.deinit()
                    raise
            return result
        return call
    wrap(ZerithCtrl, 'init', initialize)

    def deinitialize(old):
        def call(self, *args, **kwargs):
            policy.disarm()
            return old(self, *args, **kwargs)
        return call
    wrap(ZerithCtrl, 'deinit', deinitialize)

    def init_state(old):
        def call(self, channel, msg):
            if msg.system_init != 2:
                policy.disarm()
            return old(self, channel, msg)
        return call
    wrap(ZerithCtrl, 'init_state_handler', init_state)

    def mode_state(old):
        def call(self, channel, msg):
            if msg.control_mode != 0:
                policy.disarm()
            return old(self, channel, msg)
        return call
    wrap(ZerithCtrl, 'control_mode_handler', mode_state)

    def send_control(old):
        def call(self, traj, vel, acc, height_offset, motor_ctrl=None, trace_info=None):
            if policy.active:
                traj = project(traj, policy.lift_height)
                vel, acc = project(vel, policy.lift_height, True), project(acc, policy.lift_height, True)
                if policy.lift_height is not None:
                    height_offset = 0.0
                    self.waist_last_published[0] = policy.lift_height
                # Preserve the vendor X-lock of the lift, but never its nonzero waist cache.
                self.waist_last_published[1:3] = 0.0
            return old(self, traj, vel, acc, height_offset, motor_ctrl, trace_info)
        return call
    wrap(ZerithCtrl, 'send_control_cmd', send_control)

    def get_head(old):
        def call(self, *args, **kwargs):
            if policy.active:
                return {'head_yaw': 0.0, 'head_pitch': 0.0, 'source': 'zero_lock'}
            return old(self, *args, **kwargs)
        return call
    wrap(HeadCtrl, 'get_head_command', get_head)

    def wbc_constructor(old):
        def call(self, *args, **kwargs):
            # Ctrl's WBC is a feedback/FK observer, not the teleop IK planner.
            # It MUST retain the original model for truthful Cartesian feedback.
            urdf_path = args[0] if args else kwargs['urdf_path']
            self._zero_lock_ik = not getattr(context, 'feedback', False) and '_high_level' not in Path(str(urdf_path)).stem
            result = old(self, *args, **kwargs)
            if self._zero_lock_ik:
                planners.append(self)
            return result
        return call
    wrap(ZerithWBC, '__init__', wbc_constructor)

    def update_dummy(old):
        def call(self, *args, **kwargs):
            if getattr(self, '_zero_lock_ik', False) and hasattr(self, '_zero_base_robot'):
                self.robot = self._zero_base_robot
                self.dummy_robot = self._zero_base_dummy
            result = old(self, *args, **kwargs)
            if not self._zero_lock_ik:
                return result
            if not hasattr(self, '_zero_base_robot'):
                self._zero_base_robot = self.robot
            self._zero_base_dummy = self.dummy_robot
            constrain_planner(self)
            return result
        return call
    wrap(ZerithWBC, 'update_dummy', update_dummy)

    def move(old):
        def call(self, *args, **kwargs):
            if not self._zero_lock_ik:
                return old(self, *args, **kwargs)
            self.cfg, self.home_cfg = project(self.cfg, policy.lift_height), project(self.home_cfg, policy.lift_height)
            result = old(self, *args, **kwargs)
            self.cfg = project(self.cfg, policy.lift_height)
            return self.cfg.copy()
        return call
    wrap(ZerithWBC, 'move_to_cartesian', move)

    def calibrated(old):
        def call(self, *args, **kwargs):
            result = old(self, *args, **kwargs)
            if result and self.is_initialized and policy.active:
                self.paused_pose = project(self.paused_pose)
                pause_node(self, State)
                policy.calibration_seq += 1
                namespace['logger'].info('标定成功：短按 A 启动遥操；头腰偏差只预警，不暂停')
                after_joystick(self, policy, State)
            return result
        return call
    wrap(Teleop, '_run_calibration_phase', calibrated)

    def recalibrated(old):
        def call(self, *args, **kwargs):
            result = old(self, *args, **kwargs)
            if self.is_initialized and policy.active and self.state != State.ERROR:
                pause_node(self, State)
                policy.calibration_seq += 1
                namespace['logger'].info('重新标定成功：短按 A 启动遥操')
                after_joystick(self, policy, State)
            return result
        return call
    wrap(Teleop, '_handle_recalibration', recalibrated)

    def joystick(old):
        def call(self, *args, **kwargs):
            result = old(self, *args, **kwargs)  # vendor emergency/deinit handling first
            after_joystick(self, policy, State)
            return result
        return call
    wrap(Teleop, 'update_joystick_state', joystick)
    return policy, original
