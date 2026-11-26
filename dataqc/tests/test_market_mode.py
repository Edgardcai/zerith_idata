import json
from pathlib import Path
import shutil

import h5py
import numpy as np
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from test_pipeline import source
from dataqc import market, market_api, market_job
from dataqc.io import load, write_json
from dataqc.export import path_for, rows


@pytest.fixture
def batch(source,tmp_path,monkeypatch):
    base=tmp_path/'data';root=base/'market_recordings2'/'group_9078';root.mkdir(parents=True)
    p=root/'20261021_9078_1';shutil.copytree(source,p)
    phase_steps=[]
    for i,(phase,b,e,prompt) in enumerate(zip(market.PHASES,[0,30,120,140],[30,120,140,150],
            ['Navigate to A.','Grasp Milk with the left hand and then grasp Tea with the right hand.','Bring items to the handoff point.','Hand over the items.'])):
        phase_steps.append(dict(phase=phase,start_frame_id=b,end_frame_id=e-1,frame_count=e-b,prompt=prompt,outcome='completed',
                               items=[dict(hand='left',target='Milk'),dict(hand='right',target='Tea')]))
    write_json(p/'episode_meta.json',dict(episode_id='1'*32,complete=True,outcome='completed',step_index=phase_steps,inference=dict(orderId='test',remoteUrl='ws://localhost:9078')))
    write_json(p/'review.json',dict(episode_uuid='1'*32,grade='A',reviewed=True,grade_source='manual',updated_at=1,product_recognition=dict(status='completed',grade='F')))
    with h5py.File(p/'episode.hdf5','r+') as f:
        f.attrs['task_name']='Navigate to A.'
        f.attrs['chassis_recorded']=True
        del f['subtask_transitions'];f.create_dataset('subtask_transitions',data=[30,120,140,150])
        f['action/effector/position'][142:]=0
        f['observation/state/effector/position'][143:]=0
    monkeypatch.setattr(market_api,'BASE',base)
    monkeypatch.setattr(market_job,'BASE',base)
    monkeypatch.setattr(market_api,'HOME',tmp_path/'reports')
    monkeypatch.setattr(market_job,'HOME',tmp_path/'reports')
    return base,p


def test_four_phases_no_grasp_only_false_warning(batch):
    base,p=batch
    report=market.inspect(p)
    assert [s['phase'] for s in report['phases']]==list(market.PHASES)
    assert next(c for c in report['checks'] if c['key']=='phase_layout')['status']=='pass'
    assert not any(c['key'] in ('prompt','gripper_sequence','duration','stationary') for c in report['checks'])
    assert not report['hard_fail']
    assert report['phases'][1]['yolo']['grade']=='F'
    assert report['phases'][1]['auto_grade']=='B'
    write_json(market_api.report_path(p),report)
    row=market_api.snapshot(p,base,True)
    assert row['effective_grade']=='A' and row['manual_grade']=='A'


def test_missing_overlap_and_unobserved_actions(batch):
    _,p=batch;d=load(p)
    d['meta']['step_index'][1]['start_frame_id']=29
    d['meta']['step_index'].pop()
    phases,checks=market.inspect_phases(d)
    assert checks[0]['status']=='warn'
    assert any(p.get('missing') and p['phase']=='handover' for p in phases)
    d=load(p);d['action'][40,7]=np.nan;d['action_valid']=np.ones((150,23),dtype=bool);d['action_valid'][40,7]=False
    phases,_=market.inspect_phases(d)
    assert any('未观测' in str(c['detail']) for c in phases[1]['checks'])


