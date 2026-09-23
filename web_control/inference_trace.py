"""Bounded, asynchronous inference diagnostics. Never calls a robot setter."""
from __future__ import annotations

from collections import deque
import csv
from datetime import datetime
import json
import math
import os
from pathlib import Path
import queue
import re
import threading
import time
import uuid

NAMES = ([f'left_j{i}' for i in range(1, 8)] + ['left_gripper']
         + [f'right_j{i}' for i in range(1, 8)] + ['right_gripper', 'lift',
         'waist_pitch', 'waist_yaw', 'head_yaw', 'head_pitch', 'base_linear', 'base_angular'])
UNITS = ['rad'] * 23
UNITS[16], UNITS[21], UNITS[22] = 'm', 'm/s', 'rad/s'


class InferenceTrace:
    def __init__(self, directory=None):
        self.directory = Path(directory or os.environ.get('PI05_TRACE_DIR',
                              str(Path(__file__).resolve().parents[1] / 'logs/inference')))
        self.lock = threading.RLock()
        self.active = False
        self.run_id = None
        self.error = None
        self.dropped = 0
        self.recent = deque(maxlen=4000)
        self.thread = None
        self.origin = 0.0
        self.metadata = {}
        self._history_cache = None

    def start(self, metadata):
        if self.thread and self.thread.is_alive():
            raise RuntimeError('上一轮推理记录尚未写入完成')
        run_id = datetime.now().strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:8]
        folder = self.directory / run_id
        folder.mkdir(parents=True, exist_ok=False)
        # Fail before executing any policy action if no recording can be created.
        events = (folder / 'events.jsonl').open('w', encoding='utf-8')
        try:
            samples = (folder / 'samples.csv').open('w', encoding='utf-8', newline='')
        except BaseException:
            events.close()
            raise
        origin = time.monotonic()
        metadata = dict(metadata, schema_version=1, run_id=run_id,
                        started_unix=time.time(), origin_monotonic=origin,
                        names=NAMES, units=UNITS, sample_rate_hz=30,
                        timestamp_note='host SDK read time; motors read sequentially',
                        error_sign='held_command_minus_sdk_state')
        try:
            (folder / 'metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
        except BaseException:
            events.close()
            samples.close()
            raise
        with self.lock:
            self.run_id, self.origin = run_id, origin
            self.error, self.dropped = None, 0
            self.recent.clear()
            self.queue = queue.Queue(maxsize=8192)
            self.done = threading.Event()
            self.active = True
            self.metadata = metadata
            self.thread = threading.Thread(target=self._write, args=(folder, events, samples),
                                           name='pi05-trace-writer', daemon=True)
            self.thread.start()
        self.record('start')

    def record(self, kind, *, monotonic=None, **values):
        with self.lock:
            if not self.active:
                return
            stamp = time.monotonic() if monotonic is None else monotonic
            event = dict(kind=kind, monotonic=stamp, t=stamp - self.origin, **values)
            if kind in ('state', 'step', 'request', 'chunk', 'end', 'fault'):
                self.recent.append({k: v for k, v in event.items()
                                    if k not in ('actions', 'server_payload', 'observation_state')})
            try:
                self.queue.put_nowait(event)
            except queue.Full:
                self.dropped += 1

    def finish(self, reason):
        with self.lock:
            if not self.active:
                return
            self.record('end', reason=reason)
            self.active = False
            self.done.set()
        self.thread.join(timeout=2.0)

    def status(self):
        with self.lock:
            return dict(active=self.active, run_id=self.run_id, directory=str(self.directory),
                        error=self.error, dropped_events=self.dropped,
                        writing=bool(self.thread and self.thread.is_alive()))

    def _write(self, folder, events, samples):
        groups = ('server_action', 'sent_action', 'state', 'held_action', 'error',
                  'speed', 'torque', 'kp', 'kd', 'motor_error', 'refresh_gap_ms')
        fields = ['kind', 't', 'monotonic', 'chunk', 'index'] + [f'{g}.{n}' for g in groups for n in NAMES]
        writer = csv.DictWriter(samples, fieldnames=fields)
        count = [0] * 23
        squares = [0.] * 23
        peaks = [0.] * 23
        max_gap = 0.
        event_count = 0
        try:
            writer.writeheader()
            last_flush = time.monotonic()
            while not self.done.is_set() or not self.queue.empty():
                try:
                    item = self.queue.get(timeout=0.1)
                except queue.Empty:
                    item = None
                if item is not None:
                    events.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + '\n')
                    event_count += 1
                    if item['kind'] in ('state', 'step'):
                        row = {k: item.get(k, '') for k in fields[:5]}
                        for group in groups:
                            for i, value in enumerate(item.get(group, []) or []):
                                if i < 23:
                                    row[f'{group}.{NAMES[i]}'] = value
                        writer.writerow(row)
                    if item['kind'] == 'state':
                        for i, value in enumerate(item.get('error', [])):
                            if value is not None:
                                count[i] += 1
                                squares[i] += value * value
                                peaks[i] = max(peaks[i], abs(value))
                        max_gap = max([max_gap] + [v for v in item.get('refresh_gap_ms', []) if v is not None])
                if time.monotonic() - last_flush >= 0.5:
                    events.flush()
                    samples.flush()
                    last_flush = time.monotonic()
            summary = dict(self.status(), events=event_count, max_refresh_gap_ms=max_gap,
                           rmse=[math.sqrt(s / n) if n else None for s, n in zip(squares, count)],
                           max_abs_error=[v if n else None for v, n in zip(peaks, count)])
            summary['writing'] = False
            (folder / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        except Exception as exc:
            with self.lock:
                self.error = f'{type(exc).__name__}: {exc}'
                self.active = False
        finally:
            events.close()
            samples.close()

    def folder(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r'\d{8}_\d{6}_[a-f0-9]{8}', run_id):
            raise ValueError('invalid trace run id')
        result = (self.directory / run_id).resolve()
        if result.parent != self.directory.resolve() or not result.is_dir():
            raise ValueError('trace not found')
        return result

    def runs(self):
        result = []
        if self.directory.exists():
            for folder in sorted(self.directory.iterdir(), reverse=True):
                if (re.fullmatch(r'\d{8}_\d{6}_[a-f0-9]{8}', folder.name)
                        and (folder / 'metadata.json').is_file()):
                    result.append(folder.name)
                    if len(result) == 100:
                        break
        return dict(self.status(), runs=result)

    def data(self, run_id=None):
        with self.lock:
            run_id = run_id or self.run_id
            if run_id and run_id == self.run_id:
                return dict(self.status(), metadata=self.metadata.copy(), events=list(self.recent),
                            window='latest 4000 diagnostic events')
        if not run_id:
            saved = self.runs()["runs"]
            if saved:
                return self.data(saved[0])
            return dict(self.status(), metadata={'names': NAMES, 'units': UNITS}, events=[])
        folder = self.folder(run_id)
        event_file = folder / 'events.jsonl'
        info = event_file.stat()
        cache_key = (run_id, info.st_size, info.st_mtime_ns)
        with self.lock:
            if self._history_cache and self._history_cache[0] == cache_key:
                return self._history_cache[1]
        recent = deque(maxlen=4000)
        with event_file.open() as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:  # possible last line during flush
                    continue
                if event['kind'] in ('state', 'step', 'request', 'chunk', 'end', 'fault'):
                    recent.append({k: v for k, v in event.items()
                                   if k not in ('actions', 'server_payload', 'observation_state')})
        summary_file = folder / 'summary.json'
        summary = json.loads(summary_file.read_text()) if summary_file.exists() else None
        result = dict(run_id=run_id, active=False, events=list(recent),
                    error=(summary or {}).get('error'),
                    dropped_events=(summary or {}).get('dropped_events', 0),
                    metadata=json.loads((folder / 'metadata.json').read_text()),
                    summary=summary,
                    window='latest 4000 diagnostic events')
        with self.lock:
            self._history_cache = (cache_key, result)
        return result
