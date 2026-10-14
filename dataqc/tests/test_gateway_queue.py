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
    monkeypatch.setattr(vision, 'cooldown', lambda *a, **kw: None)
    def handle(request):
        calls.append(request)
        return responses.pop(0)
    original = httpx.Client
    monkeypatch.setattr(vision.httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(handle)))
    return calls


def limited(code='gateway_concurrency_limit', headers=None):
    return httpx.Response(429, headers=headers, json={'error': {'code': code}})


def test_retry_releases_shared_slot_then_succeeds(tmp_path, monkeypatch):
    calls = mock_gateway(monkeypatch, tmp_path, [limited(), limited(), httpx.Response(200, json={'status': 'completed'})])
    delays = []
    def wait(delay, progress, message):
        delays.append(delay)
        with gateway_queue.LOCK_PATH.open('a') as handle:
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


def test_two_request_slots_overlap_but_never_exceed_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_queue, 'LOCK_PATH', tmp_path / 'queue.lock')
    active = peak = 0
    mutex = threading.Lock()
    def task(i):
        nonlocal active, peak
        with gateway_queue.request_slot(lambda _: None, 2):
            with mutex:
                active += 1
                peak = max(peak, active)
            time.sleep(.15)
            with mutex:
                active -= 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(task, range(4)))
    assert peak == 2


def test_fifo_waiters_do_not_starve(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_queue, 'LOCK_PATH', tmp_path / 'queue.lock')
    order = []
    queued = [threading.Event() for _ in range(3)]
    def task(i):
        with gateway_queue.request_slot(lambda _: queued[i].set()):
            order.append(i)
    with ThreadPoolExecutor(max_workers=3) as pool:
        with gateway_queue.request_slot(lambda _: None):
            futures = []
            for i in range(3):
                futures.append(pool.submit(task, i))
                assert queued[i].wait(5)
        for future in futures:
            future.result(5)
    assert order == [0, 1, 2]


def test_shared_account_cooldown_and_capacity_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_queue, 'LOCK_PATH', tmp_path / 'queue.lock')
    gateway_queue.cooldown(.2, concurrency_limited=True)
    started = time.monotonic()
    log = []
    with gateway_queue.request_slot(log.append, 2):
        assert time.monotonic() - started >= .18
    assert any('最多 1 路' in line for line in log)


def test_stream_requires_terminal_response_and_keeps_usage(tmp_path, monkeypatch):
    import json
    final = dict(id='resp_fixture', status='completed', usage={'total_tokens': 12}, output=[])
    stream = '\n\n'.join('data: ' + json.dumps(e) for e in [
        {'type': 'response.created'},
        {'type': 'response.output_text.delta', 'delta': '{'},
        {'type': 'response.completed', 'response': final}]) + '\n\n'
    calls = mock_gateway(monkeypatch, tmp_path, [httpx.Response(200, headers={'content-type': 'text/event-stream'}, text=stream)])
    result = vision._request_vlm('http://test/v1', 'test', {}, {'api_timeout': 1, 'api_stream': True}, lambda _: None)
    assert result.json() == final and len(calls) == 1
    assert result.extensions['dataqc_transport']['response_id'] == 'resp_fixture'


def test_interrupted_stream_is_retried_and_never_accepted(tmp_path, monkeypatch):
    import json
    partial = 'data: ' + json.dumps({'type': 'response.output_text.delta', 'delta': '{}'}) + '\n\n'
    final = {'status': 'completed', 'output': []}
    calls = mock_gateway(monkeypatch, tmp_path, [
        httpx.Response(200, headers={'content-type': 'text/event-stream'}, text=partial),
        httpx.Response(200, json=final)])
    delays = []
    monkeypatch.setattr(vision, 'wait_seconds', lambda seconds, *a: delays.append(seconds))
    result = vision._request_vlm('http://test/v1', 'test', {}, {'api_timeout': 1, 'api_stream': True}, lambda _: None)
    assert result.json() == final and len(calls) == 2 and delays == [2]


def test_stream_heartbeats_cannot_bypass_attempt_deadline():
    class Heartbeats(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(10):
                time.sleep(.02)
                yield b': keepalive\n\n'
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200,
                     headers={'content-type': 'text/event-stream'}, stream=Heartbeats()))) as client:
        with pytest.raises(vision.RequestDeadlineExceeded):
            vision._stream_response(client, 'http://test/responses', {}, {}, lambda _: None, {}, deadline=.05)


def test_heartbeats_do_not_reset_generation_progress_timeout():
    class Heartbeats(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(10):
                time.sleep(.02)
                yield b'data: {"type":"response.in_progress"}\n\n'
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200,
                     headers={'content-type': 'text/event-stream'}, stream=Heartbeats()))) as client:
        with pytest.raises(vision.ResponseStalled):
            vision._stream_response(client, 'http://test/responses', {}, {}, lambda _: None, {}, deadline=1, progress_timeout=.05)


def test_continuous_output_can_exceed_progress_timeout():
    class Generation(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(6):
                time.sleep(.02)
                yield b'data: {"type":"response.output_text.delta","delta":"x"}\n\n'
            yield b'data: {"type":"response.completed","response":{"status":"completed","output":[]}}\n\n'
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200,
                     headers={'content-type': 'text/event-stream'}, stream=Generation()))) as client:
        timing = {}
        response = vision._stream_response(client, 'http://test/responses', {}, {}, lambda _: None, timing, deadline=1, progress_timeout=.05)
        assert response.json()['status'] == 'completed' and timing['generated_characters'] == 6