def test_recursive_discovery_paths_and_manual_revisions(batch,tmp_path):
    base,p=batch
    (base/'lerobot').mkdir();shutil.copytree(p,base/'lerobot'/'should-not-scan')
    (base/'alias').symlink_to(p,target_is_directory=True)
    assert market.discover(base)==[p]
    client=TestClient(market_api.app)
    assert client.get('/api/records',params={'root':str(base)}).json()['records'][0]['id']==str(p.relative_to(base))
    assert client.get('/api/records',params={'root':str(tmp_path)}).status_code==400
    assert client.get('/api/episode',params={'root':str(base),'id':'../outside'}).status_code==400
    assert client.get('/api/episode',params={'root':str(base),'id':'alias'}).status_code==400
    row=market_api.snapshot(p,base,True)
    body=dict(root=str(base),id=row['id'],uuid=row['uuid'],revision=row['revision'],phase_index=1,grade='B',note='已看视频')
    result=client.post('/api/review',json=body)
    assert result.status_code==200,result.text
    assert result.json()['phases'][1]['manual_grade']=='B'
    assert client.post('/api/review',json=body).status_code==409
    assert client.post('/api/review',json=body,headers={'Origin':'http://evil.example'}).status_code==403
    # A later data change invalidates stage approval instead of silently reusing it.
    meta=json.loads((p/'episode_meta.json').read_text());meta['extra']='changed';write_json(p/'episode_meta.json',meta)
    assert market_api.snapshot(p,base,True)['phases'][1]['manual_grade'] is None


def test_merged_export_keeps_phases_tasks_and_ports(batch):
    base,p=batch
    second=base/'market_recordings'/'different_8888'/'20261021_8888_1';shutil.copytree(p,second)
    meta=json.loads((second/'episode_meta.json').read_text());meta['episode_id']='2'*32;meta['inference']['remoteUrl']='ws://localhost:8888';write_json(second/'episode_meta.json',meta)
    review=json.loads((second/'review.json').read_text());review['episode_uuid']='2'*32;write_json(second/'review.json',review)
    for source in (p,second):write_json(market_api.report_path(source),market.inspect(source))
    result=market_job.export_dataset(base,[p,second],dict(id='testmerge',grades=['A','B'],manual_only=False),lambda _:None)
    batch_out=Path(result['path']);assert result['episodes']==2 and not result['skipped']
    assert batch_out.parent==base/'lerobot'
    assert not (batch_out/'data').exists()
    out=batch_out/'A'
    for i in range(2):
        table=pq.read_table(path_for(out,i))
        assert set(table['phase_index'].to_pylist())=={0,1,2,3}
        assert table['task_index'].to_pylist()==[0]*30+[1]*90+[2]*20+[3]*10
        assert table['index'].to_pylist()==list(range(i*150,(i+1)*150))
        assert table['action_valid'].type.list_size==23
    mapping=json.loads((out/'meta/episode_name_mapping.json').read_text())['episodes']
    assert {m['source_remote'] for m in mapping}=={'ws://localhost:9078','ws://localhost:8888'}
    assert all([p['phase'] for p in m['stages']]==list(market.PHASES) for m in mapping)
    assert len(rows(out/'meta/tasks.jsonl'))==4


def clone_episode(base, source, ident, grade):
    destination=base/'market_recordings'/f'port_{ident}'/f'20261021_8888_{ident}'
    shutil.copytree(source,destination)
    meta=json.loads((destination/'episode_meta.json').read_text())
    meta['episode_id']=f'{ident:032x}'
    meta['inference']['remoteUrl']=f'ws://localhost:{8880+ident}'
    write_json(destination/'episode_meta.json',meta)
    review=json.loads((destination/'review.json').read_text())
    review.update(episode_uuid=meta['episode_id'],grade=grade)
    write_json(destination/'review.json',review)
    return destination


