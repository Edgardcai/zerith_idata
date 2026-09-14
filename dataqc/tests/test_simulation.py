"""Real simulation fixture: strict schema, shared QC, UI/CLI and lossless export."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import h5py
import numpy as np
import pytest
from fastapi.testclient import TestClient

from dataqc import api, config, db, vision, yolo_gate
from dataqc.checks import raw_checks
from dataqc.io import CAMS, discover, fingerprint, hdf5_path, load, read_json, video_path, write_json
from dataqc.motion import source_height
from dataqc.repair import derive
from dataqc.export import create_dataset, split_dataset, validate_dataset
from dataqc.worker import visual_decision

ROOT=Path(__file__).resolve().parents[1]
SAMPLE=Path('/data/sim_data/0909_newscene_test3_1demo_converted/demo_0')
LEGACY=ROOT/'legacy/scripts/embodied_data_pipeline-main'
sys.path.insert(0,str(LEGACY))
from quality_pipeline.episode_io import read_raw_episode
from quality_pipeline.profiles import load_profile
from integrations.zerith_rules import run_manual_checks


@pytest.fixture
def sim(tmp_path):
    if not SAMPLE.exists():pytest.skip('本机仿真验收样本未挂载')
    root=tmp_path/'sim_data'/'task_0'/'demo_0'
    shutil.copytree(SAMPLE,root)
    return root


def check(report,key):return next(c for c in report['checks'] if c['key']==key)


def offline(monkeypatch,tmp_path):
    cfg=config.DEFAULTS | dict(vlm_enabled=False,api_file=config.settings()['api_file'],yolo_path='/missing/model',stationary_frames=40)
    monkeypatch.setattr(config,'settings',lambda:dict(cfg))
    monkeypatch.setattr(config,'VAR',tmp_path/'work')
    monkeypatch.setattr(yolo_gate,'predict_samples',lambda *a,**k:pytest.fail('类别识别关闭'))
    return cfg


def test_simulation_read_qc_and_manual_share_rules(sim,tmp_path,monkeypatch):
    cfg=offline(monkeypatch,tmp_path);before=fingerprint(sim)
    d=load(sim)
    assert d['n']==446 and d['state'].shape==d['action'].shape==(446,23)
    assert d['transitions']==[217,446] and len(d['original_stages'])==3
    assert not d['task'].endswith('.') and d['original_task'].endswith('.')
    assert d['gripper_feedback_available'] is False
    report=raw_checks(sim)
    assert check(report,'schema')['status']=='pass'
    assert check(report,'lift_height')['detail']['expected_m']==pytest.approx(.4)
    assert check(report,'gripper_feedback')['status']=='na'
    assert check(report,'gripper_sequence')['status']=='pass'
    assert check(report,'gripper_sequence')['detail']['channels']['left']['action_close_frames']==[133]
    assert check(report,'posture_state')['detail']['measurement_source']=='state/raw/joint_position_29'
    assert all(check(report,'video_'+cam)['status']=='pass' for cam in CAMS)
    assert check(report,'stationary')['status']=='fail'
    assert [s['frames'] for s in check(report,'stationary')['detail']['intervals']]==[74,75]
    visual=yolo_gate.inspect(sim,report,cfg,tmp_path/'vision')
    assert visual_decision(visual,report)['grade']=='REVIEW'
    profile=load_profile(LEGACY/'robot_profiles/zerith.yaml')
    episode=read_raw_episode(sim,profile)
    assert episode.n_frames==446 and list(episode.camera_counts.values())==[446]*3
    manual=run_manual_checks(episode,profile)
    assert manual['raw_report']==report and manual['review_required'] and not manual['accepted']
    assert fingerprint(sim)==before


@pytest.mark.parametrize('problem',['frame_missing','component_conflict','field_order','unsafe_video','missing_raw','metadata_count'])
def test_malformed_simulation_rejected(sim,problem):
    meta=read_json(sim/'meta/episode_meta.json')
    if problem=='field_order':meta['state_action_fields'][0]='unknown'
    if problem=='metadata_count':meta['frame_count']-=1
    if problem=='unsafe_video':
        meta['states_file']='../../outside.h5'
    write_json(sim/'meta/episode_meta.json',meta)
    with h5py.File(hdf5_path(sim),'a') as f:
        if problem=='frame_missing':del f['4']
        if problem=='component_conflict':f['4/action/vector'][0]+=1
        if problem=='missing_raw':del f['4/state/raw/joint_position_29']
    report=raw_checks(sim)
    assert report['hard_fail'] and check(report,'schema')['status']=='fail'


def test_height_is_per_episode_and_drift_still_fails(sim):
    with h5py.File(hdf5_path(sim),'a') as f:
        for g in f.values():
            for kind in ('state','action'):
                g[kind+'/vector'][16]=.65
                g[kind+'/waist/position'][0]=.65
    assert source_height(sim)['expected_m']==pytest.approx(.65)
    assert check(raw_checks(sim),'lift_height')['status']=='pass'
    with h5py.File(hdf5_path(sim),'a') as f:
        for kind in ('state','action'):
            f['200/'+kind+'/vector'][16]=.69
            f['200/'+kind+'/waist/position'][0]=.69
    assert check(raw_checks(sim),'lift_height')['status']=='fail'


def test_raw_posture_and_stage_annotations_are_checked(sim):
    with h5py.File(hdf5_path(sim),'a') as f:
        names=json.loads(f.attrs['source_articulation_dof_names_json'])
        for i in range(30):f[f'{i}/state/raw/joint_position_29'][names.index('body_pitch_joint')]=.2
    assert check(raw_checks(sim),'posture_state')['status']=='warn'
    meta=read_json(sim/'meta/episode_meta.json');meta['subtask_segments'][0]['end']=215
    write_json(sim/'meta/episode_meta.json',meta)
    report=raw_checks(sim)
    assert check(report,'stages')['status']=='warn' and check(report,'stages')['detail']['issues']
    assert check(report,'gripper_sequence')['status']=='warn'


def test_catalog_replay_and_no_network_run_creation(sim,tmp_path,monkeypatch):
    cfg=offline(monkeypatch,tmp_path)
    monkeypatch.setattr(api,'settings',lambda:cfg)
    monkeypatch.setattr(api,'SOURCE_ROOT',tmp_path/'zerith_data')
    monkeypatch.setattr(api,'SIM_SOURCE_ROOT',sim.parent.parent)
    monkeypatch.setattr(db,'DB',tmp_path/'qc.sqlite3');db.init()
    client=TestClient(api.app)
    catalog=client.get('/api/datasets').json()
    assert len(catalog)==1 and catalog[0]['count']==1 and catalog[0]['source_label']=='仿真'
    response=client.post('/api/runs',json=dict(root=str(sim.parent),vlm_enabled=False))
    assert response.status_code==200,response.text
    assert db.episodes(response.json()['id'])[0]['root']==str(sim)
    assert discover(sim.parent)==[str(sim)]
    for endpoint in ('episodes','replay'):
        response=client.get('/api/hdf5/'+endpoint,params=dict(root=str(sim.parent if endpoint=='episodes' else sim)))
        assert response.status_code==200,response.text
    response=client.get('/api/hdf5/video/cam_high',params=dict(root=str(sim)),headers={'range':'bytes=0-99'})
    assert response.status_code==206 and len(response.content)==100
    assert client.get('/api/hdf5/replay',params=dict(root=str(tmp_path))).status_code==422


def test_simulation_derive_export_and_split(sim,tmp_path,monkeypatch):
    cfg=offline(monkeypatch,tmp_path);before=fingerprint(sim)
    # Round-trip validation of the full untrimmed sample uses an explicit test-only
    # threshold; deployed settings and the default-40 REVIEW assertion stay intact.
    threshold=80
    report=raw_checks(sim,threshold)
    decision=visual_decision(yolo_gate.inspect(sim,report,cfg,tmp_path/'vision'),report)
    assert decision['grade']=='A',decision
    derived=derive(sim,tmp_path/'derived',report,decision,threshold,before)
    d=load(derived);original=load(sim)
    assert np.array_equal(d['state'],original['state']) and np.array_equal(d['action'],original['action'])
    assert np.array_equal(d['measured_state'],original['measured_state'])
    assert d['transitions']==[217,446]
    assert source_height(derived)['expected_m']==pytest.approx(.4)
    out=tmp_path/'lerobot'
    create_dataset([dict(root=derived,grade="A")],out,threshold)
    assert validate_dataset(out,threshold)['passed']
    mapping=read_json(out/'meta/episode_name_mapping.json')['episodes'][0]
    assert mapping['source_format']=='zerith_sim_v1' and mapping['source_hdf5'].endswith('states/aligned_joints.h5')
    import workbench
    app=workbench.load_legacy()
    monkeypatch.setattr(app,'stationary_threshold_for_cfg',lambda cfg:threshold)
    preflight=app.validate_lerobot_stage_split_grade(dict(robot_type='zerith'),'A',out)
    assert preflight['compatible'],preflight
    result=split_dataset(out,tmp_path/'split',threshold)
    assert result
    for hand in ('left','right'):
        assert validate_dataset(tmp_path/'split'/hand,threshold)['passed']
    assert fingerprint(sim)==before


def test_collection_simulation_cli_and_replay(sim,tmp_path,monkeypatch):
    import workbench
    app=workbench.load_legacy()
    cfg=offline(monkeypatch,tmp_path)
    payload=dict(robot_type='zerith',dataset_name=sim.parent.name,hdf5_root=str(sim.parent),qc_root=str(tmp_path/'qc'),vlm_enabled=False,stationary_threshold=40)
    before=fingerprint(sim)
    paths=app.derive_paths(payload)
    home=tmp_path/'runtime';write_json(home/'config/settings.json',cfg)
    cmd,cwd,env=app.qc_command(paths)
    env.update(DATAQC_HOME=str(home),PYTHONPATH=os.pathsep.join([str(ROOT),str(ROOT/'vendor')]))
    result=subprocess.run(cmd,cwd=cwd,env=env,text=True,capture_output=True,timeout=90)
    assert result.returncode==0,result.stdout+result.stderr
    status=app.dataset_status(payload)
    assert len(status['episodes'])==1,status
    assert status['episodes'][0]['shared_review_required'],status
    counts = status['qc_overview']['records'][0]
    assert counts['left_gripper_close_events'] == counts['right_gripper_close_events'] == 1
    replay=app.start_replay(payload)
    try:
        from urllib.request import urlopen, Request
        assert replay['url'] == '/replay/'
        base = f"http://127.0.0.1:{replay['port']}"
        with urlopen(base + '/api/episodes') as response:
            records = json.load(response)
        assert len(records['episodes']) == 1
        with urlopen(base + '/api/episode?index=0') as response:
            episode = json.load(response)
        assert episode['state_dim'] == episode['action_dim'] == 23
        assert episode['frame_count'] == 446
        assert episode['stage_info']['transitions'] == [217, 446]
        assert len(episode['videos']) == 3
        assert episode['frame_trim_supported'] is False
        from urllib.error import HTTPError
        with pytest.raises(HTTPError) as blocked:
            urlopen(Request(base + '/api/frame-delete', data=json.dumps(dict(
                episode_index=0, start_frame=0, end_frame=1)).encode(),
                headers={'Content-Type': 'application/json'}))
        assert blocked.value.code == 400
        assert '派生副本' in json.loads(blocked.value.read())['error']
        for video in episode['videos']:
            with urlopen(Request(base + video['url'], headers={'Range': 'bytes=0-127'})) as response:
                assert response.status == 206 and len(response.read()) == 128
        with urlopen(base + '/') as response:
            html = response.read().decode()
        assert 'raw-grade.js' in html and 'raw-review-filter.js' in html
    finally:
        for proc in app.REPLAY_PROCESSES.values():
            proc.terminate()
            proc.wait(timeout=5)
    roots=[]
    monkeypatch.setattr(app,'discover_recursive_hdf5_dataset_choices',lambda p:roots.append(str(p)) or [])
    assert app.discover_machine_datasets('simulation')['machine']=='simulation'
    assert roots==['/data/sim_data']
    assert fingerprint(sim)==before


@pytest.mark.parametrize('status,expected',[('pass','REVIEW'),('fail','F')])
def test_simulation_enabled_category_uses_terra_and_mapped_images(sim,tmp_path,monkeypatch,status,expected):
    weights=tmp_path/'fixture.pt';weights.write_bytes(b'fixture')
    cfg=config.DEFAULTS | dict(vlm_enabled=True,yolo_path=str(weights),api_model='gpt-5.6-terra')
    calls=[]
    def predict(root,samples,*args):
        assert len(samples)==6
        from dataqc.io import frame
        assert all(frame(root,s['camera'],s['frame']).shape==(480,640,3) for s in samples)
        return [[] for _ in samples],{0:'untrained product'}
    def vlm(content,schema,settings,*args):
        from dataqc.motion_review import MotionReview
        if schema is MotionReview:
            from test_assessment import motion_ok
            return motion_ok()
        assert settings['api_model']=='gpt-5.6-terra'
        calls.append(content)
        return dict(hands=[dict(hand=h,status=status,observed_item=item,reason='fixture verification',evidence_ids=[f'cam_{h}_wrist:{frame}']) for h,item,frame in [('left','Sprite',133),('right','Nongfu Spring',352)]])
    monkeypatch.setattr(yolo_gate,'predict_samples',predict)
    monkeypatch.setattr(vision,'call_vlm',vlm)
    report=raw_checks(sim,80)
    result=yolo_gate.inspect(sim,report,cfg,tmp_path/'enabled')
    assert len(calls)==1 and result['vlm_called']
    assert visual_decision(result,report)['grade']==expected


def test_simulation_safe_trim_preserves_source_identity(sim,tmp_path,monkeypatch):
    cfg=offline(monkeypatch,tmp_path)
    # Construct genuinely constant waits, unlike the real sample's accumulated drift.
    with h5py.File(hdf5_path(sim),'a') as f:
        for start,end in [(59,133),(277,352)]:
            for kind in ('state','action'):
                v=f[f'{start}/{kind}/vector'][:]
                for i in range(start,end):
                    g=f[f'{i}/{kind}']
                    g['vector'][:]=v
                    g['joint/position'][:]=np.r_[v[:7],v[8:15]]
                    g['left_effector/position'][:]=v[7:8];g['right_effector/position'][:]=v[15:16]
                    g['waist/position'][:]=v[16:19];g['head/position'][:]=v[19:21];g['robot/velocity'][:]=v[21:23]
    report=raw_checks(sim);before=fingerprint(sim)
    decision=visual_decision(yolo_gate.inspect(sim,report,cfg,tmp_path/'vision'),report)
    decision.update(grade='A',safe_trim_ids=list(range(len(check(report,'stationary')['detail']['intervals']))))
    out=derive(sim,tmp_path/'trimmed',report,decision,40,before)
    d=load(out);original=load(sim);prov=read_json(out/'provenance.json')
    assert d['n']<446 and prov['removed_frames']==446-d['n']
    assert d['source_frame_indices']==prov['source_frame_indices']
    assert np.array_equal(d['state'],original['state'][prov['source_frame_indices']])
    assert np.allclose(np.diff(d['t']),1/30)
    post=raw_checks(out)
    assert not post['hard_fail'],post
    assert fingerprint(sim)==before


def test_collection_simulation_identity_and_inplace_guard(sim):
    import workbench
    app=workbench.load_legacy()
    item=app.dataset_choice_entry(sim.parent,sim.parent.parent)
    assert item['robot_type']=='zerith' and item['source_format']=='zerith_sim_v1'
    before=fingerprint(sim)
    for call in [lambda:app.repair_commands(dict(hdf5_root=sim.parent),None,[sim.name]),
                 lambda:app.optimize_hdf5_commands(dict(hdf5_root=sim.parent))]:
        with pytest.raises(ValueError,match='仿真格式'):call()
    assert fingerprint(sim)==before
