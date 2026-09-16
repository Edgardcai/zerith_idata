import json
from pathlib import Path
import sys

import h5py
import numpy as np
import pytest

from test_pipeline import source
from test_yolo_gate import moving_source, cfg, detector
from dataqc import config, vision, yolo_gate
from dataqc.checks import raw_checks
from dataqc.io import fingerprint, write_json, read_json
from dataqc.export import validate_dataset
from dataqc.worker import visual_decision

ROOT=Path(__file__).resolve().parents[1]
LEGACY=ROOT/'legacy/scripts/embodied_data_pipeline-main'
sys.path.insert(0,str(LEGACY))
from quality_pipeline.episode_io import read_raw_episode
from quality_pipeline.profiles import load_profile
from quality_pipeline.qc import run_quality_checks
from integrations.zerith_rules import run_manual_checks


@pytest.fixture
def shared(moving_source,cfg,tmp_path,monkeypatch):
    monkeypatch.setattr(config,'settings',lambda:dict(cfg))
    monkeypatch.setattr(config,'VAR',tmp_path/'work')
    monkeypatch.setattr(yolo_gate,'predict_samples',detector())
    profile=load_profile(LEGACY/'robot_profiles/zerith.yaml')
    return moving_source,profile


def test_manual_and_auto_share_measurements_and_grade(shared,cfg,tmp_path):
    root,profile=shared
    with h5py.File(root/'episode.hdf5','a') as f:
        t=f['timestamp/t'][:];t[95:]+=150;f['timestamp/t'][:]=t
    before=fingerprint(root)
    manual=run_quality_checks(read_raw_episode(root,profile),profile)
    auto=raw_checks(root,cfg['stationary_frames'])
    visual=yolo_gate.inspect(root,auto,cfg,tmp_path/'auto')
    assert manual['raw_report']==auto
    assert manual['quality_grade']==visual_decision(visual,auto)['grade']=='B'
    assert manual['accepted'] and not manual['review_required']
    gap=next(c for c in manual['checks']if c['name']=='timestamp_monotonic')
    assert gap['status']=='warn' and gap['detail']['gaps'][0]['frame']==95
    assert gap['detail']['gaps'][0]['interval_seconds']==pytest.approx(.183333333)
    assert '94→95' in gap['detail']['shared_text']
    assert fingerprint(root)==before


def test_manual_hard_fail_never_calls_vision(shared,monkeypatch):
    root,profile=shared
    with h5py.File(root/'episode.hdf5','a') as f:
        a=f['action/arm/position'][:];a[50,0]=float('nan');f['action/arm/position'][:]=a
    monkeypatch.setattr(yolo_gate,'inspect',lambda *a,**kw:pytest.fail('Hard fail must stop vision'))
    report=run_quality_checks(read_raw_episode(root,profile),profile)
    assert report['quality_grade']=='F' and not report['accepted'] and '50' in report['reason']


def test_zerith_and_aloha_closure_dimensions_are_separate():
    import lerobot_manual_screening as screening
    a=np.zeros((140,23));a[45:,7]=1.5;a[95:,15]=1.5
    assert [v['frame_index'] for v in screening.locate_prompt_closures(a,['left','right'])]==[45,95]
    a=np.ones((140,14));a[35:,6]=0;a[85:,13]=0
    assert [v['frame_index']for v in screening.locate_prompt_closures(a,['left','right'])]==[35,85]


def test_manual_warning_grade_not_overwritten_by_collector_a(shared,tmp_path):
    import workbench
    app=workbench.load_legacy()
    root,profile=shared
    report=run_manual_checks(read_raw_episode(root,profile),profile)
    row=dict(episode_id=root.name,manual_quality_grade='A',**app.qc_warning_summary_from_report(report))
    assert app.finalise_qc_rows([row])[0]['quality_grade']=='B'
    row.update(shared_quality_grade='F',manual_quality_grade='A')
    assert app.finalise_qc_rows([row])[0]['quality_grade']=='F'


