import subprocess
import cv2
import numpy as np
import pytest
from dataqc import io, video_encoding as encoding
from test_pipeline import source


def test_zerith_default_machine_and_bounded_workers(tmp_path,monkeypatch):
    import workbench
    app=workbench.load_legacy()
    cfg=app.derive_paths(dict(hdf5_root=str(tmp_path/'episodes'),qc_root=str(tmp_path/'qc')))
    assert cfg['robot_type']=='zerith' and cfg['convert_jobs']==2
    assert cfg['gpu_device']=='0' and cfg['lerobot_cuda'] is True
    cmd,_,_=app.qc_command(cfg)
    assert cmd[cmd.index('--num-workers')+1]=='2'
    cfg['convert_jobs']=8
    cmd,_,_=app.qc_command(cfg)
    assert cmd[cmd.index('--num-workers')+1]=='2'
    roots=[]
    monkeypatch.setattr(app,'discover_recursive_hdf5_dataset_choices',lambda root:roots.append(str(root)) or [])
    monkeypatch.setattr(app,'discover_recursive_mcap_dataset_choices',lambda root:roots.append(str(root)) or [])
    result=app.discover_machine_datasets('zerith')
    assert roots==['/data/zerith_data'] and result['machine']=='zerith'
    assert [r['type'] for r in result['scan_roots']]==['HDF5']


def test_gpu_failure_retries_same_selection_with_cpu(tmp_path,monkeypatch):
    attempts=[]
    monkeypatch.setattr(encoding,'selected_encoder',lambda:(encoding.encoder_args('0'),'GPU'))
    def encode(src,dst,indices,args):
        attempts.append((list(indices),args))
        if 'h264_nvenc' in args:raise BrokenPipeError()
    monkeypatch.setattr(io,'_encode_selected_frames',encode)
    io.encode_selection('source',tmp_path/'out.mp4',[0,4,9])
    assert len(attempts)==2 and attempts[0][0]==attempts[1][0]==[0,4,9]
    assert 'libx264' in attempts[1][1]
    with encoding.video_encoding('cpu'):
        assert encoding._DEVICE.get()=='cpu'
    assert encoding._DEVICE.get()=='0'


def test_unavailable_gpu_uses_cpu(monkeypatch):
    encoding.available_encoder.cache_clear()
    monkeypatch.setattr(subprocess,'run',lambda *a,**kw:subprocess.CompletedProcess(a,1))
    assert 'libx264' in encoding.available_encoder('9')[0]
    encoding.available_encoder.cache_clear()


def test_selection_preserves_frames_and_rejects_missing_frames(tmp_path):
    src=tmp_path/'src.mp4'
    writer=cv2.VideoWriter(str(src),cv2.VideoWriter_fourcc(*'mp4v'),30,(64,48))
    for i in range(12):writer.write(np.full((48,64,3),i*18,np.uint8))
    writer.release()
    dst=tmp_path/'out.mp4'
    with encoding.video_encoding('cpu'):
        io.encode_selection(src,dst,[1,5,10])
        cap=cv2.VideoCapture(str(dst));means=[]
        while True:
            ok,img=cap.read()
            if not ok:break
            means.append(img.mean())
        cap.release()
        assert len(means)==3
        assert np.allclose(means,[18,90,180],atol=8)
        with pytest.raises(ValueError,match='不完整'):io.encode_selection(src,dst,[1,99])
    for indices in [[],[1,1],[5,1],[-1]]:
        with pytest.raises(ValueError,match='索引'):io.encode_selection(src,dst,indices)


def test_two_worker_cli_processes_both_episodes(source,tmp_path):
    import json,os,shutil
    import h5py
    from pathlib import Path
    import workbench
    with h5py.File(source/'episode.hdf5','a') as f:
        f['action/arm/position'][10,0]=float('nan')
    group=tmp_path/'group'
    for index in range(2):shutil.copytree(source,group/f'episode_{index:06d}')
    app=workbench.load_legacy()
    cfg=app.derive_paths(dict(hdf5_root=str(group),qc_root=str(tmp_path/'reports'),dataset_name='parallel_fixture',convert_jobs=2))
    cmd,cwd,env=app.qc_command(cfg)
    root=Path(__file__).resolve().parents[1]
    env.update(DATAQC_HOME=str(tmp_path/'runtime'),PYTHONPATH=os.pathsep.join([str(root),str(root/'vendor')]))
    result=subprocess.run(cmd,cwd=cwd,env=env,text=True,capture_output=True,timeout=60)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'Batch QC workers: 2, episodes: 2' in result.stderr
    summary=json.loads((Path(cmd[cmd.index('--output')+1])/'batch_summary.json').read_text())
    assert len(summary['episodes'])==2
    assert all(row['quality_grade']=='F' for row in summary['episodes'])
