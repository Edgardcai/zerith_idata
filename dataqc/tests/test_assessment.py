import copy
import json
import threading
from pathlib import Path
import pytest
import numpy as np
from dataqc import config, motion_review, assessment, vision, yolo_gate, worker, db
from dataqc.checks import raw_checks
from dataqc.io import read_json
from dataqc.motion import arm_and_posture_checks
from test_pipeline import source
from test_yolo_gate import moving_source, cfg, detector


def motion_ok():
    return dict(summary='数值动作符合已定义指标；物理风险不可由这些数据排除',findings=[
        dict(criterion=c,status='not_observable' if c.endswith('风险') else 'pass',reason='按现有指标复核',
             evidence_ids=[] if c.endswith('风险') else ['trajectory:summary']) for c in motion_review.CRITERIA])


def category_ok(status='pass'):
    return dict(hands=[dict(hand=h,status=status,observed_item=item,reason='已核对画面',evidence_ids=[f'cam_{h}_wrist:{f}'])
        for h,item,f in [('left','Milk',70),('right','Tea',110)]])


def clean(root):
    r=raw_checks(root)
    for c in r['checks']:
        if c['status']=='warn':c['status']='pass'
    return r


@pytest.mark.parametrize('col',[17,18,19,20])
def test_posture_002_boundaries(col):
    s=np.zeros((150,23));a=s.copy();t=np.arange(150)/30
    s[:,col]=np.float32(.02);a[:,col]=np.tile([-.02,.02],75)
    checks=arm_and_posture_checks(s,a,t)
    assert all(c['status']=='pass' for c in checks)
    s[:,col]=.0202;a[:,col]=np.tile([-.0202,.0202],75)
    checks=arm_and_posture_checks(s,a,t)
    assert all(c['status']=='warn' for c in checks if c['key'].startswith('posture_'))


def test_unchecked_still_calls_text_vlm_and_no_images(moving_source,cfg,tmp_path,monkeypatch):
    def forbidden(*a,**k):pytest.fail('类别关闭不得读取识别图片或模型')
    monkeypatch.setattr(yolo_gate,'yolo_match',forbidden)
    monkeypatch.setattr(vision,'image_content',forbidden)
    calls=[]
    def call(content,schema,*a):
        calls.append(content)
        assert schema is motion_review.MotionReview
        assert all(x['type']=='input_text' for x in content)
        return motion_ok()
    monkeypatch.setattr(vision,'call_vlm',call)
    result=assessment.inspect(moving_source,clean(moving_source),cfg|dict(vlm_enabled=False,yolo_path='/missing/model'),tmp_path/'qc')
    assert len(calls)==1 and result['decision']['grade']=='A'
    assert result['category_review']['status']=='skipped' and result['motion_review']['status']=='pass'
    assert result['decision']['category_qc']['complete'] is False
    assert not config.DEFAULTS['vlm_enabled']


@pytest.mark.parametrize('yolo_ok,vlm_status,grade',[(True,'pass','A'),(True,'fail','REVIEW'),(True,'uncertain','REVIEW'),(False,'pass','REVIEW'),(False,'fail','REVIEW')])
def test_category_is_conjunction_and_parallel(moving_source,cfg,tmp_path,monkeypatch,yolo_ok,vlm_status,grade):
    monkeypatch.setattr(yolo_gate,'predict_samples',detector(warn=[] if yolo_ok else ['left']))
    barrier=threading.Barrier(2,timeout=10);calls=[]
    def call(content,schema,*a):
        calls.append(schema.__name__)
        barrier.wait()
        if schema is motion_review.MotionReview:
            assert all(c['type']=='input_text' for c in content)
            return motion_ok()
        assert schema is yolo_gate.MatchingReview
        assert sum(c['type']=='input_image' for c in content)==10
        return category_ok(vlm_status)
    monkeypatch.setattr(vision,'call_vlm',call)
    result=assessment.inspect(moving_source,clean(moving_source),cfg|dict(vlm_enabled=True),tmp_path/'qc')
    assert sorted(calls)==['MatchingReview','MotionReview']
    assert result['decision']['grade']==grade,result


