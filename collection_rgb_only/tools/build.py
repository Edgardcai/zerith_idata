import hashlib
import json
import marshal
import sys
from pathlib import Path

if sys.version_info[:2] != (3, 10):
    raise SystemExit('Use Python 3.10')
sys.path.insert(0, '/home/robot/teleop_zero_lock/tools')
from archive import Archive

root = Path('/home/robot/collection_rgb_only')
source = Path('/home/robot/collection_web/research/replay_jitter_20260909/server.original')
expected = '2e311a08afd94990972258bcf13beb46ea30fdd7f45b0b630ca747268838f054'
assert hashlib.sha256(source.read_bytes()).hexdigest() == expected
a = Archive(source)
text = (root/'tools/bootstrap.py').read_text()
for key, value in {'__PATCH_SOURCE__': (root/'patch.py').read_text(),
                   '__RGB_SOURCE__': (root/'rgb_mode.py').read_text(),
                   '__SELF_TEST_SOURCE__': (root/'tests/self_test.py').read_text(),
                   '__SERVER_CODE__': a.read('server')}.items():
    text = text.replace(key, repr(value))
out = root/'runtime/server_fast_candidate'
digest = a.build(out, {'server': marshal.dumps(compile(text, 'storage_fast_entry.py', 'exec'))})
manifest = {'sha256': digest, 'original_sha256': expected, 'image_mode': 'color_only',
            'changed_entries': ['server'], 'preserved_entries': len(a.entries)-1}
(root/'runtime/build.json').write_text(json.dumps(manifest, indent=2)+'\n')
print(json.dumps(manifest), flush=True)
