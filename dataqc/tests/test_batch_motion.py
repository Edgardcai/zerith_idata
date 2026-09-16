import copy
import json
import shutil
import threading
import time
from pathlib import Path

import pytest
from dataqc import batch_motion, batch_prepare, motion_review, vision, db, worker, api
from dataqc.io import read_json, write_json, fingerprint
from dataqc.checks import raw_checks
from dataqc.robots import ZerithAdapter
from test_pipeline import source
from test_yolo_gate import moving_source, cfg
from test_assessment import motion_ok


def response(content):
    packet=json.loads(content[1]['text'])
    return dict(episodes=[dict(episode_id=e['episode_id'],needs_detail=False,summary='指标复核完成，物理风险不可观测',
        statuses=[f['status'] for f in motion_ok()['findings']],evidence_ids=['trajectory:summary'],issues=[]) for e in reversed(packet['episodes'])])


def items_for(root, tmp_path, count):
    report=raw_checks(root)
    result=[]
    for i in range(count):
        target=tmp_path/'group_0'/f'ep{i:03d}'
        shutil.copytree(root,target)
        result.append(dict(root=target,report=copy.deepcopy(report),cache=tmp_path/'work'/f'episode_{i:06d}'/'motion'))
    return result


@pytest.mark.parametrize('grade',['A','B','F'])
@pytest.mark.parametrize('fatal',[False,True])
def test_numeric_recheck_preserves_manual_grade(source,cfg,tmp_path,monkeypatch,grade,fatal):
    monkeypatch.setattr(db,'DB',tmp_path/'db.sqlite3');db.init()
    rid=db.create(str(source),'auto',cfg,[str(source)]);ep=db.episodes(rid)[0]
    decision=dict(grade=grade,reason='人工已复核',note='保留备注')
    db.update('episodes',ep['id'],grade=grade,data=dict(manual_decision=decision))
    report=dict(checks=[dict(key='finite' if fatal else 'stationary',status='fail' if fatal else 'warn',label='测试',detail={})])
    monkeypatch.setattr(batch_prepare,'measure',lambda *args:(report,{}))
    monkeypatch.setattr(batch_motion,'review_many',lambda *args:pytest.fail('Manual records must not invoke models'))
    batch_prepare.prepare_run(db.get_run(rid),tmp_path/'work',lambda _:None)
    current=db.episodes(rid)[0]
    assert current['grade']==grade
    assert current['data']['manual_decision']==decision
    if fatal:assert current['status']=='rejected'  # Keep structural export protection independent of human grade.


def test_worker_upgrade_keeps_human_grade_but_blocks_corrupt_export(source,cfg,tmp_path,monkeypatch):
    import h5py
    monkeypatch.setattr(db,'DB',tmp_path/'db.sqlite3');db.init()
    monkeypatch.setattr(worker,'VAR',tmp_path/'var');monkeypatch.setattr(worker,'EXPORTS',tmp_path/'exports')
    rid=db.create(str(source),'auto',cfg,[str(source)]);ep=db.episodes(rid)[0]
    manual=dict(grade='A',reason='先前人工已确认',note='保留原因')
    db.update('episodes',ep['id'],grade='A',status='ready',data=dict(manual_decision=manual,raw_report=dict(version='zerith_qc_6',checks=[])))
    with h5py.File(source/'episode.hdf5','a') as f:f['action/arm/position'][12,0]=float('nan')
    monkeypatch.setattr(ZerithAdapter,'inspect',lambda *a,**k:pytest.fail('No model for corrupt values'))
    worker.process_run(db.get_run(rid))
    current=db.episodes(rid)[0]
    assert current['grade']=='A' and current['status']=='rejected'
    assert current['data']['manual_decision']==manual
    assert not db.get_run(rid)['exports']


