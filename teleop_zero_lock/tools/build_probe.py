import sys,marshal,hashlib,json,types
from pathlib import Path
from archive import Archive
root=Path(__file__).resolve().parents[1]
a=Archive(root/'vendor/teleop.original')
original=a.read('teleop')
probe='''import os,sys,types,marshal,inspect,json
if sys.argv[1:] != ['--offline-probe']:
    raise SystemExit('This inspection executable only accepts --offline-probe; no live mode.')
os.environ['JAX_PLATFORMS']='cpu'
import logging.handlers
_real_handler=logging.handlers.RotatingFileHandler
class LocalHandler(_real_handler):
    def __init__(self,filename,*args,**kwargs):
        if str(filename).startswith('/var/log/robot/'):
            filename='/home/robot/teleop_zero_lock/runtime/'+os.path.basename(filename)
        super().__init__(filename,*args,**kwargs)
logging.handlers.RotatingFileHandler=LocalHandler
blocked=[]
class DeniedZCM:
    def __init__(self,*args,**kwargs):
        blocked.append(str(args));raise RuntimeError('OFFLINE: ZCM construction blocked')
stub=types.ModuleType('zerocm');stub.ZCM=DeniedZCM;sys.modules['zerocm']=stub
namespace={'__name__':'vendor_teleop','__file__':os.path.join(sys._MEIPASS,'teleop.py')}
exec(marshal.loads(ORIGINAL),namespace)
from controllers.ZerithCtrl import ZerithCtrl
from processors.ZerithWBC import ZerithWBC
from controllers.HeadCtrl import HeadCtrl
import pyroki,pyroki_snippets,jax_dataclasses,jaxls
print('OFFLINE_IMPORT_SUCCESS',flush=True)
for cls in [namespace['TeleopController'],ZerithCtrl,ZerithWBC,HeadCtrl,pyroki.Robot]:
    print(cls.__name__,str(inspect.signature(cls)),flush=True)
    for name in ['send_control_cmd','send_head_cmd','move_to_cartesian','init','init_home','set_init','get_head_command']:
        if hasattr(cls,name):print(name,str(inspect.signature(getattr(cls,name))),flush=True)
print('Robot annotations',pyroki.Robot.__annotations__,flush=True)
print('JAXLS',hasattr(jaxls,'Cost'),'ZCM constructor attempts',blocked,flush=True)
exec(compile(open('/home/robot/teleop_zero_lock/tests/probe_gravity.py').read(),'probe_model.py','exec'),globals())
'''.replace('ORIGINAL',repr(original))
hash=a.build(root/'runtime/teleop_probe',{'teleop':marshal.dumps(compile(probe,'teleop_offline_probe.py','exec'))})
print(json.dumps({'sha256':hash,'original_sha256':hashlib.sha256(a.path.read_bytes()).hexdigest(),'entry_count':len(a.entries),'changed_entry':'teleop'}))