def test_duplicate_uuids_preserve_both_sources_and_independent_qc(batch):
    base,p=batch
    second=base/'market_recordings2'/'copied'/'duplicate'
    shutil.copytree(p,second)
    with h5py.File(second/'episode.hdf5','r+') as f:
        f['observation/state/arm/position'][:]+=.01
    for source in (p,second):
        write_json(market_api.report_path(source),market.inspect(source))
    assert market_api.report_path(p)!=market_api.report_path(second)
    assert all(market_api.snapshot(source,base)['qc_status']=='completed' for source in (p,second))
    result=market_job.export_dataset(base,[p,second],dict(id='duplicates',grades=['A','B']),lambda _:None)
    output=Path(result['path'])
    assert result['episodes']==2 and not result['skipped']
    mappings=json.loads((output/'A/meta/episode_name_mapping.json').read_text())['episodes']
    assert [m['source_uuid'] for m in mappings]==['1'*32]*2
    assert [m['source_relative_path'] for m in mappings]==[str(source.relative_to(base)) for source in (p,second)]
    for i,source in enumerate((p,second)):
        converted=np.asarray(pq.read_table(path_for(output/'A',i))['observation.state'].to_pylist())
        np.testing.assert_allclose(converted,load(source)['state'],rtol=1e-6,atol=1e-6)
    selection=json.loads((output/'selection.json').read_text())
    assert selection['duplicate_uuid_groups']==1
    assert selection['duplicate_uuid_policy']=='preserve_all_source_directories'


def test_legacy_uuid_report_is_reused_only_for_matching_source(batch):
    _,p=batch
    legacy=market_api.HOME/'reports'/('episode_'+'1'*32+'.json')
    write_json(legacy,market.inspect(p))
    assert market_api.report_path(p)==legacy
    second=p.parent/'different_copy'
    shutil.copytree(p,second)
    assert market_api.report_path(second)!=legacy
    assert market_api.snapshot(second,p.parents[1])['qc_status']=='pending'


def test_grade_datasets_are_independent_and_mapping_uses_grade_paths(batch):
    base,p=batch
    b=clone_episode(base,p,2,'B')
    a=clone_episode(base,p,3,'A')
    f=clone_episode(base,p,4,'F')
    for source in (p,b,a,f):write_json(market_api.report_path(source),market.inspect(source))
    result=market_job.export_dataset(base,[p,b,a,f],dict(id='grades',grades=['A','B','F']),lambda _:None)
    output=Path(result['path'])
    assert {d['grade']:d['episodes'] for d in result['datasets']}=={'A':2,'B':1,'F':1}
    for grade,length in [('A',2),('B',1),('F',1)]:
        dataset=output/grade
        assert all((dataset/folder).is_dir() for folder in ('data','meta','videos'))
        info=json.loads((dataset/'meta/info.json').read_text())
        conversion=json.loads((dataset/'meta/conversion.json').read_text())
        assert conversion['quality_check']=='completed' and conversion['quality_check_scope']=='market_recorded_phases'
        assert info['total_episodes']==length and info['total_frames']==length*150
        assert len(rows(dataset/'meta/tasks.jsonl'))==4
        maps=json.loads((dataset/'meta/episode_name_mapping.json').read_text())['episodes']
        assert [m['grade'] for m in maps]==[grade]*length
        for i in range(length):
            table=pq.read_table(path_for(dataset,i))
            assert table['episode_index'].to_pylist()==[i]*150
            assert table['index'].to_pylist()==list(range(i*150,(i+1)*150))
        aggregated=json.loads((dataset/'meta/stats.json').read_text())
        assert aggregated['index']['count']==[length*150]
    write_json(market_api.HOME/'jobs/20261022_120000_12345678.json',dict(
        id='20261022_120000_12345678',root=str(base),status='completed',created=1,result=result))
    mapped=TestClient(market_api.app).get('/api/mapping',params={'root':str(base)}).json()
    assert len(mapped['episodes'])==4
    assert all(Path(m['dataset_path']).name==m['grade'] for m in mapped['episodes'])
    write_json(market_api.HOME/'jobs/20261022_120000_12345678.json',dict(
        id='20261022_120000_12345678',root=str(base),status='completed',created=1,result=result,
        source_identity=dict(device=base.stat().st_dev,inode=-1)))
    assert TestClient(market_api.app).get('/api/mapping',params={'root':str(base)}).json()['episodes']==[]