def test_21_episodes_three_calls_two_concurrent_after_all_numeric(moving_source,cfg,tmp_path,monkeypatch):
    items=items_for(moving_source,tmp_path,21)
    monkeypatch.setattr(db,'DB',tmp_path/'db.sqlite3');db.init()
    monkeypatch.setattr(worker,'VAR',tmp_path/'var');monkeypatch.setattr(worker,'EXPORTS',tmp_path/'exports')
    measured=set();calls=[];active=0;maximum=0;lock=threading.Lock()
    def measure(self,root,*args):
        measured.add(str(root));return copy.deepcopy(items[0]['report'])
    monkeypatch.setattr(ZerithAdapter,'check',measure)
    def call(content,schema,*args):
        nonlocal active,maximum
        assert schema.__name__=='BatchMotionReview'
        assert len(measured)==21
        with lock:active+=1;maximum=max(maximum,active)
        packet=json.loads(content[1]['text']);calls.append(len(packet['episodes']))
        time.sleep(.05)
        with lock:active-=1
        return response(content)
    monkeypatch.setattr(vision,'call_vlm',call)
    rid=db.create(str(tmp_path/'group_0'),'manual',cfg|dict(vlm_enabled=False),[str(i['root']) for i in items])
    worker.process_run(db.get_run(rid))
    assert sorted(calls)==[1,10,10] and maximum==2
    assert all(e['status']=='review' and e['grade']=='B' for e in db.episodes(rid))
    assert all(e['data']['visual']['motion_review']['status']=='pass' for e in db.episodes(rid))


@pytest.mark.parametrize('bad',['missing','duplicate','unknown'])
def test_batch_id_validation_never_accepts_incomplete_group(moving_source,cfg,tmp_path,monkeypatch,bad):
    items=items_for(moving_source,tmp_path,2)
    def call(content,*args):
        r=response(content)
        if bad=='missing':r['episodes'].pop()
        elif bad=='duplicate':r['episodes'][0]=r['episodes'][1]
        else:r['episodes'][0]['episode_id']='unknown'
        return r
    monkeypatch.setattr(vision,'call_vlm',call)
    results=batch_motion.review_many(items,cfg,tmp_path/'batch')
    assert all('error' in r for r in results.values())
    assert not any((i['cache']/'motion_report.json').exists() for i in items)


def test_partial_failure_only_retries_failed_episode_and_caches_success(moving_source,cfg,tmp_path,monkeypatch):
    items=items_for(moving_source,tmp_path,3);calls=[]
    def call(content,*args):
        r=response(content);calls.append(len(r['episodes']))
        if len(calls)==1:r['episodes'][0]['evidence_ids']=['frame:999999']
        return r
    monkeypatch.setattr(vision,'call_vlm',call)
    first=batch_motion.review_many(items,cfg,tmp_path/'batch')
    assert sum('result' in r for r in first.values())==2
    second=batch_motion.review_many(items,cfg,tmp_path/'batch')
    assert all('result' in r for r in second.values()) and calls==[3,1]
    progress=[]
    batch_motion.review_many(items,cfg,tmp_path/'batch',progress.append)
    assert calls==[3,1]
    assert any('缓存复用 3 条 · 待新审 0 条' in line for line in progress)
    assert any('本次无需新调用' in line for line in progress)


def test_needs_detail_only_rechecks_requested_record(moving_source,cfg,tmp_path,monkeypatch):
    items=items_for(moving_source,tmp_path,2);calls=[]
    def call(content,schema,*args):
        calls.append(schema.__name__)
        if schema is motion_review.MotionReview:return motion_ok()
        r=response(content);r['episodes'][0]['needs_detail']=True;return r
    monkeypatch.setattr(vision,'call_vlm',call)
    results=batch_motion.review_many(items,cfg,tmp_path/'batch')
    assert calls==['BatchMotionReview','MotionReview']
    assert all('result' in r for r in results.values())
    assert sum(r['result']['batch']['detail_requested'] for r in results.values())==1


def test_auth_failure_stops_new_batches(moving_source,cfg,tmp_path,monkeypatch):
    items=items_for(moving_source,tmp_path,5);calls=[]
    def fail(*args):calls.append(1);raise vision.APIUnavailable('unavailable')
    monkeypatch.setattr(vision,'call_vlm',fail)
    results=batch_motion.review_many(items,cfg|dict(motion_batch_size=1,motion_batch_concurrency=1),tmp_path/'batch')
    assert len(calls)==1 and all(r['api_unavailable'] for r in results.values())


