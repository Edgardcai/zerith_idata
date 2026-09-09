"""Deploy only after operator ends collection and deinitializes the robot."""
import json
import os
import pwd
import subprocess
import sys
import urllib.request
from pathlib import Path
from switch import ROOT, check, digest

if os.geteuid() != 0:
    raise SystemExit('Run with sudo')
check()
with urllib.request.urlopen('http://127.0.0.1:8080/api/state', timeout=5) as response:
    state = json.load(response)
if state.get('busy') or state.get('replay', {}).get('active') or state.get('pi05', {}).get('active'):
    raise SystemExit('8080 still has an active operation')
binary = ROOT/'runtime/server_fast'
marker = json.loads((ROOT/'runtime/validated.json').read_text())
if not marker.get('passed') or digest(binary) != marker['sha256']:
    raise SystemExit('Exact binary not validated')
boot_override = Path('/etc/systemd/system/zerith-storage-fast.service.d/rgb-only.conf')
boot_text = '[Service]\nExecStart=\nExecStart=/usr/bin/python3 /home/robot/collection_rgb_only/tools/boot.py\n'
user_override = Path('/home/robot/.config/systemd/user/zerith-h1-web-control.service.d/rgb-only.conf')
user_text = '[Service]\nEnvironment=ZERITH_CAMERA_DEPTH=0\n'
for path, text in [(boot_override, boot_text), (user_override, user_text)]:
    if path.exists() and path.read_text() != text:
        raise SystemExit('Refusing to overwrite an unrelated override: '+str(path))
subprocess.run([sys.executable, str(ROOT/'tools/switch.py'), 'start', '--replace-fast'], check=True)
for path, text in [(boot_override, boot_text), (user_override, user_text)]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o644)
account = pwd.getpwnam('robot')
os.chown(user_override.parent, account.pw_uid, account.pw_gid)
os.chown(user_override, account.pw_uid, account.pw_gid)
subprocess.run(['systemctl', 'daemon-reload'], check=True)
def user_systemctl(*args):
    subprocess.run(['runuser', '-u', 'robot', '--', 'env',
                    'XDG_RUNTIME_DIR=/run/user/'+str(account.pw_uid),
                    'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/'+str(account.pw_uid)+'/bus',
                    'systemctl', '--user', *args], check=True)
user_systemctl('daemon-reload')
user_systemctl('restart', 'zerith-h1-web-control.service', 'zerith-collection-web.service')
print('RGB-only collection and preview deployed; boot override installed.', flush=True)
