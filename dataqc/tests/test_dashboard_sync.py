import copy
import json
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dataqc import collection_dashboard_api as api
from dataqc.dashboard_sync import RemoteInventory, validate
from test_collection_dashboard import episode, scan
from test_collection_plans import document


def source(tmp_path, monkeypatch):
    episode(tmp_path/'data', '20261013_scene/a', grade='A')
    episode(tmp_path/'data', '20261013_scene/b', grade='B')
    episode(tmp_path/'data', '20261013_scene/f', grade='F')
    index=scan(tmp_path)
    plans=tmp_path/'plans'
    document(plans/'第一批.docx', [('Apple', 'Pear', 2)])
    monkeypatch.setattr(api, 'inventory', lambda:index)
    monkeypatch.setattr(api, 'plan_root', lambda:plans)
    app=FastAPI();app.include_router(api.router)
    client=TestClient(app)
    payload=client.get('/api/collection-dashboard/snapshot').json()
    validate(payload)
    return payload,client,index


def test_hourly_failure_restart_and_recovery_keeps_last_success(tmp_path,monkeypatch):
    payload,_,_=source(tmp_path,monkeypatch)
    now=[10000.0];calls=[];outcome=[payload]
    def fetch():
        calls.append(now[0])
        if isinstance(outcome[0],Exception):raise outcome[0]
        return copy.deepcopy(outcome[0])
    cache=tmp_path/'remote.json'
    index=RemoteInventory('http://source:9990',cache,clock=lambda:now[0],fetch=fetch)
    assert index.sync_once() and len(index.rows())==3
    first=index.state()['updated_at']
    assert index.state()['synced_at']==10000
    now[0]+=3599
    assert not index.sync_once()
    assert not index.refresh(force=True)
    assert len(calls)==1
    now[0]+=1;outcome[0]=TimeoutError('offline')
    assert index.sync_once()
    assert len(index.rows())==3 and index.state()['updated_at']==first
    assert index.state()['synced_at']==10000 and index.state()['error']
    assert index.state()['next_update_at']==17200
    for _ in range(10):assert not index.sync_once()
    restored=RemoteInventory('http://source:9990',cache,clock=lambda:now[0],fetch=fetch)
    assert len(restored.rows())==3 and restored.state()['error']
    assert not restored.sync_once() and len(calls)==2
    now[0]+=3600;outcome[0]=copy.deepcopy(payload)
    outcome[0]['rows']=outcome[0]['rows'][:1]
    outcome[0]['state']['updated_at']+=1
    assert restored.sync_once() and len(calls)==3 and len(restored.rows())==1
    assert restored.state()['updated_at']==first+1 and not restored.state()['error']
    assert restored.state()['synced_at']==now[0]


def test_bad_response_and_first_failure_do_not_publish_incomplete_data(tmp_path,monkeypatch):
    payload,_,_=source(tmp_path,monkeypatch)
    now=[10000.0];result=[payload]
    index=RemoteInventory('http://source',tmp_path/'cache.json',clock=lambda:now[0],fetch=lambda:result[0])
    index.sync_once()
    for bad in ({}, dict(payload,rows=[{}]), dict(payload,plans={}), dict(payload,state={'ready':False})):
        now[0]+=3600;result[0]=bad
        index.sync_once()
        assert len(index.rows())==3 and index.state()['error']
        saved=json.loads(index.cache_path.read_text())
        assert saved['payload']['rows']==payload['rows']
    empty=RemoteInventory('http://source',tmp_path/'empty.json',clock=lambda:now[0],fetch=lambda:{})
    empty.sync_once()
    assert not empty.state()['ready'] and empty.rows()==[]
    assert not empty.sync_once()


def test_cached_api_filters_plans_csv_documents_and_no_upstream_on_reads(tmp_path,monkeypatch):
    payload,client,_=source(tmp_path,monkeypatch)
    calls=[]
    def fetch():calls.append(1);return payload
    index=RemoteInventory('http://source:9990',tmp_path/'remote.json',fetch=fetch)
    index.sync_once();monkeypatch.setattr(api,'inventory',lambda:index)
    summary=client.get('/api/collection-dashboard/summary?start=2026-10-13&end=2026-10-13').json()
    assert summary['metrics']['total']==3 and summary['metrics']['grades']['F']==1
    assert summary['state']['refresh_mode']=='hourly'
    assert client.get('/api/collection-dashboard/records?grade=B').json()['total']==1
    assert client.get('/api/collection-dashboard/plans?basis=ab').json()['plans'][0]['actual_episodes']==2
    assert client.get('/api/collection-dashboard/plans?basis=all').json()['plans'][0]['actual_episodes']==3
    assert client.get('/api/collection-dashboard/export.csv?kind=daily').status_code==200
    assert client.get('/api/collection-dashboard/export.csv?kind=plans').status_code==200
    assert client.get('/api/collection-dashboard/plan-document/batch1').content.startswith(b'PK')
    assert client.post('/api/collection-dashboard/refresh').json()['started'] is False
    assert client.get('/api/collection-dashboard/plans?basis=bad').status_code==400
    assert client.get('/api/collection-dashboard/snapshot').status_code==409
    row=index.rows()[0]
    response=client.get(f"/api/collection-dashboard/video/{row['id']}/cam_high",follow_redirects=False)
    assert response.status_code==307 and response.headers['location'].startswith('http://source:9990/')
    assert client.get('/api/collection-dashboard/video/unknown/cam_high').status_code==404
    assert len(calls)==1


def test_source_outage_snapshot_is_http_error_not_fake_new_success(tmp_path,monkeypatch):
    payload,client,index=source(tmp_path,monkeypatch)
    index.root.rename(tmp_path/'offline')
    index.refresh(force=True,background=False)
    assert client.get('/api/collection-dashboard/snapshot').status_code==503
    assert index.rows()==payload['rows']


def test_background_scheduler_starts_without_dashboard_read(tmp_path,monkeypatch):
    import threading
    payload,_,_=source(tmp_path,monkeypatch)
    called=threading.Event();calls=[]
    def fetch():calls.append(1);called.set();return payload
    index=RemoteInventory('http://source',tmp_path/'remote.json',fetch=fetch)
    index.start()
    assert called.wait(3)
    index.stop()
    assert index.state()['ready'] and len(calls)==1
    index.start();index.stop()
    assert len(calls)==1


def test_runtime_adapter_preserves_existing_lifespan(tmp_path,monkeypatch):
    from contextlib import asynccontextmanager
    import threading
    from integrations import dashboard_runtime
    payload,_,_=source(tmp_path,monkeypatch)
    called=threading.Event();events=[]
    def fetch():called.set();return payload
    index=RemoteInventory('http://source',tmp_path/'remote.json',fetch=fetch)
    monkeypatch.setattr(dashboard_runtime,'inventory',lambda:index)
    @asynccontextmanager
    async def original(app):
        events.append('start')
        yield
        events.append('stop')
    app=FastAPI(lifespan=original)
    dashboard_runtime.install_runtime(app)
    with TestClient(app):
        assert called.wait(3) and events==['start']
    assert events==['start','stop'] and not index._thread.is_alive()
