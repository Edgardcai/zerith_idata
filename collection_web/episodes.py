"""Managed-episode index, incremental event observation, and finalized-file operations."""
import json
import os
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import h5py
import numpy as np


def atomic_json(path, value):
    path=Path(path); tmp=path.with_name(path.name+'.tmp')
    with tmp.open('w') as f:
        json.dump(value,f,ensure_ascii=False,indent=2); f.flush(); os.fsync(f.fileno())
    tmp.replace(path)


def tail(path, size=16384):
    try:
        with Path(path).open('rb') as f:
            f.seek(0,2); f.seek(max(0,f.tell()-size)); return f.read().decode('utf-8',errors='replace')
    except OSError:return ''


def warning_kind(line):
    if 'WARNING' not in line:return None
    if '[WebRTC]' in line:return 'preview_warnings'
    return 'warnings'


def validate_finished(directory):
    directory=Path(directory)
    meta=json.loads((directory/'episode_meta.json').read_text())
    with h5py.File(directory/'episode.hdf5','r') as f:
        t=np.asarray(f['timestamp/t'][:],dtype=float)
        n=len(t)
        if n<1 or not np.isfinite(t).all():raise ValueError('时间戳缺失或无效')
        paths=['action/arm/position','action/effector/position','action/waist/position','action/head/position','action/base/velocity',
            'observation/state/arm/position','observation/state/effector/position','observation/state/waist/position','observation/state/head/position','observation/state/base/velocity']
        for k,width in zip(paths,[14,2,3,2,2]*2):
            if f[k].shape!=(n,width) or not np.isfinite(f[k][:]).all():raise ValueError(f'字段不完整：{k}')
        for name in ['cam_high','cam_left_wrist','cam_right_wrist']:
            if f[f'observation/images/rs/{name}/color'].shape[0]!=n:raise ValueError('图像帧数不一致')
            v=directory/'videos'/'rs'/(name+'.mp4')
            if not v.is_file() or v.stat().st_size<32:raise ValueError('视频缺失或尚未保存')
        if int(f.attrs.get('total_frames',-1))!=n:raise ValueError('最终帧数尚未确认')
        total=int(f.attrs.get('total_subtasks',1)); completed=int(f.attrs.get('completed_subtasks',0))
        steps=[s for s in meta.get('step_index',[]) if s.get('end_frame_id',-1)>=s.get('start_frame_id',0)]
        warnings=[]
        if completed<total:warnings.append(f'只完成 {completed}/{total} 阶段')
        scale=1000 if float(np.median(t))>1e11 else 1
        dt=np.diff(t)/scale; duration=float((t[-1]-t[0])/scale) if n>1 else 0
        if len(dt) and (dt<0).any():raise ValueError('时间戳倒退')
        rate=float(f.attrs.get('control_frequency',30)); actual=(n-1)/duration if duration>0 else 0
        if n>1 and actual<rate*.9:warnings.append(f'实际采样 {actual:.1f} Hz')
        if len(dt) and (dt==0).any():warnings.append(f'{int((dt==0).sum())} 个重复时间戳')
        return {'frames':n,'duration_s':duration,'rate_hz':rate,'actual_hz':actual,'completed_steps':completed,
            'total_steps':total,'steps':steps,'warnings':warnings,'timestamp_unit':'ms' if scale==1000 else 's'}