def test_change_after_raw_checks_rejected_without_request(moving_source,cfg,tmp_path,monkeypatch):
    items=items_for(moving_source,tmp_path,1);items[0]['fingerprint_expected']={'invalid':'snapshot'}
    monkeypatch.setattr(vision,'call_vlm',lambda *args:pytest.fail('Changed source must not be sent'))
    result=batch_motion.review_many(items,cfg,tmp_path/'batch')
    assert all('error' in r for r in result.values())


def test_real_http_usage_counted_once_and_compact_payload(moving_source,cfg,tmp_path,monkeypatch):
    items=items_for(moving_source,tmp_path,3)
    results=batch_motion.review_many(items,cfg,tmp_path/'batch')
    assert all('result' in r for r in results.values())
    records=[r for i in items for r in vision.usage_records(i['cache'])]
    assert sum(r['usage']['total_tokens'] for r in records)==20
    assert sum(r['request_count'] for r in records)==1
    full,_=motion_review.prepare(moving_source,items[0]['report'],cfg)
    compact=batch_motion.compact_payload(full)
    assert len(json.dumps(compact)) < len(json.dumps(full))*.45
    assert set(k for k in compact['evidence'] if k.startswith('check:')) == set(k for k in full['evidence'] if k.startswith('check:'))


def test_pause_keeps_completed_results_for_resume(moving_source,cfg,tmp_path,monkeypatch):
    items=items_for(moving_source,tmp_path,3);calls=[]
    monkeypatch.setattr(vision,'call_vlm',lambda content,*args:calls.append(1) or response(content))
    def pause(text):
        if text.startswith('提交 VLM 批次 2'):raise worker.Paused()
    with pytest.raises(worker.Paused):
        batch_motion.review_many(items,cfg|dict(motion_batch_size=1,motion_batch_concurrency=1),tmp_path/'batch',pause)
    assert (items[0]['cache']/'motion_report.json').exists()
    results=batch_motion.review_many(items,cfg|dict(motion_batch_size=1),tmp_path/'batch')
    assert len(calls)==3 and all('result' in r for r in results.values())


def test_collection_uses_same_bulk_results_and_restores_environment(moving_source,cfg,tmp_path,monkeypatch):
    import os
    from argparse import Namespace
    from dataqc import config
    from integrations import batch_collection
    from integrations.zerith_rules import run_manual_checks
    from test_unified import LEGACY, read_raw_episode, load_profile
    items=items_for(moving_source,tmp_path,3)
    monkeypatch.setattr(config,'settings',lambda:cfg|dict(vlm_enabled=False))
    monkeypatch.setattr(config,'VAR',tmp_path/'var')
    profile=load_profile(LEGACY/'robot_profiles/zerith.yaml')
    calls=[]
    def call(content,schema,*args):
        assert schema.__name__=='BatchMotionReview'
        assert len(list((tmp_path/'var/manual').glob('*/raw_report.json')))==3
        calls.append(1);return response(content)
    monkeypatch.setattr(vision,'call_vlm',call)
    before=os.environ.get('DATAQC_MOTION_OUTCOMES')
    with batch_collection.prepare_collection([i['root'] for i in items],profile,Namespace(num_workers=2),tmp_path/'reports'):
        reports=[run_manual_checks(read_raw_episode(i['root'],profile),profile) for i in items]
    assert os.environ.get('DATAQC_MOTION_OUTCOMES')==before
    assert len(calls)==1
    assert all(r['quality_grade']=='B' and r['visual']['motion_review']['batch']['size']==3 for r in reports)


def test_batch_limits_validated_by_settings(cfg):
    from pydantic import ValidationError
    fields=dict(yolo_path=cfg['yolo_path'],device='0',export_grades=['A','B'])
    assert api.Settings(**fields).motion_batch_size==10
    for override in [dict(motion_batch_size=0),dict(motion_batch_size=21),dict(motion_batch_concurrency=0),dict(motion_batch_concurrency=5)]:
        with pytest.raises(ValidationError):api.Settings(**fields,**override)
