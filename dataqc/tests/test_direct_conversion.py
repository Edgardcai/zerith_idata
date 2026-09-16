from pathlib import Path
import h5py
import numpy as np
import pyarrow.parquet as pq
from test_pipeline import source
from dataqc.conversion_job import build
from dataqc.export import path_for, validate_dataset
from dataqc.io import load, fingerprint, read_json
from integrations.manual_export import direct_entries, post_qc_step


def test_direct_conversion_defers_hard_fail_and_preserves_data(source, tmp_path, monkeypatch):
    # Reproduce the reported three-frame early right-gripper feedback.
    with h5py.File(source/'episode.hdf5', 'a') as f:
        state=f['observation/state/effector/position'][:]
        state[107:110, 1]=.28
        f['observation/state/effector/position'][:]=state
    before=fingerprint(source)
    out=tmp_path/'output'
    # No QC report, no approval, and no derive/trim are needed.
    from dataqc import conversion_job
    monkeypatch.setattr(conversion_job, 'prepare_episode', lambda *_: (_ for _ in ()).throw(AssertionError('QC gate called')))
    result=build(dict(direct=True, entries=[dict(episode_dir=str(source),quality_grade='A')], threshold=40,outputs={'A':str(out)}))
    assert result[0]['result']['quality_check']=='not_run'
    assert result[0]['result']['passed'] is None
    assert not (out/'qc_report.json').exists()
    original=load(source)
    actual=pq.read_table(path_for(out,0)).to_pydict()
    assert len(actual['action'])==original['n']
    np.testing.assert_array_equal(np.array(actual['action'],dtype=np.float32),original['action'].astype(np.float32))
    np.testing.assert_array_equal(np.array(actual['observation.state'],dtype=np.float32),original['state'].astype(np.float32))
    report=validate_dataset(out)
    assert report['passed']
    issues=[i['check'] for i in report['warnings'] if isinstance(i,dict) and 'check' in i]
    assert any(c['key']=='gripper_sequence' and any('110' in t and '107' in t for t in c['detail']['issues']) for c in issues)
    assert any(c['key']=='stationary' for c in issues)
    assert fingerprint(source)==before
    assert read_json(out/'meta/episode_name_mapping.json')['episodes'][0]['grade']=='A'


def test_non_template_prompt_converts_then_fails_qc(source,tmp_path):
    with h5py.File(source/'episode.hdf5','a') as f:
        f.attrs['task_name']='把物品放到桌上'
        del f['subtask_transitions']
    out=tmp_path/'output'
    build(dict(direct=True,entries=[dict(episode_dir=str(source),quality_grade='UNRATED')],threshold=40,outputs={'UNRATED':str(out)}))
    assert not (out/'qc_report.json').exists()
    report=validate_dataset(out)
    assert report['passed']
    assert any(w.get('check')=='prompt' for w in report['warnings'])


def test_direct_selection_ignores_reports_and_retains_all_grades(monkeypatch,tmp_path):
    import workbench
    app=workbench.load_legacy()
    rows=[dict(episode_id='epA',manual_quality_grade='A',quality_grade='F',grade_export_block='硬失败'),
          dict(episode_id='epF',quality_grade='F'),dict(episode_id='pending',quality_grade='B',shared_review_required=True)]
    monkeypatch.setattr(app,'dataset_status',lambda _:dict(episodes=rows))
    monkeypatch.setattr(app,'stringify_config',lambda cfg:cfg)
    monkeypatch.setattr(app,'lerobot_source_episode_entries',lambda _:[dict(episode_id=n) for n in ('epA','epF','pending','new')])
    entries=direct_entries(app,dict(hdf5_root=tmp_path))
    assert [e['quality_grade'] for e in entries]==['A','F','B','UNRATED']
    assert app.lerobot_grade_dataset_dir(dict(lerobot_root=tmp_path,repo_id='test'),'UNRATED')==tmp_path/'test/UNRATED'
    assert 'data-stage="lerobot_post_qc"' in app.HTML


def test_post_qc_button_writes_failure_report_without_removing_output(source,tmp_path,monkeypatch):
    import workbench
    app=workbench.load_legacy()
    out=tmp_path/'A'
    build(dict(direct=True, entries=[dict(episode_dir=str(source),quality_grade='A')],threshold=40,outputs={'A':str(out)}))
    (out/'videos/chunk-000/observation.images.cam_high/episode_000000.mp4').write_bytes(b'broken-video')
    monkeypatch.setattr(app,'available_lerobot_replay_grades',lambda cfg:[dict(dataset_dir=str(out))])
    monkeypatch.setattr(app,'stationary_threshold_for_cfg',lambda cfg:40)
    logs=[]
    monkeypatch.setattr(app,'append_job',lambda job,text:logs.append(text))
    post_qc_step(app,{})({})
    assert read_json(out/'qc_report.json')['passed'] is False
    assert path_for(out,0).exists()
    assert any('质检未通过' in text for text in logs)
    assert read_json(out/'meta/episode_name_mapping.json')['episodes'][0]['grade']=='A'