def test_motion_suspected_is_review_not_f(moving_source,cfg,tmp_path,monkeypatch):
    reply=motion_ok();reply['findings'][1].update(status='suspected',evidence_ids=['frame:0'],reason='可疑运动，需复核')
    monkeypatch.setattr(vision,'call_vlm',lambda *a:reply)
    result=assessment.inspect(moving_source,clean(moving_source),cfg|dict(vlm_enabled=False),tmp_path/'qc')
    assert result['decision']['grade']=='REVIEW'


def test_unreadable_height_reference_is_not_a_qc_check(moving_source,cfg,tmp_path,monkeypatch):
    from dataqc.io import write_json
    write_json(moving_source/'collection_task.json',dict(targets=dict(lift_height='unknown')))
    monkeypatch.setattr(vision,'call_vlm',lambda *args:motion_ok())
    report=clean(moving_source)
    result=assessment.inspect(moving_source,report,cfg|dict(vlm_enabled=False),tmp_path/'qc')
    assert result['decision']['grade']=='A'
    assert not any(c['key']=='lift_height' for c in report['checks'])


@pytest.mark.parametrize('bad',['fake_reference','missing_reference','duplicate','physical_claim'])
def test_grounded_evidence_and_unobservable_risks(moving_source,cfg,tmp_path,monkeypatch,bad):
    reply=motion_ok()
    if bad=='fake_reference':reply['findings'][0]['evidence_ids']=['frame:99999']
    if bad=='missing_reference':reply['findings'][0]['evidence_ids']=[]
    if bad=='duplicate':reply['findings'][1]=reply['findings'][0].copy()
    if bad=='physical_claim':reply['findings'][-1].update(status='pass',evidence_ids=['trajectory:summary'])
    monkeypatch.setattr(vision,'call_vlm',lambda *a:reply)
    result=assessment.inspect(moving_source,clean(moving_source),cfg|dict(vlm_enabled=False),tmp_path/'qc')
    if bad=='physical_claim':
        assert result['motion_review']['findings'][-1]['status']=='not_observable'
        assert result['decision']['grade']=='A'
    else:assert result['decision']['grade']=='REVIEW' and result['errors']['motion']


def test_metrics_warnings_and_hard_fail_not_overridden(moving_source,cfg,tmp_path,monkeypatch):
    monkeypatch.setattr(vision,'call_vlm',lambda *a:motion_ok())
    report=clean(moving_source)
    for state,grade in [('warn','B'),('fail','F')]:
        report['checks'][0]['status']=state
        result=assessment.inspect(moving_source,report,cfg|dict(vlm_enabled=False),tmp_path/state)
        assert result['decision']['grade']==grade


def test_motion_cache_and_combined_usage(moving_source,cfg,tmp_path,monkeypatch):
    calls=[]
    monkeypatch.setattr(vision,'call_vlm',lambda *a:calls.append(1) or motion_ok())
    report=clean(moving_source);cache=tmp_path/'qc'
    for _ in range(2):assessment.inspect(moving_source,report,cfg|dict(vlm_enabled=False),cache)
    assert len(calls)==1
    report['checks'][0]['status']='warn'
    assessment.inspect(moving_source,report,cfg|dict(vlm_enabled=False),cache)
    assert len(calls)==2
    for name in ('motion','category'):
        folder=cache/name;folder.mkdir(exist_ok=True)
        (folder/'usage-ledger.jsonl').write_text(json.dumps(dict(request_hash=name,usage=dict(total_tokens=7)))+'\n')
    assert sum(x['usage']['total_tokens'] for x in vision.usage_records(cache))==14


def test_worker_unavailable_analysis_is_incomplete_retryable(moving_source,cfg,tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DB',tmp_path/'db.sqlite3');db.init()
    monkeypatch.setattr(worker,'VAR',tmp_path/'work');monkeypatch.setattr(worker,'EXPORTS',tmp_path/'exports')
    monkeypatch.setattr(vision,'call_vlm',lambda *a:(_ for _ in ()).throw(vision.APIUnavailable('fixture unavailable')))
    rid=db.create(str(moving_source),'auto',cfg|dict(vlm_enabled=False),[str(moving_source)])
    with pytest.raises(worker.Paused):worker.process_run(db.get_run(rid))
    ep=db.episodes(rid)[0]
    assert ep['status']=='incomplete' and ep['grade']!='F'
    assert ep['data']['visual']['errors']['motion']
    assert db.get_run(rid)['status']=='paused'
