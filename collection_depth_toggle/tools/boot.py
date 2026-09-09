"""Start the validated RGB-only server after safe factory startup."""
import json
import subprocess
import sys
import time
from switch import ROOT, check, digest, pids

deadline = time.monotonic()+120
last_error = None
while time.monotonic() < deadline:
    fast = pids('server_fast')
    if fast:
        expected = json.loads((ROOT/'runtime/validated.json').read_text())['sha256']
        if all(digest('/proc/%d/exe' % pid) == expected for pid in fast):
            print('Validated RGB-only server already running', flush=True)
            raise SystemExit(0)
    try:
        check()
        if not pids('server') and not fast:
            raise RuntimeError('Waiting for factory startup')
        break
    except Exception as exc:
        last_error = str(exc)
        time.sleep(2)
else:
    raise SystemExit('RGB-only startup not applied: '+str(last_error))
raise SystemExit(subprocess.call([sys.executable, str(ROOT/'tools/switch.py'),
                                 'start', '--replace-fast']))