@pytest.mark.parametrize('problem',['incomplete','failed_qc','manual_only','bad_uuid','unhashable_uuid','hard_fail'])
def test_invalid_records_are_excluded_even_with_old_report_and_manual_A(batch,problem):
    base,p=batch
    job=dict(id='excluded',grades=['A','B'])
    if problem=='incomplete':
        meta=json.loads((p/'episode_meta.json').read_text());meta['complete']=False;write_json(p/'episode_meta.json',meta)
    elif problem=='failed_qc':job['errors']=[dict(id=str(p.relative_to(base)),error='QC failed')]
    elif problem=='manual_only':
        review=json.loads((p/'review.json').read_text());review['reviewed']=False;write_json(p/'review.json',review)
        job['manual_only']=True
    elif problem in ('bad_uuid','unhashable_uuid'):
        meta=json.loads((p/'episode_meta.json').read_text());meta['episode_id']='invalid' if problem=='bad_uuid' else [];write_json(p/'episode_meta.json',meta)
    report=market.inspect(p)
    if problem=='hard_fail':report['hard_fail']=True
    write_json(market_api.report_path(p),report)
    with pytest.raises(ValueError,match='没有可导出的记录'):
        market_job.export_dataset(base,[p],job,lambda _:None)
    assert not (base/'lerobot/market_all_excluded').exists()
    selection=json.loads((base/'lerobot/market_all_excluded.partial/selection.json').read_text())
    assert selection['included']==0 and len(selection['skipped'])==1


@pytest.mark.parametrize('change',['grade','source'])
def test_changes_during_conversion_prevent_entire_batch_publication(batch,monkeypatch,change):
    from dataqc import export
    base,p=batch
    write_json(market_api.report_path(p),market.inspect(p))
    original=export.write_episode
    def mutate(payload):
        result=original(payload)
        if change=='grade':
            review=json.loads((p/'review.json').read_text());review.update(grade='B',updated_at=2);write_json(p/'review.json',review)
        else:
            meta=json.loads((p/'episode_meta.json').read_text());meta['changed']=True;write_json(p/'episode_meta.json',meta)
        return result
    monkeypatch.setattr(export,'write_episode',mutate)
    with pytest.raises(ValueError,match='发生变化'):
        market_job.export_dataset(base,[p],dict(id='changed',grades=['A','B']),lambda _:None)
    assert not (base/'lerobot/market_all_changed').exists()


def test_cancel_before_publication_keeps_partial_batch(batch):
    base,p=batch
    write_json(market_api.report_path(p),market.inspect(p))
    def progress(text):
        if text.startswith('校验 '):raise market_job.Cancelled()
    with pytest.raises(market_job.Cancelled):
        market_job.export_dataset(base,[p],dict(id='cancel',grades=['A','B']),progress)
    assert not (base/'lerobot/market_all_cancel').exists()


def test_incremental_qc_reuses_report_after_human_grade_change(batch,monkeypatch):
    base,p=batch
    write_json(market_api.report_path(p),market.inspect(p))
    review=json.loads((p/'review.json').read_text());review['grade']='B';write_json(p/'review.json',review)
    job=market_api.HOME/'jobs/20261022_120000_12345678.json'
    write_json(job,dict(root=str(base),kind='qc',force=False,id=job.stem))
    monkeypatch.setattr(market,'inspect',lambda *_:pytest.fail('Unchanged data should reuse QC'))
    market_job.run(job.stem)
    saved=json.loads(job.read_text())
    assert saved['status']=='completed' and saved['reused']==1 and saved['done']==1
    assert market_api.snapshot(p,base)['effective_grade']=='B'


@pytest.mark.parametrize('force_failure',[False,True])
def test_qc_export_job_keeps_grade_outputs_and_does_not_reuse_failed_forced_qc(batch,monkeypatch,force_failure):
    base,p=batch
    other=clone_episode(base,p,2,'B')
    for source in (p,other):write_json(market_api.report_path(source),market.inspect(source))
    original=market.inspect
    if force_failure:
        def inspect(source,progress):
            if source==p:raise ValueError('forced QC error')
            return original(source,progress)
        monkeypatch.setattr(market,'inspect',inspect)
    job=market_api.HOME/'jobs/20261022_120000_12345678.json'
    write_json(job,dict(root=str(base),kind='qc_export',force=force_failure,id=job.stem,grades=['A','B']))
    market_job.run(job.stem)
    saved=json.loads(job.read_text())
    if force_failure:
        assert saved['status']=='partial' and len(saved['errors'])==1
        assert [d['grade'] for d in saved['result']['datasets']]==['B']
        assert len(saved['result']['skipped'])==1
    else:
        assert saved['status']=='completed' and saved['reused']==2
        assert saved['result']['quality_check']=='completed'
        assert {d['grade']:d['episodes'] for d in saved['result']['datasets']}=={'A':1,'B':1}


