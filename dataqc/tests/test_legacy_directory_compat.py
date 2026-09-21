"""Historical name-based failures are display migrations, never save-time gates."""
import copy
import pytest
from dataqc.quality_policy import display_report
from dataqc.reporting import presentation


def test_historical_timestamp_failure_is_warning():
    checks=[dict(key=k,label=k,status='pass',detail={}) for k in ('state','action','finite')]
    checks.append(dict(key='timestamps',label='时间戳',status='fail',detail={}))
    report=dict(quality_grade='F',raw_report=dict(checks=checks))
    projected=display_report(report)
    assert projected['quality_grade']=='B'
    assert projected['raw_report']['checks'][-1]['status']=='warn'

@pytest.mark.parametrize('actual_failure',[False,True])
def test_old_name_failure_retired_without_hiding_corrupt_values(actual_failure):
    checks=[dict(key=k,label=k,status='fail' if k=='finite' and actual_failure else 'pass',detail={}) for k in ('state','action','finite')]
    checks.append(dict(key='lift_height',label='升降柱高度',status='fail',detail=dict(error='目录末尾缺少升降高度，无法核对预期值')))
    report=dict(quality_grade='F',accepted=False,rules_version='zerith_qc_5',raw_report=dict(version='zerith_qc_5',checks=checks))
    original=copy.deepcopy(report)
    projected=display_report(report)
    assert projected['quality_grade']==('F' if actual_failure else 'B')
    assert not any(c['key']=='lift_height' for c in projected['raw_report']['checks'])
    assert not any(c['key']=='lift_height' for c in presentation(report)['items'])
    assert report==original


@pytest.mark.parametrize('manual',[False,True])
def test_current_b_keeps_specific_reason_and_manual_choice(manual):
    checks=[dict(key=k,label=k,status='pass',detail={}) for k in ('state','action','finite')]
    reason='右手阶段完成情况无法确认，需人工查看末帧'
    baseline=dict(quality_grade='B',accepted=False,review_required=True,reason=reason)
    report=dict(baseline,raw_report=dict(checks=checks),qc_original=dict(baseline))
    if manual:report.update(manual_review=True,quality_grade='A',reason='人工已确认')
    original=copy.deepcopy(report)
    projected=display_report(report)
    assert projected['qc_original']['reason']==reason
    assert projected['quality_grade']==('A' if manual else 'B')
    assert projected['reason']==('人工已确认' if manual else reason)
    assert report==original
    if not manual:
        assert reason in presentation(projected)['summary']


def test_historical_f_keeps_reason_and_explains_retired_rule():
    checks=[dict(key=k,label=k,status='pass',detail={}) for k in ('state','action','finite')]
    checks.append(dict(key='lift_height',label='升降柱高度',status='fail',detail={}))
    reason='目标高度 0.800 m，实测 0.400 m'
    report=dict(quality_grade='F',reason=reason,raw_report=dict(checks=checks))
    projected=display_report(report)
    assert projected['quality_grade']=='B'
    assert reason in projected['reason']
    assert '不再作为硬失败的检查：升降柱高度' in projected['reason']
    assert '历史原因：' in projected['reason']
    assert display_report(projected)['reason']==projected['reason']


def test_current_c_is_not_regraded_because_review_is_pending():
    checks=[dict(key=k,label=k,status='pass',detail={}) for k in ('state','action','finite')]
    report=dict(quality_grade='C',review_required=True,reason='人工待核对',raw_report=dict(checks=checks))
    assert display_report(report)['quality_grade']=='C'
