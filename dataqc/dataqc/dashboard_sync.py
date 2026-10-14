"""Hourly metadata replication with a durable last-good snapshot and no retries."""
import base64
import copy
import json
import math
import os
from pathlib import Path
import threading
import time

import httpx

from .collection_dashboard import GRADES, TZ, metrics, date_filter

INTERVAL = 3600
SCHEMA = 1


def validate(payload):
    if not isinstance(payload, dict) or payload.get('schema') != SCHEMA:
        raise ValueError('数据源快照版本不匹配')
    rows = payload.get('rows')
    state = payload.get('state', {})
    updated = state.get('updated_at')
    if not state.get('ready') or not isinstance(updated, (int, float)) or not math.isfinite(updated) or updated <= 0:
        raise ValueError('数据源尚无成功统计')
    if not isinstance(rows, list):raise ValueError('数据源记录无效')
    ids = set()
    for row in rows:
        required = ('id', 'path', 'date', 'left', 'right', 'grade', 'scene', 'issues',
                    'duplicate_count', 'identity_conflict', 'date_mismatch', 'date_source',
                    'grade_source', 'collection_grade', 'qc_grade', 'manual_quality_grade',
                    'manual_note', 'identity', 'prompt')
        if not isinstance(row, dict) or any(k not in row for k in required):
            raise ValueError('数据源记录字段不完整')
        if row['grade'] not in GRADES or row['id'] in ids:
            raise ValueError('数据源等级或记录标识无效')
        if any(not isinstance(row[k], str) for k in ('id', 'path', 'date', 'left', 'right', 'scene')):
            raise ValueError('数据源记录格式无效')
        if not isinstance(row['issues'], list):raise ValueError('数据源问题列表无效')
        if row['date']:date_filter(row['date'], row['date'])
        ids.add(row['id'])
    metrics(rows)
    for basis in ('all', 'ab'):
        result = payload.get('plans', {}).get(basis, {})
        plans = result.get('plans')
        if result.get('basis') != basis or not isinstance(plans, list) or len(plans) != 3:
            raise ValueError('数据源计划快照不完整')
        for p in plans:
            if p.get('status') not in ('ready', 'pending') or not p.get('id'):
                raise ValueError('数据源计划格式无效')
            if p['status'] == 'ready':
                for key in ('products', 'completion', 'episode_target', 'actual_episodes',
                            'product_count', 'completed_products', 'remaining', 'document_url'):
                    if key not in p:raise ValueError('数据源计划字段不完整')
    for document in payload.get('documents', {}).values():
        base64.b64decode(document, validate=True)
    return payload


class RemoteInventory:
    def __init__(self, url, cache_path, *, clock=time.time, fetch=None):
        self.remote_url = url.rstrip('/')
        if not self.remote_url.startswith(('http://', 'https://')):
            raise ValueError('看板数据源应为 HTTP 地址')
        self.cache_path = Path(cache_path)
        self.lock = threading.RLock()
        self.clock = clock
        self.fetch = fetch or self._fetch
        self.running = False
        self.last_attempt = 0
        self.synced_at = None
        self.error = ''
        self.payload = None
        self.snapshot = dict(rows=[], updated_at=None, scanned_count=0, duplicate_count=0, warnings=[])
        self._stop = threading.Event()
        self._thread = None
        try:
            data = json.loads(self.cache_path.read_text())
            if data.get('source_url') == self.remote_url:
                self.last_attempt = float(data.get('last_attempt') or 0)
                self.synced_at = data.get('synced_at')
                self.error = data.get('error', '')
                if data.get('payload') is not None:self._adopt(validate(data['payload']))
        except (OSError, ValueError, TypeError, KeyError):pass

    def _adopt(self, payload):
        self.payload = payload
        source = payload['state']
        self.snapshot = dict(rows=payload['rows'], updated_at=source['updated_at'],
                             scanned_count=source.get('scanned_count', len(payload['rows'])),
                             duplicate_count=source.get('duplicate_count', 0), warnings=source.get('warnings', []))

    def _persist(self, *, payload, synced_at, error):
        data = dict(source_url=self.remote_url, last_attempt=self.last_attempt,
                    synced_at=synced_at, error=error, payload=payload)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix('.tmp')
        with temporary.open('w') as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.cache_path)

    def _fetch(self):
        # A single GET: HTTP errors, timeouts and redirects do not trigger another request.
        with httpx.Client(timeout=60, trust_env=False, follow_redirects=False,
                          transport=httpx.HTTPTransport(retries=0)) as client:
            response = client.get(self.remote_url + '/api/collection-dashboard/snapshot')
            response.raise_for_status()
            if len(response.content) > 64 * 1024 * 1024:raise ValueError('数据源快照过大')
            return response.json()

    def sync_once(self):
        with self.lock:
            now = self.clock()
            if self.running or (self.last_attempt and now < self.last_attempt + INTERVAL):return False
            self.running = True
            self.last_attempt = now
            try:self._persist(payload=self.payload, synced_at=self.synced_at, error=self.error)
            except OSError as exc:
                self.running = False
                self.error = '更新状态无法保存：' + str(exc)
                return False
        try:
            payload = validate(self.fetch())
            completed = self.clock()
            with self.lock:
                self._persist(payload=payload, synced_at=completed, error='')
                self._adopt(payload)
                self.synced_at = completed
                self.error = ''
        except Exception as exc:
            with self.lock:
                self.error = '本次同步失败，保留上次成功数据，下个小时再更新：' + str(exc)
                try:self._persist(payload=self.payload, synced_at=self.synced_at, error=self.error)
                except OSError:pass
        finally:
            with self.lock:self.running = False
        return True

    def _loop(self):
        while not self._stop.is_set():
            self.sync_once()
            with self.lock:delay = max(1, self.last_attempt + INTERVAL - self.clock())
            self._stop.wait(delay)

    def start(self):
        if self._thread and self._thread.is_alive():return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name='dashboard-hourly-sync')
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:self._thread.join(timeout=2)

    def refresh(self, force=False, background=True):
        # Browser refreshes and filters always read cache; only the hourly loop calls upstream.
        return False

    def rows(self):
        with self.lock:return self.snapshot['rows']

    def state(self):
        with self.lock:
            source = dict(self.payload['state']) if self.payload else {}
            return dict(source, source=self.remote_url, timezone=str(TZ), scanning=self.running,
                        phase='正在同步采集统计' if self.running else '按小时更新',
                        ready=self.payload is not None, error=self.error,
                        updated_at=self.snapshot['updated_at'], synced_at=self.synced_at,
                        refresh_mode='hourly', last_attempt_at=self.last_attempt or None,
                        next_update_at=self.last_attempt + INTERVAL if self.last_attempt else None)

    def plans(self, basis):
        if basis not in ('all', 'ab'):raise ValueError('未知采集口径')
        with self.lock:
            if self.payload:return copy.deepcopy(self.payload['plans'][basis])
        return dict(basis=basis, plans=[dict(id='batch'+str(i), name=name, status='pending',
                    error='等待首次成功同步') for i,name in enumerate(('第一批','第二批','第三批'),1)])

    def document(self, batch_id):
        with self.lock:
            value = self.payload.get('documents', {}).get(batch_id) if self.payload else None
            return base64.b64decode(value) if value else None