def test_independent_output_validation_detects_broken_index(batch):
    from dataqc.market_validation import validate_dataset
    base,p=batch
    write_json(market_api.report_path(p),market.inspect(p))
    result=market_job.export_dataset(base,[p],dict(id='validate',grades=['A']),lambda _:None)
    output=Path(result['path'])/'A'
    import pyarrow as pa
    table=pq.read_table(path_for(output,0))
    table=table.set_column(table.schema.get_field_index('index'),'index',pa.array(np.arange(150)+1))
    pq.write_table(table,path_for(output,0))
    with pytest.raises(ValueError,match='index 不连续'):validate_dataset(output,'A')


def test_existing_mixed_export_can_be_split_without_deleted_raw_recordings(batch):
    from dataqc.market_regroup import regroup_dataset
    from dataqc.export import jsonl
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    base,p=batch
    other=clone_episode(base,p,2,'A')
    for source in (p,other):write_json(market_api.report_path(source),market.inspect(source))
    result=market_job.export_dataset(base,[p,other],dict(id='old',grades=['A']),lambda _:None)
    old=Path(result['path'])/'A'
    mapping=json.loads((old/'meta/episode_name_mapping.json').read_text())
    mapping['episodes'][1]['grade']='B';write_json(old/'meta/episode_name_mapping.json',mapping)
    episodes=rows(old/'meta/episodes.jsonl');episodes[1]['quality_grade']='B';jsonl(old/'meta/episodes.jsonl',episodes)
    original=pq.read_table(path_for(old,1))
    shutil.rmtree(p);shutil.rmtree(other)
    separated=regroup_dataset(old,base/'lerobot/recovered')
    assert separated['source_qc_rerun'] is False
    assert {d['grade']:d['episodes'] for d in separated['datasets']}=={'A':1,'B':1}
    b=Path(separated['path'])/'B'
    table=pq.read_table(path_for(b,0))
    assert table['observation.state'].equals(original['observation.state'])
    assert table['action'].equals(original['action'])
    assert table['action_valid'].equals(original['action_valid'])
    assert table['phase_index'].equals(original['phase_index'])
    assert table['episode_index'].to_pylist()==[0]*150
    assert table['index'].to_pylist()==list(range(150))
    dataset=LeRobotDataset('local/market-B',root=b,video_backend='pyav')
    assert len(dataset)==150 and dataset[0]['action'].shape==(23,)
    assert dataset[0]['observation.images.cam_high'].shape==(3,48,64)
    assert dataset[149]['task']=='Hand over the items.'
    assert old.is_dir()


def test_job_api_uses_selected_directory_and_rejects_global_root(batch,monkeypatch):
    base,p=batch
    selected=base/'market_recordings2'
    clone_episode(base,p,2,'B')
    class Process:pid=123456789
    monkeypatch.setattr(market_api.subprocess,'Popen',lambda *a,**k:Process())
    client=TestClient(market_api.app)
    assert client.post('/api/jobs',json=dict(root=str(base),kind='qc')).status_code==400
    cfg=client.get('/api/config').json()
    assert 'default_root' not in cfg and cfg['browse_scope']=='all_directories_v1'
    assert cfg['sources']==[str(base/'market_recordings'),str(selected)]
    response=client.post('/api/jobs',json=dict(root=str(selected),kind='qc_export'))
    assert response.status_code==200,response.text
    job=response.json()
    assert job['root']==str(selected) and job['source_identity']==market_api.directory_identity(selected)
    monkeypatch.setattr(market_api,'jobs',lambda:[dict(status='running')])
    assert client.post('/api/jobs',json=dict(root=str(base/'market_recordings'),kind='qc')).status_code==409