def test_split_ignores_failed_qc_and_does_not_run_checks(source,tmp_path,monkeypatch):
    from dataqc import export
    from dataqc.io import write_json
    with h5py.File(source/'episode.hdf5','a') as f:
        values=f['observation/state/effector/position'][:]
        values[107:110,1]=.28
        f['observation/state/effector/position'][:]=values
    before=fingerprint(source)
    full=tmp_path/'full'
    build(dict(direct=True,entries=[dict(episode_dir=str(source),quality_grade='A')],threshold=40,outputs={'A':str(full)}))
    report=dict(passed=False,issues=['historical QC failure'])
    write_json(full/'qc_report.json',report)
    monkeypatch.setattr(export,'validate_dataset',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('split must not run QC')))
    results=export.split_dataset(full,tmp_path/'hands')
    assert len(results)==2 and sum(r['frames'] for r in results)==150
    parent=pq.read_table(path_for(full,0))
    for result,(start,end) in zip(results,[(0,90),(90,150)]):
        output=Path(result['path'])
        assert result['quality_check']=='not_run' and result['passed'] is None
        assert not (output/'qc_report.json').exists()
        table=pq.read_table(path_for(output,0))
        for column in ('action','observation.state'):
            assert table[column].equals(parent[column].slice(start,end-start))
    assert read_json(full/'qc_report.json')==report
    assert fingerprint(source)==before


def test_batch_skips_bad_episodes_and_reindexes_successes(source,tmp_path):
    import shutil
    from dataqc.direct_export import create_direct_dataset
    from dataqc.io import write_json
    bad=tmp_path/'bad';shutil.copytree(source,bad)
    (bad/'episode.hdf5').write_bytes(b'broken HDF5')
    other=tmp_path/'other';shutil.copytree(source,other)
    with h5py.File(other/'episode.hdf5','a') as f:f.attrs['task_name']='任意任务描述'
    result=create_direct_dataset([dict(root=str(r),grade='F',direct=True) for r in (bad,source,bad,other)],tmp_path/'batch')
    assert result['status']=='partial' and result['episodes']==2
    assert result['included_indices']==[1,3] and len(result['skipped'])==2
    first=pq.read_table(path_for(tmp_path/'batch',0));second=pq.read_table(path_for(tmp_path/'batch',1))
    assert set(second['episode_index'].to_pylist())=={1}
    assert second['index'].to_pylist()==list(range(len(first),len(first)+len(second)))
    assert set(second['task_index'].to_pylist())=={1}
    assert read_json(tmp_path/'batch/batch_report.json')['failed']==2
    assert not (tmp_path/'batch/qc_report.json').exists()


def test_portable_split_without_hdf5_skips_only_missing_stage(source,tmp_path,monkeypatch):
    import shutil
    from dataqc import export
    from dataqc.io import write_json
    out=tmp_path/'full'
    from dataqc.direct_export import create_direct_dataset
    create_direct_dataset([dict(root=str(source),grade='UNRATED',direct=True)]*2,out)
    path=out/'meta/episode_name_mapping.json';mapping=read_json(path)
    mapping['episodes'][1]['stages']=[];mapping['episodes'][1]['provenance']['stages']=[]
    write_json(path,mapping)
    shutil.rmtree(source)
    monkeypatch.setattr(export,'load',lambda *_: (_ for _ in ()).throw(AssertionError('split must read LeRobot')))
    result=export.split_dataset(out,tmp_path/'hands')
    assert len(result)==2 and sum(r['frames'] for r in result)==150
    report=read_json(tmp_path/'hands/batch_report.json')
    assert report['status']=='partial' and len(report['skipped'])==1
    for r in result:assert not (Path(r['path'])/'qc_report.json').exists()


def test_old_mapping_can_fill_stages_without_completion_attributes(source,tmp_path):
    from dataqc.export import split_dataset
    from dataqc.io import write_json
    out=tmp_path/'full'
    build(dict(direct=True,entries=[dict(episode_dir=str(source),quality_grade='C')],threshold=40,outputs={'C':str(out)}))
    path=out/'meta/episode_name_mapping.json';mapping=read_json(path)
    mapping['episodes'][0]['stages']=[];mapping['episodes'][0]['provenance']['stages']=[]
    write_json(path,mapping)
    assert len(split_dataset(out,tmp_path/'hands'))==2


