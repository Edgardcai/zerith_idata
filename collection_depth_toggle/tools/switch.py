"""Switch only the factory camera/collection server while robot is deinitialized."""
import argparse
import hashlib
import json
import os
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path('/home/robot/collection_depth_toggle')
TARGET = 'robot_startup:server'

def tmux(*args):
    return subprocess.check_output(['tmux', *args], text=True).strip()

def pids(kind):
    result = []
    for path in Path('/proc').iterdir():
        if not path.name.isdigit(): continue
        try:
            argv = (path/'cmdline').read_bytes().split(b'\0')[0].decode()
            if Path(argv).name == kind: result.append(int(path.name))
        except (OSError, UnicodeError): pass
    return result

def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''): h.update(chunk)
    return h.hexdigest()

def state():
    with urllib.request.urlopen('http://127.0.0.1:8090/api/status', timeout=5) as response:
        return json.load(response)

def check():
    s = state()
    checks = {c['key']: c for c in s['device']['checks']}
    if checks.get('init', {}).get('detail') != '反初始化完成':
        raise RuntimeError('请先反初始化')
    if s['collection']['phase'] not in ('idle', 'closed') or s['collection'].get('current'):
        raise RuntimeError('请先结束录制和采集会话')

def stop_pane(pane):
    completion = '所有服务关闭完毕，安全退出。'
    before = tmux('capture-pane', '-p', '-t', pane, '-S', '-')
    previous_completions = ''.join(before.split()).count(completion)
    tmux('send-keys', '-t', pane, 'C-c')
    end = time.monotonic()+25
    while pids('server') or pids('server_fast'):
        if time.monotonic() > end:
            output = tmux('capture-pane', '-p', '-t', pane, '-S', '-')
            compact = ''.join(output.split())
            if compact.count(completion) <= previous_completions:
                raise RuntimeError('服务未完成资源清理，未强杀；请检查 server 窗口')
            check()
            # The factory has closed RPC, all camera pipelines and file services.
            # Only its interpreter shutdown remains stuck. Reap those exact
            # processes, never robotd, Motion_Control, SDKService or teleop.
            print('原厂已确认全部服务关闭，清理卡在解释器退出阶段的进程。', flush=True)
            for pid in sorted(pids('server')+pids('server_fast'), reverse=True):
                try: os.kill(pid, signal.SIGKILL)
                except ProcessLookupError: pass
            end = time.monotonic()+5
            while pids('server') or pids('server_fast'):
                if time.monotonic() > end: raise RuntimeError('退出残留进程尚未消失')
                time.sleep(.1)
            return
        time.sleep(.25)

def probe():
    code = """import sys
sys.path.insert(0, '/home/robot/collection_web')
from protocol import Vendor
v=Vendor(); stream=v.devices(timeout=4)
try:
    result=next(stream)
    assert result.json_data
finally:
    stream.cancel(); v.close()
"""
    result = subprocess.run(['/home/robot/collection_web/.venv/bin/python', '-c', code],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6)
    return result.returncode == 0

def server_pane():
    if 'server' not in tmux('list-windows', '-t', 'robot_startup', '-F', '#{window_name}').splitlines():
        return None
    panes = tmux('list-panes', '-t', TARGET, '-F', '#{pane_id}').splitlines()
    if len(panes) != 1: raise RuntimeError('server 窗口不是单一 pane')
    return panes[0]

def launch(command):
    pane = server_pane()
    if pane is None:
        tmux('new-window', '-d', '-t', 'robot_startup', '-n', 'server', command)
    else:
        tmux('respawn-pane', '-k', '-t', pane, command)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['plan', 'start', 'restore'], default='plan', nargs='?')
    parser.add_argument('--recover-stopped', action='store_true')
    parser.add_argument('--replace-fast', action='store_true')
    args = parser.parse_args()
    check()
    print(json.dumps({'action': args.action, 'original_pids': pids('server'),
                      'fast_pids': pids('server_fast')}, ensure_ascii=False), flush=True)
    if args.action == 'plan': return
    if os.geteuid() != 0: raise RuntimeError('请通过 sudo 执行')
    import fcntl
    with (ROOT/'runtime/switch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pane = server_pane()
        if args.action == 'start':
            manifest = json.loads((ROOT/'runtime/build.json').read_text())
            marker = json.loads((ROOT/'runtime/validated.json').read_text())
            if not marker.get('passed') or digest(ROOT/'runtime/server_fast') != marker['sha256'] or marker['sha256'] != manifest['sha256']:
                raise RuntimeError('运行包与已通过自检的版本不一致')
            old_fast = pids('server_fast')
            if old_fast:
                previous = Path('/home/robot/collection_rgb_only/runtime')
                previous_marker = json.loads((previous/'validated.json').read_text())
                allowed = {previous_marker['sha256']} if previous_marker.get('passed') else set()
                for saved in (ROOT/'runtime/releases').glob('*/validated.json'):
                    release = json.loads(saved.read_text())
                    if release.get('passed') and digest(saved.parent/'server_fast') == release['sha256']:
                        allowed.add(release['sha256'])
                if (not args.replace_fast or not previous_marker.get('passed') or
                        any(digest('/proc/%d/exe' % pid) not in allowed for pid in old_fast)):
                    raise RuntimeError('仅允许替换已验证的 RGB-only 服务')
            old = pids('server')
            if not old and not old_fast and not args.recover_stopped:
                raise RuntimeError('原厂已退出；恢复启动请使用 --recover-stopped')
            if old and digest('/proc/%d/exe' % old[0]) != manifest['original_sha256']:
                raise RuntimeError('原厂运行版本不匹配')
            command = 'cd /opt/robot && /home/robot/collection_depth_toggle/runtime/server_fast --live; exec sh'
        else:
            if pids('server') or not pids('server_fast'):
                raise RuntimeError('当前不是优化服务，未执行恢复')
            command = 'cd /opt/robot && ./server; exec sh'
        check()
        if pids('server') or pids('server_fast'):
            if pane is None: raise RuntimeError('服务存在但 server 窗口丢失，未切换')
            stop_pane(pane)
        elif pane and tmux('display-message', '-p', '-t', pane, '#{pane_current_command}') not in ('sh', 'bash', 'dash'):
            raise RuntimeError('server 窗口不是空闲 shell')
        launch(command)
        end = time.monotonic()+40
        expected = 'server_fast' if args.action == 'start' else 'server'
        while time.monotonic() < end:
            if pids(expected) and probe():
                print('采集服务已启动，50051 状态流验证通过。', flush=True)
                (ROOT/'runtime/last_switch.json').write_text(json.dumps(
                    {'at': time.time(), 'action': args.action, 'pids': pids(expected)})+'\n')
                return
            time.sleep(1)
        output = tmux('capture-pane', '-p', '-t', TARGET, '-S', '-60')
        if args.action == 'start':
            pane = server_pane()
            if pids('server_fast') and pane: stop_pane(pane)
            fallback = ('/home/robot/collection_rgb_only/runtime/server_fast --live'
                        if old_fast else './server')
            launch('cd /opt/robot && '+fallback+'; exec sh')
            raise RuntimeError('优化服务启动未通过，已发起原厂服务恢复。窗口输出：\n'+output)
        raise RuntimeError('原厂服务恢复未确认，请检查：\n'+output)

if __name__ == '__main__':
    main()