def test_all_directories_refresh_with_new_batches_and_grade_changes(batch):
    base,p=batch
    other=clone_episode(base,p,2,'B')
    (base/'market_recordings2').rename(base/'finished_batch')
    p=base/'finished_batch'/p.relative_to(base/'market_recordings2')
    (base/'market_link').symlink_to(other.parent,target_is_directory=True)
    shutil.copytree(other,base/'lerobot'/'should_not_count')
    shutil.copytree(other,base/'market_pending.partial'/'should_not_count')
    client=TestClient(market_api.app)
    cfg=client.get('/api/config').json()
    assert 'default_root' not in cfg
    assert cfg['sources']==[str(base/'finished_batch'),str(base/'market_recordings')]
    first=client.get('/api/records').json()
    assert first['root']==str(base) and first['sources']==cfg['sources']
    assert {r['path']:r['effective_grade'] for r in first['records']}=={str(p):'A',str(other):'B'}
    added=clone_episode(base,p,3,'F')
    review=market.read(other/'review.json');review.update(grade='A',updated_at=2)
    write_json(other/'review.json',review)
    fresh=client.get('/api/records').json()
    assert {r['path']:r['effective_grade'] for r in fresh['records']}=={str(p):'A',str(other):'A',str(added):'F'}
    scoped=client.get('/api/records',params={'root':str(base/'finished_batch')}).json()
    assert [r['path'] for r in scoped['records']]==[str(p)]


def test_qc_only_reads_selected_batch_while_other_directory_exists(batch,monkeypatch):
    base,p=batch
    other=clone_episode(base,p,2,'B')
    calls=[];original=market.inspect
    def inspect(source,progress):
        calls.append(source)
        return original(source,progress)
    monkeypatch.setattr(market,'inspect',inspect)
    job=market_api.HOME/'jobs/20261022_120000_12345678.json'
    write_json(job,dict(root=str(base/'market_recordings2'),kind='qc',force=False,id=job.stem))
    market_job.run(job.stem)
    saved=json.loads(job.read_text())
    assert saved['status']=='completed' and saved['total']==1 and calls==[p]
    assert not market_api.report_path(other).exists()


@pytest.mark.parametrize('legacy',[False,True])
def test_rotating_recording_directories_preserves_reviews_without_leaking_to_next_batch(batch,legacy):
    base,p=batch
    frozen=base/'market_recordings2';active=base/'market_recordings'
    relative=p.relative_to(frozen)
    frozen.rename(active);p=active/relative
    report=market.inspect(p)
    manual=dict(uuid='1'*32,signature=market.signature(p),phases={'1':dict(grade='B',note='已看视频',updated=2)})
    if legacy:
        write_json(market_api.HOME/'reports'/(market_api.key(p)+'.json'),report)
        write_json(market_api.HOME/'manual'/(market_api.key(p)+'.json'),manual)
    else:
        write_json(market_api.report_path(p),report);write_json(market_api.manual_path(p),manual)
    active.rename(frozen);moved=frozen/relative
    shutil.copytree(moved,active/relative)
    new=active/relative
    meta=json.loads((new/'episode_meta.json').read_text());meta['episode_id']='2'*32;write_json(new/'episode_meta.json',meta)
    write_json(new/'review.json',dict(episode_uuid='2'*32,grade='B',reviewed=False))
    original=market_api.snapshot(moved,frozen,True)
    fresh=market_api.snapshot(new,active,True)
    assert original['qc_status']=='completed' and original['phases'][1]['manual_grade']=='B'
    assert fresh['qc_status']=='pending' and fresh['manual_grade'] is None
    assert all(phase.get('manual_grade') is None for phase in fresh['phases'])


