import copy
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from dataqc import config
from dataqc.io import read_json, write_json
from integrations.replay_grades import integrate_grades


@pytest.fixture
def grades(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'VAR',tmp_path/'var')
    rows=[]
    for i,(capture,qc) in enumerate([('A','B'),('B','A'),('F','A'),('','F')]):
        root=tmp_path/'dataset'/f'episode{i}';root.mkdir(parents=True)
        (root/'episode.hdf5').write_bytes(b'fixture')
        write_json(root/'review.json',dict(episode_uuid=f'uuid-{i}',grade=capture))
        report=tmp_path/'reports'/root.name
        write_json(report/'qc_report.json',dict(quality_grade=qc,reason='original QC',accepted=qc in ('A','B'),review_required=False))
        rows.append(dict(episode_id=root.name,episode_dir=str(root),qc_output=str(report),qc_ok=True,quality_grade='F',quality_description='',shared_quality_grade=qc))
    class Handler:
        do_GET=lambda self:None
        do_POST=lambda self:None
    app=SimpleNamespace(Handler=Handler,JOBS_LOCK=threading.RLock(),JOBS={},
        dataset_status=lambda payload:dict(episodes=copy.deepcopy(rows)),derive_paths=lambda p:dict(p),
        stringify_config=lambda p:dict(p),start_replay=lambda p:{},invalidate_status_cache=lambda:None,
        qc_overview_quality_grade_counts=lambda rows:{g:sum(r['quality_grade']==g for r in rows)for g in ('A','B','C','F')})
    integrate_grades(app)
    return app,dict(hdf5_root=str(tmp_path/'dataset')),rows


def requests(status,grade=None):
    return [dict(episode_name=r['episode_id'],revision=r['grade_revision'],grade=grade) for r in status['episodes']]


def test_provenance_defaults_and_bulk_roundtrip(grades):
    app,cfg,raw=grades;initial=app.dataset_status(cfg)
    assert [r['quality_grade']for r in initial['episodes']]==['B','A','A','F']
    assert initial['grade_comparison']['changed_count']==3
    reports=[(Path(r['qc_output'])/'qc_report.json').read_bytes() for r in raw]
    before=app.apply_grade_choices(cfg,requests(initial),'collection')
    assert before['saved_count']==3 and before['skipped']==['episode3']
    assert [r['quality_grade']for r in before['status']['episodes']]==['A','B','F','F']
    after=app.apply_grade_choices(cfg,requests(before['status']),'qc')['status']
    assert [r['quality_grade']for r in after['episodes']]==['B','A','A','F']
    assert reports==[(Path(r['qc_output'])/'qc_report.json').read_bytes()for r in raw]
    assert after['grade_comparison']==initial['grade_comparison']


def test_explicit_save_restart_regrade_and_stale_conflict(grades):
    app,cfg,rows=grades;s=app.dataset_status(cfg);req=requests(s,'F')[:1]
    out=app.apply_grade_choices(cfg,req)['status']['episodes'][0]
    assert (out['collection_grade'],out['qc_grade'],out['quality_grade'],out['manual_quality_grade'])==('A','B','F','F')
    with pytest.raises(ValueError,match='已更新'):app.apply_grade_choices(cfg,req)
    assert app.dataset_status(cfg)['episodes'][0]['quality_grade']=='F'
    path=Path(rows[0]['qc_output'])/'qc_report.json';write_json(path,dict(quality_grade='A',accepted=True))
    row=app.dataset_status(cfg)['episodes'][0]
    assert row['quality_grade']=='F' and row['manual_quality_grade']=='F' and row['collection_grade']=='A'
    assert row['qc_grade']=='A'


def test_bulk_atomic_on_stale_and_busy(grades):
    app,cfg,rows=grades;s=app.dataset_status(cfg);req=requests(s,'F');req[-1]['revision']='stale'
    with pytest.raises(ValueError):app.apply_grade_choices(cfg,req)
    assert [r['quality_grade']for r in app.dataset_status(cfg)['episodes']]==['B','A','A','F']
    app.JOBS['test']=dict(status='running',resource_keys=[f"hdf5:{cfg['hdf5_root']}"])
    with pytest.raises(ValueError,match='正在处理'):app.apply_grade_choices(cfg,requests(s),'collection')


def test_capture_identity_survives_renumber_and_edits(grades):
    app,cfg,rows=grades;app.dataset_status(cfg)
    root=Path(rows[0]['episode_dir']);write_json(root/'review.json',dict(episode_uuid='uuid-0',grade='F'))
    moved=root.with_name('episode99');root.rename(moved);rows[0].update(episode_dir=str(moved),episode_id=moved.name)
    assert app.dataset_status(cfg)['episodes'][0]['collection_grade']=='A'


