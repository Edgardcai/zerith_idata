import copy
import json
from pathlib import Path

import h5py
import numpy as np
import pytest

from test_pipeline import source
from dataqc import config, db, worker, yolo_gate as gate, vision
from dataqc.checks import raw_checks
from dataqc.io import fingerprint, read_json, write_json
from dataqc.export import validate_dataset


@pytest.fixture
def moving_source(source):
    # Remove the synthetic fixture's redundant wait while keeping its gripper events.
    with h5py.File(source/'episode.hdf5','a') as f:
        for key in ('action/arm/position','observation/state/arm/position'):
            a=f[key][:];a[:,0]=np.arange(len(a))*.012;f[key][:]=a
    return source


@pytest.fixture
def cfg(tmp_path):
    model=tmp_path/'model.pt';model.write_bytes(b'test model')
    return config.settings() | dict(vlm_enabled=True,yolo_path=str(model),vision_version=gate.VERSION,yolo_frame_offset=40,yolo_confidence=.25,yolo_thresholds_path='')


def detector(warn=()):
    def predict(root,samples,cfg,digest,thresholds):
        return [[dict(name=s['expected'] if s['hand'] not in warn else 'Other',confidence=.9,box=[0,0,10,10],is_product=True)]for s in samples],{0:'Milk',1:'Tea',2:'Other'}
    return predict


def test_old_two_of_three_rule_name_normalization_and_gripper_exclusion():
    moments=[dict(frame=i,detections=[dict(name=name,is_product=True)])for i,name in enumerate(['Yili Peach Yogurt','Yili-Peach_Yogurt','Robot Arm / Gripper'])]
    r=gate.match_hand('right','Yili Peach Yogurt',moments,{0:'Yili Peach Yogurt'})
    assert r['status']=='pass' and r['matched_count']==2
    moments[1]['detections']=[]
    assert gate.match_hand('right','Yili Peach Yogurt',moments,{0:'Yili Peach Yogurt'})['status']=='warn'
    duplicate=[dict(frame=0,detections=[dict(name='Milk')])for _ in range(3)]
    assert gate.match_hand('left','Milk',duplicate,{0:'Milk'})['status']=='warn'
    assert gate.match_hand('left','Pepsi',moments,{0:'Coca-Cola'})['supported'] is False


def test_match_pass_still_requires_image_vlm(moving_source,cfg,tmp_path,monkeypatch):
    monkeypatch.setattr(gate,'predict_samples',detector())
    r=gate.inspect(moving_source,raw_checks(moving_source),cfg,tmp_path/'cache')
    assert r['vlm_called'] is True and r['decision']['grade']=='B'  # image quality warnings retained
    assert r['pipeline']=='yolo_first' and r['coverage']['full_motion_review'] is False
    assert r['decision']['safe_trim_ids']==[]
    assert [[m['frame']for m in h['moments']]for h in r['yolo']['hands']]==[[30,70,110],[70,110,149]]
    assert all(m['camera']==f"cam_{h['hand']}_wrist"for h in r['yolo']['hands']for m in h['moments'])
    assert worker.visual_decision(r,raw_checks(moving_source))['grade']=='B'


@pytest.mark.parametrize('status,grade',[('pass','REVIEW'),('fail','F'),('uncertain','REVIEW')])
def test_all_hands_receive_image_vlm_even_on_yolo_warning(moving_source,cfg,tmp_path,monkeypatch,status,grade):
    monkeypatch.setattr(gate,'predict_samples',detector(warn=['left']))
    calls=[]
    def call(content,schema,config,*args):
        from dataqc.motion_review import MotionReview
        from test_assessment import motion_ok
        if schema is MotionReview:return motion_ok()
        calls.append(content)
        assert schema is gate.MatchingReview and config['api_attempts']==1
        assert sum(x['type']=='input_image'for x in content)==10
        text=''.join(x.get('text','')for x in content)
        assert 'cam_left_wrist' in text and 'cam_right_wrist' in text
        return dict(hands=[dict(hand=h,status=status,observed_item=item,reason='核对结论',evidence_ids=[f'cam_{h}_wrist:{f}']) for h,item,f in [('left','Milk',70),('right','Tea',110)]])
    monkeypatch.setattr(vision,'call_vlm',call)
    r=gate.inspect(moving_source,raw_checks(moving_source),cfg,tmp_path/'cache')
    assert len(calls)==1 and r['decision']['grade']==grade and r['vlm_called']
    assert len(r['hand_checks'])==2


