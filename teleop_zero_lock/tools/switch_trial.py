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


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['plan','start','restore'],nargs='?',default='plan')
    parser.add_argument('--operator-ready',action='store_true')
    args=parser.parse_args()
    state=snapshot();errors=check(state,starting=args.action!='restore')
    report={'action':args.action,'issues':errors,'battery':state['device']['battery'],
            'original_pids':teleop_pids(),'trial_pids':trial_pids()}
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
            if not originals:raise RuntimeError('未找到原厂 teleop，需先人工检查')
            if digest('/proc/%d/exe'%originals[0])!=manifest['original_sha256']:
                raise RuntimeError('正在运行的厂商版本与试验包不匹配')
            command='cd /home/robot/teleop_zero_lock && ./runtime/teleop_zero_lock --live; exec sh'
        else:
            if teleop_pids():raise RuntimeError('原厂 teleop 已经运行，无需恢复')
            if not trial_pids():raise RuntimeError('未发现运行中的试验进程，需人工检查窗口')
            command='cd /opt/robot && ./teleop; exec sh'
        # Recheck immediately before interrupting the teleop pane.
        errors=check(snapshot(),starting=args.action=='start')
        if errors:raise RuntimeError('状态已变化：'+'；'.join(errors))
        tmux('send-keys','-t',pane,'C-c')
        deadline=time.monotonic()+10
        while teleop_pids() or trial_pids():
            if time.monotonic()>deadline:
                raise RuntimeError('遥操未正常退出；未强杀、未启动第二个进程，请检查窗口')
            time.sleep(.2)
        tmux('respawn-pane','-k','-t',pane,command)
        print('已启动目标程序。请等待启动检查完成后再按 A；本脚本不会初始化或开始采集。',flush=True)
        (ROOT/'runtime/last_switch.json').write_text(json.dumps({'timestamp':time.time(),'action':args.action})+'\n')
    return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as e:
        print('未完成切换：'+str(e),file=sys.stderr)
        raise SystemExit(1)
