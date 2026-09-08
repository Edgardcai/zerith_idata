"""Read-only trial checks. No robot command interface is imported."""
import json
import urllib.request
from pathlib import Path

ROOT = Path('/home/robot/teleop_zero_lock')

def snapshot():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:8090/api/status', timeout=3) as response:
        return json.load(response)

def check(state, starting=True):
    device = state['device']
    checks = {item['key']: item for item in device['checks']}
    errors = []
    for key in ('service', 'motors', 'communication', 'mode'):
        if not checks.get(key, {}).get('ok'):
            errors.append('检查未通过：' + key)
    if checks.get('init', {}).get('detail') != '反初始化完成':
        errors.append('请先完成反初始化')
    if state['collection']['phase'] != 'idle' or state['collection'].get('current'):
        errors.append('请先结束采集会话')
    if starting:
        if not device.get('vr_connected'):
            errors.append('请连接 Meta Quest VR')
    return errors

def teleop_pids():
    result = []
    for item in Path('/proc').iterdir():
        if not item.name.isdigit():
            continue
        try:
            if (item/'comm').read_text().strip() == 'teleop':
                result.append(int(item.name))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return result

if __name__ == '__main__':
    state = snapshot()
    errors = check(state)
    print(json.dumps({'ready_to_switch': not errors, 'issues': errors,
                      'battery': state['device'].get('battery'),
                      'original_teleop_pids': teleop_pids()}, ensure_ascii=False, indent=2))
    raise SystemExit(bool(errors))
