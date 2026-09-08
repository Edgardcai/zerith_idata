"""This source is embedded into the copied PyInstaller entry, never imported."""
import os, sys, types, marshal, json, hashlib, logging.handlers
from pathlib import Path
ROOT = Path('/home/robot/teleop_zero_lock')
mode = sys.argv[1:]
if mode not in (['--offline-self-test'], ['--live']):
    raise SystemExit('Use --offline-self-test, or the documented operator trial switch procedure.')
offline = mode == ['--offline-self-test']
blocked = []
if offline:
    class DeniedZCM:
        def __init__(self, *args, **kwargs):
            blocked.append(str(args))
            raise RuntimeError('OFFLINE: real ZCM construction blocked')
    stub = types.ModuleType('zerocm'); stub.ZCM = DeniedZCM
    sys.modules['zerocm'] = stub
else:
    if os.geteuid() != 0:
        raise SystemExit('Trial requires the original teleop process ownership (root).')
    preflight = types.ModuleType('trial_preflight')
    exec(__PREFLIGHT_SOURCE__, preflight.__dict__)
    errors = preflight.check(preflight.snapshot())
    if preflight.teleop_pids():
        errors.append('Original teleop still running; refusing a second command publisher')
    marker = json.loads((ROOT/'runtime/validated.json').read_text())
    with open(sys.executable,'rb') as executable:
        digest=hashlib.file_digest(executable,'sha256').hexdigest() if hasattr(hashlib,'file_digest') else hashlib.sha256(executable.read()).hexdigest()
    if marker.get('sha256') != digest or marker.get('passed') is not True:
        errors.append('This exact binary has not passed its offline self-test')
    if errors:
        raise SystemExit('; '.join(errors))

os.environ['JAX_PLATFORMS'] = 'cpu'
(ROOT/'runtime/logs').mkdir(exist_ok=True)
# Redirect only the factory logger module's explicit directory constant.
# Its rotation/retention/symlink operations must all stay inside the trial directory.
import processors
logger_code=marshal.loads(__LOGGER_CODE__)
assert logger_code.co_consts.count('/var/log/robot/teleop') == 1
logger_code=logger_code.replace(co_consts=tuple(str(ROOT/'runtime/logs') if c == '/var/log/robot/teleop' else c for c in logger_code.co_consts))
logger_module=types.ModuleType('processors.logger')
logger_module.__file__=str(Path(sys._MEIPASS)/'processors/logger.py')
logger_module.__package__='processors'
sys.modules['processors.logger']=logger_module
processors.logger=logger_module
exec(logger_code,logger_module.__dict__)
assert logger_module.LOG_DIR == str(ROOT/'runtime/logs')
namespace = {'__name__':'vendor_teleop', '__file__':str(Path(sys._MEIPASS)/'teleop.py')}
exec(marshal.loads(__ORIGINAL_CODE__), namespace)
import jax
jax.config.update('jax_compilation_cache_dir', str(ROOT/'runtime/jax_cache'))
patch = types.ModuleType('zero_lock');patch.__file__=str(ROOT/'patch/zero_lock.py')
sys.modules['zero_lock'] = patch
exec(__PATCH_SOURCE__, patch.__dict__)
if offline:
    import unittest
    testmod=types.ModuleType('test_policy');testmod.__file__=str(ROOT/'tests/test_policy.py')
    exec(__POLICY_TEST_SOURCE__,testmod.__dict__)
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(testmod))
    if not result.wasSuccessful():raise SystemExit(1)
    from controllers.ZerithCtrl import ZerithCtrl
    from processors.ZerithWBC import ZerithWBC
    from controllers.HeadCtrl import HeadCtrl
    exec(__MODEL_TEST_SOURCE__,globals())
    if blocked: raise RuntimeError('Unexpected real ZCM construction attempt')
    with open(sys.executable,'rb') as executable:
        digest=hashlib.sha256(executable.read()).hexdigest()
    (ROOT/'runtime/validated.json').write_text(json.dumps({'sha256':digest,'passed':True,'unit_tests':result.testsRun})+'\n')
    print('EXACT_BINARY_OFFLINE_VALIDATED',digest,flush=True)
else:
    def initialization_check():
        state=preflight.snapshot()
        device=state['device']
        checks={item['key']:item for item in device['checks']}
        if not device.get('vr_connected') or any(not checks.get(k,{}).get('ok') for k in ('service','motors','communication','mode')):
            raise RuntimeError('VR、通信或电机状态检查未通过')
    def config_loader():
        path=ROOT/'config.json'
        return json.loads(path.read_text()) if path.exists() else {'lift_enabled':False,'lift_height_m':.4}
    policy,_ = patch.install(namespace, ROOT/'runtime/status.json', initialization_check, config_loader)
    print('ZERO_LOCK_TRIAL: awaiting operator initialization; target zero is NOT proof of actual zero.',flush=True)
    node=namespace['TeleopController'](initial_teleop_height=float(os.environ.get('TELEOP_INITIAL_HEIGHT','1.85')))
    try:
        node.start()
    finally:
        policy.disarm()
        policy.last_write=-float('inf')
        policy.write_status()
