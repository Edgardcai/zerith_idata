import fcntl
import multiprocessing
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from dataqc import gateway_queue, vision


def _hold_slot(path, entered, release):
    gateway_queue.LOCK_PATH = path
    with gateway_queue.request_slot(lambda _: None):
        entered.set()
        release.wait(10)


def test_process_lock_and_release_on_termination(tmp_path, monkeypatch):
    path = tmp_path / 'queue.lock'
    monkeypatch.setattr(gateway_queue, 'LOCK_PATH', path)
    ctx = multiprocessing.get_context('spawn')
    entered, release = ctx.Event(), ctx.Event()
    process = ctx.Process(target=_hold_slot, args=(path, entered, release))
    process.start()
    try:
        assert entered.wait(10)
        with path.open('a') as handle:
            with pytest.raises(BlockingIOError):
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        process.terminate()
        process.join(10)
        with gateway_queue.request_slot(lambda _: None):
            pass
    finally:
        if process.is_alive():
            process.terminate()
        process.join(10)


def test_threads_share_single_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_queue, 'LOCK_PATH', tmp_path / 'queue.lock')
    monkeypatch.setattr(gateway_queue, 'wait_seconds', lambda *a: time.sleep(.01))
    active = 0
    peak = 0
    barrier = threading.Barrier(3)

    def task():
        nonlocal active, peak
        barrier.wait()
        with gateway_queue.request_slot(lambda _: None):
            active += 1
            peak = max(peak, active)
            time.sleep(.03)
            active -= 1

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: task(), range(3)))
    assert peak == 1


def mock_gateway(monkeypatch, tmp_path, responses):
    monkeypatch.setattr(gateway_queue, 'LOCK_PATH', tmp_path / 'queue.lock')
    calls = []
    def handle(request):
        calls.append(request)
        return responses.pop(0)
    original = httpx.Client
    monkeypatch.setattr(vision.httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(handle)))
    return calls


def limited(code='gateway_concurrency_limit', headers=None):
    return httpx.Response(429, headers=headers, json={'error': {'code': code}})


def test_retry_holds_shared_slot_then_succeeds(tmp_path, monkeypatch):
    calls = mock_gateway(monkeypatch, tmp_path, [limited(), limited(), httpx.Response(200, json={'status': 'completed'})])
    delays = []
    def wait(delay, progress, message):
        delays.append(delay)
        with gateway_queue.LOCK_PATH.open('a') as handle:
            with pytest.raises(BlockingIOError):
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(vision, 'wait_seconds', wait)
    result = vision._request_vlm('http://test/v1', 'test', {}, {'api_timeout': 1, 'api_attempts': 1}, lambda _: None)
    assert result.status_code == 200 and len(calls) == 3
    assert delays == [30, 60]


def test_quota_is_not_retried(tmp_path, monkeypatch):
    calls = mock_gateway(monkeypatch, tmp_path, [limited('insufficient_quota')])
    monkeypatch.setattr(vision, 'wait_seconds', lambda *a: pytest.fail('Quota must not retry'))
    with pytest.raises(vision.APIUnavailable, match='额度'):
        vision._request_vlm('http://test/v1', 'test', {}, {'api_timeout': 1}, lambda _: None)
    assert len(calls) == 1


def test_retry_exhaustion_is_explicit(tmp_path, monkeypatch):
    calls = mock_gateway(monkeypatch, tmp_path, [limited(headers={'retry-after': '45'}) for _ in range(3)])
    delays = []
    monkeypatch.setattr(vision, 'wait_seconds', lambda seconds, *a: delays.append(seconds))
    with pytest.raises(vision.RateLimited, match='重试已达上限'):
        vision._request_vlm('http://test/v1', 'test', {}, {'api_timeout': 1, 'api_rate_limit_retries': 2}, lambda _: None)
    assert delays == [45, 45] and len(calls) == 3


def test_pause_during_backoff_releases_slot(tmp_path, monkeypatch):
    from dataqc.worker import Paused
    mock_gateway(monkeypatch, tmp_path, [limited()])
    def progress(message):
        if '自动重试' in message:
            raise Paused()
    with pytest.raises(Paused):
        vision._request_vlm('http://test/v1', 'test', {}, {'api_timeout': 1}, progress)
    with gateway_queue.request_slot(lambda _: None):
        pass
