import copy
import os
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from dataqc import config, io, vision
from dataqc.checks import raw_checks
from dataqc.incremental import completed_report
from dataqc.io import parse_task, prompt_issues, write_json, read_json
from dataqc.quality_policy import display_report
from test_pipeline import source
from test_grade_choices import grades, requests


@pytest.mark.parametrize('task,expected',[
 ('Grasp HK Orange Fanta with the left hand and then grasp Vita Coconut with the right hand.',{'left':'HK Orange Fanta','right':'Vita Coconut'}),
 (' grasp  商品 with herbs / 500ml  with the LEFT hand. ',{'left':'商品 with herbs / 500ml'}),
 ('Grasp Then & Grasp 2.0 with the right hand',{'right':'Then & Grasp 2.0'}),
 ('Grasp Yili Peach Yogurt with the left hand and then grasp Coca-Cola with the right hand',{'left':'Yili Peach Yogurt','right':'Coca-Cola'}),
])
def test_arbitrary_product_names(task,expected):
 assert parse_task(task)==expected
 assert not prompt_issues(task)


@pytest.mark.parametrize('task,reason',[
 ('','为空'),('Grasp with the left hand.','物品名称为空'),
 ('Grasp tea','缺少操作手'),
 ('Grasp X with the right hand and then grasp Y with the left hand.','顺序'),
])
def test_prompt_errors_explain_problem(task,reason):
 assert parse_task(task) is None
 assert reason in ' '.join(prompt_issues(task))


def test_human_notes_survive_repeated_reports_and_bulk(grades):
 app,cfg,rows=grades
 req=requests(app.dataset_status(cfg),'B')[:1]
 req[0].update(note='右腕视频遮挡',problem='视觉异常')
 app.apply_grade_choices(cfg,req)
 for i in range(5):
  write_json(Path(rows[0]['qc_output'])/'qc_report.json',dict(quality_grade='F',reason=str(i)))
  status=app.dataset_status(cfg)
  row=app.apply_grade_choices(cfg,requests(status),'qc')['status']['episodes'][0]
  assert (row['quality_grade'],row['manual_note'],row['manual_problem'])==('B','右腕视频遮挡','视觉异常')
  assert row['qc_grade']=='F'
 with pytest.raises(ValueError,match='已更新'):app.apply_grade_choices(cfg,req)


def test_grade_save_does_not_scan_or_read_media(grades,monkeypatch):
 from integrations import zerith_rules
 app,cfg,rows=grades;initial=app.dataset_status(cfg)
 def forbidden(*a,**k):pytest.fail('grade save must not read HDF5/media or rescan dataset')
 monkeypatch.setattr(app,'invalidate_status_cache',forbidden)
 monkeypatch.setattr(io,'load',forbidden);monkeypatch.setattr(io,'fingerprint',forbidden)
 from integrations import zerith_rules
 monkeypatch.setattr(zerith_rules,'prepare_manual_approval',forbidden,raising=False)
 result=app.apply_grade_choices(cfg,requests(initial,'A')[:1])
 assert result['saved_count']==1
 snapshot=app.replay_grade_snapshot(rows[0]['episode_dir'])
 after=app.save_replay_grade(dict(root=rows[0]['episode_dir'],grade='F',revision=snapshot['revision'],note='',problem=''))
 assert after['current_grade']=='F' and after['manual_note']==''


def test_fingerprint_reuses_content_hash_and_detects_rewrite(source,monkeypatch):
 first=io.fingerprint(source)
 real=io.sha;calls=[]
 monkeypatch.setattr(io,'sha',lambda p:calls.append(p) or real(p))
 assert io.fingerprint(source)==first and calls==[]
 path=source/'episode.hdf5';old=path.stat()
 with path.open('r+b') as f:f.seek(-1,2);v=f.read(1);f.seek(-1,2);f.write(bytes([v[0]^1]))
 os.utime(path,ns=(old.st_atime_ns,old.st_mtime_ns))
 assert io.fingerprint(source)!=first and len(calls)==1


def test_incremental_reuses_completed_model_and_retries_errors(source,tmp_path,monkeypatch):
 import sys
 sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'legacy/scripts/embodied_data_pipeline-main'))
 from quality_pipeline.episode_io import read_raw_episode
 from quality_pipeline.profiles import load_profile
 from integrations.zerith_rules import run_manual_checks,manual_cache,settings_for_profile
 from test_assessment import motion_ok
 monkeypatch.setattr(config,'VAR',tmp_path/'var')
 calls=[]
 def model(*a,**k):calls.append(1);return motion_ok()
 monkeypatch.setattr(vision,'call_vlm',model)
 profile=load_profile(Path(__file__).resolve().parents[1]/'legacy/scripts/embodied_data_pipeline-main/robot_profiles/zerith.yaml')
 ep=read_raw_episode(source,profile)
 first=run_manual_checks(ep,profile);second=run_manual_checks(ep,profile)
 assert len(calls)==1 and second['execution']=='cached'
 assert second['quality_grade']=='B'
 cfg=settings_for_profile(profile);cache=manual_cache(source,cfg)
 assert completed_report(cache,cfg)
 assert not completed_report(cache,cfg|dict(api_model='changed-model'))
 damaged=copy.deepcopy(first);damaged['visual']['errors']={'motion':'timeout'};write_json(cache/'manual_report.json',damaged)
 assert not completed_report(cache,cfg)
 # Full recheck refreshes numeric results but reuses a valid model signature.
 monkeypatch.setenv('DATAQC_CATEGORY_POLICY',json.dumps(dict(qc_force=True,qc_refresh_token='test-force')))
 third=run_manual_checks(ep,profile)
 assert len(calls)==1 and third['raw_report']['refresh_token']=='test-force'
 # New source identity/content creates a new automatic cache, never edits manual storage.
 write_json(source/'collection_task.json',dict(config=dict(task_name='changed metadata')))
 monkeypatch.delenv('DATAQC_CATEGORY_POLICY')
 assert manual_cache(source,settings_for_profile(profile))!=cache


