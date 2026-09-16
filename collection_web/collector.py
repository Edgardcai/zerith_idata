import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from episodes import tail, atomic_json
from tasks import validate_task, dataset_name, scene_config
from protocol import Vendor

class Collector:
    def __init__(self,store,monitor,runtime,vendor_factory=Vendor):
        self.store=store;self.monitor=monitor;self.runtime=Path(runtime);self.vendor_factory=vendor_factory
        self.lock=threading.RLock();self.operation=threading.Lock();self.shutdown=threading.Event()
        self.session=store.last_session();self.stream=None;self.vendor=None;self.rpc_thread=None
        self.phase='idle';self.error=None;self.responses=[];self.events=[];self.transport=False;self.accepted=False
        self.selected_scene=None
        selection_file=self.runtime/'selected_scene.json'
        if selection_file.exists():
            self.selected_scene=self.scene_paths(json.loads(selection_file.read_text()))
        if self.session and self.session['state'] not in ('closed','rejected'):
            self.phase='disconnected';self.error='网站曾重启，请先确认原会话状态';self.store.set_session(self.session['id'],'disconnected')
        self.thread=threading.Thread(target=self._watch,daemon=True);self.thread.start()

    def external_active(self):
        active=[]
        for dataset in self.store.root.iterdir():
            if not dataset.is_dir() or dataset.is_symlink():continue
            # Only recent files: no HDF5 reads while the vendor is writing.
            for p in dataset.glob('*/collection.log'):
                try:
                    if time.time()-p.stat().st_mtime<10 and '采集流结束' not in tail(p):active.append(str(p.parent))
                except OSError:pass
        return active

    def scene_paths(self,payload):
        values=scene_config(payload)
        dataset=self.store.root/dataset_name(values['scene_id'],values['task_id'])
        if dataset.is_symlink() or (dataset.exists() and not dataset.is_dir()):raise ValueError('场景输出目录异常')
        return {**values,'dataset':str(dataset)}

    def apply_scene(self,payload):
        with self.operation:
            with self.lock:
                if self.phase not in ('idle','closed','rejected'):raise ValueError('请先结束当前会话，再应用场景')
            selection=self.scene_paths(payload)
            Path(selection['dataset']).mkdir(exist_ok=True)
            atomic_json(self.runtime/'selected_scene.json',selection)
            with self.lock:self.selected_scene=selection
            return selection

    def task_paths(self,payload):
        config,targets=validate_task(payload)
        source=self.store.root/f"{config['task_id']}_{config['task_name']}"
        dataset=Path(self.scene_paths(config)['dataset'])
        for directory in (source,dataset):
            if directory.is_symlink() or (directory.exists() and not directory.is_dir()):raise ValueError('任务输出目录异常')
        return {'config':config,'targets':targets,'dataset':str(dataset),'source_dataset':str(source)}

    def preflight(self,payload):
        paths=self.task_paths(payload)
        result=self.monitor.snapshot();checks=list(result['checks'])
        parents=[p if p.exists() else self.store.root for p in map(Path,[paths['dataset'],paths['source_dataset']])]
        writable=all(os.access(p,os.W_OK|os.X_OK) for p in parents)
        same_device=len({p.stat().st_dev for p in parents})==1
        free=shutil.disk_usage(self.store.root).free
        checks.append({'key':'storage','label':'存储','ok':writable and same_device and free>5*1024**3,'detail':f'剩余 {free/1024**3:.1f} GB'+('' if writable else '，目录不可写')+('' if same_device else '，归档目录需在同一文件系统')})
        active=self.external_active()
        checks.append({'key':'exclusive','label':'采集空闲','ok':not active,'detail':'已有数据正在采集' if active else '未发现正在写入的数据'})
        return {'checks':checks,'ready':all(c['ok'] for c in checks),**paths,'external_active':active}

    def start(self,payload):
        with self.operation:
            with self.lock:
                if self.phase not in ('idle','closed','rejected'):raise ValueError('请先结束当前会话')
            report=self.preflight(payload)
            if not self.selected_scene or report['dataset']!=self.selected_scene['dataset']:
                raise ValueError('请先应用当前日期和场景，再启动采集')
            if not report['ready']:raise ValueError('启动前检查未通过：'+'、'.join(c['label'] for c in report['checks'] if not c['ok']))
            ident=uuid.uuid4().hex
            if report['dataset']!=report['source_dataset']:
                destination=Path(report['dataset']);destination.mkdir(exist_ok=True)
                # A scene contains many prompts: task metadata belongs to episodes.
            baseline=self.store.new_session(ident,report['config'],report['targets'],report['dataset'],report['source_dataset'])
            session={'id':ident,'started':time.time(),'config':report['config'],'targets':report['targets'],'dataset':report['dataset'],'source_dataset':report['source_dataset'],'baseline':baseline}
            atomic_json(self.runtime/f'session_{ident}.json',session)
            with self.lock:
                self.session=session;self.phase='connecting';self.error=None;self.responses=[];self.events=[];self.accepted=False;self.transport=False
                try:
                    self.vendor=self.vendor_factory();self.stream=self.vendor.meta(report['config'])
                except Exception:
                    if self.vendor:self.vendor.close()
                    self.phase='rejected';self.error='采集接口启动失败';self.store.set_session(ident,'rejected')
                    raise
                stream=self.stream
            self.rpc_thread=threading.Thread(target=self._receive,args=(ident,stream),daemon=True);self.rpc_thread.start()
            return self.status()

    def _receive(self,ident,stream):
        try:
            for reply in stream:
                try:value=json.loads(reply.json_data)
                except ValueError:value={'message':reply.json_data}
                with self.lock:
                    if not self.session or self.session['id']!=ident:break
                    self.transport=True
                    self.responses.append({'at':time.time(),'value':value});self.responses=self.responses[-30:]
                with (self.runtime/f'meta_{ident}.jsonl').open('a') as f:f.write(json.dumps({'at':time.time(),'value':value},ensure_ascii=False)+'\n')
                # Vendor JSON is preserved; acceptance comes from verified task metadata,
                # not a generic response string which might itself describe an error.
        except Exception as exc:
            with self.lock:
                if self.session and self.session['id']==ident and self.phase not in ('closed','closing'):
                    self.phase='disconnected';self.error='采集接口连接中断'
                    self.events.append({'at':time.time(),'type':'connection_error','detail':str(exc)})
                    self.store.set_session(ident,'disconnected')
        finally:
            with self.lock:
                if self.session and self.session['id']==ident:
                    self.transport=False
                    if self.phase not in ('closed','closing','disconnected'):
                        self.phase='disconnected';self.error='采集接口连接已结束';self.store.set_session(ident,'disconnected')

    def _watch(self):
        while not self.shutdown.wait(.5):
            with self.lock:
                session=self.session.copy() if self.session else None
                phase=self.phase
            if not session or phase in ('idle','closed','rejected'):continue
            try:
                taskfile=Path(session.get('source_dataset') or session['dataset'])/'task_meta.json'
                if taskfile.exists() and taskfile.stat().st_mtime>=session['started']-1:
                    actual=json.loads(taskfile.read_text())
                    if (all(actual.get(k)==session['config'].get(k) for k in ['task_id','task_name','scene_id','subtask_num','frequency'])
                            and actual.get('record_depth',False)==session['config'].get('record_depth',False)):
                        with self.lock:
                            self.accepted=True
                            if self.phase=='connecting':
                                self.phase='waiting';self.error=None;self.store.set_session(session['id'],'waiting')
                events=self.store.observe(session,allow_new=phase not in ('disconnected','closing'))
                with self.lock:
                    self.events.extend({'at':time.time(),**e} for e in events);self.events=self.events[-30:]
                    if self.phase=='connecting' and time.time()-session['started']>15:
                        self.error='尚未收到任务落盘确认，请检查 Apifox 是否仍占用调用'
            except Exception as exc:
                with self.lock:self.error='目录监测：'+str(exc)

    def _cleanup_source(self,session):
        """Remove only an ended session's empty staging directory and metadata.

        Never recursively delete: unfinished episodes, unknown files and links
        must survive. Keep the exact vendor metadata in the session audit files.
        Called under operation after cancelling/joining the session RPC.
        """
        source=Path(session.get('source_dataset') or session['dataset'])
        destination=Path(session['dataset'])
        expected=self.store.root/f"{session['config']['task_id']}_{session['config']['task_name']}"
        if source==destination or source!=expected:return False
        for path in (source,destination):
            if path.is_symlink() or path.resolve()!=path or path.parent!=self.store.root:return False
        if not source.exists():return True
        if not source.is_dir() or not destination.is_dir():return False
        with self.store.lock:
            if any(row['state'] not in ('completed','deleted') for row in self.store.list(session['id'])):return False
            entries=list(source.iterdir())
            metadata=source/'task_meta.json'
            if entries:
                if entries!=[metadata] or metadata.is_symlink() or not metadata.is_file():return False
                actual=json.loads(metadata.read_text())
                if not isinstance(actual,dict) or any(actual.get(k)!=v for k,v in session['config'].items()):return False
                atomic_json(self.runtime/f"source_task_meta_{session['id']}.json",actual)
                metadata.unlink()
            source.rmdir()
        return True

    def end(self):
        with self.operation:
            if self.external_active():raise ValueError('请先在 Meta Quest 结束录制，等待数据保存后再结束会话')
            with self.lock:session=self.session.copy() if self.session else None
            if session and self.store.list(session['id'],active_only=True):
                raise ValueError('仍有数据正在采集或保存，请等待完成后再结束会话')
            with self.lock:
                self.phase='closing';stream=self.stream;vendor=self.vendor
            if stream:stream.cancel()
            if vendor:vendor.close()
            if self.rpc_thread:self.rpc_thread.join(3)
            with self.lock:
                self.phase='closed';self.transport=False;self.error=None
                if self.session:self.store.set_session(self.session['id'],'closed')
                session=dict(self.session) if self.session else None
            if session and not (self.rpc_thread and self.rpc_thread.is_alive()):
                try:
                    if self._cleanup_source(session):
                        with self.lock:self.events.append({'at':time.time(),'type':'source_cleanup','detail':'已清理采集临时目录'})
                except (OSError,ValueError) as exc:
                    # Cleanup failure must not turn a successfully ended session
                    # into an RPC error or endanger saved/unfinished data.
                    with self.lock:self.events.append({'at':time.time(),'type':'source_cleanup_skipped','detail':str(exc)})
            return self.status()

    def status(self,compact=False):
        with self.lock:
            session=dict(self.session) if self.session else None
            connected=self.transport or bool(self.accepted and self.stream and self.stream.is_active())
            result={'phase':self.phase,'accepted':self.accepted,'connected':connected,'error':self.error,'selected_scene':self.selected_scene,
                'session':session,'responses':list(self.responses),'events':list(self.events)}
        rows=self.store.list(session['id'],active_only=compact) if session else []
        active=[r for r in rows if r['state'] in ('recording','saving','finalizing')]
        result['current']=active[0] if active else None
        if result['current']:
            current=result['current'];progress=current['progress']
            for response in result['responses']:
                event=response['value']
                if not isinstance(event,dict) or event.get('uid')!=current['uuid']:continue
                stage=event.get('completed_subtask_index')
                if event.get('event_type')=='EPISODE_PROGRESS' and isinstance(stage,int):
                    progress['completed_steps']=max(progress.get('completed_steps',0),stage)
        if active and result['phase'] not in ('disconnected',):result['display_phase']=active[0]['state']
        else:result['display_phase']=result['phase']
        if not compact:result['episodes']=rows
        result['counts']=self.store.counts(session['id'] if session else None)
        return result

    def close(self):
        self.shutdown.set()
        if self.stream:self.stream.cancel()
        if self.vendor:self.vendor.close()
        if self.rpc_thread:self.rpc_thread.join(3)
        self.thread.join(3)
