#!/usr/bin/env python3
"""Switch ONLY the factory teleop tmux pane after operator preparation.

Default invocation is read-only. Never stops robotd, Motion_Control or server.
"""
import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from preflight import ROOT, check, snapshot, teleop_pids


def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def trial_pids():
    result=[]
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():continue
        try:
            argv=(p/'cmdline').read_bytes().split(b'\0')
            if argv and argv[0].decode(errors='replace') in (str(ROOT/'runtime/teleop_zero_lock'),'./runtime/teleop_zero_lock'):
                result.append(int(p.name))
        except (FileNotFoundError,PermissionError,ProcessLookupError):pass
    return result


def tmux(*args):
    return subprocess.check_output(['tmux',*args],text=True).strip()


def wait_started(started_at, timeout=45):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        pids=trial_pids()
        try:
            state=json.loads((ROOT/'runtime/status.json').read_text())
            if (state.get('pid') in pids and state.get('timestamp',0)>=started_at
                    and 0<=time.time()-state['timestamp']<3
                    and state.get('version')=='collection-1.0'):
                return state['pid']
        except (OSError,ValueError,TypeError):pass
        if time.time()-started_at>5 and not pids:
            break
        time.sleep(.25)
    output=tmux('capture-pane','-p','-t','robot_startup:teleop','-S','-40')
    raise RuntimeError('锁定扩展未确认启动，请勿初始化。遥操窗口输出：\n'+output)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['plan','start','restore'],nargs='?',default='plan')
    parser.add_argument('--operator-ready',action='store_true')
    parser.add_argument('--allow-pending-save',action='store_true',
                        help='允许采集会话等待或后台保存时切换；不取消保存，不允许正在录制')
    parser.add_argument('--recover-stopped',action='store_true',
                        help='在原厂和扩展遥操均已退出时，恢复启动已验证的扩展')
    args=parser.parse_args()
    state=snapshot();errors=check(state,starting=args.action!='restore',allow_pending_save=args.allow_pending_save)
    report={'action':args.action,'issues':errors,'battery':state['device']['battery'],
            'original_pids':teleop_pids(),'trial_pids':trial_pids(),
            'allow_pending_save':args.allow_pending_save}
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
    if args.action=='plan':return bool(errors)
    if os.geteuid()!=0:raise RuntimeError('请通过 sudo 运行切换脚本')
    if not args.operator_ready:raise RuntimeError('真机操作员准备好后使用 --operator-ready')
    if errors:raise RuntimeError('当前状态不允许切换：'+'；'.join(errors))
    with (ROOT/'runtime/switch.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        panes=tmux('list-panes','-t','robot_startup:teleop','-F','#{pane_id}').splitlines()
        if len(panes)!=1:raise RuntimeError('teleop 窗口布局不符合预期，未执行切换')
        pane=panes[0]
        if args.action=='start':
            manifest=json.loads((ROOT/'runtime/build.json').read_text())
            validated=json.loads((ROOT/'runtime/validated.json').read_text())
            if validated.get('passed') is not True or digest(ROOT/'runtime/teleop_zero_lock') != validated.get('sha256') or validated['sha256']!=manifest['sha256']:
                raise RuntimeError('当前试验包没有通过匹配版本的离线自检')
            if trial_pids():raise RuntimeError('试验程序已经运行')
            originals=teleop_pids()
            if not originals and not args.recover_stopped:
                raise RuntimeError('未找到原厂 teleop；确认已退出后使用 --recover-stopped')
            if not originals:
                pane_command=tmux('display-message','-p','-t',pane,'#{pane_current_command}')
                if pane_command not in ('sh','bash','dash'):
                    raise RuntimeError('恢复窗口不是空闲 shell，未执行切换：'+pane_command)
            if originals and digest('/proc/%d/exe'%originals[0])!=manifest['original_sha256']:
                raise RuntimeError('正在运行的厂商版本与试验包不匹配')
            extra=' --allow-pending-save' if args.allow_pending_save else ''
            command='cd /home/robot/teleop_zero_lock && ./runtime/teleop_zero_lock --live'+extra+'; exec sh'
        else:
            if teleop_pids():raise RuntimeError('原厂 teleop 已经运行，无需恢复')
            if not trial_pids():raise RuntimeError('未发现运行中的试验进程，需人工检查窗口')
            command='cd /opt/robot && ./teleop; exec sh'
        # Recheck immediately before interrupting the teleop pane.
        errors=check(snapshot(),starting=args.action=='start',allow_pending_save=args.allow_pending_save)
        if errors:raise RuntimeError('状态已变化：'+'；'.join(errors))
        tmux('send-keys','-t',pane,'C-c')
        deadline=time.monotonic()+10
        while teleop_pids() or trial_pids():
            if time.monotonic()>deadline:
                raise RuntimeError('遥操未正常退出；未强杀、未启动第二个进程，请检查窗口')
            time.sleep(.2)
        started_at=time.time()
        tmux('respawn-pane','-k','-t',pane,command)
        if args.action=='start':
            print('正在等待锁定扩展状态更新（最多 45 秒）……',flush=True)
            pid=wait_started(started_at)
            print('锁定扩展已启动并收到实时状态，PID='+str(pid)+'。可以按 A 初始化；本脚本不会初始化或开始采集。',flush=True)
        else:
            print('已发起原厂程序启动，请检查遥操窗口输出。',flush=True)
        (ROOT/'runtime/last_switch.json').write_text(json.dumps({'timestamp':time.time(),'action':args.action})+'\n')
    return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as e:
        print('未完成切换：'+str(e),file=sys.stderr)
        raise SystemExit(1)
