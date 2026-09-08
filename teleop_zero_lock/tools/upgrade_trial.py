#!/usr/bin/env python3
"""Replace a stopped trial executable with its validated candidate."""
import fcntl,json,os,time
from preflight import ROOT,check,snapshot,teleop_pids
from switch_trial import digest,tmux,trial_pids

def main():
    if os.geteuid()!=0:raise RuntimeError('Run as original teleop owner')
    with (ROOT/'runtime/switch.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        errors=check(snapshot())
        if errors:raise RuntimeError('; '.join(errors))
        candidate=ROOT/'runtime/teleop_zero_lock_candidate'
        validation=json.loads((ROOT/'runtime/validated.json').read_text())
        if not validation.get('passed') or digest(candidate)!=validation['sha256']:
            raise RuntimeError('Candidate has not passed its offline self-test')
        if teleop_pids() or not trial_pids():raise RuntimeError('Unexpected active process layout')
        panes=tmux('list-panes','-t','robot_startup:teleop','-F','#{pane_id}').splitlines()
        if len(panes)!=1:raise RuntimeError('Unexpected teleop pane layout')
        tmux('send-keys','-t',panes[0],'C-c')
        deadline=time.monotonic()+10
        while trial_pids():
            if time.monotonic()>deadline:raise RuntimeError('Trial did not exit; no force-kill or replacement performed')
            time.sleep(.2)
        (ROOT/'runtime/teleop_zero_lock').replace(ROOT/'runtime/teleop_zero_lock.previous')
        candidate.replace(ROOT/'runtime/teleop_zero_lock')
        tmux('respawn-pane','-k','-t',panes[0],'cd /home/robot/teleop_zero_lock && ./runtime/teleop_zero_lock --live; exec sh')
        print('Validated trial upgraded; wait for startup before operator initialization.',flush=True)
if __name__=='__main__':main()
