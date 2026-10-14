import csv
import datetime as dt
import io
import json
import hashlib
import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dataqc.collection_dashboard import Inventory, aggregate, filtered, metrics, summary
from dataqc import collection_dashboard_api as api


def episode(root,name,*,left='Apple',right='Pear',grade='A',date='2026-10-12T16:00:00+00:00',identity=None):
    path=root/name;path.mkdir(parents=True)
    (path/'episode.hdf5').write_bytes(b'example hdf5 '+name.encode())
    (path/'videos/rs').mkdir(parents=True);(path/'videos/rs/cam_high.mp4').write_bytes(b'video')
    (path/'episode_meta.json').write_text(json.dumps(dict(collection_ts=dt.datetime.fromisoformat(date).timestamp()*1000)))
    (path/'review.json').write_text(json.dumps(dict(episode_uuid=identity or '',grade=grade,reviewed=False)))
    (path/'collection_task.json').write_text(json.dumps(dict(targets=dict(left=left,right=right),config=dict(task_name='test task',scene_name='scene1'))))
    return path


def scan(tmp_path):
    index=Inventory(tmp_path/'data',tmp_path/'cache/index.json');index.refresh(force=True,background=False)
    assert not index.state()['error'],index.state()
    return index


def test_original_counts_and_product_same_hand_dedup(tmp_path):
    root=tmp_path/'data'
    episode(root,'20261012_scene/a',left='Apple',right='Apple',grade='A')
    episode(root,'20261013_scene/b',left='apple',right='Pear',grade='F')
    for group in ('left_hand','righthand','.qc_reports','recording.partial','exports'):
        episode(root,group+'/ignored')
    lerobot=episode(root,'converted/ignored');(root/'converted/meta').mkdir();(root/'converted/meta/info.json').write_text('{}')
    (root/'alias').symlink_to(root/'20261012_scene',target_is_directory=True)
    index=scan(tmp_path);rows=index.rows();m=metrics(rows)
    assert m['total']==2 and m['left']==2 and m['right']==2
    assert m['date_mismatches']==1 and m['days']==1
    products,daily=aggregate(rows);apple=next(r for r in products if r['name'].casefold()=='apple')
    assert apple['total']==2 and apple['left']==2 and apple['right']==1
    assert apple['grades']['A']==apple['grades']['F']==1
    assert sum(r['total'] for r in products)==3 # intentional overlap, never used as dataset total
    assert daily[0]['date']=='2026-10-13' and daily[0]['total']==2


def test_exact_copies_and_identity_conflicts(tmp_path):
    root=tmp_path/'data';uid='a'*32
    original=episode(root,'group/a',identity=uid)
    shutil.copytree(original,root/'group/copy')
    different=episode(root,'group/different',identity=uid,grade='B')
    index=scan(tmp_path)
    assert index.state()['scanned_count']==3 and index.state()['duplicate_count']==1
    assert len(index.rows())==2 and all(r['identity_conflict'] for r in index.rows())
    assert sum(r['duplicate_count'] for r in index.rows())==1
    # A different video is a distinct record even when HDF5 is identical.
    (root/'group/copy/videos/rs/cam_high.mp4').write_bytes(b'different recording')
    index.refresh(force=True,background=False)
    assert len(index.rows())==3 and index.state()['duplicate_count']==0


def test_incremental_cache_grade_update_delete_and_failure(tmp_path,monkeypatch):
    from dataqc import collection_dashboard as module
    root=tmp_path/'data';path=episode(root,'group/a')
    index=scan(tmp_path)
    original=module.read_record;called=[]
    def track(*a):called.append(a);return original(*a)
    monkeypatch.setattr(module,'read_record',track)
    restored=Inventory(root,tmp_path/'cache/index.json');restored.refresh(force=True,background=False)
    assert not called and metrics(restored.rows())['total']==1
    (path/'review.json').write_text('{"grade":"F"}')
    restored.refresh(force=True,background=False)
    assert len(called)==1 and restored.rows()[0]['grade']=='F'
    root.rename(tmp_path/'offline');restored.refresh(force=True,background=False)
    assert restored.state()['error'] and len(restored.rows())==1
    (tmp_path/'offline').rename(root);shutil.rmtree(path);restored.refresh(force=True,background=False)
    assert restored.rows()==[] and not restored.state()['error']


