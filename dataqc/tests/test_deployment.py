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


def test_render_custom_port_and_data_roots(tmp_path):
    from deploy.install_services import render
    render(tmp_path, '/opt/dataqc/bin/python', '/srv/projects/caizj/dataqc/runtime',
           port=9990, real_root='/srv/data/datasets/public/zerith_data',
           sim_root='/srv/data/datasets/public/zerith_sim_data', binary_path='/opt/video/bin')
    web=(tmp_path/'dataqc-web.service').read_text()
    worker=(tmp_path/'dataqc-worker.service').read_text()
    assert '--port 9990' in web
    for unit in (web, worker):
        assert 'DATAQC_REAL_ROOT=/srv/data/datasets/public/zerith_data' in unit
        assert 'DATAQC_SIM_ROOT=/srv/data/datasets/public/zerith_sim_data' in unit
        assert 'DATAQC_PORT=9990' in unit
        assert '/opt/video/bin' in unit
        assert '@' not in unit
    with pytest.raises(ValueError):render(tmp_path, '/bin/python', '/tmp/qc', port=65536)


def test_custom_roots_reach_ui_and_api(tmp_path):
    import os, subprocess, sys
    project=__import__('pathlib').Path(__file__).resolve().parents[1]
    code='''
import workbench
from dataqc import api
app=workbench.load_legacy()
assert str(api.SOURCE_ROOT)=='/srv/data/datasets/public/zerith_data'
assert str(api.SIM_SOURCE_ROOT)=='/srv/data/datasets/public/zerith_sim_data'
for machine, expected in [('zerith',api.SOURCE_ROOT),('simulation',api.SIM_SOURCE_ROOT)]:
 app.discover_recursive_hdf5_dataset_choices=lambda root: []
 assert app.discover_machine_datasets(machine)['scan_root']==str(expected)
 assert str(expected) in app.HTML
 assert str(expected) in app.CROSS_PLATFORM_HTML
assert '服务端 9990' in app.HTML
assert api.get_settings()['source_roots']['real']==str(api.SOURCE_ROOT)
'''
    env=dict(os.environ, DATAQC_HOME=str(tmp_path/'runtime'), DATAQC_PORT='9990',
             DATAQC_REAL_ROOT='/srv/data/datasets/public/zerith_data',
             DATAQC_SIM_ROOT='/srv/data/datasets/public/zerith_sim_data')
    result=subprocess.run([sys.executable,'-c',code],cwd=project,env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stdout+result.stderr
