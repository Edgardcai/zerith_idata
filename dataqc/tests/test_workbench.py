import json
import shutil
from pathlib import Path

import h5py
import numpy as np
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from test_pipeline import source, decision
from dataqc import api, db, library, config, worker, vision
from dataqc.checks import numeric_checks, raw_checks
from dataqc.export import create_dataset, path_for, validate_dataset
from dataqc.io import fingerprint, load, read_json
from dataqc.motion import warning_grade
from dataqc.repair import derive


@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DB',tmp_path/'db.sqlite3');db.init();library.init()
    monkeypatch.setattr(library,'EXPORTS',tmp_path/'exports')
    monkeypatch.setattr(library,'VAR',tmp_path/'var')
    return TestClient(api.app)


@pytest.fixture
def lr(source,tmp_path):
    report=raw_checks(source);d=decision();d['grade']='B'
    fixed=derive(source,tmp_path/'fixed',report,d,40,fingerprint(source))
    target=tmp_path/'lr';create_dataset([{'root':fixed,'grade':'B'},{'root':fixed,'grade':'B'}],target)
    return target


def test_gaps_warn_and_keep_exact_locations():
    s=np.zeros((150,23));t=np.arange(150)/30;t[50:]+=.12;t[91]=t[90]
    c=numeric_checks(s,s,t)
    clock=next(c for c in c if c['key']=='timestamps')
    assert clock['status']=='warn'
    assert clock['detail']['gaps'][0]['previous_frame']==49
    assert clock['detail']['gaps'][0]['frame']==50
    assert clock['detail']['gaps'][0]['interval_seconds']==pytest.approx(.153333333)
    assert any(g['interval_seconds']==0 for g in clock['detail']['gaps'])
    assert all(c['status']=='pass' for c in c if c['key'].startswith('arm_'))
    assert warning_grade({'grade':'A','reason':'视觉正常'},{'checks':c})['grade']=='B'
    assert warning_grade({'grade':'F','reason':'拿错物品'},{'checks':c})['grade']=='F'