def test_pending_without_model_grade_uses_capture_but_stays_in_review(grades):
    app,cfg,rows=grades
    write_json(Path(rows[0]['qc_output'])/'qc_report.json',dict(quality_grade='',review_required=True,accepted=False))
    row=app.dataset_status(cfg)['episodes'][0]
    assert row['collection_grade']=='A' and row['quality_grade']=='A' and row['grade_review_required']
    assert row['review_pending'] and row['grade_source']=='collection'


def test_replay_reads_custom_report_root_and_shared_choices(grades):
    app,cfg,rows=grades
    cfg['qc_root']='custom/qc'
    app.dataset_status(cfg)
    root=rows[0]['episode_dir']
    before=app.replay_grade_snapshot(root)
    after=app.save_replay_grade(dict(root=root,grade='F',revision=before['revision']))
    assert (after['collection_grade'],after['qc_grade'],after['current_grade'])==('A','B','F')
    assert app.dataset_status(cfg)['episodes'][0]['quality_grade']=='F'
    with pytest.raises(ValueError,match='已更新'):
        app.save_replay_grade(dict(root=root,grade='A',revision=before['revision']))


def test_historical_manual_grade_is_preserved_without_fabricating_qc(grades):
    app,cfg,rows=grades
    write_json(Path(rows[0]['qc_output'])/'qc_report.json',dict(quality_grade='F',manual_review=dict(grade='F')))
    row=app.dataset_status(cfg)['episodes'][0]
    assert row['qc_grade']=='' and row['qc_grade_label']=='历史结论未保留'
    assert row['quality_grade']=='F' and row['grade_source']=='manual'


def test_review_queue_updates_without_losing_original_qc(grades):
    app,cfg,rows=grades
    path=Path(rows[0]['qc_output'])/'qc_report.json'
    write_json(path,dict(quality_grade='',review_required=True,accepted=False))
    app.dataset_status(cfg)
    listing=app.replay_grade_list(rows[0]['episode_dir'])['episodes']
    assert len(listing)==4 and sum(r['review_pending'] for r in listing)==1
    row=listing[0]
    app.save_replay_grade(dict(root=row['root'],grade='F',revision=row['revision']))
    result=app.dataset_status(cfg)
    assert result['grade_comparison']['pending_count']==0
    assert result['episodes'][0]['grade_review_required']
    assert read_json(path)['review_required']
    # New QC cannot undo human review or reopen its resolved queue.
    write_json(path,dict(quality_grade='',review_required=True,accepted=False,reason='new QC'))
    latest=app.replay_grade_list(row['root'])['episodes'][0]
    assert not latest['review_pending'] and latest['current_grade']=='F'


def test_bulk_never_overwrites_individual_manual_review(grades):
    app,cfg,rows=grades
    app.apply_grade_choices(cfg,requests(app.dataset_status(cfg),'F')[:1])
    for source in ('collection','qc'):
        result=app.apply_grade_choices(cfg,requests(app.dataset_status(cfg)),source)
        assert result['preserved_manual']==['episode0']
        row=result['status']['episodes'][0]
        assert row['manual_quality_grade']=='F' and row['quality_grade']=='F'


def test_all_grade_conflicts_keep_human_result_across_reports(grades):
    app,cfg,rows=grades
    for human in ('A','B','C','F'):
        app.apply_grade_choices(cfg,requests(app.dataset_status(cfg),human)[:1])
        for model in ('A','B','C','F'):
            write_json(Path(rows[0]['qc_output'])/'qc_report.json',dict(quality_grade=model,accepted=model in ('A','B'),review_required=False))
            row=app.dataset_status(cfg)['episodes'][0]
            assert (row['quality_grade'],row['qc_grade'],row['collection_grade'])==(human,model,'A')
            assert row['grade_source']=='manual'


def test_new_report_drops_old_export_approval_but_preserves_grade(grades):
    app,cfg,rows=grades
    app.apply_grade_choices(cfg,requests(app.dataset_status(cfg),'A')[:1])
    path=next((config.VAR/'grade-selections').glob('*.json'))
    store=read_json(path);store['choices']['uuid-0']['approval']={'quality_grade':'A','accepted':True}
    write_json(path,store)
    assert app.dataset_status(cfg)['episodes'][0]['grade_approval']
    write_json(Path(rows[0]['qc_output'])/'qc_report.json',dict(quality_grade='B',review_required=True,reason='new review'))
    row=app.dataset_status(cfg)['episodes'][0]
    assert row['quality_grade']=='A' and row['grade_approval'] is None