def test_missing_bad_metadata_dates_and_percent_denominator(tmp_path):
    root=tmp_path/'data';a=episode(root,'20261012_scene/a')
    (a/'episode_meta.json').write_text('{}');(a/'review.json').write_text('bad')
    b=episode(root,'unknown/b');(b/'episode_meta.json').write_text('{}')
    index=scan(tmp_path);result=summary(index,start='2026-10-12',end='2026-10-12')
    assert result['metrics']['total']==1 and result['metrics']['grades']['UNRATED']==1
    assert result['global_metrics']['total']==2 and result['global_metrics']['unknown_dates']==1
    assert len(result['trend'])==14
    assert not filtered(index.rows(),start='2026-10-14',end='2026-10-14')
    with pytest.raises(ValueError):filtered(index.rows(),start='2026-10-13',end='2026-10-12')
    assert len(filtered(index.rows(),issues=True))==2


def test_api_filters_pagination_exports_and_local_video(tmp_path,monkeypatch):
    root=tmp_path/'data';episode(root,'20261013_scene/a',left='=FORMULA',grade='F')
    episode(root,'20261013_scene/b',grade='B')
    index=scan(tmp_path);monkeypatch.setattr(api,'inventory',lambda:index)
    app=FastAPI();app.include_router(api.router);client=TestClient(app)
    s=client.get('/api/collection-dashboard/summary',params=dict(grade='F')).json()
    assert s['metrics']['total']==1 and s['global_metrics']['total']==2
    response=client.get('/api/collection-dashboard/summary?start=invalid');assert response.status_code==400
    assert client.get('/api/collection-dashboard/records?limit=10000').status_code==422
    response=client.get('/api/collection-dashboard/records?limit=1&offset=1');assert len(response.json()['rows'])==1
    response=client.get('/api/collection-dashboard/export.csv?kind=products&grade=F')
    assert response.status_code==200
    exported=list(csv.reader(io.StringIO(response.content.decode('utf-8-sig'))))
    assert any(r[0]=="'=FORMULA" for r in exported[1:])
    response=client.get('/api/collection-dashboard/export.csv?kind=products&search=FORMULA')
    searched=list(csv.reader(io.StringIO(response.content.decode('utf-8-sig'))))
    assert len(searched)==2 and searched[1][0]=="'=FORMULA"
    record=index.rows()[0]
    assert client.get(f"/api/collection-dashboard/video/{record['id']}/cam_high").content==b'video'
    assert client.get(f"/api/collection-dashboard/video/{record['id']}/bad-camera").status_code==404
    assert client.get('/api/collection-dashboard/video/no-record/cam_high').status_code==404
    assert client.get('/collection-dashboard/assets/not-allowed.py').status_code==404
    assert client.get('/collection-dashboard').status_code==200


def saved_grades(tmp_path, dataset, *, choices=None, captures=None):
    selections=tmp_path/'grades';selections.mkdir(exist_ok=True)
    key=hashlib.sha256(str(dataset.resolve()).encode()).hexdigest()
    path=selections/(key+'.json')
    path.write_text(json.dumps(dict(config=dict(hdf5_root=str(dataset),qc_root=str(tmp_path/'data/qc_reports')),
                                    choices=choices or {},capture=captures or {})))
    return path


def qc_run(tmp_path, dataset, name, reports):
    root=tmp_path/'data/qc_reports'/name;root.mkdir(parents=True)
    rows=[]
    for episode_name,report in reports.items():
        output=root/episode_name;output.mkdir()
        (output/'qc_report.json').write_text(json.dumps(report))
        rows.append(dict(episode_id=episode_name,input=str(dataset/episode_name),output=str(output),ok=True))
    (root/'batch_summary.json').write_text(json.dumps(dict(input=str(dataset),episodes=rows)))
    return root


