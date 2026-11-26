#!/usr/bin/env python3
"""Select validated programs before creating any factory teleop/server pane."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time

from manager import CONFIG, RUNTIME, Host, digest, validate

FACTORY_HASH = 'b29800369167f6d1dddbb49955e442bff7cdf294fea4f1a4e2daf67718a3fc03'


def render(source, config):
    replacements = {
        'ROBOT_BIN_PATH="$(cd "$(dirname "$0")" && pwd)"': 'ROBOT_BIN_PATH="/opt/robot"',
        'cd $ROBOT_BIN_PATH && ./teleop; exec sh':
            'cd /home/robot/teleop_zero_lock && ' + config['teleop']['binary'] + ' --boot; exec sh',
        'cd $ROBOT_BIN_PATH && ./server; exec sh':
            'cd /home/robot/collection_depth_toggle && ' + config['collection']['binary'] + ' --boot; exec sh',
        'cd $ROBOT_BIN_PATH && ./wifi_config; exec sh':
            'cd /opt/robot && /usr/local/lib/robot-wifi-client-only/wifi_config; exec sh',
    }
    for old, new in replacements.items():
        if source.count(old) != 1:
            raise RuntimeError('Factory startup layout changed: ' + old)
        source = source.replace(old, new)
    return source


def prepare(config):
    for component in config.values():
        validate(component)
        if digest('/opt/robot/' + component['original_name']) != component['original_sha256']:
            raise RuntimeError('Factory component version changed')
    source = Path('/opt/robot/start_all_h1_tmux.sh')
    if digest(source) != FACTORY_HASH:
        raise RuntimeError('Factory startup script changed; review required')
    script = render(source.read_text(), config)
    subprocess.run(['sh', '-n'], input=script, text=True, check=True)
    return script


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true', help='Read-only preflight inside the robotd mount namespace')
    args = parser.parse_args()
    config = json.loads(CONFIG.read_text())
    if args.check:
        prepare(config)
        print('Direct boot scripts, original versions and validated binaries: OK')
        return
    if os.geteuid() != 0 or not os.environ.get('INVOCATION_ID'):
        raise RuntimeError('Run through robotd.service')
    # The original mount and all unchanged factory windows remain in robotd's
    # private mount namespace. No change is made to the read-only image.
    subprocess.run(['mount', '-t', 'ext4', '-o', 'ro,nosuid,nodev',
                    '/dev/mapper/robot_data', '/opt/robot'], check=True)
    script = prepare(config)
    if subprocess.run(['tmux', 'has-session', '-t', 'robot_startup'],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        raise RuntimeError('Existing robot_startup session; refusing a second startup')
    RUNTIME.mkdir(mode=0o755, parents=True, exist_ok=True)
    marker = {'invocation_id': os.environ['INVOCATION_ID'],
              'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    (RUNTIME / 'direct-boot.json').write_text(json.dumps(marker) + '\n')
    (RUNTIME / 'direct-boot.json').chmod(0o644)
    launcher = RUNTIME / 'start_all_h1_tmux.sh'
    launcher.write_text(script)
    launcher.chmod(0o700)
    host = Host(config)
    host.publish('starting', '开机直接启动新版采集和锁腰遥操，等待状态就绪')
    child = subprocess.Popen(['sh', str(launcher)])
    while True:
        # The original script ends with tmux attach, which can exit without a
        # terminal; tmux itself retains ownership of all component processes.
        child.poll()
        try:
            inventory = host.inventory()
            state = host.snapshot()
            checks = {c['key']: c for c in state.get('device', {}).get('checks', [])}
            teleop = state.get('teleop', {})
            valid = all(v['new'] and not v['original'] and not v['unknown'] for v in inventory.values())
            if valid and checks.get('service', {}).get('ok') is True and teleop.get('available') is True and teleop.get('pid') in inventory['teleop']['new']:
                host.publish('ready', '新版采集服务和锁腰遥操均已核对进程版本及实时状态', inventory)
            else:
                host.publish('wait', '已选择新版，等待进程和实时状态；不会回退启动旧版', inventory)
        except Exception as exc:
            host.publish('wait', str(exc))
        time.sleep(5)


if __name__ == '__main__':
    main()
