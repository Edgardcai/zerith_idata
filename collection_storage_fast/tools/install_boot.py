"""Install boot integration; running server/robot is not restarted."""
import os
import shutil
import subprocess
from pathlib import Path

if os.geteuid() != 0:
    raise SystemExit('Run with sudo')
source = Path(__file__).with_name('zerith-storage-fast.service')
target = Path('/etc/systemd/system/zerith-storage-fast.service')
if target.exists() and target.read_bytes() != source.read_bytes():
    raise SystemExit('Existing service differs; refusing to overwrite it')
shutil.copyfile(source, target)
target.chmod(0o644)
subprocess.run(['systemctl', 'daemon-reload'], check=True)
subprocess.run(['systemctl', 'enable', 'zerith-storage-fast.service'], check=True)
subprocess.run(['systemctl', 'start', 'zerith-storage-fast.service'], check=True)
print('Storage boot integration enabled')
