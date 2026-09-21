"""One local request slot shared by QC threads and processes."""
from contextlib import contextmanager
from pathlib import Path
import fcntl
import time


LOCK_PATH = Path.home() / '.cache/dataqc/vlm-gateway.lock'


def wait_seconds(seconds, progress, message):
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        progress(f'{message} · 剩余约 {max(1, int(remaining))} 秒')
        time.sleep(min(5, remaining))


@contextmanager
def request_slot(progress):
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Separate open descriptions also serialize threads within one process.
    # The OS releases the lock when a cancelled/killed process exits.
    with LOCK_PATH.open('a') as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                wait_seconds(5, progress, '模型请求排队中：其他质检任务正在调用或等待重试')
        try:
            progress('已获得模型请求名额 · 本机质检共用单路队列')
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
