"""Restart both services after active jobs finish, without rewriting settings."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path(os.environ.get('DATAQC_HOME', ROOT / 'runtime'))
STATUS = RUNTIME / 'var/deployment.json'
BASE = os.environ.get('DATAQC_SERVICE_URL', 'http://127.0.0.1:9990')


def get(path):
    with urlopen(BASE + path, timeout=15) as response:
        return json.load(response)


def active_jobs():
    return ([dict(id=j['id'], kind='collection', status=j['status']) for j in get('/api/jobs')['jobs']
             if j['status'] not in ('completed', 'failed', 'stopped')] +
            [dict(id=j['id'], kind='automatic', status=j['status']) for j in get('/auto/api/runs')
             if j['status'] in ('queued', 'running', 'retry_wait')])


def ready():
    return get('/healthz').get('ok') and get('/auto/api/health').get('worker_alive')


def record(status, **detail):
    value = dict(status=status, time=time.time(), **detail)
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    tmp.replace(STATUS)
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main(wait_seconds=21600):
    deadline = time.monotonic() + wait_seconds
    previous = None
    while time.monotonic() < deadline:
        jobs = active_jobs()
        if jobs:
            if jobs != previous:
                record('waiting_for_idle', active_jobs=jobs)
            previous = jobs
            time.sleep(10)
            continue
        time.sleep(2)
        if active_jobs():
            continue
        record('restarting')
        subprocess.run(['systemctl', '--user', 'restart', 'dataqc-web', 'dataqc-worker'], check=True)
        for _ in range(30):
            time.sleep(1)
            try:
                if ready():
                    subprocess.run(['systemctl', '--user', 'is-active', '--quiet', 'dataqc-web', 'dataqc-worker'], check=True)
                    record('active')
                    return
            except (OSError, ValueError):
                pass
        raise RuntimeError('服务重启后健康检查未通过')
    record('timeout')
    raise RuntimeError('等待服务空闲超时，未停止运行中的任务')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--wait-seconds', type=int, default=21600)
    args = parser.parse_args()
    if args.check_only:
        print(json.dumps(dict(ready=ready(), active_jobs=active_jobs()), ensure_ascii=False))
    else:
        main(args.wait_seconds)
