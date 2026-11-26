import copy
from pathlib import Path

import numpy as np
import pytest

from dataqc import batch_motion, motion_review, recorded_task, vision
from dataqc.motion import arm_and_posture_checks, gripper_check
from dataqc.io import write_json
from test_batch_motion import items_for, response
from test_yolo_gate import moving_source, cfg
from test_pipeline import source


def data():
    state = np.zeros((160, 23)); action = state.copy()
    state[:, 17] = action[:, 17] = .3
    state[:, 20] = action[:, 20] = .2
    state[:10, 7] = .3; action[:10, 7] = 1.5
    state[:50, 15] = .3; action[:50, 15] = 1.5
    state[90:, 7] = .3; action[90:, 7] = 1.5
    state[130:, 15] = .3; action[130:, 15] = 1.5
    return dict(state=state, action=action, t=np.arange(160)/30, n=160, task='Free task text',
        transitions=[40,80,120,160], collection=dict(targets=dict(prompt_mode='tabletop',
        stage_prompts=['Place the item held in the left hand on the table.',
                       'Place the item held in the right hand on the table.',
                       'Grasp Milk from the table with the left hand.',
                       'Grasp Tea from the table with the right hand.'],
        fixed_pose=dict(config=dict(zip(recorded_task.ANGLE_FIELDS,[.3,0,0,.2]))))))


def old_report(d):
    return dict(checks=arm_and_posture_checks(d['state'],d['action'],d['t'])+
        [gripper_check(d['state'],d['action'],d['task'],d['transitions'])])


def test_recorded_pose_and_all_four_stages_use_metadata_not_zero_or_task_wording():
    d=data();old=old_report(d);original=copy.deepcopy(old)
    corrected=recorded_task.correct(d,old)
    checks={c['key']:c for c in corrected['checks']}
    assert checks['posture_state']['status']=='pass'
    assert checks['posture_action']['status']=='pass'
    assert checks['posture_state']['detail']['channels'][0]['target_rad']==.3
    assert checks['gripper_sequence']['status']=='pass'
    assert [s['hand'] for s in checks['gripper_sequence']['detail']['stages']]==['left','right','left','right']
    assert [s['operation'] for s in checks['gripper_sequence']['detail']['stages']]==['place','place','grasp','grasp']
    assert old==original
    assert recorded_task.correct(d,corrected) is corrected


@pytest.mark.parametrize('bad',['posture','boundary','feedback'])
def test_actual_errors_remain_visible(bad):
    d=data()
    if bad=='posture':d['state'][:,20]=.25
    if bad=='boundary':d['transitions']=[40,80,120,159]
    if bad=='feedback':d['state'][90:115,7]=0
    report=recorded_task.correct(d,old_report(d));checks={c['key']:c for c in report['checks']}
    assert checks['posture_state' if bad=='posture' else 'gripper_sequence']['status']=='warn'


def test_absent_reference_does_not_treat_observed_pose_as_a_target():
    d=data();d['collection']['targets']['fixed_pose']={}
    report=recorded_task.correct(d,old_report(d))
    assert next(c for c in report['checks'] if c['key']=='posture_state')['status']=='warn'


def test_cache_projection_removes_only_wrong_references_and_keeps_real_warnings():
    d=data();new=recorded_task.correct(d,old_report(d))
    evidence={'check:'+c['key']:c for c in new['checks']}
    evidence['check:stationary']=dict(status='warn',detail=dict(issues=['连续静止65帧，超过40帧']))
    refs=['check:posture_state','check:stationary','check:gripper_sequence','check:gripper_sequence']
    findings=[dict(criterion=c,status='suspected' if i<4 else 'not_observable',
        reason='旧预警',evidence_ids=[refs[i]] if i<4 else []) for i,c in enumerate(motion_review.CRITERIA)]
    findings[1]['reason']='连续静止65帧，超过40帧'
    old=dict(signature='old',findings=findings,elapsed_seconds=10)
    result=recorded_task.project_review(old,dict(evidence=evidence,coverage={},semantics={}), 'new', dict(api_model='fixture'))
    assert [f['status'] for f in result['findings'][:4]]==['pass','suspected','pass','pass']
    assert '65帧' in result['findings'][1]['reason']
    assert all(f['status']=='not_observable' for f in result['findings'][4:])
    assert result['status']=='review' and result['signature']=='new'
    assert result['recorded_task_projection']['original_signature']=='old'


def test_batch_fast_mode_keeps_suspected_findings_without_single_model_calls(moving_source,cfg,tmp_path,monkeypatch):
    items=items_for(moving_source,tmp_path,2)
    def call(content,*_):
        reply=response(content)
        for e in reply['episodes']:
            e['needs_detail']=True;e['statuses'][1]='suspected'
            e['issues']=[dict(criterion=1,reason='连续性待人工复核',evidence_ids=['trajectory:summary'])]
        return reply
    monkeypatch.setattr(vision,'call_vlm',call)
    monkeypatch.setattr(motion_review,'inspect',lambda *a,**k:pytest.fail('No redundant single review'))
    results=batch_motion.review_many(items,cfg|dict(motion_detail_review=False),tmp_path/'batches')
    assert all(o['result']['status']=='review' for o in results.values())
    assert all(o['result']['batch']['detail_requested'] and not o['result']['batch']['detail_executed'] for o in results.values())
