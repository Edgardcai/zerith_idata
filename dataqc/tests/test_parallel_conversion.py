import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_pipeline import source,decision
from dataqc.io import write_json,read_json,fingerprint
from dataqc.checks import raw_checks
from dataqc.repair import derive
from dataqc.parallel import THREAD_ENV

ROOT=Path(__file__).resolve().parents[1]


def isolated(script,tmp_path,*args):
    path=tmp_path/'runner.py';path.write_text(script)
    env=dict(os.environ);env.update({k:'1'for k in THREAD_ENV});env['PYTHONPATH']=str(ROOT)
    result=subprocess.run([sys.executable,str(path),*map(str,args)],env=env,cwd=ROOT,text=True,capture_output=True,timeout=90)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
    return result.stdout


def test_serial_parallel_export_and_full_validation(source,tmp_path):
    report=raw_checks(source);d=decision();fixed=derive(source,tmp_path/'fixed',report,d,40,fingerprint(source))
    stages=read_json(fixed/'provenance.json')['stages']
    entries=[dict(root=str(fixed),grade='B')]
    for st in stages:
        entries.append(dict(root=str(fixed),grade='B',range=[st['start'],st['end']],task=f"Grasp {st['item']} with the {st['hand']} hand"))
    entries=entries*2;write_json(tmp_path/'entries.json',entries)
    script='''import json,sys,os,time
from pathlib import Path
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import cv2
from dataqc.parallel import episode_pool,ordered_map
from dataqc.export import create_dataset,validate_dataset,path_for,rows
from dataqc.io import read_json,open_video,video_path

def probe(root):
    time.sleep(.15)
    cap=open_video(video_path(root,'cam_high'));threads=cap.get(cv2.CAP_PROP_N_THREADS);cap.release()
    return dict(pid=os.getpid(),cv=cv2.getNumThreads(),arrow=pa.cpu_count(),decoder=threads)

if __name__=='__main__':
    base=Path(sys.argv[1]);entries=read_json(base/'entries.json')
    with episode_pool(1,'cpu'):create_dataset(entries,base/'serial')
    with episode_pool(4,'cpu'):
        probes=ordered_map(probe,[entries[0]['root']]*12)
        assert len({p['pid']for p in probes})>1
        assert all(p['cv']==p['arrow']==p['decoder']==1 for p in probes),probes
        create_dataset(entries,base/'parallel')
        assert validate_dataset(base/'parallel')['passed']
        for name in ['info.json','stats.json','episode_name_mapping.json']:
            assert read_json(base/'serial/meta'/name)==read_json(base/'parallel/meta'/name),name
        for name in ['episodes.jsonl','episodes_stats.jsonl','tasks.jsonl']:
            assert rows(base/'serial/meta'/name)==rows(base/'parallel/meta'/name),name
        for i in range(len(entries)):
            assert pq.read_table(path_for(base/'serial',i)).equals(pq.read_table(path_for(base/'parallel',i)))
        p=path_for(base/'parallel',2);t=pq.read_table(p)
        t=t.set_column(t.schema.get_field_index('index'),'index',pa.array([0]*len(t),type=pa.int64()));pq.write_table(t,p)
        broken=validate_dataset(base/'parallel');assert not broken['passed']
        assert any(isinstance(e,dict) and e.get('episode')==2 and 'index' in e.get('error','') for e in broken['issues'])
        print(json.dumps(probes))
'''
    isolated(script,tmp_path,tmp_path)


def test_worker_failure_does_not_publish(source,tmp_path):
    fixed=derive(source,tmp_path/'fixed',raw_checks(source),decision(),40,fingerprint(source))
    entries=[dict(root=str(fixed),grade='B'),dict(root=str(tmp_path/'missing'),grade='B')]
    write_json(tmp_path/'entries.json',entries)
    isolated('''import sys
from pathlib import Path
from dataqc.parallel import episode_pool
from dataqc.export import create_dataset
from dataqc.io import read_json
if __name__=='__main__':
 base=Path(sys.argv[1])
 try:
  with episode_pool(4,'cpu'):create_dataset(read_json(base/'entries.json'),base/'output')
 except Exception:pass
 else:raise AssertionError('failure expected')
 assert not (base/'output').exists()
''',tmp_path,tmp_path)


def test_nvenc_slots_bound_across_processes(tmp_path):
    # Lock the full quota in this process; a ninth process must wait for release.
    from contextlib import ExitStack
    from dataqc.video_encoding import nvenc_slot
    device='test-'+tmp_path.name
    env=dict(os.environ,PYTHONPATH=str(ROOT))
    with ExitStack()as stack:
        for _ in range(8):stack.enter_context(nvenc_slot(device))
        script="from dataqc.video_encoding import nvenc_slot; import sys; from pathlib import Path\nwith nvenc_slot(sys.argv[1]):Path(sys.argv[2]).write_text('acquired')"
        marker=tmp_path/'acquired'
        child=subprocess.Popen([sys.executable,'-c',script,device,str(marker)],env=env)
        try:
            with pytest.raises(subprocess.TimeoutExpired):child.wait(timeout=.6)
            assert not marker.exists()
        except BaseException:
            child.kill();child.wait();raise
    assert child.wait(timeout=10)==0 and marker.exists()


def test_conversion_workers_are_distinct_from_qc_parallelism():
    import workbench
    app=workbench.load_legacy()
    cfg=app.derive_paths(dict(lerobot_workers=4,convert_jobs=8))
    assert cfg['lerobot_workers']==4
    cmd,_,_=app.qc_command(cfg);assert cmd[cmd.index('--num-workers')+1]=='2'
    for invalid in (0,-1,1.5,True,'bad'):
        with pytest.raises(ValueError,match='转换进程数'):app.derive_paths(dict(lerobot_workers=invalid))