class EpisodeStore:
    def __init__(self, runtime, data_root):
        self.runtime=Path(runtime); self.runtime.mkdir(parents=True,exist_ok=True)
        self.root=Path(data_root).resolve(); self.lock=threading.RLock()
        self.db=sqlite3.connect(str(self.runtime/'collection.sqlite3'),check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, started REAL, dataset TEXT, config TEXT, targets TEXT, baseline TEXT, state TEXT);
          CREATE TABLE IF NOT EXISTS episodes(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, uuid TEXT, dataset TEXT, seq INTEGER,
            source TEXT, path TEXT, state TEXT, grade TEXT, detail TEXT, progress TEXT, created REAL, UNIQUE(dataset,uuid), UNIQUE(dataset,seq));
          CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, at REAL, episode_id INTEGER, action TEXT, detail TEXT);
        ''');self.db.commit()
        if 'source_dataset' not in {r['name'] for r in self.db.execute('PRAGMA table_info(sessions)')}:
            self.db.execute('ALTER TABLE sessions ADD COLUMN source_dataset TEXT');self.db.commit()
        self.offsets={};self.log_counts={}
        self.recover()

    def recover(self):
        with self.lock:
            for row in self.db.execute("SELECT * FROM episodes WHERE state IN ('finalizing','deleting')").fetchall():
                try:
                    source=self.safe(row['source']); dest=self.safe(row['path'])
                    if row['state']=='finalizing':
                        if dest.exists() and not source.exists():self._finish_index(row,dest)
                        elif source.exists() and not dest.exists():self.db.execute("UPDATE episodes SET state='saving' WHERE id=?",(row['id'],))
                        else:raise ValueError('编号恢复发现目录冲突')
                    else:
                        if dest.exists():shutil.rmtree(dest)
                        self.db.execute("UPDATE episodes SET state='deleted' WHERE id=?",(row['id'],))
                except Exception as exc:
                    self.db.execute("UPDATE episodes SET state='error',detail=? WHERE id=?",(json.dumps({'error':str(exc)},ensure_ascii=False),row['id']))
            for row in self.db.execute("SELECT * FROM episodes WHERE state='completed'").fetchall():
                try:
                    review=json.loads((self.safe(row['path'])/'review.json').read_text())
                    if review.get('episode_uuid')==row['uuid'] and review.get('grade') in ('A','B','F'):
                        self.db.execute('UPDATE episodes SET grade=? WHERE id=?',(review['grade'],row['id']))
                except (ValueError,OSError):pass
            self.db.commit()

    def safe(self,path):
        path=Path(path)
        if not path.is_absolute():raise ValueError('目录必须为绝对路径')
        if path.is_symlink():raise ValueError('不接受符号链接')
        resolved=path.resolve()
        resolved.relative_to(self.root)
        if resolved==self.root or resolved.parent==self.root:raise ValueError('不允许操作数据根或任务目录')
        # Resolve every ancestor to disallow aliases out of or inside the dataset.
        if resolved!=path:raise ValueError('目录包含链接或非法路径')
        return resolved

    def new_session(self,ident,config,targets,dataset,source_dataset=None):
        source_dataset=Path(source_dataset or dataset)
        baseline=[p.name for p in source_dataset.iterdir() if p.is_dir()] if source_dataset.exists() else []
        with self.lock:
            self.db.execute('INSERT INTO sessions(id,started,dataset,config,targets,baseline,state,source_dataset) VALUES(?,?,?,?,?,?,?,?)',(ident,time.time(),str(dataset),json.dumps(config),json.dumps(targets),json.dumps(baseline),'connecting',str(source_dataset)));self.db.commit()
        return baseline

    def set_session(self,ident,state):
        with self.lock:self.db.execute('UPDATE sessions SET state=? WHERE id=?',(state,ident));self.db.commit()

    def last_session(self):
        with self.lock:r=self.db.execute('SELECT * FROM sessions ORDER BY started DESC LIMIT 1').fetchone()
        if not r:return None
        value=dict(r)
        value['source_dataset']=value.get('source_dataset') or value['dataset']
        for k in ['config','targets','baseline']:value[k]=json.loads(value[k])
        return value

    def list(self,session_id=None):
        with self.lock:
            sql='SELECT * FROM episodes'+(' WHERE session_id=?' if session_id else '')+' ORDER BY id DESC LIMIT 1000'
            rows=self.db.execute(sql,(session_id,) if session_id else ()).fetchall()
        result=[]
        for row in rows:
            value=dict(row)
            for k in ['detail','progress']:value[k]=json.loads(value[k] or '{}')
            value['name']=f"episode_{value['seq']:06d}"
            result.append(value)
        return result

    def counts(self,session_id):
        result={'completed':0,'deleted':0,'A':0,'B':0,'F':0}
        with self.lock:
            rows=self.db.execute('SELECT state,grade,COUNT(*) AS n FROM episodes WHERE session_id=? GROUP BY state,grade',(session_id,)).fetchall()
        for row in rows:
            if row['state']=='deleted':result['deleted']+=row['n']
            if row['state']=='completed':
                result['completed']+=row['n']
                if row['grade'] in ('A','B','F'):result[row['grade']]+=row['n']
        return result

    def observe(self,session,allow_new=True):
        dataset=Path(session['dataset'])
        source_dataset=Path(session.get('source_dataset') or dataset)
        if not source_dataset.exists():return []
        for p in (dataset,source_dataset):
            if p.is_symlink() or p.resolve().parent!=self.root:raise ValueError('任务目录路径异常')
        baseline=set(session['baseline']); events=[]
        # Include known finalizing paths: a crash may have happened just after moving.
        directories=list(source_dataset.iterdir())
        if dataset!=source_dataset:
            with self.lock:
                directories.extend(Path(r['path']) for r in self.db.execute("SELECT path FROM episodes WHERE session_id=? AND state='finalizing'",(session['id'],)))
        for directory in dict.fromkeys(directories):
            if not directory.is_dir() or (directory.parent==source_dataset and directory.name in baseline) or directory.is_symlink():continue
            if not re.fullmatch(r'[a-fA-F0-9]{32}|episode_\d{6,}',directory.name):continue
            with self.lock:
                row=self.db.execute('SELECT * FROM episodes WHERE dataset=? AND (uuid=? OR path=?)',(str(dataset),directory.name,str(directory))).fetchone()
                if row is None:
                    if not allow_new:continue
                    seq=self.db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM episodes WHERE dataset=?',(str(dataset),)).fetchone()[0]
                    if dataset.exists():
                        existing=[int(m[1]) for p in dataset.iterdir() if (m:=re.fullmatch(r'episode_(\d{6,})',p.name))]
                        seq=max(seq,max(existing,default=0)+1)
                    while (dataset/f'episode_{seq:06d}').exists():seq+=1
                    self.db.execute('INSERT INTO episodes(session_id,uuid,dataset,seq,source,path,state,detail,progress,created) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (session['id'],directory.name,str(dataset),seq,str(directory),str(directory),'recording','{}','{}',time.time()));self.db.commit()
                    row=self.db.execute('SELECT * FROM episodes WHERE dataset=? AND uuid=?',(str(dataset),directory.name)).fetchone()
                    events.append({'type':'start','episode':row['id']})
                if row['session_id']!=session['id'] or row['state'] in ('completed','deleted','error','deleting'):continue
                if row['state']=='finalizing' and Path(row['path']).exists() and not Path(row['source']).exists():
                    try:
                        self._finish_index(row,self.safe(row['path']));self.db.commit()
                        events.append({'type':'completed','episode':row['id']})
                    except (OSError,ValueError):pass
                    continue
            log=directory/'collection.log'; key=str(log)
            progress=json.loads(row['progress'] or '{}')
            try:
                offset=self.offsets.get(key,progress.get('log_offset',0))
                with log.open('rb') as f:
                    if log.stat().st_size<offset:offset=0
                    f.seek(offset);chunk=f.read(1024*1024)
                end=chunk.rfind(b'\n')+1;self.offsets[key]=offset+end
                text=chunk[:end].decode('utf-8',errors='replace')
            except OSError:continue
            progress['log_offset']=offset+end; phase=row['state']
            for line in text.splitlines():
                kind=warning_kind(line)
                if kind:progress[kind]=progress.get(kind,0)+1
                if 'ERROR' in line:progress['errors']=progress.get('errors',0)+1
                match=re.search(r'子任务 (\d+) 保存完毕.*分界帧: (\d+)',line)
                if match:
                    progress['completed_steps']=int(match[1]);progress['frames']=int(match[2]);events.append({'type':'stage','episode':row['id'],'stage':int(match[1])})
                match=re.search(r'(?:累计:|帧数:)\s*(\d+)',line)
                if match:progress['frames']=max(progress.get('frames',0),int(match[1]))
                match=re.search(r'已耗时 ([\d.]+)s',line)
                if match:progress['elapsed_s']=float(match[1])
                if '[采集结束]' in line:phase='saving';events.append({'type':'saving','episode':row['id']})
                if '采集流结束' in line:progress['final_signal']=True;phase='saving'
            with self.lock:
                self.db.execute('UPDATE episodes SET state=?,progress=? WHERE id=?',(phase,json.dumps(progress),row['id']));self.db.commit()
            if progress.get('final_signal'):
                try:
                    detail=validate_finished(directory)
                    if progress.get('warnings'):detail['warnings'].append(f"写入告警 {progress['warnings']} 次")
                    if progress.get('errors'):detail['warnings'].append(f"采集错误 {progress['errors']} 次")
                    self.finalize(row['id'],detail);events.append({'type':'completed','episode':row['id']})
                except (OSError,ValueError,KeyError,RuntimeError) as exc:
                    # Keep pending and retry: final metadata may lag the log marker.
                    with self.lock:
                        self.db.execute('UPDATE episodes SET detail=? WHERE id=?',(json.dumps({'error':str(exc)},ensure_ascii=False),row['id']));self.db.commit()
        return events

    def _finish_index(self,row,path):
        session=self.db.execute('SELECT * FROM sessions WHERE id=?',(row['session_id'],)).fetchone()
        if session and session['source_dataset'] and session['source_dataset']!=session['dataset']:
            atomic_json(path/'collection_task.json',{'config':json.loads(session['config']),
                'targets':json.loads(session['targets']),'source_dataset':session['source_dataset'],'dataset':session['dataset']})
        review={'episode_uuid':row['uuid'],'number':row['seq'],'grade':row['grade'] or 'A','reviewed':False,'updated_at':time.time()}
        atomic_json(path/'review.json',review)
        self.db.execute("UPDATE episodes SET path=?,state='completed',grade=? WHERE id=?",(str(path),review['grade'],row['id']))

    def finalize(self,ident,detail):
        with self.lock:
            row=self.db.execute('SELECT * FROM episodes WHERE id=?',(ident,)).fetchone()
            if row['state']=='completed':return
            source=self.safe(row['source']); dest=self.safe(Path(row['dataset'])/f"episode_{row['seq']:06d}")
            dest.parent.mkdir(exist_ok=True)
            if dest.exists():raise ValueError('顺序编号目录已存在，未覆盖')
            self.db.execute("UPDATE episodes SET state='finalizing',path=?,detail=? WHERE id=?",(str(dest),json.dumps(detail,ensure_ascii=False),ident));self.db.commit()
            try:source.rename(dest)
            except Exception:
                self.db.execute("UPDATE episodes SET state='saving',path=? WHERE id=?",(str(source),ident));self.db.commit();raise
            row=self.db.execute('SELECT * FROM episodes WHERE id=?',(ident,)).fetchone()
            self._finish_index(row,dest);self.db.commit()

    def rate(self,ident,grade):
        if grade not in ('A','B','F'):raise ValueError('评级只支持 A / B / F')
        with self.lock:
            row=self.db.execute('SELECT * FROM episodes WHERE id=?',(ident,)).fetchone()
            if not row or row['state']!='completed':raise ValueError('只能评价保存完成的数据')
            path=self.safe(row['path']);atomic_json(path/'review.json',{'episode_uuid':row['uuid'],'number':row['seq'],'grade':grade,'reviewed':True,'updated_at':time.time()})
            self.db.execute('UPDATE episodes SET grade=? WHERE id=?',(grade,ident));self.db.execute('INSERT INTO audit(at,episode_id,action,detail) VALUES(?,?,?,?)',(time.time(),ident,'grade',grade));self.db.commit()

    def delete(self,ident):
        with self.lock:
            row=self.db.execute('SELECT * FROM episodes WHERE id=?',(ident,)).fetchone()
            if not row or row['state'] not in ('completed','deleting'):raise ValueError('只能放弃已保存的数据')
            path=self.safe(row['path'])
            self.db.execute("UPDATE episodes SET state='deleting' WHERE id=?",(ident,));self.db.commit()
            try:
                if path.exists():shutil.rmtree(path)
            except OSError:
                # Deleting remains durable and recoverable; never claim success on partial removal.
                raise
            self.db.execute("UPDATE episodes SET state='deleted' WHERE id=?",(ident,));self.db.execute('INSERT INTO audit(at,episode_id,action,detail) VALUES(?,?,?,?)',(time.time(),ident,'delete',row['path']));self.db.commit()

    def close(self):
        with self.lock:self.db.close()
