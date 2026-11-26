#!/usr/bin/env python3
"""Install the reviewed direct boot path without restarting robotd."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

from manager import CONFIG, Host, validate

SOURCE = Path(__file__).resolve().parent
LIB = Path('/usr/local/lib/zerith-collection-autostart')
DROPIN = Path('/etc/systemd/system/robotd.service.d/collection-direct-boot.conf')


def configuration():
    result = {}
    for name, root, binary, switch, original in (
        ('collection', '/home/robot/collection_depth_toggle', 'server_fast', 'switch.py', 'server'),
        ('teleop', '/home/robot/teleop_zero_lock', 'teleop_zero_lock', 'switch_trial.py', 'teleop'),
    ):
        root = Path(root)
        manifest = json.loads((root / 'runtime/build.json').read_text())
        component = {'binary': str(root / 'runtime' / binary),
                     'switch': str(root / 'tools' / switch), 'original_name': original,
                     'sha256': manifest['sha256'], 'original_sha256': manifest['original_sha256']}
        validate(component)
        result[name] = component
    return result


def systemctl(*args, check=True):
    return subprocess.run(['systemctl', *args], check=check, text=True, capture_output=True)


def atomic(path, content, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        temporary = Path(f.name)
        f.write(content.encode())
    temporary.chmod(mode)
    temporary.replace(path)


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo')
    config = configuration()
    for unit in ('zerith-collection-autostart.service', 'zerith-storage-fast.service'):
        state = systemctl('show', unit, '-p', 'ActiveState', '--value').stdout.strip()
        if state in ('activating', 'deactivating', 'reloading'):
            raise RuntimeError('Wait for or stop the handover manager before installing: ' + unit)
    for name in ('manager.py', 'direct_boot.py'):
        atomic(LIB / name, (SOURCE / name).read_text())
    atomic(CONFIG, json.dumps(config, ensure_ascii=False, indent=2) + '\n')
    atomic(DROPIN, (SOURCE / 'direct-boot.conf').read_text())
    systemctl('disable', 'zerith-collection-autostart.service')
    systemctl('disable', 'zerith-storage-fast.service')
    systemctl('daemon-reload')
    effective = systemctl('show', 'robotd.service', '-p', 'ExecStart', '--value').stdout
    if str(LIB / 'direct_boot.py') not in effective:
        raise RuntimeError('Another robotd override is taking precedence')
    host = Host(config)
    host.publish('pending_restart', '已安装直接启动配置，下次开机生效；当前运行版本未切换', host.inventory())
    print('Installed direct boot for next robotd start. Current robot processes unchanged.')


if __name__ == '__main__':
    main()
