"""Supervise web and worker inside the caller's existing resource-control scope."""
import argparse
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def environment(args):
    return dict(os.environ, DATAQC_HOME=str(args.runtime), DATAQC_PORT=str(args.port),
                DATAQC_REAL_ROOT=str(args.real_root), DATAQC_SIM_ROOT=str(args.sim_root),
                PYTHONPATH=os.pathsep.join([str(ROOT), str(ROOT/'vendor')]),
                PATH=os.pathsep.join([str(Path(sys.executable).parent), str(args.binary_path), '/usr/local/bin', '/usr/bin', '/bin']),
                PYTHONUNBUFFERED='1', PYTHONDONTWRITEBYTECODE='1', OMP_NUM_THREADS='1',
                YOLO_CONFIG_DIR=str(args.runtime/'var/yolo-config'))


def supervise(args):
    directory = args.runtime/'var/services'
    directory.mkdir(parents=True, exist_ok=True)
    (args.runtime/'var/yolo-config').mkdir(parents=True, exist_ok=True)
    with (directory/'supervisor.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('服务已经运行')
        stop = False
        def shutdown(*_):
            nonlocal stop
            stop = True
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        pid_file = directory/'supervisor.pid'
        pid_file.write_text(str(os.getpid()))
        env = environment(args)
        commands = {'web': [sys.executable, str(ROOT/'workbench.py'), '--host', '0.0.0.0', '--port', str(args.port)],
                    'worker': [sys.executable, '-m', 'dataqc.worker']}
        processes = {}
        logs = {name: (directory/(name+'.log')).open('ab', buffering=0) for name in commands}
        try:
            while not stop:
                for name, command in commands.items():
                    proc = processes.get(name)
                    if proc is None or proc.poll() is not None:
                        if proc is not None:
                            print(f'{name} exited {proc.returncode}; restarting', flush=True)
                        processes[name] = subprocess.Popen(command, cwd=ROOT, env=env,
                            stdin=subprocess.DEVNULL, stdout=logs[name], stderr=subprocess.STDOUT,
                            start_new_session=True)
                time.sleep(2)
        finally:
            for proc in processes.values():
                if proc.poll() is None:os.killpg(proc.pid, signal.SIGTERM)
            for proc in processes.values():
                try:proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
            for log in logs.values():log.close()
            pid_file.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8091)
    parser.add_argument('--runtime', type=Path, default=ROOT/'runtime')
    parser.add_argument('--real-root', type=Path, default=Path('/data/zerith_data'))
    parser.add_argument('--sim-root', type=Path, default=Path('/data/sim_data'))
    parser.add_argument('--binary-path', type=Path, default=Path('/usr/bin'))
    parser.add_argument('--detach', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:parser.error('端口必须介于 1 和 65535')
    for name in ('runtime','real_root','sim_root','binary_path'):
        setattr(args,name,getattr(args,name).expanduser().resolve())
    if args.detach:
        directory=args.runtime/'var/services'
        directory.mkdir(parents=True,exist_ok=True)
        with (directory/'supervisor.log').open('ab',buffering=0) as log:
            process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()), *[a for a in sys.argv[1:] if a!='--detach']],
                stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        time.sleep(1)
        if process.poll() is not None:raise RuntimeError('启动失败，请检查 supervisor.log')
        print(f'服务监督进程 PID {process.pid}')
    else:
        supervise(args)


if __name__=='__main__':main()