def test_buttons_ignore_qc_and_completion_attributes_for_all_grades(source,tmp_path,monkeypatch):
    import workbench
    from dataqc.io import write_json
    app=workbench.load_legacy()
    cfg=app.derive_paths(dict(robot_type='zerith',hdf5_root=str(tmp_path/'missing_hdf5'),lerobot_root=str(tmp_path/'twohands'),repo_id='task'))
    from dataqc import export
    monkeypatch.setattr(export,'validate_dataset',lambda *_: (_ for _ in ()).throw(AssertionError('button must not run QC')))
    for grade in ('A','B','C','F','UNRATED'):
        directory=app.lerobot_grade_dataset_dir(cfg,grade)
        write_json(directory/'meta/info.json',dict(total_episodes=2))
        write_json(directory/'meta/episode_name_mapping.json',dict(episodes=[dict(source_format='zerith_sim_v1')]))
        write_json(directory/'qc_report.json',dict(passed=False))
    status=app.lerobot_stage_split_status(cfg)
    assert status['available'] and len(status['grades'])==5
    assert app.lerobot_stage_split_output_dir(cfg,'left_hand','UNRATED').name=='UNRATED'
    assert 'LeRobot 质检（可选）' in app.HTML and '直接切分左右手' in app.HTML


def test_no_success_leaves_existing_output_unpublished(source,tmp_path):
    from dataqc.direct_export import create_direct_dataset
    result=create_direct_dataset([dict(root=str(tmp_path/'missing'),grade='A',direct=True)],tmp_path/'output')
    assert result['path'] is None and result['episodes']==0
    assert not (tmp_path/'output').exists()
    assert read_json(result['batch_report'])['status']=='failed'


def test_manual_split_all_grades_continues_after_bad_grade(source,tmp_path,monkeypatch):
    import shutil,workbench
    from integrations.manual_export import split_step
    from dataqc.direct_export import create_direct_dataset
    from dataqc import config
    from dataqc.io import write_json
    app=workbench.load_legacy()
    cfg=app.derive_paths(dict(robot_type='zerith',hdf5_root=str(source.parent),lerobot_root=str(tmp_path/'lerobot/twohands'),repo_id='portable',lerobot_cuda=False))
    for grade in ('A','C','F','UNRATED'):
        create_direct_dataset([dict(root=str(source),grade=grade,direct=True)],app.lerobot_grade_dataset_dir(cfg,grade))
    (app.lerobot_grade_dataset_dir(cfg,'A')/'meta/episodes.jsonl').write_text('not-json')
    shutil.rmtree(source)
    monkeypatch.setattr(config,'VAR',tmp_path/'var')
    logs=[];monkeypatch.setattr(app,'append_job',lambda job,text:logs.append(text))
    job={};split_step(app,cfg)(job)
    report=read_json(tmp_path/'var/manual-last-split.json')
    assert report['completed_segments']==6 and len(report['skipped'])==1
    assert '部分完成' in job['label']
    for grade in ('C','F','UNRATED'):
        for hand in ('left_hand','righthand'):
            assert (app.lerobot_stage_split_output_dir(cfg,hand,grade)/'meta/info.json').exists()


def test_optional_qc_still_measures_export_without_hdf5(source,tmp_path):
    import shutil
    out=tmp_path/'full'
    build(dict(direct=True,entries=[dict(episode_dir=str(source),quality_grade='A')],threshold=40,outputs={'A':str(out)}))
    shutil.rmtree(source)
    report=validate_dataset(out)
    assert report['source_correspondence_checked'] is False
    assert report['frames']==150
    assert any(isinstance(issue,dict) and isinstance(issue.get('check'),dict) and issue['check'].get('key')=='stationary' for issue in report['warnings'])
    assert all('Unable to' not in str(issue) for issue in report['warnings'])


def test_duplicate_source_is_reported_without_stopping_batch(source,tmp_path):
    entry=dict(episode_dir=str(source),quality_grade='A')
    result=build(dict(direct=True,entries=[entry,entry],threshold=40,outputs={'A':str(tmp_path/'A')}))
    assert result[0]['result']['episodes']==1 and len(result[0]['result']['skipped'])==1
    assert read_json(tmp_path/'A/batch_report.json')['status']=='partial'


def test_load_lerobot_directory_derives_paths_without_hdf5(tmp_path):
    import workbench
    from dataqc.io import write_json
    app=workbench.load_legacy();source=tmp_path/'lerobot/twohands/task/UNRATED'
    write_json(source/'meta/info.json',dict(robot_type='zerith',total_episodes=2))
    choice=app.dataset_choice_entry(source,tmp_path)
    assert choice['dataset_type']=='lerobot' and choice['robot_type']=='zerith'
    cfg=app.derive_paths(dict(robot_type='zerith',lerobot_root=str(source),lerobot_source_path=str(source)))
    assert cfg['lerobot_dataset_dir']==source and cfg['repo_id']=='task'
    assert cfg['lerobot_root']==tmp_path/'lerobot/twohands'
    assert app.lerobot_stage_split_status(cfg)['available']
    group=app.dataset_choice_entry(source.parent,tmp_path)
    assert group['dataset_type']=='lerobot_group'
    cfg=app.derive_paths(dict(robot_type='zerith',lerobot_root=str(source.parent),lerobot_source_path=str(source.parent)))
    assert app.lerobot_stage_split_status(cfg)['source_grades']==['UNRATED']
