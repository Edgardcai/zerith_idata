"""Category coverage must not silently downgrade or pretend a skipped check passed."""
import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_pipeline import source
from test_yolo_gate import moving_source, cfg, detector
from dataqc import api, config, db, vision, worker, yolo_gate as gate
from dataqc.checks import raw_checks
from dataqc.io import read_json


def clean_report(root):
    report=raw_checks(root)
    # Isolate category grading from this fixture's intentionally blurry images.
    for check in report['checks']:
        if check['status']=='warn':check['status']='pass'
    return report


def unsupported(root,samples,cfg,digest,thresholds):
    predictions,classes=detector(warn=['left'])(root,samples,cfg,digest,thresholds)
    return predictions,{k:v for k,v in classes.items() if v!='Milk'}


@pytest.mark.parametrize('enabled,status,expected',[(True,'pass','REVIEW'),(True,'fail','REVIEW'),(True,'uncertain','REVIEW'),(False,'pass','A')])
@pytest.mark.parametrize('predict',[unsupported,detector(warn=['left'])])
def test_unsupported_or_unstable_class(moving_source,cfg,tmp_path,monkeypatch,enabled,status,expected,predict):
    monkeypatch.setattr(gate,'predict_samples',predict if enabled else lambda *a,**kw:pytest.fail('disabled YOLO must never run'))
    calls=[]
    def call(content,schema,*args):
        from dataqc.motion_review import MotionReview
        from test_assessment import motion_ok, category_ok
        calls.append(schema.__name__)
        if schema is MotionReview:return motion_ok()
        assert enabled
        return category_ok(status)
    monkeypatch.setattr(vision,'call_vlm',call)
    report=clean_report(moving_source);before=copy.deepcopy(report)
    result=gate.inspect(moving_source,report,cfg|dict(vlm_enabled=enabled),tmp_path)
    assert report==before
    assert result['decision']['grade']==expected
    assert len(calls)==1+int(enabled)
    if not enabled:
        assert result['category_review']['status']=='skipped'
        assert result['motion_review']['status']=='pass'
        assert not result['decision']['category_qc']['complete']


@pytest.mark.parametrize('enabled',[True,False])
def test_real_numeric_warnings_still_b(moving_source,cfg,tmp_path,monkeypatch,enabled):
    monkeypatch.setattr(gate,'predict_samples',detector())
    result=gate.inspect(moving_source,raw_checks(moving_source),cfg|dict(vlm_enabled=enabled),tmp_path)
    assert result['decision']['grade']=='B'
    assert '预警默认 B' in result['decision']['reason']


def test_cache_switch_never_reuses_skipped_as_pass(moving_source,cfg,tmp_path,monkeypatch):
    monkeypatch.setattr(gate,'predict_samples',unsupported)
    from test_assessment import motion_ok,category_ok
    from dataqc.motion_review import MotionReview
    monkeypatch.setattr(vision,'call_vlm',lambda content,schema,*args:motion_ok() if schema is MotionReview else category_ok('fail'))
    report=clean_report(moving_source)
    off=gate.inspect(moving_source,report,cfg|dict(vlm_enabled=False),tmp_path)
    on=gate.inspect(moving_source,report,cfg|dict(vlm_enabled=True),tmp_path)
    assert off['matching_policy']!=on['matching_policy']
    assert off['decision']['grade']=='A' and on['decision']['grade']=='REVIEW'
    assert off['yolo']['status']=='na' and on['yolo']['signature']


