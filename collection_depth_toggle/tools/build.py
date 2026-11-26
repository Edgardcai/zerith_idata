import argparse
import hashlib
import json
import marshal
import sys
from pathlib import Path

if sys.version_info[:2] != (3, 10):
    raise SystemExit('Use Python 3.10')
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root.parent/'teleop_zero_lock/tools'))
from archive import Archive

parser = argparse.ArgumentParser(description='Build a candidate from a matching factory server')
parser.add_argument('--vendor-server', required=True, type=Path)
source = parser.parse_args().vendor_server
expected = '2e311a08afd94990972258bcf13beb46ea30fdd7f45b0b630ca747268838f054'
if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
    raise SystemExit('Factory server SHA-256 does not match the supported version')
a = Archive(source)
text = (root/'tools/bootstrap.py').read_text()
for key, value in {'__PATCH_SOURCE__': (root/'patch.py').read_text(),
                   '__RGB_SOURCE__': (root/'rgb_mode.py').read_text(),
                   '__TIMING_SOURCE__': (root/'timing.py').read_text(),
                   '__TIMING_TEST_SOURCE__': (root/'tests/timing_self_test.py').read_text(),
                   '__AUDIO_CACHE_SOURCE__': (root/'audio_cache.py').read_text(),
                   '__STABILITY_TEST_SOURCE__': (root/'tests/stability_self_test.py').read_text(),
                   '__SELF_TEST_SOURCE__': (root/'tests/self_test.py').read_text(),
                   '__SERVER_CODE__': a.read('server'),
                   '__BOOT_GUARD_SOURCE__': (root.parent/'collection_autostart/boot_guard.py').read_text()}.items():
    text = text.replace(key, repr(value))
(root/'runtime').mkdir(exist_ok=True)
out = root/'runtime/server_fast_candidate'
digest = a.build(out, {'server': marshal.dumps(compile(text, 'storage_fast_entry.py', 'exec'))})
manifest = {'sha256': digest, 'original_sha256': expected, 'image_mode': 'selectable_depth_default_off',
            'changed_entries': ['server'], 'preserved_entries': len(a.entries)-1}
manifest['timing_version'] = 1
manifest['stability_revision'] = 2
(root/'runtime/candidate_build.json').write_text(json.dumps(manifest, indent=2)+'\n')
print(json.dumps(manifest), flush=True)