def test_grade_precedence_all_aggregates_filters_and_csv(tmp_path,monkeypatch):
    root=tmp_path/'data';dataset=root/'group';uid='a'*32
    episode(root,'group/manual',identity=uid)
    episode(root,'group/automatic')
    episode(root,'group/collection',grade='F')
    episode(root,'group/missing',grade='')
    episode(root,'group/incomplete')
    episode(root,'group/historical')
    saved_grades(tmp_path,dataset,choices={uid:dict(source='manual',grade='C',note='遮挡',problem='视觉异常')})
    qc_run(tmp_path,dataset,'group_old',dict(
        manual=dict(quality_grade='F',qc_original=dict(quality_grade='B')),
        automatic=dict(quality_grade='B'),
        incomplete=dict(quality_grade='F',execution='incomplete'),
        historical=dict(quality_grade='F',manual_review=True,qc_original=dict(quality_grade='A'))))
    index=Inventory(root,tmp_path/'cache/index.json',tmp_path/'grades');index.refresh(force=True,background=False)
    assert not index.state()['error']
    rows={Path(r['path']).name:r for r in index.rows()}
    assert (rows['manual']['grade'],rows['manual']['qc_grade'],rows['manual']['collection_grade'])==('C','B','A')
    assert rows['manual']['grade_source']=='manual' and rows['manual']['manual_note']=='遮挡'
    assert rows['automatic']['grade']=='B' and rows['automatic']['grade_source']=='qc'
    assert rows['collection']['grade']=='F' and rows['collection']['grade_source']=='collection'
    assert rows['missing']['grade']=='UNRATED'
    assert rows['incomplete']['grade']=='A' and rows['incomplete']['qc_grade']==''
    assert rows['historical']['grade']=='F' and rows['historical']['qc_grade']=='A'
    expected=dict(A=1,B=1,C=1,F=2,UNRATED=1)
    result=summary(index)
    assert result['global_metrics']['grades']==expected
    assert result['daily'][0]['grades']==expected
    assert all(p['grades']==expected for p in result['products'])
    assert [r['path'] for r in filtered(index.rows(),grade='C')]==['group/manual']
    monkeypatch.setattr(api,'inventory',lambda:index)
    app=FastAPI();app.include_router(api.router);client=TestClient(app)
    exported=list(csv.DictReader(io.StringIO(client.get('/api/collection-dashboard/export.csv?kind=records&grade=C').content.decode('utf-8-sig'))))
    assert len(exported)==1
    assert (exported[0]['采用等级'],exported[0]['等级来源'],exported[0]['自动/VLM等级'],exported[0]['人工备注'])==('C','人工','B','遮挡')


def test_grade_refresh_after_review_and_new_qc_preserves_source_cache(tmp_path,monkeypatch):
    from dataqc import collection_dashboard as module
    root=tmp_path/'data';dataset=root/'group';uid='a'*32
    episode(root,'group/a',identity=uid)
    store=saved_grades(tmp_path,dataset,choices={uid:dict(source='manual',grade='F')},
                       captures={uid:dict(grade='B')})
    first=qc_run(tmp_path,dataset,'group_first',dict(a=dict(quality_grade='C')))
    index=Inventory(root,tmp_path/'cache/index.json',tmp_path/'grades');index.refresh(force=True,background=False)
    assert (index.rows()[0]['grade'],index.rows()[0]['collection_grade'])==('F','B')
    def unexpected_read(*args):raise AssertionError('Unchanged collection metadata should remain cached')
    monkeypatch.setattr(module,'read_record',unexpected_read)
    # A newer complete run replaces the old automatic grade, retaining human F.
    import os
    os.utime(first/'batch_summary.json',(1,1))
    latest=qc_run(tmp_path,dataset,'group_latest',dict(a=dict(quality_grade='A')))
    index.refresh(force=True,background=False)
    assert not index.state()['error']
    assert (index.rows()[0]['grade'],index.rows()[0]['qc_grade'])==('F','A')
    # Changing only the independent grade store is reflected without touching source files.
    saved=json.loads(store.read_text());saved['choices'][uid]['grade']='C';store.write_text(json.dumps(saved))
    index.refresh(force=True,background=False)
    assert index.rows()[0]['grade']=='C'
    saved['choices']={};store.write_text(json.dumps(saved))
    restored=Inventory(root,tmp_path/'cache/index.json',tmp_path/'grades');restored.refresh(force=True,background=False)
    assert restored.rows()[0]['grade']=='A'
    # A changed report signature invalidates its projection, but unfinished QC cannot supply F.
    (latest/'a/qc_report.json').write_text(json.dumps(dict(quality_grade='F',execution='incomplete')))
    restored.refresh(force=True,background=False)
    assert (restored.rows()[0]['grade'],restored.rows()[0]['qc_grade'])==('B','')