def test_off_worker_exports_and_preserves_skipped_provenance(moving_source,cfg,tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DB',tmp_path/'db.sqlite3');db.init()
    monkeypatch.setattr(worker,'VAR',tmp_path/'var');monkeypatch.setattr(worker,'EXPORTS',tmp_path/'exports')
    monkeypatch.setattr(gate,'yolo_match',lambda *a,**kw:pytest.fail('disabled YOLO must never run'))
    rid=db.create(str(moving_source),'auto',cfg|dict(vlm_enabled=False),[str(moving_source)])
    worker.process_run(db.get_run(rid));run=db.get_run(rid);ep=db.episodes(rid)[0]
    assert run['status']=='completed' and ep['status']=='ready'
    assert ep['grade']=='B'  # existing image quality warnings retained
    prov=read_json(Path(ep['data']['repaired_root'])/'provenance.json')
    assert prov['decision']['category_qc']['vlm_enabled'] is False
    assert not prov['decision']['category_qc']['complete']
    assert prov['decision']['motion_qc']['status']=='pass'
    assert run['exports']


def test_off_preserves_stationary_review_and_hard_fail(source,cfg,tmp_path,monkeypatch):
    from integrations.zerith_rules import assess
    import h5py
    monkeypatch.setattr(gate,'yolo_match',lambda *a,**kw:pytest.fail('disabled YOLO must never run'))
    _,_,d=assess(source,cfg|dict(vlm_enabled=False),tmp_path/'stationary',lambda _:None)
    assert d['grade']=='REVIEW'
    with h5py.File(source/'episode.hdf5','a') as f:
        a=f['action/arm/position'][:];a[50,0]=float('nan');f['action/arm/position'][:]=a
    monkeypatch.setattr(gate,'inspect',lambda *a,**kw:pytest.fail('hard failure stops category QC'))
    _,v,d=assess(source,cfg|dict(vlm_enabled=False),tmp_path/'fatal',lambda _:None)
    assert d['grade']=='F' and v=={}


def test_settings_patch_and_run_override(tmp_path,monkeypatch):
    path=tmp_path/'settings.json';initial=config.DEFAULTS|dict(api_model='gpt-5.6-terra',stationary_frames=60)
    path.write_text(json.dumps(initial))
    monkeypatch.setattr(api,'CONFIG',path);monkeypatch.setattr(api,'settings',lambda:json.loads(path.read_text()))
    monkeypatch.setattr(db,'DB',tmp_path/'db.sqlite3');db.init()
    monkeypatch.setattr(api,'sha',lambda _: 'hash')
    root=tmp_path/'datasets';group=root/'Milk_Tea_0';(group/'episode1').mkdir(parents=True);(group/'episode1/episode.hdf5').touch()
    monkeypatch.setattr(api,'SOURCE_ROOT',root)
    client=TestClient(api.app)
    assert client.patch('/api/settings/category-qc',json={'vlm_enabled':'false'}).status_code==422
    assert client.patch('/api/settings/category-qc',json={'vlm_enabled':False}).status_code==200
    assert json.loads(path.read_text())==initial|dict(vlm_enabled=False)
    assert api.Settings.model_validate({k:v for k,v in initial.items() if k in api.Settings.model_fields}).api_model=='gpt-5.6-terra'
    for override,expected in [({},False),({'vlm_enabled':True},True),({'vlm_enabled':False},False)]:
        response=client.post('/api/runs',json=dict(root=str(group))|override)
        assert response.status_code==200,response.text
        run=db.get_run(response.json()['id']);assert run['config']['vlm_enabled']==expected
        assert run['config']['stationary_frames']==60
        db.update('runs',run['id'],status='completed')


def test_collection_toggle_is_per_job_and_snapshotted(tmp_path):
    import workbench
    app=workbench.load_legacy()
    configs=[app.derive_paths(dict(hdf5_root=str(tmp_path),vlm_enabled=enabled)) for enabled in (False,True)]
    for cfg in configs:
        _,_,env=app.qc_command(cfg)
        policy=json.loads(env['DATAQC_CATEGORY_POLICY'])
        assert all(policy[k]==cfg[k] for k in ('vlm_enabled','api_model','motion_batch_size','motion_batch_concurrency','qc_force'))
        assert policy['qc_refresh_token']
    with pytest.raises(ValueError,match='布尔'):
        app.derive_paths(dict(hdf5_root=str(tmp_path),vlm_enabled='false'))


@pytest.mark.parametrize('enabled',[True,False])
def test_collection_requires_category_conjunction_only_when_enabled(moving_source,cfg,tmp_path,monkeypatch,enabled):
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'legacy/scripts/embodied_data_pipeline-main'))
    from quality_pipeline.episode_io import read_raw_episode
    from quality_pipeline.profiles import load_profile
    from integrations.zerith_rules import run_manual_checks
    import workbench
    monkeypatch.setattr(config,'settings',lambda:cfg|dict(vlm_enabled=enabled))
    monkeypatch.setattr(config,'VAR',tmp_path/'var')
    monkeypatch.setattr(gate,'predict_samples',unsupported)
    monkeypatch.setattr(worker.get_adapter(),'check',lambda *a,**kw:clean_report(moving_source))
    profile=load_profile(Path(__file__).resolve().parents[1]/'legacy/scripts/embodied_data_pipeline-main/robot_profiles/zerith.yaml')
    report=run_manual_checks(read_raw_episode(moving_source,profile),profile)
    app=workbench.load_legacy()
    row=dict(episode_id=moving_source.name,manual_quality_grade='B',**app.qc_warning_summary_from_report(report))
    assert report['review_required'] is enabled
    if not enabled:
        assert report['accepted'] and report['quality_grade']=='A'
        assert app.finalise_qc_rows([row])[0]['quality_grade']=='A'
    check=next(c for c in report['checks'] if c['name']=='visual_matching')
    assert check['status']==('warn' if enabled else 'pass')


def test_disabled_requires_no_yolo_weights_or_images_but_keeps_motion_vlm(moving_source,cfg,tmp_path,monkeypatch):
    def forbidden(*a,**kw):pytest.fail('recognition disabled: no model, hash or image extraction')
    for name in ('yolo_match','sha','frame'):
        monkeypatch.setattr(gate,name,forbidden)
    off=cfg|dict(vlm_enabled=False,yolo_path='/missing/model.pt',yolo_thresholds_path='/missing/thresholds')
    report=clean_report(moving_source)
    r=gate.inspect(moving_source,report,off,tmp_path)
    assert r['decision']['grade']=='A' and r['yolo_enabled'] is False
    assert r['motion_vlm_called'] and r['category_review']['status']=='skipped'


def test_disabled_api_creates_without_model_or_credentials(tmp_path,monkeypatch):
    def forbidden(*a,**kw):pytest.fail('recognition disabled: no model or API dependency')
    monkeypatch.setattr(api,'sha',forbidden);monkeypatch.setattr(api,'api_config',forbidden)
    monkeypatch.setattr(db,'DB',tmp_path/'db.sqlite3');db.init()
    root=tmp_path/'datasets';group=root/'Milk_Tea_0';(group/'episode1').mkdir(parents=True);(group/'episode1/episode.hdf5').touch()
    monkeypatch.setattr(api,'SOURCE_ROOT',root)
    monkeypatch.setattr(api,'settings',lambda:config.DEFAULTS|dict(yolo_path='/missing/model.pt',vlm_enabled=True))
    response=TestClient(api.app).post('/api/runs',json=dict(root=str(group),vlm_enabled=False))
    assert response.status_code==200,response.text
    assert db.get_run(response.json()['id'])['config']['vlm_enabled'] is False
