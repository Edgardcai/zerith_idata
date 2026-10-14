"""FIFO, process-safe request slots and shared account-rate-limit cooldown."""
from contextlib import contextmanager
from pathlib import Path
import fcntl
import json
import os
import time
import uuid

LOCK_PATH = Path(os.environ.get('DATAQC_GATEWAY_LOCK', str(Path.home() / '.cache/dataqc/vlm-gateway.lock')))
MAX_SLOTS = 4


def wait_seconds(seconds, progress, message):
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        progress(f'{message} · 剩余约 {max(1, int(remaining))} 秒')
        time.sleep(min(5, remaining))


@contextmanager
def _guard():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with Path(str(LOCK_PATH) + '.guard').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _state():
    try:
        return json.loads(Path(str(LOCK_PATH) + '.state').read_text())
    except (OSError, ValueError):
        return {}


def cooldown(seconds, *, concurrency_limited=False):
    """Account-wide throttling applies to all local callers, even during retries."""
    with _guard():
        state = _state()
        state['until'] = max(state.get('until', 0), time.time() + seconds)
        if concurrency_limited:
            state['single_until'] = max(state.get('single_until', 0), time.time() + max(300, seconds))
        Path(str(LOCK_PATH) + '.state').write_text(json.dumps(state))


def _tickets(folder, own):
    live = []
    for path in sorted(folder.glob('*.ticket')):
        if path == own:
            live.append(path)
            continue
        with path.open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                live.append(path)
            else:
                # A killed waiter leaves a file but never an OS lock.
                path.unlink(missing_ok=True)
    return live


@contextmanager
def request_slot(progress, slots=1):
    slots = max(1, min(MAX_SLOTS, int(slots)))
    folder = Path(str(LOCK_PATH) + '.queue')
    ticket_handle = None
    slot_handle = None
    ticket = None
    started = time.monotonic()
    next_log = 0
    try:
        with _guard():
            folder.mkdir(exist_ok=True)
            ticket = folder / f'{time.time_ns():020d}-{uuid.uuid4().hex}.ticket'
            ticket_handle = ticket.open('x')
            fcntl.flock(ticket_handle, fcntl.LOCK_EX)
        while slot_handle is None:
            with _guard():
                state = _state()
                remaining = max(0, state.get('until', 0) - time.time())
                capacity = 1 if state.get('single_until', 0) > time.time() else slots
                tickets = _tickets(folder, ticket)
                position = tickets.index(ticket) + 1
                if not remaining and position == 1:
                    free = []
                    busy = 0
                    # Count every slot when an account throttle reduces capacity.
                    for index in range(MAX_SLOTS):
                        path = LOCK_PATH if index == 0 else Path(str(LOCK_PATH) + f'.slot{index}')
                        handle = path.open('a')
                        try:
                            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            busy += 1
                            handle.close()
                        else:
                            free.append(handle)
                    if busy < capacity and free:
                        slot_handle = free.pop(0)
                        ticket.unlink()
                    for handle in free:
                        handle.close()
            if slot_handle is None:
                if time.monotonic() >= next_log:
                    progress(f'模型请求排队 · 队列位置 {position} · 已等待 {time.monotonic()-started:.1f} 秒'
                             + (f' · 账户冷却剩余 {remaining:.0f} 秒' if remaining else ''))
                    next_log = time.monotonic() + 10
                time.sleep(.1)
        progress(f'已获得模型请求名额 · 本机最多 {capacity} 路 · 排队 {time.monotonic()-started:.1f} 秒')
        yield
    finally:
        if slot_handle is not None:
            slot_handle.close()
        if ticket is not None:
            with _guard():
                ticket.unlink(missing_ok=True)
                if ticket_handle is not None:
                    ticket_handle.close()
