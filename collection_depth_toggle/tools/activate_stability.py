"""Promote the tested stability binary only while collection is closed/deinitialized."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from switch import ROOT, check, digest


def main():
    verify_only = sys.argv[1:] == ['--verify-only']
    if sys.argv[1:] and not verify_only:
        raise SystemExit('Usage: activate_stability.py [--verify-only]')
    runtime = ROOT/'runtime'
    marker = json.loads((runtime/'candidate_validated.json').read_text())
    manifest = json.loads((runtime/'candidate_build.json').read_text())
    candidate = runtime/'server_fast_candidate'
    sha = digest(candidate)
    if not marker.get('passed') or marker.get('stability_revision') != 2 or manifest.get('stability_revision') != 2 or marker.get('timing_version') != 1 or marker['sha256'] != sha or manifest['sha256'] != sha:
        raise SystemExit('Candidate does not match the exact tested stability binary')
    backup = runtime/'releases/pre_stability_20260918'
    old_marker = json.loads((backup/'validated.json').read_text())
    if not old_marker.get('passed') or digest(backup/'server_fast') != old_marker['sha256']:
        raise SystemExit('Validated rollback binary missing')
    if digest(runtime/'server_fast') != old_marker['sha256']:
        raise SystemExit('Active release changed since backup; refusing to overwrite')
    if verify_only:
        print(json.dumps({'verified': True, 'sha256': sha, 'rollback': str(backup)}), flush=True)
        return
    if os.geteuid() != 0:
        raise SystemExit('Use sudo; this switches only the camera/collection server')
    check()
    owner = (runtime/'server_fast').stat()
    # Keep the candidate as a verified release too, so the existing switch guard
    # can recognize its process if a rollback is required after startup.
    release = runtime/'releases'/('stability_'+sha[:12])
    release.mkdir(exist_ok=True)
    for src, name in ((candidate,'server_fast'), (runtime/'candidate_build.json','build.json'),
                      (runtime/'candidate_validated.json','validated.json')):
        shutil.copy2(src, release/name)
    def promote(source, name):
        tmp = runtime/(name+'.promoting')
        shutil.copy2(source, tmp)
        os.chown(tmp, owner.st_uid, owner.st_gid)
        os.replace(tmp, runtime/name)
    try:
        promote(candidate, 'server_fast')
        promote(runtime/'candidate_build.json', 'build.json')
        promote(runtime/'candidate_validated.json', 'validated.json')
        subprocess.run([sys.executable, str(ROOT/'tools/switch.py'), 'start', '--replace-fast'], check=True)
    except BaseException:
        for name in ('server_fast','build.json','validated.json'):
            promote(backup/name, name)
        # Preserve diagnostics and the prior manifest; do not touch robot control.
        print('Stability activation failed; previous binary and markers restored. Checking safe service recovery.', flush=True)
        try:
            check()
            subprocess.run([sys.executable, str(ROOT/'tools/switch.py'), 'start',
                            '--replace-fast', '--recover-stopped'], check=True)
        except Exception as exc:
            print('Previous service recovery not confirmed: '+str(exc), flush=True)
        raise
    result={'active':True, 'timing_version':1, 'stability_revision':2, 'sha256':sha, 'rollback':str(backup)}
    (runtime/'stability_deployment.json').write_text(json.dumps(result,indent=2)+'\n')
    (runtime/'stability_deployment_pending.json').unlink(missing_ok=True)
    print(json.dumps(result), flush=True)


if __name__=='__main__':
    main()
