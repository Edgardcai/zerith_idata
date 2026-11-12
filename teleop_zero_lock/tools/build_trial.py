import argparse, hashlib, json, marshal, sys
from pathlib import Path
from archive import Archive
if sys.version_info[:2] != (3,10):
    raise SystemExit('Build with Python 3.10 to match the factory archive')
root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('--output',default='runtime/teleop_zero_lock')
parser.add_argument('--vendor',type=Path,default=root/'vendor/teleop.original')
parser.add_argument('--source-commit',default=None)
args=parser.parse_args()
output=(root/args.output).resolve()
output.parent.mkdir(parents=True,exist_ok=True)
output_names={str(output)}
if output.is_relative_to(root):output_names.add('./'+str(output.relative_to(root)))
for process in Path('/proc').iterdir():
    if not process.name.isdigit():continue
    try:
        argv=(process/'cmdline').read_bytes().split(b'\0')
        if argv and argv[0].decode(errors='replace') in output_names:
            raise SystemExit('Refusing to overwrite a running executable; build to a candidate filename')
    except (FileNotFoundError,PermissionError,ProcessLookupError):pass
a=Archive(args.vendor)
original_hash=hashlib.sha256(a.path.read_bytes()).hexdigest()
if original_hash != '3c807604a116128c27bcf0ca52310a4102e436c1a277af1c0301b1ef452f7282':
    raise SystemExit('Factory binary version/hash changed; stop and re-audit')
source=(root/'tools/bootstrap.py').read_text()
inputs={'__ORIGINAL_CODE__':a.read('teleop'),
        '__LOGGER_CODE__':marshal.dumps(a.code('processors.logger')),
        '__PATCH_SOURCE__':(root/'patch/zero_lock.py').read_text(),
        '__POSE_CONFIG_SOURCE__':(root/'patch/pose_config.py').read_text(),
        '__POLICY_TEST_SOURCE__':(root/'tests/test_policy.py').read_text(),
        '__MODEL_TEST_SOURCE__':(root/'tests/probe_model.py').read_text(),
        '__PREFLIGHT_SOURCE__':(root/'tools/preflight.py').read_text(),
        '__BOOT_GUARD_SOURCE__':Path('/home/robot/collection_autostart/boot_guard.py').read_text()}
for key,value in inputs.items():source=source.replace(key,repr(value))
digest=a.build(output,{'teleop':marshal.dumps(compile(source,'zero_lock_trial_entry.py','exec'))})
manifest={'original_sha256':original_hash,'sha256':digest,'changed_entries':['teleop'],
          'preserved_entries':len(a.entries)-1,'python':'3.10','version':'collection-1.1-fixed-pose',
          'source_commit':args.source_commit,
          'physical_validation':'pending'}
(output.parent/'build.json').write_text(json.dumps(manifest,indent=2)+'\n')
print(json.dumps(manifest))
