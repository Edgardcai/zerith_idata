"""File-based interface to the independent teleop extension; no motor publisher."""
import json
import os
from pathlib import Path
import tempfile
import time
import importlib.util

# Load only the standard-library configuration helper, never the motor controller.
_spec = importlib.util.spec_from_file_location('collection_pose_config',
    Path(__file__).resolve().parents[1] / 'teleop_zero_lock/patch/pose_config.py')
_pose = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pose)
ANGLE_FIELDS, ANGLE_LIMITS, POSE_VERSION = _pose.ANGLE_FIELDS, _pose.ANGLE_LIMITS, _pose.POSE_VERSION

DEFAULT_ROOT = Path('/home/robot/teleop_zero_lock')


class TeleopStatus:
    def __init__(self, root=DEFAULT_ROOT):
        self.root = Path(root)

    def config(self):
        path = self.root / 'config.json'
        if not path.exists():
            return self.validate({})
        return self.validate(json.loads(path.read_text()))

    @staticmethod
    def validate(value):
        return _pose.validate_config(value)

    def snapshot(self):
        try:
            state = json.loads((self.root / 'runtime/status.json').read_text())
            age = time.time() - state['timestamp']
            fresh = 0 <= age < (15 if state.get('initializing') else 3)
            operator = state.get('operator', {})
            if not fresh:
                title, phase = '遥操状态连接中断', 'offline'
            elif state.get('initializing'):
                title, phase = '正在初始化', 'initializing'
            elif not state.get('active') or not operator.get('initialized'):
                title, phase = '未初始化 · 长按 A 初始化', 'uninitialized'
            elif state.get('fault'):
                title, phase = '反馈异常 · 请检查设备', 'fault'
            elif not operator.get('calibrated'):
                title, phase = '待标定 · 短按 A 标定', 'uncalibrated'
            elif operator.get('state') in ('DECOUPLED', 'TELEOP'):
                title, phase = '遥操作中', 'running'
            else:
                title, phase = '标定成功 · 短按 A 启动遥操', 'calibrated'
            warnings = list(state.get('warnings', [])) if fresh else []
            if fresh and state.get('active') and state.get('reason') in ('moving_head_to_zero', 'moving_head_to_target', 'settling_gravity_compensation'):
                warnings.insert(0, '头部正在到达固定姿态，建议稳定后录制')
            available = fresh and state.get('version') in ('collection-1.0', POSE_VERSION)
            return {'available': available,
                    'pose_config_supported': available and state.get('pose_config_supported') is True,
                    'phase': phase, 'title': title, 'warnings': warnings,
                    'calibration_seq': state.get('calibration_seq', 0), 'pid': state.get('pid'),
                    'config': self.config(), 'applied_config': state.get('config'),
                    'actual_rad': state.get('actual_rad'), 'target_rad': state.get('target_rad'),
                    'angle_fields': list(ANGLE_FIELDS), 'angle_limits_rad': list(ANGLE_LIMITS),
                    'lift_actual_m': state.get('lift_actual_m'), 'lift_target_m': state.get('lift_target_m'),
                    'deviation_policy': state.get('deviation_policy'), 'ready': fresh and state.get('ready', False)}
        except (OSError, ValueError, KeyError, TypeError):
            return {'available': False, 'phase': 'offline', 'title': '等待遥操状态',
                    'warnings': [], 'pose_config_supported': False, 'config': self.validate({})}

    def save(self, value, device, collection):
        config = self.validate(value)
        init = next((c for c in device.get('checks', []) if c['key'] == 'init'), {})
        state = self.snapshot()
        if not state['available']:
            raise ValueError('请先启动支持升降柱设置的遥操版本')
        if any(config[field] != 0 for field in ANGLE_FIELDS) and not state.get('pose_config_supported'):
            raise ValueError('请先启动支持固定角度的遥操版本')
        if init.get('detail') != '反初始化完成' or state['phase'] != 'uninitialized':
            raise ValueError('请先反初始化，再修改固定姿态')
        if collection.get('current'):
            raise ValueError('请先结束本条录制并等待保存')
        if collection.get('phase', 'idle') not in ('idle', 'closed', 'rejected'):
            raise ValueError('请先结束采集会话，再修改固定姿态')
        self.root.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix='.config-', dir=self.root)
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(config, f); f.write('\n'); f.flush(); os.fsync(f.fileno())
            os.replace(path, self.root / 'config.json')
        finally:
            if os.path.exists(path): os.unlink(path)
        return config
