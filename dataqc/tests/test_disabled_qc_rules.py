import copy

import h5py
import pytest

from dataqc import assessment, motion_review, config
from dataqc.checks import raw_checks
from dataqc.gripper_policy import project_motion
from dataqc.io import write_json
from dataqc.quality_policy import display_report
from dataqc.reporting import presentation
from dataqc.vision import validate_decision
from test_pipeline import source


def historical_report():
    checks = [dict(key=k, label=k, status='pass', detail={}, score_delta=0)
              for k in ('state', 'action', 'finite')]
    checks += [dict(key='gripper_sequence', label='夹爪次数 / 阶段 / 反馈', status='warn', score_delta=0,
        detail=dict(channels={h: dict(action_close_frames=[], state_close_frames=[], expected_closures=1)
                              for h in ('left', 'right')},
                    stages=[dict(hand='left', start=0, end=50), dict(hand='right', start=50, end=100)],
                    issues=['左爪 Action 闭合 0 次，应为 1 次'])),
        dict(key='prompt', label='提示词格式', status='warn', score_delta=-5,
             detail=dict(original='Place Milk on shelf.', issues=['任务开头缺少 Grasp']))]
    findings = [dict(criterion=c, status='not_observable' if c.endswith('风险') else 'pass',
                    reason='既有数据支持', evidence_ids=[] if c.endswith('风险') else ['check:state'])
                for c in motion_review.CRITERIA]
    findings[0].update(status='suspected', reason='提示词格式检查预警：缺少规定的Grasp开头', evidence_ids=['check:prompt'])
    findings[2].update(status='suspected', reason='双爪指令及独立反馈均无新增闭合，未达各1次要求', evidence_ids=['check:gripper_sequence'])
    return dict(quality_grade='B', accepted=False, review_required=True, reason='旧规则待复核',
        execution='completed', raw_report=dict(version='old', frames=100, task='Place Milk on shelf.', score=95, checks=checks),
        visual=dict(assessment_version=assessment.VERSION, model='fixture',
            motion_review=dict(status='review', execution='completed', findings=findings,
                evidence={'check:prompt': dict(status='warn', label='提示词格式', grading_effect='B', detail={})}),
            category_review=dict(status='skipped', enabled=False)),
        qc_original=dict(quality_grade='B', accepted=False, review_required=True, reason='旧规则待复核'))


def test_historical_count_and_prompt_only_becomes_a_without_mutating_evidence():
    original = historical_report(); before = copy.deepcopy(original)
    report = display_report(original)
    assert report['quality_grade'] == report['qc_original']['quality_grade'] == 'A'
    assert report['accepted'] and not report['review_required']
    assert report['raw_report']['score'] == 100
    assert report['visual']['motion_review']['status'] == 'pass'
    assert not any(item['problem'] for item in presentation(report)['items'])
    assert original == before and display_report(report) == report


@pytest.mark.parametrize('remaining', ['stationary', 'feedback', 'hard_failure', 'incomplete', 'manual'])
def test_disabled_rules_preserve_other_evidence_and_human_grades(remaining):
    report = historical_report()
    if remaining == 'stationary':
        report['raw_report']['checks'].append(dict(key='stationary', label='连续静止', status='warn', score_delta=-15, detail={}))
    elif remaining == 'feedback':
        check = report['raw_report']['checks'][3]
        check['detail']['issues'].append('left Action 第 30 帧闭合，State 第 60 帧响应，超出允许延迟')
    elif remaining == 'hard_failure':
        report['raw_report']['checks'][2]['status'] = 'fail'
        report.update(quality_grade='F'); report['qc_original']['quality_grade'] = 'F'
    elif remaining == 'incomplete': report['execution'] = 'incomplete'
    else: report.update(manual_review=True, quality_grade='C', reason='人工保留')
    current = display_report(report)
    assert current['quality_grade'] == dict(stationary='B', feedback='B', hard_failure='F', incomplete='', manual='C')[remaining]
    if remaining == 'manual':
        assert current['reason'] == '人工保留' and current['qc_original']['quality_grade'] == 'A'


def test_feedback_model_finding_is_not_a_retired_count_issue():
    motion = dict(status='review', findings=[dict(criterion='夹爪配合', status='suspected',
        reason='仅一次闭合，但反馈延迟30帧', evidence_ids=['check:gripper_sequence'])])
    assert project_motion(motion) == motion


def test_place_prompt_and_metadata_conflict_are_not_qc_problems(source):
    task = 'Place Milk on the shelf with the left hand, then place Tea with the right hand.'
    with h5py.File(source / 'episode.hdf5', 'a') as f:
        f.attrs['task_name'] = task
    write_json(source / 'collection_task.json', dict(config=dict(task_name='不同的自由文本'), targets=dict(left='Other')))
    report = raw_checks(source, threshold=100)
    prompt = next(c for c in report['checks'] if c['key'] == 'prompt')
    assert prompt['status'] == 'na' and prompt['score_delta'] == 0
    assert all(c['status'] == 'pass' for c in report['checks'] if c['key'] == 'metadata')
    decision = assessment.inspect(source, report, config.settings() | dict(vlm_enabled=False), source.parent / 'cache')['decision']
    assert decision['grade'] in ('A', 'B'), decision['reason']
    assert '提示词' not in decision['reason'] and '阶段标注缺失' not in decision['reason']
    validate_decision(decision, dict(n=150, task=task), [])
