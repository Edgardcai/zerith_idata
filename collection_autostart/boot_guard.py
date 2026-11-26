"""Embedded gate for first launch by robotd, never a live-switch override."""
import json
import os
from pathlib import Path

RUNTIME = Path('/run/zerith-collection-autostart')


def claim(component):
    if os.geteuid() != 0:
        raise RuntimeError('Boot launch requires root')
    invocation = os.environ.get('INVOCATION_ID')
    if not invocation or '/system.slice/robotd.service' not in Path('/proc/self/cgroup').read_text().splitlines()[0]:
        raise RuntimeError('Boot launch is only allowed inside robotd.service')
    marker = RUNTIME / 'direct-boot.json'
    st = marker.stat()
    if st.st_uid != 0 or st.st_mode & 0o022:
        raise RuntimeError('Boot authorization must be root owned and not writable by others')
    data = json.loads(marker.read_text())
    if data['invocation_id'] != invocation or data['boot_id'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip():
        raise RuntimeError('Boot authorization belongs to another service start')
    # A PyInstaller executable has a parent bootloader process; exclude our
    # ancestor chain, but reject every other old/new instance of this component.
    ancestors = set()
    pid = os.getpid()
    while pid > 0:
        ancestors.add(pid)
        stat = Path('/proc') / str(pid) / 'stat'
        pid = int(stat.read_text().rsplit(')', 1)[1].split()[1])
    names = {'teleop': {'teleop', 'teleop_zero_lock'},
             'collection': {'server', 'server_fast'}}[component]
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit() or int(proc.name) in ancestors:
            continue
        try:
            argv = (proc / 'cmdline').read_bytes().split(b'\0')[0]
            if Path(os.fsdecode(argv)).name in names:
                raise RuntimeError('Another component process is already running: ' + proc.name)
        except (FileNotFoundError, ProcessLookupError):
            continue
    # Only one launch per component per robotd invocation, even after a crash.
    with (RUNTIME / (invocation + '-' + component + '.claimed')).open('x') as stream:
        stream.write(str(os.getpid()))