def test_invalid_or_wrong_hand_refs_require_review(moving_source,cfg,tmp_path,monkeypatch):
    monkeypatch.setattr(gate,'predict_samples',detector(warn=['left']))
    monkeypatch.setattr(vision,'call_vlm',lambda *a,**kw:dict(hands=[dict(hand='left',status='fail',observed_item='Other',reason='结论',evidence_ids=['cam_right_wrist:110'])]))
    r=gate.inspect(moving_source,raw_checks(moving_source),cfg,tmp_path/'cache')
    assert r['decision']['grade']=='REVIEW'
    assert r['errors'] or any(h['status']=='uncertain' for h in r['hand_checks'])


def test_yolo_cache_invalidated_by_threshold_and_offset(moving_source,cfg,tmp_path,monkeypatch):
    calls=[]
    def predict(*args):calls.append(1);return detector()(*args)
    monkeypatch.setattr(gate,'predict_samples',predict)
    report=raw_checks(moving_source)
    for extra in ({},{},{'yolo_confidence':.3},{'yolo_frame_offset':30}):
        gate.yolo_match(moving_source,report,cfg|extra,tmp_path)
    assert len(calls)==3


def test_stationary_still_analyzed_but_never_silently_trimmed(source,cfg,tmp_path,monkeypatch):
    monkeypatch.setattr(gate,'predict_samples',detector())
    r=gate.inspect(source,raw_checks(source),cfg,tmp_path)
    assert r['decision']['grade']=='REVIEW' and r['vlm_called'] and r['decision']['safe_trim_ids']==[]


def test_double_checks_to_lerobot_export(moving_source,cfg,tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DB',tmp_path/'db.sqlite3');db.init()
    monkeypatch.setattr(worker,'VAR',tmp_path/'var');monkeypatch.setattr(worker,'EXPORTS',tmp_path/'exports')
    monkeypatch.setattr(gate,'predict_samples',detector())
    before=fingerprint(moving_source);rid=db.create(str(moving_source),'auto',cfg,[str(moving_source)])
    worker.process_run(db.get_run(rid));r=db.get_run(rid);e=db.episodes(rid)[0]
    assert e['status']=='ready' and e['grade']=='B',e['reason']
    assert r['status']=='completed' and r['exports'][0]['grade']=='B'
    assert validate_dataset(r['exports'][0]['full']['path'])['passed']
    assert len(r['exports'][0]['hands'])==2 and fingerprint(moving_source)==before
    assert e['data']['visual']['vlm_called'] is True


def test_failed_vlm_preserves_yolo_warning_and_pauses(moving_source,cfg,tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DB',tmp_path/'db.sqlite3');db.init()
    monkeypatch.setattr(worker,'VAR',tmp_path/'var');monkeypatch.setattr(worker,'EXPORTS',tmp_path/'exports')
    monkeypatch.setattr(gate,'predict_samples',detector(warn=['left']))
    def unavailable(*a,**kw):raise vision.APIUnavailable('test unavailable')
    monkeypatch.setattr(vision,'call_vlm',unavailable)
    rid=db.create(str(moving_source),'auto',cfg,[str(moving_source)])
    with pytest.raises(worker.Paused):worker.process_run(db.get_run(rid))
    e=db.episodes(rid)[0]
    assert e['status']=='incomplete' and e['grade']=='B' and e['data']['yolo_report']['status']=='warn'
    assert db.get_run(rid)['status']=='paused' and not db.get_run(rid)['exports']


def test_policy_switch_preserves_manual_and_numeric_reports(tmp_path,monkeypatch,cfg):
    from dataqc import maintenance
    monkeypatch.setattr(db,'DB',tmp_path/'db.sqlite3');db.init()
    rid=db.create('/data/test','auto',cfg|{'vision_version':'compact_v2'},['/data/test/one','/data/test/two'])
    a,b=db.episodes(rid);db.update('runs',rid,status='paused')
    report={'version':'zerith_qc_4','checks':[]}
    old={'pipeline':'old','vision_version':'compact_v2'}
    db.update('episodes',a['id'],status='review',data={'raw_report':report,'visual':old})
    db.update('episodes',b['id'],status='review',data={'raw_report':report,'visual':old,'manual_decision':{'grade':'B'}})
    assert maintenance.switch_visual_policy(rid,cfg)==1
    assert db.episode(a['id'])['status']=='queued' and db.episode(a['id'])['data']['raw_report']==report
    assert 'visual'not in db.episode(a['id'])['data']
    assert db.episode(b['id'])['data']['manual_decision']=={'grade':'B'}
    assert db.get_run(rid)['status']=='paused'
