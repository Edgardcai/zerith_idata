import json
import importlib.util
from pathlib import Path
import sys

import httpx
import pytest
from dataqc import config, vision, db, worker, batch_prepare
from dataqc.io import write_json, read_json
from test_pipeline import source
from test_grade_choices import grades, requests
from test_gateway_queue import mock_gateway, limited


def test_more_than_old_retry_limit_still_finishes(tmp_path,monkeypatch):
    calls=mock_gateway(monkeypatch,tmp_path,[limited() for _ in range(14)]+[httpx.Response(200,json={'status':'completed'})])
    waits=[];monkeypatch.setattr(vision,'wait_seconds',lambda seconds,*a:waits.append(seconds))
    result=vision._request_vlm('http://gateway','test',{},dict(api_timeout=1),lambda _:None)
    assert result.status_code==200 and len(calls)==15 and len(waits)==14
    assert max(waits)==120


@pytest.mark.parametrize('status',[408,500,502,503,504])
def test_temporary_http_failure_retries(status,tmp_path,monkeypatch):
    calls=mock_gateway(monkeypatch,tmp_path,[httpx.Response(status),httpx.Response(200,json={})])
    monkeypatch.setattr(vision,'wait_seconds',lambda *a:None)
    assert vision._request_vlm('http://gateway','test',{},dict(api_timeout=1),lambda _:None).status_code==200
    assert len(calls)==2


