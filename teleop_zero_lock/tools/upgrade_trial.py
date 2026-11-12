#!/usr/bin/env python3
"""Install a self-tested bundle only while collection and initialization are idle."""
import argparse,fcntl,json,os,tempfile,time,urllib.request
from pathlib import Path
from preflight import ROOT,check,snapshot,teleop_pids
from switch_trial import digest,tmux,trial_pids,wait_started

AUTOSTART_CONFIG=Path('/etc/zerith-collection-autostart.json')

def idle_boot_check(state,policy,init,now):
    """A program-only restart of a proven inactive controller; no robot reset."""
    errors=[]
    checks={v['key']:v for v in state['device']['checks']}
    for key in ('service','communication','mode'):
        if checks.get(key,{}).get('ok') is not True:errors.append('检查未通过：'+key)
    collection=state['collection']
    if collection.get('phase') not in ('idle','closed') or collection.get('current'):
        errors.append('请先结束采集会话')
    if init.get('fresh') is not True or init.get('system_init') not in (0,4,5):
        errors.append('机器人未确认处于未初始化、反初始化完成或错误状态')
    operator=policy.get('operator',{})
    if (not 0<=now-policy.get('timestamp',0)<2 or policy.get('active') is not False
            or policy.get('initializing') is not False or operator.get('initialized') is not False
            or operator.get('calibrated') is not False or operator.get('state')!='STOP_TELEOP'
            or operator.get('motor_control_active') is not False or operator.get('control_mode')!=0):
        errors.append('控制器未确认停止，禁止仅启动等待初始化模式')
    return errors

def deployment_check(idle_boot):
    state=snapshot()
    if not idle_boot:return check(state)
    with urllib.request.urlopen('http://127.0.0.1:25120/system_init',timeout=3) as r:init=json.load(r)
    policy=json.loads((ROOT/'runtime/status.json').read_text())
    return idle_boot_check(state,policy,init,time.time())

def atomic_json(path,value):
    mode=path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd,temporary=tempfile.mkstemp(prefix='.release-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(value,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
        os.chmod(temporary,mode);os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--candidate',type=Path,default=ROOT/'runtime/teleop_zero_lock_candidate')
    parser.add_argument('--check',action='store_true',help='Only inspect; do not stop or replace anything')
    parser.add_argument('--idle-boot',action='store_true',help='Restart a proven inactive controller into its boot wait; initialization still requires normal VR/motor checks')
    args=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError('Run as original teleop owner')
    with (ROOT/'runtime/switch.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        errors=deployment_check(args.idle_boot)
        if errors:raise RuntimeError('; '.join(errors))
        candidate=args.candidate.resolve()
        validation=json.loads((candidate.parent/'validated.json').read_text())
        manifest=json.loads((candidate.parent/'build.json').read_text())
        if (validation.get('passed') is not True or digest(candidate)!=validation.get('sha256')
                or manifest.get('sha256')!=validation['sha256']):
            raise RuntimeError('Candidate has not passed its exact-binary offline self-test')
        binary=ROOT/'runtime/teleop_zero_lock'
        if candidate==binary:raise RuntimeError('Candidate must be separate from the running executable')
        old_manifest=json.loads((ROOT/'runtime/build.json').read_text())
        old_validation=json.loads((ROOT/'runtime/validated.json').read_text())
        old_digest=digest(binary)
        if old_digest!=old_manifest['sha256'] or old_digest!=old_validation['sha256']:
            raise RuntimeError('Current executable metadata does not match; leave it running')
        if manifest['original_sha256']!=old_manifest['original_sha256']:
            raise RuntimeError('Factory versions differ')
        boot_config=json.loads(AUTOSTART_CONFIG.read_text()) if AUTOSTART_CONFIG.exists() else None
        if boot_config is not None and (boot_config['teleop']['sha256']!=old_digest
                or boot_config['teleop']['binary']!=str(binary)):
            raise RuntimeError('Autostart does not match the running release')
        if teleop_pids() or not trial_pids():raise RuntimeError('Unexpected active process layout')
        panes=tmux('list-panes','-t','robot_startup:teleop','-F','#{pane_id}').splitlines()
        if len(panes)!=1:raise RuntimeError('Unexpected teleop pane layout')
        print(json.dumps({'checked':True,'old_sha256':old_digest,'new_sha256':manifest['sha256'],
                          'source_commit':manifest.get('source_commit'),'startup_mode':'boot' if args.idle_boot else 'live'}),flush=True)
        if args.check:return
        errors=deployment_check(args.idle_boot)
        if errors:raise RuntimeError('; '.join(errors))
        tmux('send-keys','-t',panes[0],'C-c')
        deadline=time.monotonic()+10
        while trial_pids():
            if time.monotonic()>deadline:raise RuntimeError('Trial did not exit; no force-kill or replacement performed')
            time.sleep(.2)
        atomic_json(ROOT/'runtime/previous-release.json',{'manifest':old_manifest,'validated':old_validation,
                    'autostart':boot_config,'replaced_at':time.time(),'new_sha256':manifest['sha256']})
        binary.replace(ROOT/'runtime/teleop_zero_lock.previous')
        candidate.replace(binary)
        atomic_json(ROOT/'runtime/build.json',manifest)
        atomic_json(ROOT/'runtime/validated.json',validation)
        if boot_config is not None:
            boot_config['teleop']['sha256']=manifest['sha256']
            atomic_json(AUTOSTART_CONFIG,boot_config)
        started=time.time()
        mode='--boot' if args.idle_boot else '--live'
        tmux('respawn-pane','-k','-t',panes[0],'cd /home/robot/teleop_zero_lock && ./runtime/teleop_zero_lock '+mode+'; exec sh')
        pid=wait_started(started)
        print(json.dumps({'upgraded':True,'pid':pid,'sha256':manifest['sha256'],
                          'operator_initialization_required':True}),flush=True)
if __name__=='__main__':main()