def test_grade_identity_is_scoped_and_original_duplicates_stay_deduplicated(tmp_path):
    root=tmp_path/'data';uid='a'*32
    original=episode(root,'one/a',identity=uid)
    shutil.copytree(original,root/'one/copy')
    episode(root,'two/a',identity=uid)
    saved_grades(tmp_path,root/'one',choices={uid:dict(source='qc',grade='C')})
    saved_grades(tmp_path,root/'two',choices={uid:dict(source='manual',grade='F')})
    index=Inventory(root,tmp_path/'cache/index.json',tmp_path/'grades');index.refresh(force=True,background=False)
    assert not index.state()['error']
    assert index.state()['duplicate_count']==1
    assert {r['path']:r['grade'] for r in index.rows()}=={'one/a':'C','two/a':'F'}
    assert all(r['grade_source']=='manual' for r in index.rows())


def test_workbench_dashboard_tab_and_api_route():
    import workbench
    app=workbench.load_legacy()
    assert 'id="collectionDashboardWorkspaceBtn"' in app.HTML
    assert "dashboardFrame.src='/collection-dashboard?embedded=1'" in app.HTML
    assert TestClient(workbench.app).get('/collection-dashboard').status_code==200


def test_record_scope_keeps_selected_date_and_ab_basis_in_export(tmp_path,monkeypatch):
    root=tmp_path/'data'
    episode(root,'today/a',grade='A')
    episode(root,'today/b',grade='B')
    episode(root,'today/f',grade='F')
    episode(root,'yesterday/a',grade='A',date='2026-10-11T16:00:00+00:00')
    index=scan(tmp_path);monkeypatch.setattr(api,'inventory',lambda:index)
    app=FastAPI();app.include_router(api.router);client=TestClient(app)
    params=dict(start='2026-10-13',end='2026-10-13',product='Apple',basis='ab')
    selected=client.get('/api/collection-dashboard/records',params=params).json()
    assert selected['total']==2 and {r['grade'] for r in selected['rows']}=={'A','B'}
    assert all(r['date']=='2026-10-13' for r in selected['rows'])
    exported=list(csv.DictReader(io.StringIO(client.get('/api/collection-dashboard/export.csv',params=dict(params,kind='records')).content.decode('utf-8-sig'))))
    assert len(exported)==2 and {r['采用等级'] for r in exported}=={'A','B'}
    assert client.get('/api/collection-dashboard/records?basis=unknown').status_code==400


def test_record_being_written_is_deferred_until_closed(tmp_path,monkeypatch):
    import h5py
    root=tmp_path/'data';path=episode(root,'20261013_scene/current')
    (path/'collection_task.json').unlink()
    def locked(*a,**kw):raise BlockingIOError(11,'unable to lock file')
    monkeypatch.setattr(h5py,'File',locked)
    index=scan(tmp_path)
    assert not index.rows() and index.state()['warning_count']==1
    assert '保存中' in index.state()['warnings'][0]['reason']
    (path/'collection_task.json').write_text(json.dumps(dict(targets=dict(left='Apple',right='Pear'),config=dict(task_name='task'))))
    index.refresh(force=True,background=False)
    assert len(index.rows())==1 and index.state()['warning_count']==0
