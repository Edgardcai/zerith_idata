"""Activate validated storage optimization after the factory boot is ready."""
import subprocess
import sys
import time

from switch import check, pids

deadline = time.monotonic()+120
last_error = None
while time.monotonic() < deadline:
    if pids('server_fast') and not pids('server'):
        print('Validated storage server already running', flush=True)
        raise SystemExit(0)
    try:
        check()
        if not pids('server'):
            raise RuntimeError('Waiting for factory server startup')
        break
    except Exception as exc:
        last_error = str(exc)
        time.sleep(2)
else:
    raise SystemExit('Storage auto-start not applied: '+str(last_error))
raise SystemExit(subprocess.call([
    sys.executable, '/home/robot/collection_storage_fast/tools/switch.py', 'start']))
