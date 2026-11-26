#!/usr/bin/env python3
"""Wait for an idle robot, then hand over to the two validated components.

No motion commands, initialization, binary replacement, or service-wide restart.
The existing component switchers retain their own final state and hash checks.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

CONFIG = Path('/etc/zerith-collection-autostart.json')
RUNTIME = Path('/run/zerith-collection-autostart')
URL = 'http://127.0.0.1:8090/api/status'


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def validate(component):
    binary = Path(component['binary'])
    marker = json.loads((binary.parent / 'validated.json').read_text())
    manifest = json.loads((binary.parent / 'build.json').read_text())
    expected = component['sha256']
    if (marker.get('passed') is not True or marker.get('sha256') != expected
            or manifest.get('sha256') != expected or digest(binary) != expected
            or manifest.get('original_sha256') != component['original_sha256']):
        raise RuntimeError('版本或自检不匹配，请检查后重新安装开机配置：' + str(binary))


def decide(config, inventory, state):
    """Pure policy; return (ready/wait/switch, component or reason)."""
    checks = {c['key']: c for c in state.get('device', {}).get('checks', [])}
    teleop = state.get('teleop', {})
    for name in ('collection', 'teleop'):
        item = inventory[name]
        if item['unknown'] or (item['original'] and item['new']):
            return 'wait', name + ' 运行版本未知或存在新旧并行进程，拒绝自动切换'
    collection_ready = bool(inventory['collection']['new']) and checks.get('service', {}).get('ok') is True
    teleop_ready = (bool(inventory['teleop']['new']) and teleop.get('available') is True
                    and teleop.get('pid') in inventory['teleop']['new'])
    if collection_ready and teleop_ready:
        return 'ready', '新版采集服务和锁腰遥操均已核对进程版本及实时状态'
    # Never interrupt an initialized robot, active recording or pending save.
    if checks.get('init', {}).get('detail') != '反初始化完成':
        return 'wait', '等待反初始化完成；不会自动反初始化或中断遥操'
    session = state.get('collection', {})
    if session.get('phase') not in ('idle', 'closed') or session.get('current'):
        return 'wait', '等待录制保存完成并结束采集会话'
    if not collection_ready:
        if inventory['collection']['new']:
            return 'wait', '新版采集进程已存在，等待状态流；不会再启动第二份'
        return 'switch', 'collection'
    if inventory['teleop']['new']:
        return 'wait', '锁腰进程已存在，等待实时状态；不会再启动第二份'
    if state.get('device', {}).get('vr_connected') is not True:
        return 'wait', '新版采集已就绪，等待 Meta Quest 连接后启用锁腰'
    for key in ('service', 'motors', 'communication', 'mode'):
        if checks.get(key, {}).get('ok') is not True:
            return 'wait', '等待锁腰启动检查通过：' + key
    return 'switch', 'teleop'


class Host:
    def __init__(self, config):
        self.config = config
        self.hash_cache = {}
        self.last_message = None
        self.last_log = 0

    def inventory(self):
        result = {name: {'original': [], 'new': [], 'unknown': []}
                  for name in self.config}
        names = {kind: (name, original) for name, c in self.config.items()
                 for kind, original in ((c['original_name'], True), (Path(c['binary']).name, False))}
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit():
                continue
            try:
                argv = (proc / 'cmdline').read_bytes().split(b'\0')
                basename = Path(argv[0].decode()).name if argv[0] else ''
                if basename not in names:
                    continue
                name, original = names[basename]
                exe = proc / 'exe'
                st = exe.stat()
                key = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
                if key not in self.hash_cache:
                    self.hash_cache[key] = digest(exe)
                expected = self.config[name]['original_sha256' if original else 'sha256']
                group = ('original' if original else 'new') if self.hash_cache[key] == expected else 'unknown'
                result[name][group].append(int(proc.name))
            except (FileNotFoundError, ProcessLookupError):
                continue
        return result

    def factory_windows_ready(self):
        result = subprocess.run(['tmux', 'list-windows', '-t', 'robot_startup',
                                 '-F', '#{window_name}'], check=False,
                                text=True, capture_output=True, timeout=5)
        return result.returncode == 0 and {'server', 'teleop'}.issubset(
            set(result.stdout.splitlines()))

    def snapshot(self):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(URL, timeout=5) as response:
            return json.load(response)

    def publish(self, phase, reason, inventory=None):
        data = {'timestamp': time.time(), 'phase': phase, 'message': reason,
                'inventory': inventory, 'expected': {n: c['sha256'] for n, c in self.config.items()}}
        temporary = RUNTIME / 'status.json.tmp'
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        temporary.chmod(0o644)
        temporary.replace(RUNTIME / 'status.json')
        if (phase, reason) != self.last_message or time.monotonic() - self.last_log > 60:
            print(phase + ': ' + reason, flush=True)
            self.last_message = (phase, reason)
            self.last_log = time.monotonic()

    def switch(self, name, inventory):
        component = self.config[name]
        # Revalidate just before the existing switcher's independent final gates.
        validate(component)
        command = [sys.executable, component['switch'], 'start']
        if name == 'teleop':
            command.append('--operator-ready')  # Explicit standing authorization from installation.
        if not inventory[name]['original']:
            command.append('--recover-stopped')  # Existing switcher requires an idle shell pane.
        result = subprocess.run(command, check=False)
        if result.returncode:
            raise RuntimeError(name + ' 切换未确认完成；保留原有保护，稍后重新检查实际状态')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=CONFIG)
    parser.add_argument('--check', action='store_true', help='只读检查，不切换')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('需要 root 读取厂商进程版本；查看状态可直接 cat /run/zerith-collection-autostart/status.json')
    config = json.loads(args.config.read_text())
    host = Host(config)
    if args.check:
        for component in config.values():
            validate(component)
        inventory = host.inventory()
        print(json.dumps({'decision': decide(config, inventory, host.snapshot()),
                          'inventory': inventory}, ensure_ascii=False, indent=2))
        return
    RUNTIME.mkdir(mode=0o755, parents=True, exist_ok=True)
    with (RUNTIME / 'manager.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            for component in config.values():
                validate(component)
        except Exception as exc:
            host.publish('blocked', str(exc))
            raise
        # There is deliberately no 120-second startup deadline.
        while True:
            try:
                inventory = host.inventory()
                action, detail = decide(config, inventory, host.snapshot())
                if action == 'switch' and not host.factory_windows_ready():
                    action, detail = 'wait', '等待原厂 server 和 teleop 窗口创建完成'
                host.publish(action, detail, inventory)
                if action == 'ready':
                    return  # Handover once per robotd start; do not manage an operator's later rollback.
                if action == 'switch':
                    host.switch(detail, inventory)
            except Exception as exc:
                host.publish('wait', str(exc))
                time.sleep(10)
            time.sleep(5)


if __name__ == '__main__':
    main()
