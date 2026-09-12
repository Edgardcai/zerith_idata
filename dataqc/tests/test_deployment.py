import json
import pytest
from deploy import restart_when_idle as deploy


def setup(monkeypatch,tmp_path):
    monkeypatch.setattr(deploy,'ROOT',tmp_path)
    monkeypatch.setattr(deploy,'STATUS',tmp_path/'deployment.json')
    path=tmp_path/'runtime/config/settings.json';path.parent.mkdir(parents=True)
    path.write_text(json.dumps(dict(vlm_enabled=False,api_model='gpt-5.6-terra')))
    calls=[]
    monkeypatch.setattr(deploy.subprocess,'run',lambda args,**kw:calls.append(args))
    return path,calls


def test_busy_deployment_does_not_restart(monkeypatch,tmp_path):
    path,calls=setup(monkeypatch,tmp_path)
    monkeypatch.setattr(deploy,'ready',lambda:False)
    monkeypatch.setattr(deploy,'active_jobs',lambda:[dict(id='running',status='running')])
    def stop(_):raise RuntimeError('stop test wait')
    monkeypatch.setattr(deploy.time,'sleep',stop)
    with pytest.raises(RuntimeError,match='stop test wait'):deploy.main()
    assert calls==[] and 'motion_batch_size' not in json.loads(path.read_text())
    assert json.loads(deploy.STATUS.read_text())['status']=='waiting_for_idle'


def test_idle_deployment_restarts_and_verifies_both_services(monkeypatch,tmp_path):
    path,calls=setup(monkeypatch,tmp_path);states=iter([False,True])
    monkeypatch.setattr(deploy,'ready',lambda:next(states))
    monkeypatch.setattr(deploy,'active_jobs',lambda:[])
    monkeypatch.setattr(deploy.time,'sleep',lambda _:None)
    deploy.main()
    cfg=json.loads(path.read_text())
    assert 'motion_batch_size' not in cfg and 'motion_batch_concurrency' not in cfg
    assert cfg['vlm_enabled'] is False
    assert calls[0]==['systemctl','--user','restart','dataqc-web','dataqc-worker']
    assert json.loads(deploy.STATUS.read_text())['status']=='active'
