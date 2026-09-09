import hashlib
import json
import logging
import marshal
import os
import sys
import types
from pathlib import Path

ROOT = Path('/home/robot/collection_depth_toggle')
mode = sys.argv[1:]
if mode not in (['--offline-self-test'], ['--live']):
    raise SystemExit('Use --offline-self-test or tools/switch.py')
offline = mode == ['--offline-self-test']
if offline:
    def denied(*args, **kwargs):
        raise RuntimeError('OFFLINE: hardware/network constructor blocked')
    zcm = types.ModuleType('zerocm'); zcm.ZCM = denied
    sys.modules['zerocm'] = zcm
    env = types.ModuleType('real_env')
    class OfflineEnv:
        __init__ = denied
    env.Real_Env = OfflineEnv
    sys.modules['real_env'] = env
    sound = types.ModuleType('AudioTrack'); sound.play_text = denied
    sys.modules['AudioTrack'] = sound
    log = types.ModuleType('log'); log.__path__ = []
    logger = types.ModuleType('log.logger'); logger.logger = logging.getLogger('storage-offline')
    log.logger = logger; sys.modules['log'] = log; sys.modules['log.logger'] = logger
    logging.basicConfig(level=logging.WARNING)
else:
    if os.geteuid() != 0:
        raise SystemExit('Use the root-owned factory tmux server pane')
    import urllib.request
    with urllib.request.urlopen('http://127.0.0.1:8090/api/status', timeout=5) as response:
        state = json.load(response)
    checks = {c['key']: c for c in state['device']['checks']}
    if (checks.get('init', {}).get('detail') != '反初始化完成'
            or state['collection']['phase'] not in ('idle', 'closed')
            or state['collection'].get('current')):
        raise SystemExit('Close collection and deinitialize before starting storage server')
    for proc in Path('/proc').iterdir():
        if proc.name.isdigit():
            try:
                if (proc/'comm').read_text().strip() == 'server':
                    raise SystemExit('Factory server still running')
            except (OSError, PermissionError):
                pass
    marker = json.loads((ROOT/'runtime/validated.json').read_text())
    digest = hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
    if marker.get('sha256') != digest or not marker.get('passed'):
        raise SystemExit('Exact binary not validated')

import RsRecorder
rgb = types.ModuleType('storage_rgb_mode')
sys.modules[rgb.__name__] = rgb
exec(__RGB_SOURCE__, rgb.__dict__)
rgb.install_cameras(RsRecorder)
if offline:
    # Imports are harmless; block actual camera constructors in self-tests.
    RsRecorder.rs.context = denied
    RsRecorder.rs.pipeline = denied
import collect_data
import real_env
rgb.install_collector(collect_data, real_env)
patch = types.ModuleType('storage_parallel_patch')
exec(__PATCH_SOURCE__, patch.__dict__)
context, original_batch = patch.install(collect_data, workers=3)
if offline:
    exec(__SELF_TEST_SOURCE__, globals())
    context.close()
    digest = hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
    (ROOT/'runtime/validated.json').write_text(json.dumps({'passed': True, 'sha256': digest})+'\n')
    print('EXACT_BINARY_OFFLINE_VALIDATED', digest, flush=True)
else:
    print('STORAGE_DEPTH_SELECT_READY: depth recording selectable per session, default off', flush=True)
    sys.argv = sys.argv[:1]
    try:
        vendor = types.ModuleType('storage_vendor_server')
        vendor.__file__ = str(Path(sys._MEIPASS)/'server.py')
        sys.modules[vendor.__name__] = vendor
        exec(marshal.loads(__SERVER_CODE__), vendor.__dict__)
        rgb.install_service(vendor)
        vendor.asyncio.run(vendor.main())
    finally:
        context.close()
