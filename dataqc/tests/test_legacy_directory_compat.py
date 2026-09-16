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