def test_gap_warning_exports_as_b_with_source_clock(source,tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DB',tmp_path/'db.sqlite3');db.init()
    monkeypatch.setattr(worker,'VAR',tmp_path/'var');monkeypatch.setattr(worker,'EXPORTS',tmp_path/'exports')
    with h5py.File(source/'episode.hdf5','a')as f:
        times=f['timestamp/t'][:];times[100:]+=120;f['timestamp/t'][:]=times
    def inspect(*args, **kwargs):
        return dict(decision=decision(),verification=dict(status='pass',prompt_matches=True,hand_matches=True,items_match=True,stage_order_matches=True))
    monkeypatch.setattr(worker.get_adapter(),'inspect',inspect)
    before=fingerprint(source);rid=db.create(str(source),'auto',config.settings(),[str(source)])
    worker.process_run(db.get_run(rid));e=db.episodes(rid)[0];r=db.get_run(rid)
    assert e['grade']=='B' and e['status']=='ready',e['reason']
    assert r['exports'][0]['grade']=='B'
    assert '99→100' in e['reason']
    prov=read_json(Path(e['data']['repaired_root'])/'provenance.json')
    assert max(np.diff(prov['source_timestamps_seconds']))>.1
    assert fingerprint(source)==before
    assert validate_dataset(r['exports'][0]['full']['path'])['passed']


def test_library_replay_review_export_and_conflict(client,lr,tmp_path):
    original=fingerprint(lr);d=library.describe(lr);ident=d['id']
    listing=client.get(f'/api/library/{ident}/episodes');assert listing.status_code==200
    replay=client.get(f'/api/library/{ident}/episodes/1').json()
    assert len(replay['trajectory']['state'][0])==23
    assert len(replay['cameras'])==3 and replay['trajectory']['task'].startswith('Grasp')
    assert replay['stages'][0]['hand']=='left'
    video=client.get(f'/api/library/{ident}/episodes/1/video/{replay["cameras"][0]}',headers={'Range':'bytes=0-127'})
    assert video.status_code==206 and len(video.content)==128
    items=[dict(episode_index=0,grade='F',reason='人工发现拿错',actor='test',revision=0),dict(episode_index=1,grade='B',reason='时间间隔预警',actor='test',revision=0)]
    assert client.post(f'/api/library/{ident}/review',json={'annotations':items}).status_code==200
    assert client.post(f'/api/library/{ident}/review',json={'annotations':items}).status_code==409
    out=client.post(f'/api/library/{ident}/export');assert out.status_code==200,out.text
    root=Path(out.json()['output']);info=read_json(root/'meta/info.json')
    assert info['total_episodes']==1
    data=pq.read_table(path_for(root,0)).to_pydict();assert set(data['episode_index'])=={0}
    assert data['index']==list(range(len(data['index'])))
    maps=read_json(root/'meta/episode_name_mapping.json')['episodes'];assert maps[0]['source_lerobot_episode_index']==1 and maps[0]['episode_index']==0
    aggregate=read_json(root/'meta/stats.json');assert aggregate['index']['mean']==pytest.approx(np.mean(data['index']))
    assert read_json(root/'review_validation.json')['passed']
    assert fingerprint(lr)==original
    library.init();assert library.review_rows(ident)[0]['excluded']==1


def test_review_transaction_rolls_back_batch(client,lr):
    ident=library.describe(lr)['id']
    base=dict(grade='B',excluded=False,reason='预警',actor='test')
    r=client.post(f'/api/library/{ident}/review',json={'annotations':[dict(base,episode_index=0,revision=0),dict(base,episode_index=1,revision=2)]})
    assert r.status_code==409 and library.review_rows(ident)=={}


def test_compare_reuses_legacy_and_reports_both_sides(client,lr,tmp_path):
    sim=tmp_path/'simulation';shutil.copytree(lr,sim)
    response=client.post('/api/compare/analyze',json={'datasets':[{'path':str(sim),'platform':'simulation'},{'path':str(lr),'platform':'real'}],'max_episodes':1})
    assert response.status_code==200,response.text
    d=response.json();assert d['contract']['robot_type']=='zerith'
    assert d['summary']['simulation_count']==d['summary']['real_count']==1
    assert len(d['dimensions'])==46 and d['sampling']['checked_episodes']==2
    assert client.get('/api/compare/reports/'+d['id']).status_code==200
    assert client.get('/api/compare/reports/invalid').status_code==404


def test_compact_vlm_payload_has_bounded_detections():
    detections={'detections':[dict(camera=c,frame=i,detections=[dict(name='Milk',confidence=.991,box=[1.123,2.343,3.432,4.982])for _ in range(30)])for c in ['cam_high','cam_left_wrist','cam_right_wrist']for i in range(120)]}
    result=vision.compact_detections(detections,0,120)
    assert len(result)==12 and all(len(r['candidates'])==3 for r in result)
    assert len(json.dumps(result)) < len(json.dumps(detections))*.02
    c={'checks':[dict(key='timestamps',status='warn',detail=dict(issues=['gap'],gaps=[{'frame':10},{'frame':90}],bad_frames=[10,90],unused=list(range(10000))))]}
    short=vision.compact_report(c,0,50)
    assert short[0]['frames']==[10] and len(short[0]['gaps'])==1
    assert 'unused' not in json.dumps(short)


def test_terra_payload_and_403_does_not_retry(tmp_path,monkeypatch):
    import httpx
    monkeypatch.setattr(vision,'api_config',lambda cfg:('http://local/v1',cfg['api_model'],'unused-test-key'))
    calls=[]
    def handle(request):
        calls.append(json.loads(request.content));return httpx.Response(403,json={'error':'forbidden'})
    original=httpx.Client
    monkeypatch.setattr(vision.httpx,'Client',lambda **kwargs:original(transport=httpx.MockTransport(handle)))
    cfg=config.settings()
    with pytest.raises(vision.APIUnavailable):vision.call_vlm([{'type':'input_text','text':'test'}],vision.Window,cfg,tmp_path)
    assert len(calls)==1 and calls[0]['model']=='gpt-5.6-terra'
    assert calls[0]['reasoning']['effort']=='none'
    assert calls[0]['max_output_tokens']<=2500


def test_known_evidence_prefix_is_normalized_but_inventions_fail():
    item={'findings':[{'status':'fail','evidence_ids':['evidence_id=cam_high:12']}]}
    vision.validate_refs(item, {'cam_high:12':{}})
    assert item['findings'][0]['evidence_ids']==['cam_high:12']
    with pytest.raises(ValueError):
        vision.validate_refs({'status':'fail','evidence_ids':['evidence_id=cam_high:13']},{'cam_high:12':{}})


def test_visual_disagreement_needs_review():
    d=decision();d['grade']='F';d['findings']=[dict(criterion='拿对',status='fail',reason='疑似错误',evidence_ids=['cam_high:1'])]
    v=dict(status='pass',prompt_matches=True,hand_matches=True,items_match=True,stage_order_matches=True)
    assert worker.visual_decision(dict(decision=d,verification=v),{'checks':[]})['grade']=='REVIEW'
    v['items_match']=False;v['status']='fail'
    assert worker.visual_decision(dict(decision=d,verification=v),{'checks':[]})['grade']=='F'
    v['status']='uncertain'
    assert worker.visual_decision(dict(decision=d,verification=v),{'checks':[]})['grade']=='REVIEW'


def test_wrong_yolo_classes_are_not_sent_as_target_hints():
    y={'detections':[dict(camera='cam_high',frame=1,detections=[dict(name='CoconutLatte',confidence=.99,box=[0]*4)])]}
    assert vision.compact_detections(y,0,10,['If coconut','Ginger Ale'])[0]['candidates']==[]


def test_offline_refresh_keeps_pause_and_never_calls_vlm(client,source,tmp_path,monkeypatch):
    from dataqc import maintenance
    monkeypatch.setattr(maintenance,'VAR',tmp_path/'var')
    monkeypatch.setattr(worker.get_adapter(),'inspect',lambda *args:pytest.fail('offline refresh called VLM'))
    rid=db.create(str(source),'auto',config.settings(),[str(source)]);e=db.episodes(rid)[0]
    db.update('runs',rid,status='paused')
    db.update('episodes',e['id'],grade='F',status='rejected',reason='old gaps rule')
    counts=maintenance.refresh_paused_run(rid)
    assert counts['B']==1 and counts['F']==0
    assert db.get_run(rid)['status']=='paused' and db.get_run(rid)['config']['api_model']=='gpt-5.6-terra'
    new=db.episode(e['id']);assert new['grade']=='B' and new['status']=='queued' and new['revision']==1
    assert new['data']['raw_report']['version']==maintenance.RULE_VERSION
    db.update('runs',rid,status='running')
    with pytest.raises(ValueError):maintenance.refresh_paused_run(rid)


def test_library_all_excluded_and_unsafe_paths_cannot_export(client,lr):
    ident=library.describe(lr)['id']
    items=[dict(episode_index=i,grade='F',reason='排除',actor='test',revision=0)for i in range(2)]
    assert client.post(f'/api/library/{ident}/review',json={'annotations':items}).status_code==200
    assert client.post(f'/api/library/{ident}/export').status_code==422
    assert not list(library.EXPORTS.rglob('*.partial'))
    info=read_json(lr/'meta/info.json');info['data_path']='../../outside.parquet'
    (lr/'meta/info.json').write_text(json.dumps(info))
    assert client.post(f'/api/library/{ident}/export').status_code==422


def test_invalid_visual_evidence_becomes_review_without_fabricating_frames():
    r={'grade':'F','reason':'拿错','findings':[{'status':'fail','reason':'证据','evidence_ids':['cam_high:1','cam_high:2']}]}
    vision.review_invalid_refs(r,{'cam_high:1':{}})
    assert r['grade']=='REVIEW' and r['findings'][0]['status']=='uncertain'
    assert r['findings'][0]['evidence_ids']==['cam_high:1']
    assert 'cam_high:2' in r['findings'][0]['reason']


def test_token_usage_counts_failed_attempts_and_legacy_cache_once(tmp_path):
    h='a'*64;old='b'*64
    (tmp_path/(h+'.json')).write_text(json.dumps(dict(request_hash=h,usage={'total_tokens':10})))
    (tmp_path/(old+'.json')).write_text(json.dumps(dict(request_hash=old,usage={'total_tokens':30})))
    (tmp_path/'usage-ledger.jsonl').write_text('\n'.join(json.dumps(dict(request_hash=h,usage={'total_tokens':n}))for n in [10,20]))
    assert sum(r['usage']['total_tokens']for r in vision.usage_records(tmp_path))==60