def test_manual_export_roundtrip_and_split(shared,tmp_path,monkeypatch):
    import workbench
    from integrations.manual_export import convert_step,split_step
    app=workbench.load_legacy()
    root,profile=shared
    with h5py.File(root/'episode.hdf5','a') as f:
        f.attrs.update(total_subtasks=2,completed_subtasks=2)
    before=fingerprint(root)
    report=run_manual_checks(read_raw_episode(root,profile),profile)
    report_dir=tmp_path/'report';write_json(report_dir/'qc_report.json',report)
    entry=dict(episode_id=root.name,episode_dir=root,hdf5_file=root/'episode.hdf5',quality_grade='B',source_episode_index=0,status_row=dict(qc_output=str(report_dir)))
    from integrations import manual_export
    monkeypatch.setattr(manual_export,'direct_entries',lambda app,cfg:[entry])
    monkeypatch.setattr(app,'stationary_threshold_for_cfg',lambda cfg:40)
    monkeypatch.setattr(app,'lerobot_grade_dataset_dir',lambda cfg,g:tmp_path/'exports'/g)
    monkeypatch.setattr(app,'lerobot_grade_repo_id',lambda cfg,g:'test/'+g)
    monkeypatch.setattr(app,'lerobot_stage_split_output_dir',lambda cfg,h,g:tmp_path/'split'/h/g)
    monkeypatch.setattr(app,'build_renumber_plan',lambda cfg,e:[])
    monkeypatch.setattr(app,'write_renumber_plan_step',lambda cfg,e:lambda job:None)
    monkeypatch.setattr(app,'append_job',lambda job,text:None)
    convert_step(app,{'hdf5_root':root})({})
    assert validate_dataset(tmp_path/'exports/B')['passed']
    # Exercise the UI gate as well as the split job: previously the latter
    # passed while the missing num_frames field kept the button disabled.
    preflight_cfg={'hdf5_root':root,'robot_type':'zerith'}
    preflight=app.validate_lerobot_stage_split_grade(preflight_cfg,'B',tmp_path/'exports/B')
    assert preflight['compatible'],preflight
    assert preflight['checked_episode_count']==1
    split_step(app,{})({})
    for hand in ['left_hand','righthand']:
        assert validate_dataset(tmp_path/'split'/hand/'B')['passed']
        maps=read_json(tmp_path/'split'/hand/'B/meta/episode_name_mapping.json')['episodes']
        assert all(m['num_frames']==len(m['source_frames']) for m in maps)
    assert fingerprint(root)==before
    # Direct conversion does not depend on the earlier QC fingerprint.
    (root/'meta.json').write_text('{}')
    convert_step(app,{'hdf5_root':root})({})
    assert not (tmp_path/'exports/B/qc_report.json').exists()


def test_all_grades_and_unrated_included_without_qc_gates(tmp_path,monkeypatch):
    import workbench
    app=workbench.load_legacy()
    rows=[dict(episode_id=str(i),quality_grade=g,shared_rules_version='zerith_qc_5',shared_quality_grade=g,shared_review_required=review)for i,(g,review)in enumerate([('A',False),('B',False),('F',False),('B',True),('',True)])]
    monkeypatch.setattr(app,'dataset_status',lambda _:dict(episodes=rows))
    monkeypatch.setattr(app,'stringify_config',lambda cfg:cfg)
    monkeypatch.setattr(app,'lerobot_source_episode_entries',lambda _: [dict(episode_id=str(i))for i in range(5)])
    assert [e['quality_grade']for e in app.authoritative_quality_grade_entries(dict(robot_type='zerith',hdf5_root=tmp_path))]==['A','B','F','B','UNRATED']


