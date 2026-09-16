"""Managed-episode index, incremental event observation, and finalized-file operations."""
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
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


def timing_quality(f, t, dt, rate):
    """Terminal quality result, not an exception that leaves files saving forever."""
    reasons=[]; n=len(t)
    duplicates=int(np.count_nonzero(dt==0))
    backwards=int(np.count_nonzero(dt<0))
    gaps=int(np.count_nonzero(dt>1.5/rate))
    if duplicates:reasons.append(f'{duplicates} 个重复时间戳')
    if backwards:reasons.append(f'{backwards} 处时间戳倒退')
    if gaps:reasons.append(f'{gaps} 处采样间断')
    result={'status':'failed' if reasons else 'legacy_unverified','reasons':reasons,
            'duplicate_count':duplicates,'backward_count':backwards,'gap_count':gaps}
    if 'timing' not in f:return result
    try:
        g=f['timing']
        if int(g.attrs['version'])!=1:raise ValueError('不支持的时序格式版本')
        def column(path):
            a=np.asarray(g[path][:])
            if a.shape!=(n,) or not np.isfinite(a).all():raise ValueError('时序字段不完整：'+path)
            return a
        ref=column('reference_receive_ns')
        if np.any(np.diff(ref)<=0):reasons.append('主机参考时间未递增')
        max_skew=0.; max_state_age=0.
        for cam in ('cam_high','cam_left_wrist','cam_right_wrist'):
            kinds=('color','depth') if bool(f.attrs.get('depth_recorded',False)) else ('color',)
            for kind in kinds:
                p='cameras/rs/'+cam+'/'+kind+'/'
                raw=column(p+'device_timestamp_ms'); frames=column(p+'frame_number')
                epochs=column(p+'epoch'); received=column(p+'receive_ns')
                if np.any(np.diff(raw)<=0) or np.any(np.diff(frames)<=0):
                    reasons.append(cam+'/'+kind+' 帧号或时间重复/倒退')
                if np.any(np.diff(epochs)!=0):reasons.append(cam+'/'+kind+' 时钟会话发生变化')
                skew=float(np.max(np.abs(received-ref)))
                max_skew=max(max_skew,skew)
                if skew>20_000_000:reasons.append(cam+'/'+kind+' 对齐偏差超过 20ms')
                if cam=='cam_high' and kind=='color' and not np.array_equal(raw,t):
                    reasons.append('主时间戳与头部图片不一致')
        for field in ('upper_joint','gripper','chassis','waist','head'):
            for kind in ('state','control'):
                p='messages/'+field+'_'+kind+'/'
                received=column(p+'receive_ns'); seq=column(p+'seq')
                age=ref-received
                if np.any(received<0) or np.any(seq<1) or np.any(age<0):
                    reasons.append(field+'_'+kind+' 缺失或使用未来消息')
                if kind=='state':
                    max_state_age=max(max_state_age,float(age.max()))
                    if np.any(age>50_000_000):reasons.append(field+' 状态过期超过 50ms')
        stats=json.loads(g.attrs['sample_stats'])
        if int(stats.get('rejected',0))>0:reasons.append(f"{stats['rejected']} 个候选样本因无法对齐被跳过")
        events=json.loads(g.attrs.get('source_events','{}'))
        if events.get('clock_or_stream_reset',0):reasons.append('采集中相机时钟或流重置')
        if events.get('invalid_source_time',0):reasons.append('相机产生无效源时间')
        result.update(max_camera_skew_ms=max_skew/1e6,max_state_age_ms=max_state_age/1e6,
                      alignment='host_receive_causal_hold',sample_stats=stats,source_events=events)
    except (KeyError,ValueError,TypeError,OverflowError) as exc:
        reasons.append('时序元数据校验失败：'+str(exc))
    result['status']='failed' if reasons else 'passed'
    return result


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
            depth_path=f'observation/images/rs/{name}/depth'
            if bool(f.attrs.get('depth_recorded',False)):
                if depth_path not in f or f[depth_path].shape[0]!=n:
                    raise ValueError('已选择记录深度，但深度图缺失或帧数不一致')
                if any(len(v)==0 for v in f[depth_path]):raise ValueError('深度图存在空帧')
            v=directory/'videos'/'rs'/(name+'.mp4')
            if not v.is_file() or v.stat().st_size<32:raise ValueError('视频缺失或尚未保存')
        if int(f.attrs.get('total_frames',-1))!=n:raise ValueError('最终帧数尚未确认')
        total=int(f.attrs.get('total_subtasks',1)); completed=int(f.attrs.get('completed_subtasks',0))
        steps=[s for s in meta.get('step_index',[]) if s.get('end_frame_id',-1)>=s.get('start_frame_id',0)]
        warnings=[]
        if completed<total:warnings.append(f'只完成 {completed}/{total} 阶段')
        scale=1000 if f['timestamp'].attrs.get('unit')=='ms' or float(np.median(t))>1e11 else 1
        dt=np.diff(t)/scale; duration=float((t[-1]-t[0])/scale) if n>1 else 0
        rate=float(f.attrs.get('control_frequency',30)); actual=(n-1)/duration if duration>0 else 0
        if not np.isfinite(rate) or rate<=0:raise ValueError('采样频率无效')
        timing_qc=timing_quality(f,t,dt,rate)
        if timing_qc['status']=='failed':warnings.append('时序不合格：'+'；'.join(timing_qc['reasons']))
        if n>1 and actual<rate*.9:warnings.append(f'实际采样 {actual:.1f} Hz')
        if len(dt) and (dt==0).any():warnings.append(f'{int((dt==0).sum())} 个重复时间戳')
        long_intervals=int(np.count_nonzero(dt > 1.5/rate))
        max_interval_ms=float(dt.max()*1000) if len(dt) else 0.0
        if long_intervals:
            warnings.append(f'{long_intervals} 处采样间断，最大 {max_interval_ms:.1f} ms；回放需按实际时间插值')
        return {'frames':n,'duration_s':duration,'rate_hz':rate,'actual_hz':actual,'completed_steps':completed,
            'total_steps':total,'steps':steps,'warnings':warnings,'timestamp_unit':'ms' if scale==1000 else 's',
            'long_interval_count':long_intervals,'max_interval_ms':max_interval_ms,
            'timing_qc':timing_qc,
            'record_depth':all(f'observation/images/rs/{name}/depth' in f for name in ['cam_high','cam_left_wrist','cam_right_wrist'])}


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
          CREATE TABLE IF NOT EXISTS pending_deletions(episode_id INTEGER PRIMARY KEY, intent TEXT NOT NULL);
        ''');self.db.commit()
        if 'source_dataset' not in {r['name'] for r in self.db.execute('PRAGMA table_info(sessions)')}:
            self.db.execute('ALTER TABLE sessions ADD COLUMN source_dataset TEXT');self.db.commit()
        self.offsets={};self.log_counts={}
        self.video_paths={};self.video_path_lock=threading.Lock()
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
                    else:self.delete(row['id'])
                except Exception as exc:
                    if row['state']=='deleting':
                        detail=json.loads(row['detail'] or '{}');detail['delete_error']=str(exc)
                        self.db.execute('UPDATE episodes SET detail=? WHERE id=?',(json.dumps(detail,ensure_ascii=False),row['id']))
                    else:self.db.execute("UPDATE episodes SET state='error',detail=? WHERE id=?",(json.dumps({'error':str(exc)},ensure_ascii=False),row['id']))
            for row in self.db.execute("SELECT * FROM episodes WHERE state='completed'").fetchall():
                try:
                    path=self.episode_directory(row)/'review.json'
                    if path.is_symlink():continue
                    review=json.loads(path.read_text())
                    if not isinstance(review,dict):continue
                    last=self.db.execute("SELECT MAX(at) FROM audit WHERE episode_id=? AND action='grade'",(row['id'],)).fetchone()[0]
                    # An older archive must not undo a newer manual rating.
                    if last is not None and float(review.get('updated_at',0))<last:continue
                    if review.get('episode_uuid')==row['uuid'] and review.get('grade') in ('A','B','F'):
                        self.db.execute('UPDATE episodes SET grade=? WHERE id=?',(review['grade'],row['id']))
                except (ValueError,OSError,TypeError):pass
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

    def groups(self):
        with self.lock:
            rows=self.db.execute('''SELECT dataset, COUNT(*) AS total, MAX(id) AS latest_id,
                SUM(state NOT IN ('deleted','deleting')) AS retained,
                SUM(state='completed') AS completed, SUM(state='deleted') AS deleted,
                SUM(state='completed' AND grade='A') AS A,
                SUM(state='completed' AND grade='B') AS B,
                SUM(state='completed' AND grade='F') AS F
                FROM episodes GROUP BY dataset ORDER BY MAX(id) DESC''').fetchall()
        return [dict(row) for row in rows]

    def get(self,ident):
        with self.lock:
            row=self.db.execute('SELECT id,state,path,uuid,dataset,seq FROM episodes WHERE id=?',(ident,)).fetchone()
        return dict(row) if row else None

    def video_directory(self,row):
        return self.episode_directory(row)

    def episode_directory(self,row):
        """Locate renamed recordings by identity, never by a guessed sequence number.

        This read-only index leaves collection numbering and history untouched.
        Cache directory scans, but recheck identity before serving each recording.
        """
        def identity(path):
            try:
                path=self.safe(path)
                for name,key in [('review.json','episode_uuid'),('episode_meta.json','source_episode_id'),
                                 ('episode_meta.json','episode_id')]:
                    meta=path/name
                    if meta.is_symlink():continue
                    try:
                        value=json.loads(meta.read_text()).get(key)
                        if isinstance(value,str) and value:return value
                    except (OSError,ValueError,AttributeError):pass
            except ValueError:pass
            return None

        original=self.safe(row['path'])
        if identity(original)==row['uuid']:return original
        dataset=original.parent
        # A collection path must stay inside its recorded dataset.
        if dataset!=Path(row['dataset']):raise ValueError('录像目录与采集记录不一致')
        with self.video_path_lock:
            def lookup(folder):
                if folder.is_symlink() or folder.resolve()!=folder:return []
                try:stamp=folder.stat().st_mtime_ns
                except OSError:return []
                cached=self.video_paths.get(str(folder))
                if not cached or cached[0]!=stamp:
                    index={}
                    for child in folder.iterdir():
                        if child.name.startswith('.collection-discard-') or child.is_symlink() or not child.is_dir():continue
                        uid=identity(child)
                        if uid:index.setdefault(uid,[]).append(child)
                    cached=(stamp,index);self.video_paths[str(folder)]=cached
                return cached[1].get(row['uuid'],[])
            matches=lookup(dataset)
            if not matches:
                # Prefer the preserved original batch over a copied/edited scene.
                matches=lookup(self.root/'raw_data'/dataset.name)
            if not matches:
                # Historical datasets may be renamed or archived in raw_data.
                # Scan dataset directories only, never HDF5 contents or video trees.
                folders=[]
                for parent in (self.root,self.root/'raw_data'):
                    if not parent.is_dir() or parent.is_symlink():continue
                    folders.extend(p for p in parent.iterdir() if p.is_dir() and
                                   not p.is_symlink() and p.name not in ('lerobot','qc_reports','raw_data'))
                for folder in folders:
                    if folder!=dataset:matches.extend(lookup(folder))
                archived=[p for p in matches if p.is_relative_to(self.root/'raw_data')]
                if len(archived)==1:matches=archived
            if len(matches)>1:raise ValueError('发现多份相同标识的录像，无法确定原始数据')
            if len(matches)==1 and identity(matches[0])==row['uuid']:return self.safe(matches[0])
        raise ValueError('录像已移动或删除，采集目录及 raw_data 归档中未找到对应数据')

    def location(self,row):
        """Expose current location while preserving collection history and numbering."""
        result=dict(collection_name=f"episode_{row['seq']:06d}",current_name='',current_path='',
                    location_status='pending',location_error='',renamed=False)
        if row['state'] in ('deleted','deleting'):
            result['location_status']='discarded' if row['state']=='deleted' else 'discarding'
        elif row['state']=='completed':
            try:
                path=self.episode_directory(row)
                result.update(current_name=path.name,current_path=str(path),location_status='located',
                              renamed=str(path)!=row['path'])
            except (OSError,ValueError) as exc:
                result.update(location_status='unavailable',location_error=str(exc))
        return result

    def list(self,session_id=None,dataset=None,active_only=False,with_locations=False):
        with self.lock:
            clauses=[];params=[]
            if session_id:clauses.append('session_id=?');params.append(session_id)
            if dataset is not None:clauses.append('dataset=?');params.append(dataset)
            if active_only:clauses.append("state IN ('recording','saving','finalizing')")
            sql='SELECT * FROM episodes'+(' WHERE '+' AND '.join(clauses) if clauses else '')+' ORDER BY id DESC'
            if dataset is None:sql+=' LIMIT 1000'
            rows=self.db.execute(sql,params).fetchall()
        result=[]
        for row in rows:
            value=dict(row)
            for k in ['detail','progress']:value[k]=json.loads(value[k] or '{}')
            value['name']=f"episode_{value['seq']:06d}"
            if with_locations:value.update(self.location(value))
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

    def reconcile_missing(self,session_id,now=None):
        """Missing recording directories must not leave the session busy forever.

        Keep files and grades untouched. Require two observations separated by
        30 seconds, and protect a destination created by an in-flight rename.
        """
        now=time.time() if now is None else now
        events=[]
        with self.lock:
            rows=self.db.execute("SELECT * FROM episodes WHERE session_id=? AND state IN ('recording','saving')",(session_id,)).fetchall()
            for row in rows:
                source=self.safe(row['source']);path=self.safe(row['path'])
                destination=self.safe(Path(row['dataset'])/f"episode_{row['seq']:06d}")
                progress=json.loads(row['progress'] or '{}')
                if any(p.exists() for p in (source,path,destination)):
                    if 'missing_since' in progress:
                        progress.pop('missing_since')
                        self.db.execute('UPDATE episodes SET progress=? WHERE id=?',(json.dumps(progress),row['id']))
                    continue
                if 'missing_since' not in progress:
                    progress['missing_since']=now
                    self.db.execute('UPDATE episodes SET progress=? WHERE id=?',(json.dumps(progress),row['id']))
                    continue
                if now-progress['missing_since']<30:continue
                detail=json.loads(row['detail'] or '{}')
                detail.update(error='采集目录已不存在，录制状态已解除；请检查是否被采集端取消或在外部移动／删除',missing_source=str(source),missing_detected_at=now)
                self.db.execute("UPDATE episodes SET state='error',detail=? WHERE id=?",(json.dumps(detail,ensure_ascii=False),row['id']))
                self.db.execute('INSERT INTO audit(at,episode_id,action,detail) VALUES(?,?,?,?)',(now,row['id'],'missing_directory',json.dumps(detail,ensure_ascii=False)))
                events.append({'type':'missing_directory','episode':row['id']})
            self.db.commit()
        return events

    def observe(self,session,allow_new=True):
        dataset=Path(session['dataset'])
        source_dataset=Path(session.get('source_dataset') or dataset)
        missing_events=self.reconcile_missing(session['id'])
        if not source_dataset.exists():return missing_events
        for p in (dataset,source_dataset):
            if p.is_symlink() or p.resolve().parent!=self.root:raise ValueError('任务目录路径异常')
        baseline=set(session['baseline']); events=missing_events
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
        if session:
            atomic_json(path/'collection_task.json',{'config':json.loads(session['config']),
                'targets':json.loads(session['targets']),'source_dataset':session['source_dataset'],'dataset':session['dataset']})
        detail=json.loads(row['detail'] or '{}')
        qc=detail.get('timing_qc',{})
        review={'episode_uuid':row['uuid'],'number':row['seq'],'grade':row['grade'] or 'A','reviewed':False,'updated_at':time.time(),'timing_qc':qc}
        atomic_json(path/'timing_quality.json',qc)
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
            qc=json.loads(row['detail'] or '{}').get('timing_qc',{})
            # Collection history keeps its original path and sequence after QC
            # renames/archives a batch. Resolve the same UUID used by playback.
            path=self.episode_directory(row)
            review_path=path/'review.json'
            if review_path.is_symlink():raise ValueError('评级文件路径无效')
            review=json.loads(review_path.read_text()) if review_path.exists() else {}
            if not isinstance(review,dict):raise ValueError('评级文件格式无效')
            review.update(episode_uuid=row['uuid'],number=row['seq'],grade=grade,
                          reviewed=True,updated_at=time.time(),timing_qc=qc)
            atomic_json(review_path,review)
            self.db.execute('UPDATE episodes SET grade=? WHERE id=?',(grade,ident));self.db.execute('INSERT INTO audit(at,episode_id,action,detail) VALUES(?,?,?,?)',(time.time(),ident,'grade',grade));self.db.commit()

    def delete(self,ident):
        with self.lock:
            row=self.db.execute('SELECT * FROM episodes WHERE id=?',(ident,)).fetchone()
            if not row or row['state'] not in ('completed','deleting'):raise ValueError('只能放弃已保存的数据')
            pending=self.db.execute('SELECT intent FROM pending_deletions WHERE episode_id=?',(ident,)).fetchone()
            if pending:intent=json.loads(pending['intent'])
            else:
                path=self.episode_directory(row);stat=path.stat()
                intent={'source':str(path),'trash':str(path.with_name('.collection-discard-'+uuid.uuid4().hex)),
                        'device':stat.st_dev,'inode':stat.st_ino,'quarantined':False}
                with self.db:
                    self.db.execute('INSERT INTO pending_deletions VALUES(?,?)',(ident,json.dumps(intent)))
                    self.db.execute("UPDATE episodes SET state='deleting' WHERE id=?",(ident,))
            try:
                self._complete_delete(row,intent)
            except (OSError,ValueError) as exc:
                detail=json.loads(row['detail'] or '{}');detail['delete_error']=str(exc)
                self.db.execute('UPDATE episodes SET detail=? WHERE id=?',(json.dumps(detail,ensure_ascii=False),ident));self.db.commit()
                raise

    def _complete_delete(self,row,intent):
        """Journal the approved directory, then rename before removal.

        A crash or partial rmtree can resume without metadata and without ever
        deleting a different recording that has reused the original pathname.
        """
        ident=row['id'];source=self.safe(intent['source']);trash=self.safe(intent['trash'])
        if trash.parent!=source.parent or not trash.name.startswith('.collection-discard-'):
            raise ValueError('删除恢复目录无效')
        def same_directory(path):
            stat=path.stat()
            if (stat.st_dev,stat.st_ino)!=(intent['device'],intent['inode']):
                raise ValueError('删除目标已被其他目录替换，未删除任何替代数据')
        if not trash.exists() and not intent['quarantined']:
            # Still intact before quarantine: verify UUID again as well as inode.
            source=self.episode_directory(row)
            same_directory(source)
            if source.parent!=trash.parent:raise ValueError('删除目标已跨目录移动，请先检查数据位置')
            source.rename(trash)
        if trash.exists():
            same_directory(trash)
            if not intent['quarantined']:
                intent['quarantined']=True
                self.db.execute('UPDATE pending_deletions SET intent=? WHERE episode_id=?',(json.dumps(intent),ident));self.db.commit()
            shutil.rmtree(trash)
        detail=json.loads(row['detail'] or '{}');detail.pop('delete_error',None)
        with self.db:
            self.db.execute("UPDATE episodes SET state='deleted',detail=? WHERE id=?",(json.dumps(detail,ensure_ascii=False),ident))
            self.db.execute('DELETE FROM pending_deletions WHERE episode_id=?',(ident,))
            self.db.execute('INSERT INTO audit(at,episode_id,action,detail) VALUES(?,?,?,?)',(time.time(),ident,'delete',json.dumps(intent)))

    def close(self):
        with self.lock:self.db.close()