def test_network_timeout_retries_and_can_be_cancelled(tmp_path,monkeypatch):
    from dataqc.worker import Paused
    original=httpx.Client;calls=[]
    def handle(request):
        calls.append(1)
        if len(calls)<3:raise httpx.ReadTimeout('timeout',request=request)
        return httpx.Response(200,json={})
    monkeypatch.setattr(vision.httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(vision,'wait_seconds',lambda *a:None)
    assert vision._request_vlm('http://gateway','test',{},dict(api_timeout=1),lambda _:None).status_code==200
    assert len(calls)==3
    calls.clear()
    monkeypatch.setattr(vision,'wait_seconds',lambda *a:(_ for _ in ()).throw(Paused()))
    with pytest.raises(Paused):vision._request_vlm('http://gateway','test',{},dict(api_timeout=1),lambda _:None)


def test_astra_schema_store_and_reasoning_budget(tmp_path,monkeypatch):
    response=dict(observations='checked',findings=[])
    calls=mock_gateway(monkeypatch,tmp_path,[httpx.Response(200,json=dict(status='completed',output=[dict(content=[dict(type='output_text',text=json.dumps(response))])]))])
    cfg=config.settings()|dict(api_model='gpt-6-astra',reasoning_effort='none',max_output_tokens=1200,evidence_ids=['trajectory:summary'])
    assert vision.call_vlm([dict(type='input_text',text='test')],vision.Window,cfg,tmp_path/'cache')==response
    body=json.loads(calls[0].content)
    assert body['model']=='gpt-6-astra' and body['store'] is False
    assert body['reasoning']['effort']=='low' and body['max_output_tokens']>=8192
    assert body['text']['format']['strict'] is True
    finding=body['text']['format']['schema']['$defs']['Finding']
    assert finding['properties']['evidence_ids']['items']['enum']==['trajectory:summary']


def test_incomplete_episode_blocks_entire_automatic_export(source,tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DB',tmp_path/'db.sqlite3');db.init()
    monkeypatch.setattr(worker,'VAR',tmp_path/'work');monkeypatch.setattr(worker,'EXPORTS',tmp_path/'exports')
    monkeypatch.setattr(batch_prepare,'prepare_run',lambda *a:{})
    # These states persisted after an earlier attempt: one good, one unfinished.
    rid=db.create(str(source),'auto',config.settings(),[str(source),str(source)])
    first,last=db.episodes(rid)
    db.update('episodes',first['id'],status='ready',grade='A')
    db.update('episodes',last['id'],status='incomplete',reason='接口未完成')
    monkeypatch.setattr(worker.get_adapter(),'export',lambda *a:pytest.fail('partial exports forbidden'))
    worker.process_run(db.get_run(rid))
    result=db.get_run(rid)
    assert result['status']=='needs_review' and result['exports']==[]
    assert '未完成' in result['phase'] and '1/2' in result['phase']
    assert not (tmp_path/'exports').exists()


def test_retry_wait_does_not_give_up_after_three_attempts(source,tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DB',tmp_path/'db.sqlite3');db.init()
    monkeypatch.setattr(worker,'VAR',tmp_path/'work');monkeypatch.setattr(worker,'EXPORTS',tmp_path/'exports')
    monkeypatch.setattr(batch_prepare,'prepare_run',lambda *a:{})
    monkeypatch.setattr(worker.get_adapter(),'inspect',lambda *a,**k:(_ for _ in ()).throw(vision.RateLimited(30)))
    rid=db.create(str(source),'auto',config.settings(),[str(source)]);ep=db.episodes(rid)[0]
    db.update('episodes',ep['id'],data=dict(auto_retry_count=12))
    worker.process_run(db.get_run(rid));after=db.episode(ep['id'])
    assert after['status']=='retry_wait' and after['data']['auto_retry_count']==13
    assert db.get_run(rid)['status']=='retry_wait' and not db.get_run(rid)['exports']


def test_manual_report_failure_is_not_completed_or_reusable(source,tmp_path,monkeypatch):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'legacy/scripts/embodied_data_pipeline-main'))
    from quality_pipeline.episode_io import read_raw_episode
    from quality_pipeline.profiles import load_profile
    from integrations.zerith_rules import run_manual_checks,manual_cache,settings_for_profile
    from dataqc.incremental import completed_report
    from dataqc.quality_policy import display_report
    monkeypatch.setattr(config,'VAR',tmp_path/'var')
    monkeypatch.setattr(vision,'call_vlm',lambda *a,**k:(_ for _ in ()).throw(vision.APIUnavailable('额度不足')))
    profile=load_profile(Path(__file__).resolve().parents[1]/'legacy/scripts/embodied_data_pipeline-main/robot_profiles/zerith.yaml')
    report=run_manual_checks(read_raw_episode(source,profile),profile)
    assert report['execution']=='incomplete' and report['quality_grade']=='' and not report['accepted']
    assert display_report(report)['quality_grade']==''
    cfg=settings_for_profile(profile)
    assert not completed_report(manual_cache(source,cfg),cfg)


def test_failed_report_not_counted_as_reviewed_and_human_grade_survives(grades):
    app,cfg,rows=grades
    app.apply_grade_choices(cfg,requests(app.dataset_status(cfg),'A')[:1])
    path=Path(rows[0]['qc_output'])/'qc_report.json'
    old=read_json(path);old.update(execution='incomplete',quality_grade='',review_required=True,visual={'errors':{'motion':'接口未完成'}})
    write_json(path,old)
    status=app.dataset_status(cfg);row=status['episodes'][0]
    assert row['qc_grade']=='' and row['qc_grade_label']=='未完成'
    assert row['quality_grade']=='A' and row['grade_source']=='manual'
    assert status['grade_comparison']['incomplete_count']==1


def test_cli_incomplete_report_causes_failed_batch(source,tmp_path,monkeypatch):
    from argparse import Namespace
    legacy=Path(__file__).resolve().parents[1]/'legacy/scripts/embodied_data_pipeline-main'
    sys.path.insert(0,str(legacy))
    spec=importlib.util.spec_from_file_location('completion_cli',legacy/'scripts/run_quality_pipeline.py')
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    profile=cli.load_profile(legacy/'robot_profiles/zerith.yaml')
    args=Namespace(task=None,actions_json=None,manual_json=None,artifact_mode='compact',frame_sample=1,num_workers=1,manual_failure_json=None)
    monkeypatch.setattr(config,'VAR',tmp_path/'var')
    monkeypatch.setattr(vision,'call_vlm',lambda *a,**k:(_ for _ in ()).throw(vision.APIUnavailable('接口凭据不可用')))
    out=tmp_path/'reports'
    status=cli._run_batch_after_prepare(args,profile,source.parent,out,[source])
    summary=read_json(out/'batch_summary.json')
    assert status==1 and summary['ok'] is False and summary['failed_to_process']==1
    row=summary['episodes'][0]
    assert row['execution']=='incomplete' and row['quality_grade']=='' and row['delete_status']=='未完成'