def test_manual_export_rebinds_new_model_report_without_changing_human_grade(tmp_path,monkeypatch):
    import workbench
    from integrations import zerith_rules
    app=workbench.load_legacy()
    row=dict(episode_id='episode1',quality_grade='A',manual_quality_grade='A',
             shared_rules_version='zerith_qc_5',shared_quality_grade='B',shared_review_required=True)
    monkeypatch.setattr(app,'dataset_status',lambda _:dict(episodes=[row]))
    monkeypatch.setattr(app,'stringify_config',lambda cfg:cfg)
    monkeypatch.setattr(app,'lerobot_source_episode_entries',lambda _:[dict(episode_id='episode1')])
    def forbidden(*args):pytest.fail('manual conversion must not execute QC approval')
    monkeypatch.setattr(zerith_rules,'prepare_manual_approval',forbidden,raising=False)
    entries=app.authoritative_quality_grade_entries(dict(robot_type='zerith',hdf5_root=tmp_path))
    assert entries[0]['quality_grade']=='A' and row['quality_grade']=='A'


def test_old_cli_status_conversion_and_manual_approval(shared,tmp_path,monkeypatch):
    import workbench,shutil,subprocess,os
    app=workbench.load_legacy()
    source,profile=shared
    group=tmp_path/'dataset_Milk_Tea_0';root=group/'episode_000000'
    shutil.copytree(source,root)
    env_home=tmp_path/'runtime';settings=config.settings()
    write_json(env_home/'config/settings.json',settings)
    monkeypatch.setattr(config,'VAR',env_home/'var')
    # Populate only inference results; the CLI still measures / grades / writes all reports.
    report=run_manual_checks(read_raw_episode(root,profile),profile)
    payload=dict(robot_type='zerith',dataset_name=group.name,data_root=str(tmp_path/'data'),hdf5_root=str(group),
                 qc_root=str(tmp_path/'qc'),lerobot_root=str(tmp_path/'lerobot/twohands'),repo_id='fixture',stationary_threshold=40,convert_jobs=1)
    cfg=app.derive_paths(payload)
    cmd,cwd,env=app.qc_command(cfg)
    env.update(DATAQC_HOME=str(env_home),PYTHONPATH=os.pathsep.join([str(ROOT),str(ROOT/'vendor')]))
    result=subprocess.run(cmd,cwd=cwd,env=env,text=True,capture_output=True,timeout=45)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
    status=app.dataset_status(payload)
    row=status['episodes'][0]
    from dataqc.motion import RULE_VERSION
    assert row['quality_grade']=='B' and row['shared_rules_version']==RULE_VERSION,row
    assert not row['shared_review_required'] and row['warning_count']>0
    # Explicit manual grading persists, including approval before conversion.
    app.save_qc_report_quality_grade(cfg,root.name,'B','人工复核完成')
    monkeypatch.setattr(app,'append_job',lambda *args:None)
    for step in app.lerobot_commands(cfg):step({})
    output=app.lerobot_grade_dataset_dir(cfg,'B')
    assert validate_dataset(output)['passed']
    status=app.dataset_status(payload)
    assert status['episodes'][0]['quality_grade']=='B'
    for step in app.split_lerobot_stage_commands(cfg):step({})
    for hand in ('left_hand','righthand'):
        assert validate_dataset(app.lerobot_stage_split_output_dir(cfg,hand,'B'))['passed']


def test_selected_grade_screening_does_not_include_siblings(tmp_path):
    """Batch processing a selected physical folder must not scan its siblings."""
    import importlib.util
    import workbench
    from lerobot_fixture import make_dataset
    make_dataset(tmp_path/'sample'/'A')
    make_dataset(tmp_path/'sample'/'B')
    app=workbench.load_legacy()
    a=app.manual_screening.discover_datasets(tmp_path/'sample'/'A')['datasets'][0]
    b=app.manual_screening.discover_datasets(tmp_path/'sample'/'B')['datasets'][0]
    combined=app.manual_screening.discover_datasets(tmp_path/'sample')['datasets'][0]
    assert len({a['id'],b['id'],combined['id']})==3
    for group,grade in [(a,'A'),(b,'B')]:
        selected=app.manual_screening.find_dataset_group(group['id'])
        assert [source['path'] for source in selected['sources']]==[str(tmp_path/'sample'/grade)]
        assert selected['total_episodes']==2
    assert combined['total_episodes']==4