def test_replacing_selected_directory_stops_task_before_export(batch,monkeypatch):
    base,p=batch
    selected=base/'market_recordings2'
    original=market.inspect(p)
    identity=market_api.directory_identity(selected)
    def replace_directory(source,progress):
        selected.rename(base/'frozen_elsewhere');selected.mkdir()
        return original
    monkeypatch.setattr(market,'inspect',replace_directory)
    job=market_api.HOME/'jobs/20261022_120000_12345678.json'
    write_json(job,dict(root=str(selected),kind='qc_export',force=True,id=job.stem,
        grades=['A','B'],source_identity=identity))
    with pytest.raises(ValueError,match='目录已被移动、删除或替换'):market_job.run(job.stem)
    assert json.loads(job.read_text())['status']=='failed'
    assert not (base/'lerobot'/('market_all_'+job.stem)).exists()


def set_recorded_phases(path, names):
    meta=market.read(path/'episode_meta.json')
    bounds=np.linspace(0,150,len(names)+1,dtype=int).tolist()
    meta['step_index']=[dict(phase=name,start_frame_id=b,end_frame_id=e-1,frame_count=e-b,
        prompt=f'{name} instruction {i}',outcome='completed',items=[dict(hand='left',target='Milk')])
        for i,(name,b,e) in enumerate(zip(names,bounds,bounds[1:]))]
    write_json(path/'episode_meta.json',meta)
    with h5py.File(path/'episode.hdf5','r+') as f:
        del f['subtask_transitions'];f.create_dataset('subtask_transitions',data=bounds[1:])


def test_mixed_actual_layouts_export_every_frame_and_grade(batch):
    from dataqc.market_validation import validate_dataset
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    base,p=batch
    five=clone_episode(base,p,2,'A');three=clone_episode(base,p,3,'B');retry=clone_episode(base,p,4,'F')
    layouts=[(p,market.PHASES),(five,market.PHASES+('return',)),
             (three,market.PHASES[:3]),(retry,('navigation','grasp','grasp','inspection','transport','handover','return'))]
    for source,names in layouts:
        set_recorded_phases(source,names)
        report=market.inspect(source)
        assert next(c for c in report['checks'] if c['key']=='phase_layout')['status']=='pass'
        workflow=next(c for c in report['checks'] if c['key']=='phase_workflow')
        assert workflow['status']==('pass' if source in (p,five) else 'warn')
        write_json(market_api.report_path(source),report)
    result=market_job.export_dataset(base,[p,five,three,retry],dict(id='actual-phases',grades=['A','B','F']),lambda _:None)
    assert result['episodes']==4 and result['frames']==600 and result['skipped']==[]
    output=Path(result['path'])
    assert {d['grade']:d['episodes'] for d in result['datasets']}=={'A':2,'B':1,'F':1}
    for grade in ('A','B','F'):
        dataset=output/grade
        assert validate_dataset(dataset,grade)['passed']
        mappings=market.read(dataset/'meta/episode_name_mapping.json')['episodes']
        for m in mappings:
            source=base/m['source_relative_path'];original=load(source)
            names=[s['phase'] for s in original['meta']['step_index']]
            assert m['phase_names']==names==[s['phase'] for s in m['stages']]
            assert all(not s.get('missing') for s in m['stages'])
            table=pq.read_table(path_for(dataset,m['episode_index']))
            np.testing.assert_allclose(table['observation.state'].to_pylist(),original['state'],rtol=1e-6)
            np.testing.assert_allclose(table['action'].to_pylist(),original['action'],rtol=1e-6)
            assert len(table)==150 and set(table['phase_index'].to_pylist())==set(range(len(names)))
            assert m['stages'][0]['start']==0 and m['stages'][-1]['end']==150
    ds=LeRobotDataset('local/actual-A',root=output/'A',video_backend='pyav')
    assert len(ds)==300 and ds[299]['task']=='return instruction 4'
    assert ds[299]['observation.images.cam_high'].shape==(3,48,64)
    b=market.read(output/'B/market_qc_report.json')
    assert b['phase_sequences']==[list(market.PHASES[:3])]


