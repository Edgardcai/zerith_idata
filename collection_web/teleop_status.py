"""File-based interface to the independent teleop extension; no motor publisher."""
import json
import math
import os
from pathlib import Path
import tempfile
import time

DEFAULT_ROOT = Path('/home/robot/teleop_zero_lock')


class TeleopStatus:
    def __init__(self, root=DEFAULT_ROOT):
        self.root = Path(root)

    def config(self):
        path = self.root / 'config.json'
        if not path.exists():
            return {'lift_enabled': False, 'lift_height_m': .4}
        return self.validate(json.loads(path.read_text()))

    @staticmethod
    def validate(value):
        if not isinstance(value, dict) or not isinstance(value.get('lift_enabled'), bool):
            raise ValueError('升降柱设置格式错误')
        height = value.get('lift_height_m')
        if isinstance(height, bool):
            raise ValueError('升降柱高度必须为 0–0.8 m')
        try:
            height = float(height)
        except (ValueError, TypeError):
            raise ValueError('升降柱高度必须为 0–0.8 m')
        if not math.isfinite(height) or not 0 <= height <= .8:
            raise ValueError('升降柱高度必须为 0–0.8 m')
        return {'lift_enabled': value['lift_enabled'], 'lift_height_m': height}

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
            if fresh and state.get('active') and state.get('reason') in ('moving_head_to_zero', 'settling_gravity_compensation'):
                warnings.insert(0, '头部正在归零，建议稳定后录制')
            return {'available': fresh and state.get('version') == 'collection-1.0',
                    'phase': phase, 'title': title, 'warnings': warnings,
                    'calibration_seq': state.get('calibration_seq', 0), 'pid': state.get('pid'),
                    'config': self.config(), 'applied_config': state.get('config'),
                    'lift_actual_m': state.get('lift_actual_m'), 'lift_target_m': state.get('lift_target_m'),
                    'deviation_policy': state.get('deviation_policy'), 'ready': fresh and state.get('ready', False)}
        except (OSError, ValueError, KeyError, TypeError):
            return {'available': False, 'phase': 'offline', 'title': '等待遥操状态',
                    'warnings': [], 'config': {'lift_enabled': False, 'lift_height_m': .4}}

    def save(self, value, device, collection):
        config = self.validate(value)
        init = next((c for c in device.get('checks', []) if c['key'] == 'init'), {})
        state = self.snapshot()
        if not state['available']:
            raise ValueError('请先启动支持升降柱设置的遥操版本')
        if init.get('detail') != '反初始化完成' or state['phase'] != 'uninitialized':
            raise ValueError('请先反初始化，再修改升降柱高度')
        if collection.get('current'):
            raise ValueError('请先结束本条录制并等待保存')
        self.root.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix='.config-', dir=self.root)
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(config, f); f.write('\n'); f.flush(); os.fsync(f.fileno())
            os.replace(path, self.root / 'config.json')
        finally:
            if os.path.exists(path): os.unlink(path)
        return config