def test_height_removed_and_hard_failures_still_f(source):
 import h5py
 with h5py.File(source/'episode.hdf5','a') as f:f['action/waist/position'][:,0]=.4
 write_json(source/'collection_task.json',dict(targets=dict(lift_height=.8)))
 report=raw_checks(source)
 assert not any(c['key']=='lift_height' for c in report['checks'])
 assert not report['hard_fail']
 with h5py.File(source/'episode.hdf5','a') as f:f['action/arm/position'][10,0]=float('nan')
 assert raw_checks(source)['hard_fail']


def test_historical_f_reinterpreted_without_mutation():
 checks=[dict(key=k,status='pass',detail={},label=k) for k in ('state','action','finite')]
 checks.append(dict(key='lift_height',status='fail',label='升降',detail={}))
 report=dict(quality_grade='F',raw_report=dict(version='zerith_qc_6',checks=checks))
 projected=display_report(report)
 assert projected['quality_grade']=='B' and projected['review_required']
 assert not projected['raw_report']['hard_fail'] and report['quality_grade']=='F'
 assert len(report['raw_report']['checks'])==4


def test_atomic_json_writers_do_not_share_temporary_files(tmp_path):
 from concurrent.futures import ThreadPoolExecutor
 path=tmp_path/'heartbeat.json'
 def write(i):
  write_json(path,dict(writer=i,data=[i]*100))
  current=read_json(path)
  assert current['data']==[current['writer']]*100
 with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(write,range(100)))
 assert not list(tmp_path.glob('*.tmp'))


def test_cli_append_and_full_recheck_keep_manual_choice(source,tmp_path,monkeypatch):
 import shutil, subprocess
 import workbench
 runtime=tmp_path/'runtime';monkeypatch.setattr(config,'VAR',runtime/'var')
 write_json(runtime/'config/settings.json',config.settings()|dict(vlm_enabled=False))
 group=tmp_path/'group';group.mkdir()
 for i in range(2):shutil.copytree(source,group/f'episode{i}')
 app=workbench.load_legacy()
 cfg=app.derive_paths(dict(hdf5_root=str(group),qc_root=str(tmp_path/'qc'),dataset_name='incremental',vlm_enabled=False))
 def run(force=False):
  cmd,cwd,env=app.qc_command(cfg|dict(qc_force=force))
  env['DATAQC_HOME']=str(runtime)
  result=subprocess.run(cmd,cwd=cwd,env=env,text=True,capture_output=True,timeout=90)
  assert result.returncode==0,result.stdout+result.stderr
  report_dir=Path(cmd[cmd.index('--output')+1])
  for manifest_path in report_dir.glob('*/manifest.json'):
   assert read_json(manifest_path)['processed_dir']==str(manifest_path.parent)
  return result.stdout
 first=run();assert '复用 0 条' in first
 status=app.dataset_status(app.stringify_config(cfg))
 req=requests(status,'B')[:1];req[0].update(note='已人工看过，保留',problem='视觉异常')
 app.apply_grade_choices(cfg,req)
 second=run();assert '复用 2 条' in second and '复用完整质检结果' in second
 shutil.copytree(source,group/'episode2')
 third=run();assert '发现 3 条 · 复用 2 条 · 新增/变化/未完成 1 条' in third
 fourth=run(True);assert '复用 0 条' in fourth and '缓存复用 3 条 · 待新审 0 条' in fourth
 row=app.dataset_status(app.stringify_config(cfg))['episodes'][0]
 assert (row['manual_quality_grade'],row['manual_note'])==('B','已人工看过，保留')


def test_atomic_json_preserves_report_permissions(tmp_path):
 import stat
 ordinary=tmp_path/'ordinary.json';ordinary.write_text('{}')
 created=tmp_path/'created.json';write_json(created,{'ok':True})
 assert stat.S_IMODE(created.stat().st_mode)==stat.S_IMODE(ordinary.stat().st_mode)
 created.chmod(0o640);write_json(created,{'updated':True})
 assert stat.S_IMODE(created.stat().st_mode)==0o640