@pytest.mark.parametrize('problem',['gap','overlap','hdf5_boundary','empty','invalid_name'])
def test_actual_layout_still_rejects_unusable_annotations(batch,problem):
    base,p=batch
    meta=market.read(p/'episode_meta.json')
    if problem=='gap':meta['step_index'][1]['start_frame_id']+=1
    elif problem=='overlap':meta['step_index'][1]['start_frame_id']-=1
    elif problem=='empty':meta['step_index']=[]
    elif problem=='invalid_name':meta['step_index'][1]['phase']=[]
    else:
        with h5py.File(p/'episode.hdf5','r+') as f:f['subtask_transitions'][0]=29
    write_json(p/'episode_meta.json',meta)
    write_json(market_api.report_path(p),market.inspect(p))
    with pytest.raises(ValueError,match='没有可导出的记录'):
        market_job.export_dataset(base,[p],dict(id='bad-layout',grades=['A','B']),lambda _:None)
    selection=market.read(base/'lerobot/market_all_bad-layout.partial/selection.json')
    assert selection['included']==0 and len(selection['skipped'])==1


@pytest.mark.parametrize('changed',[False,True])
def test_v1_cache_refresh_rechecks_phases_and_only_reuses_unchanged_media(batch,monkeypatch,changed):
    from dataqc import checks
    base,p=batch
    set_recorded_phases(p,market.PHASES+('return',))
    previous=market.inspect(p);previous['version']='market_four_phase_v1'
    previous['checks']=[c for c in previous['checks'] if c['key']!='phase_workflow']
    old_layout=next(c for c in previous['checks'] if c['key']=='phase_layout')
    old_layout.update(status='warn',detail=dict(issues=['old four-phase restriction']))
    if changed:
        meta=market.read(p/'episode_meta.json');meta['changed']=True;write_json(p/'episode_meta.json',meta)
    calls=[];original=checks.raw_checks
    def raw_checks(*args,**kwargs):
        calls.append(True)
        return original(*args,**kwargs)
    monkeypatch.setattr(checks,'raw_checks',raw_checks)
    refreshed=market.inspect(p,previous=previous)
    assert bool(calls)==changed
    assert refreshed['version']==market.VERSION
    assert refreshed['signature']==market.signature(p)
    assert next(c for c in refreshed['checks'] if c['key']=='phase_layout')['status']=='pass'
    assert refreshed['phases'][-1]['label']=='返回'
    if not changed:
        assert refreshed['reused_structure_report']==previous['created']
        assert [c for c in refreshed['checks'] if c['key'].startswith('video_')]==[c for c in previous['checks'] if c['key'].startswith('video_')]


def test_five_phase_manual_grades_aggregate_and_survive_rule_upgrade(batch):
    base,p=batch
    set_recorded_phases(p,market.PHASES+('return',))
    review=market.read(p/'review.json');review['reviewed']=False;write_json(p/'review.json',review)
    write_json(market_api.report_path(p),market.inspect(p))
    write_json(market_api.manual_path(p),dict(uuid='1'*32,signature=market.signature(p),
        phases={str(i):dict(grade='A' if i<4 else 'B',note='reviewed') for i in range(5)}))
    row=market_api.snapshot(p,base,True)
    assert row['effective_grade']=='B' and not row['phase_manual_pending']
    assert row['phases'][-1]['manual_grade']=='B'


def test_partial_recordings_are_reported_outside_scanned_job(batch):
    base,p=batch
    shutil.copytree(p,p.with_name(p.name+'.partial'))
    write_json(market_api.report_path(p),market.inspect(p))
    job=market_api.HOME/'jobs/20261103_120000_12345678.json'
    write_json(job,dict(root=str(base/'market_recordings2'),kind='qc_export',force=False,id=job.stem,grades=['A','B']))
    market_job.run(job.stem)
    saved=market.read(job)
    assert saved['status']=='partial' and saved['total']==1
    assert saved['result']['episodes']==1 and not saved['result']['skipped']
    assert len(saved['result']['not_scanned'])==1
    selection=market.read(Path(saved['result']['path'])/'selection.json')
    assert selection['not_scanned']==saved['result']['not_scanned']
    assert selection['requested_grades']==['A','B'] and selection['scanned']==1
