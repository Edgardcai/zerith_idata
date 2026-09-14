from pathlib import Path
import shutil
import h5py
import numpy as np
import pytest
from test_pipeline import source
from dataqc.io import load,write_json,parse_task,fingerprint
from dataqc.motion import source_height,lift_check,RULE_VERSION
from dataqc.checks import numeric_checks
from dataqc.reporting import presentation


def test_mixed_targets_and_names_do_not_determine_height(tmp_path):
    s=np.zeros((3,23));a=s.copy()
    for name,height in [('anything_9',.4),('renamed',.8)]:
        root=tmp_path/'merged'/name
        write_json(root/'collection_task.json',dict(targets=dict(lift_height=str(height))))
        s[:,16]=a[:,16]=height
        assert lift_check(s,a,root)['status']=='pass'
        assert source_height(root)['policy']=='episode_target'
        a[1,16]+=0.03
        assert lift_check(s,a,root)['detail']['channels']['action']['bad_frames']==[1]


@pytest.mark.parametrize('value',[True,'unknown','NaN','Infinity'])
def test_invalid_reference_is_incomplete_not_measured_failure(tmp_path,value):
    write_json(tmp_path/'collection_task.json',dict(targets=dict(lift_height=value)))
    s=np.zeros((2,23));check=lift_check(s,s,tmp_path)
    assert check['status']=='warn' and check['detail']['requires_review']


def test_conflicting_targets_are_not_silently_chosen(tmp_path):
    write_json(tmp_path/'collection_task.json',dict(targets=dict(lift_height=.4)))
    write_json(tmp_path/'episode_meta.json',dict(target_lift_height=.8))
    ref=source_height(tmp_path)
    assert '冲突' in ref['error'] and len(ref['references'])==2


def test_action_fallback_and_task_survive_rename_without_source_mutation(source,tmp_path):
    renamed=tmp_path/'random_99';shutil.move(source,renamed)
    before=fingerprint(renamed)
    assert source_height(renamed)['expected_m']==0
    assert source_height(renamed)['policy']=='per_episode_first_action'
    assert load(renamed)['task'].startswith('Grasp Milk')
    assert fingerprint(renamed)==before


def test_prompt_fallback_and_optional_period(source):
    with h5py.File(source/'episode.hdf5','a') as f:del f.attrs['task_name']
    task='Grasp Tea with the right hand.'
    write_json(source/'collection_task.json',dict(config=dict(task_name=task)))
    assert load(source)['task']==task and parse_task(task)=={'right':'Tea'}
    assert not parse_task('Grasp Tea with the right hand. extra')


def test_all_timestamp_locations_visible_and_not_truncated():
    s=np.zeros((30,23));t=np.arange(30)*.2
    raw=dict(version=RULE_VERSION,frames=30,checks=numeric_checks(s,s,t))
    report=presentation(raw)
    gaps=[i for i in report['items'] if i['key']=='timestamps']
    assert len(gaps)==29 and gaps[-1]['frames']==[29]
    assert all('200.0 ms' in i['text'] for i in gaps)


def test_nan_locations_and_model_unobservable_are_distinct():
    s=np.zeros((3,23));s[1,12]=np.nan
    raw=dict(version=RULE_VERSION,frames=3,checks=numeric_checks(s,np.zeros_like(s),np.arange(3)/30))
    report=presentation(dict(raw_report=raw,visual=dict(motion_review=dict(findings=[dict(criterion='掉落风险',status='not_observable',reason='缺少物体运动证据',evidence_ids=[])]))))
    invalid=next(i for i in report['items'] if i['key']=='finite')
    assert invalid['frames']==[1] and '[1, 12]' in invalid['text']
    risk=next(i for i in report['items'] if i['label']=='掉落风险')
    assert not risk['problem'] and risk['status']=='na'


def test_old_report_is_explicitly_outdated_without_changing_grade():
    source=dict(quality_grade='A',raw_report=dict(version='zerith_qc_5',checks=[]))
    report=presentation(source)
    assert report['outdated'] and '旧规则' in report['summary']
    assert source['quality_grade']=='A'


def test_comparison_task_type_is_independent_of_path():
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'legacy/scripts/embodied_data_pipeline-main'))
    from zerith_lerobot_qc import prompt_mode,prompt_error
    assert prompt_error('Grasp Coca-Cola with the left hand.',prompt_mode(Path('/righthand/renamed/A'))) == ''
