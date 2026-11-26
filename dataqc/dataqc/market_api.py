"""Independent market QC UI/API, with isolated reports and explicit manual edits."""
from contextlib import contextmanager
from functools import lru_cache
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode, urlparse, parse_qs
from urllib.request import build_opener, ProxyHandler, Request
import uuid

from fastapi import FastAPI, HTTPException, Request as WebRequest
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from . import config, market
from .io import write_json

BASE = Path(os.environ.get('DATAQC_MARKET_ROOT', '/home/robot/workspace/robot_station/data')).resolve()
EXTRA_ROOTS = tuple(Path(p).expanduser().resolve() for p in
                    os.environ.get('DATAQC_MARKET_EXTRA_ROOTS', '').split(os.pathsep) if p)
HOME = config.VAR / 'market-qc'
WEB = Path(__file__).resolve().parents[1] / 'web'
VIEWER = os.environ.get('DATAQC_MARKET_VIEWER', 'http://127.0.0.1:2889')
app = FastAPI()


def recording_roots():
    return tuple(dict.fromkeys((BASE, *EXTRA_ROOTS)))


def browse_root():
    return Path(os.path.commonpath(recording_roots()))


def discover_recordings(root):
    # The common ancestor is only a browsing scope, never a recursive scan root.
    roots = recording_roots() if root == browse_root() else (root,)
    return sorted({p for folder in roots for p in market.discover(folder)})


def resolve(root, ident=None):
    p = Path(root).expanduser()
    if not p.is_absolute(): raise ValueError('请选择绝对目录')
    if ident is not None:
        rel = Path(ident)
        if rel.is_absolute() or '..' in rel.parts: raise ValueError('片段路径无效')
        p = p / rel
    # Reject symbolic links, even those that resolve within the root.
    if any(v.is_symlink() for v in [p, *p.parents] if v != BASE.parent): raise ValueError('不支持符号链接目录')
    p = p.resolve()
    allowed = any(p.is_relative_to(folder) for folder in recording_roots())
    if ident is None and p == browse_root():
        allowed = True
    if (not allowed or any(p.is_relative_to(folder / 'lerobot') for folder in recording_roots())
            or not p.is_dir()):
        raise ValueError('请选择数据根目录内的商超录制目录')
    if ident is not None and (not (p / 'episode.hdf5').is_file() or (p / 'episode.hdf5').is_symlink()):
        raise ValueError('没有可读取的 episode.hdf5')
    return p


def key(path):
    return hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()


@lru_cache(maxsize=8)
def legacy_records(folder, changed, kind):
    """Index old path-keyed records so moving a recording preserves its reviews."""
    records={}
    for candidate in sorted(Path(folder).glob('*.json'),key=lambda p:p.stat().st_mtime_ns):
        if not re.fullmatch(r'[0-9a-f]{64}',candidate.stem):continue
        value=market.read(candidate)
        ident=value.get('uuid' if kind=='manual' else 'signature')
        if isinstance(ident,str):records[ident]=candidate
    return records


def stored_path(path,kind):
    path=Path(path);folder=HOME/kind
    ident=market.read(path/'episode_meta.json').get('episode_id')
    if not isinstance(ident,str) or not re.fullmatch(r'[0-9a-fA-F]{32}',ident):
        return folder/(key(path)+'.json')
    expected=ident if kind=='manual' else market.signature(path)
    canonical=folder/('episode_'+ident+(('_'+expected) if kind=='reports' else '')+'.json')
    if canonical.exists():return canonical
    # Copies can share a UUID while having distinct file signatures. Keep their
    # QC evidence independent, and still find unchanged reports after a move.
    uuid_legacy=folder/('episode_'+ident+'.json')
    if market.read(uuid_legacy).get('uuid' if kind=='manual' else 'signature')==expected:
        return uuid_legacy
    legacy=folder/(key(path)+'.json')
    value=market.read(legacy)
    if value.get('uuid' if kind=='manual' else 'signature')==expected:return legacy
    if folder.is_dir():
        previous=legacy_records(str(folder),folder.stat().st_mtime_ns,kind).get(expected)
        if previous:return previous
    return canonical


def report_path(path):return stored_path(path,'reports')
def manual_path(path):return stored_path(path,'manual')


def directory_identity(path):
    stat=Path(path).stat()
    return dict(device=stat.st_dev,inode=stat.st_ino)


def revision(path):
    return hashlib.sha256(json.dumps([market.read(Path(path)/'review.json'), market.read(manual_path(path)),
                                     market.read(Path(path)/'episode_meta.json').get('episode_id')], sort_keys=True).encode()).hexdigest()


def snapshot(path, root, detail=False):
    meta = market.read(path/'episode_meta.json'); review = market.read(path/'review.json')
    report = market.read(report_path(path)); manual = market.read(manual_path(path))
    current_signature = market.signature(path)
    fresh = report.get('signature') == current_signature and report.get('version') == market.VERSION
    if not fresh: report = {}
    if manual.get('uuid') != meta.get('episode_id'): manual = {}
    phase_rows = report.get('phases') or market.stages(meta)[0]
    phases = []
    # Stage reviews survive QC reruns, but not a changed frame layout or source data.
    phase_manual = manual.get('phases', {}) if manual.get('signature') == current_signature else {}
    for row in phase_rows:
        m = phase_manual.get(str(row['index']), {}) if row.get('index') is not None else {}
        phases.append(dict(row, manual_grade=m.get('grade'), note=m.get('note', ''),
                           effective_grade=m.get('grade') or row.get('auto_grade')))
    review_matches = not review.get('episode_uuid') or review.get('episode_uuid') == meta.get('episode_id')
    existing = review.get('grade') if review_matches and review.get('reviewed') is True and review.get('grade') in market.GRADES else None
    local = manual.get('overall') or {}
    # Latest explicit human edit wins, regardless of which review website wrote it.
    manual_grade = existing
    if local.get('grade') in market.GRADES and (not existing or local.get('updated', 0) > (review.get('updated_at') or 0)):
        manual_grade = local['grade']
    stage_grades = [p.get('manual_grade') for p in phases]
    from_stages = (('F' if 'F' in stage_grades else 'B' if 'B' in stage_grades else 'A')
                   if phases and all(p.get('valid') for p in phases) and all(g in market.GRADES for g in stage_grades) else None)
    if report.get('hard_fail'): auto = 'F'
    else: auto = from_stages or report.get('auto_grade')
    recognition = review.get('product_recognition') or {}
    inference = meta.get('inference') or {}
    value = dict(id=str(path.relative_to(root)), uuid=meta.get('episode_id'), name=path.name,
                 path=str(path), source_group=str(path.parent.relative_to(root)),
                 remote=inference.get('remoteUrl'), order_id=inference.get('orderId'),
                 phases=phases if detail else [dict(phase=p['phase'], label=p['label'], index=p['index'],
                     effective_grade=p.get('effective_grade'), manual_grade=p.get('manual_grade')) for p in phases],
                 phase_sequence=[p['phase'] for p in phases if not p.get('missing')],
                 manual_grade=manual_grade, source_grade=review.get('grade'), yolo_grade=recognition.get('grade') if isinstance(recognition,dict) else None,
                 auto_grade=report.get('auto_grade'), effective_grade=manual_grade or auto,
                 qc_status='completed' if fresh else 'pending', hard_fail=report.get('hard_fail', False),
                 revision=revision(path), complete=meta.get('complete'),
                 source_updated=path.stat().st_mtime,
                 manual_pending=not manual_grade, source_review_mismatch=not review_matches, phase_manual_pending=not (phases and all(p.get('valid') for p in phases) and all(stage_grades)),
                 report=report if detail else None)
    from .market_presentation import metrics, presentation
    value['frames'] = report.get('frames') or meta.get('frame_count') or max((p.get('end') or 0 for p in phase_rows), default=0)
    value['manual_note'] = local.get('note', '')
    value['metrics'] = metrics(report)
    problems = sum(c.get('status') in ('warn','fail') for c in report.get('checks',[]))
    value['summary'] = f'全流程检查完成 · {problems} 项全局问题' if fresh else '尚无全流程质检报告'
    if detail: value['presentation'] = presentation(value)
    return value


@contextmanager
def locked(name):
    HOME.mkdir(parents=True, exist_ok=True)
    with (HOME / (name+'.lock')).open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def jobs():
    values = []
    for p in sorted((HOME/'jobs').glob('*.json'), reverse=True):
        j = market.read(p)
        if j.get('status') in ('queued', 'running'):
            try: command = Path(f"/proc/{j.get('pid')}/cmdline").read_bytes()
            except OSError: command = b''
            if j.get('id','').encode() not in command or b'dataqc.market_job' not in command:
                j.update(status='interrupted', message='服务中断，可重新运行并复用已完成报告')
        values.append(j)
    return sorted(values, key=lambda j:j.get('created',0), reverse=True)[:30]


@app.middleware('http')
async def same_origin(request, call_next):
    if request.method == 'POST':
        origin=request.headers.get('origin')
        if origin and urlparse(origin).netloc != request.headers.get('host'):
            from fastapi.responses import JSONResponse
            return JSONResponse({'detail':'请求来源无效'}, status_code=403)
    return await call_next(request)


@app.exception_handler(ValueError)
async def value_error(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse({'detail':str(exc)}, status_code=400)


@app.get('/')
def page():
    return HTMLResponse(app.state.workbench_html)

@app.get('/app.js')
def script(): return FileResponse(WEB/'market.js', media_type='application/javascript')

@app.get('/style.css')
def style(): return FileResponse(WEB/'market.css', media_type='text/css')

@app.get('/api/config')
def configuration():
    return dict(root=str(BASE), browse_root=str(browse_root()), output=str(BASE/'lerobot'), version=market.VERSION, export_layout=market.EXPORT_LAYOUT,
                browse_scope='all_directories_v1',job_scope='selected_directory_v1',refresh_interval_seconds=5,
                sources=source_directories())


def source_directories(paths=None):
    """Find recording batch roots from recognized episodes, regardless of their name."""
    paths = discover_recordings(browse_root()) if paths is None else paths
    sources = set()
    for path in paths:
        owner = max((folder for folder in recording_roots() if path.is_relative_to(folder)),
                    key=lambda folder: len(folder.parts))
        relative = path.relative_to(owner)
        sources.add(str(owner / relative.parts[0]) if owner == BASE and relative.parts else str(owner))
    # Keep the existing robot45 empty-directory choices.
    sources.update(str(p) for p in BASE.glob('market*')
                   if p.is_dir() and not p.is_symlink() and not p.name.endswith('.partial'))
    sources.update(str(p) for p in EXTRA_ROOTS if p.is_dir())
    return sorted(sources)


@app.get('/api/records')
def records(root: str | None = None):
    root = resolve(str(browse_root()) if root is None else root)
    paths = discover_recordings(root)
    rows = []
    for p in paths:
        try: rows.append(snapshot(p,root))
        except (OSError,ValueError,TypeError) as exc:
            rows.append(dict(id=str(p.relative_to(root)),name=p.name,path=str(p),error=str(exc),qc_status='error',phases=[]))
    return dict(root=str(root), records=rows, sources=source_directories(paths))

@app.get('/api/episode')
def episode(root: str, id: str):
    return snapshot(resolve(root,id),resolve(root),True)

@app.get('/api/jobs')
def list_jobs(): return dict(jobs=jobs())


class Job(BaseModel):
    model_config=ConfigDict(extra='forbid')
    root: str
    kind: Literal['qc','export','qc_export']='qc'
    force: bool=False
    grades: list[Literal['A','B','F']]=Field(default_factory=lambda:['A','B'], min_length=1)
    manual_only: bool=False


@app.post('/api/jobs')
def start(body: Job):
    root = resolve(body.root)
    if root in (BASE, browse_root()):raise ValueError('请先选择要质检或转换的具体录制目录')
    with locked('jobs'):
        if any(j['status'] in ('running','queued') for j in jobs()): raise HTTPException(409,'已有商超任务运行，请等待完成或停止')
        ident = time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]
        folder=HOME/'jobs';folder.mkdir(parents=True,exist_ok=True)
        job=body.model_dump() | dict(root=str(root),source_identity=directory_identity(root),
            id=ident,status='queued',created=time.time(),done=0,total=0,message='等待执行：'+str(root))
        write_json(folder/(ident+'.json'),job)
        with (folder/(ident+'.log')).open('ab') as log:
            p=subprocess.Popen([sys.executable,'-m','dataqc.market_job',ident], stdout=log,stderr=log,
                               cwd=str(WEB.parent),start_new_session=True)
        job['pid']=p.pid;write_json(folder/(ident+'.json'),job)
    return job

@app.post('/api/jobs/{ident}/cancel')
def cancel(ident: str):
    if not __import__('re').fullmatch(r'\d{8}_\d{6}_[0-9a-f]{8}',ident): raise ValueError('任务编号无效')
    (HOME/'jobs'/(ident+'.cancel')).touch()
    return dict(ok=True)


class Review(BaseModel):
    model_config=ConfigDict(extra='forbid')
    root: str
    id: str
    uuid: str
    revision: str
    phase_index: int | None=None
    grade: Literal['A','B','F']
    note: str=Field(default='',max_length=1000)

@app.post('/api/review')
def review(body: Review):
    path=resolve(body.root,body.id)
    with locked('manual'):
        meta=market.read(path/'episode_meta.json')
        if meta.get('episode_id') != body.uuid: raise HTTPException(409,'片段标识变化，请刷新')
        if revision(path) != body.revision: raise HTTPException(409,'人工评级已变化，请刷新后再保存')
        if path.name.endswith('.partial') or meta.get('outcome') in (None,'','recording'):
            raise ValueError('录制尚未结束，不能评级')
        existing=market.read(manual_path(path))
        if existing.get('uuid') != body.uuid: existing={}
        sig=market.signature(path)
        if existing.get('signature') != sig: existing['phases']={}
        existing.update(uuid=body.uuid,signature=sig)
        value=dict(grade=body.grade,note=body.note,updated=time.time())
        if body.phase_index is None:
            # Share the established 2889 review.json when its root is configured.
            opener=build_opener(ProxyHandler({}))
            try:
                with opener.open(VIEWER+'/api/config',timeout=5) as r: cfg=json.load(r)
                owners=[Path(s['directory']).resolve() for s in cfg['sources'] if path.is_relative_to(Path(s['directory']).resolve())]
                if owners:
                    owner=max(owners,key=lambda p:len(p.parts))
                    payload=dict(directory=str(owner),id=str(path.relative_to(owner)),uuid=body.uuid,action='grade',grade=body.grade)
                    req=Request(VIEWER+'/api/correct',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
                    with opener.open(req,timeout=100) as r: result=json.load(r)
                    if not result.get('ok'): raise ValueError(result.get('error','2889 保存失败'))
            except Exception as exc:
                raise ValueError('整体评级未保存：'+str(exc)) from exc
            existing['overall']=value
        else:
            rows,_=market.stages(meta)
            if not any(p['index']==body.phase_index and p['valid'] for p in rows): raise ValueError('阶段不存在或帧边界无效')
            existing.setdefault('phases',{})[str(body.phase_index)]=value
        write_json(manual_path(path),existing)
    return snapshot(path,resolve(body.root),True)

@app.get('/api/report')
def report(root: str):
    root=resolve(root)
    return dict(root=str(root),version=market.VERSION,records=[snapshot(p,root,True) for p in discover_recordings(root)])

@app.get('/api/media')
def media(root: str,id: str):
    path=resolve(root,id);root=resolve(root)
    opener=build_opener(ProxyHandler({}))
    with opener.open(VIEWER+'/api/episode?'+urlencode(dict(directory=str(root),id=str(path.relative_to(root)))),timeout=20) as r:
        data=json.load(r)
    videos=data.get('videos',[])
    for v in videos:
        if v.get('url'):
            media_key=parse_qs(urlparse(v['url']).query).get('key',[''])[0]
            v['url']='/market/api/video?'+urlencode(dict(key=media_key))
    return dict(videos=videos)

@app.get('/api/video')
def video(key: str, request: WebRequest):
    from starlette.responses import StreamingResponse
    from urllib.error import HTTPError
    if not __import__('re').fullmatch(r'[0-9a-f]{64}',key): raise ValueError('视频标识无效')
    headers={}
    if request.headers.get('range'):headers['Range']=request.headers['range']
    try:
        response=build_opener(ProxyHandler({})).open(Request(VIEWER+'/media?'+urlencode(dict(key=key)),headers=headers),timeout=20)
    except HTTPError as exc:raise HTTPException(exc.code,'视频尚未准备好') from exc
    def stream():
        try:
            while chunk:=response.read(256*1024):yield chunk
        finally:response.close()
    forwarded={k:response.headers[k] for k in ('Content-Length','Content-Range','Accept-Ranges') if response.headers.get(k)}
    return StreamingResponse(stream(),status_code=response.status,media_type='video/mp4',headers=forwarded)


@app.get('/workbench.js')
def workbench_script(): return FileResponse(WEB/'market-workbench.js', media_type='application/javascript')

@app.get('/replay-bridge.js')
def replay_bridge(): return FileResponse(WEB/'market-replay.js', media_type='application/javascript')

@app.get('/replay/')
def replay_page():
    from integrations.market_ui import replay_page
    return HTMLResponse(replay_page())

@app.get('/api/mapping')
def mapping(root: str):
    root=resolve(root)
    for job in jobs():
        output=(job.get('result') or {}).get('path')
        if job.get('root')!=str(root) or not output: continue
        if job.get('source_identity') and job['source_identity']!=directory_identity(root):continue
        path=Path(output).resolve()
        if not path.is_relative_to(BASE/'lerobot') or path.name.endswith('.partial') or not path.is_dir(): continue
        datasets=[];episodes=[]
        if (path/'meta/info.json').is_file():
            candidates=[(None,path)]
            layout='mixed_legacy'
        else:
            candidates=[(grade,path/grade) for grade in market.GRADES if (path/grade/'meta/info.json').is_file()]
            layout=market.EXPORT_LAYOUT
        for grade,dataset in candidates:
            if dataset.is_symlink(): continue
            entries=market.read(dataset/'meta/episode_name_mapping.json').get('episodes',[])
            datasets.append(dict(grade=grade,path=str(dataset),episodes=len(entries)))
            episodes.extend(dict(entry,dataset_path=str(dataset)) for entry in entries)
        if datasets:return dict(path=str(path),layout=layout,datasets=datasets,episodes=episodes)
    return dict(path='',datasets=[],episodes=[])

@app.get('/api/replay-episodes')
def replay_episodes(root: str):
    rows=records(root)['records']
    return dict(episodes=[dict(index=i,id=r['id'],name=r['id'],episode_dir=r['path'],
        frame_count=r.get('frames',0),quality_grade=r.get('effective_grade') or '',qc_status=r.get('qc_status')) for i,r in enumerate(rows)])

@app.get('/api/replay-data')
def replay_data(root: str,id: str,index: int=0):
    from integrations.market_ui import replay_module
    from .io import load, clean
    path=resolve(root,id); module=replay_module(); row=snapshot(path,resolve(root),True)
    data=module.load_episode(path,0,'zerith',quality_grade=row['effective_grade'] or '')
    payload=module.episode_payload(data,index)
    # Use the same mask-aware arrays as QC/export; unknown commands remain gaps.
    raw=load(path)
    payload.update(state_joint_position=raw['state'].tolist(),action_joint_position=raw['action'].tolist(),
        videos=[dict(key=key,label=label,file=cam+'.mp4',url='') for key,label,cam in [('head_color','头部','cam_high'),('hand_left_color','左腕','cam_left_wrist'),('hand_right_color','右腕','cam_right_wrist')]],quality_grade=row['effective_grade'] or '',frame_trim_supported=False,
        stage_info=dict(available=True,editable=False,source='market',errors=[],
            stages=[dict(stage_number=p['index']+1,label=p['label'],start=p['start'],end=p['end']-1)
                    for p in row['phases'] if p.get('valid')],frame_count=raw['n']),
        market=row)
    return clean(payload)

@app.get('/api/replay-grades')
def replay_grades(root: str):
    from .market_presentation import grade_snapshot
    return dict(episodes=[grade_snapshot(r) for r in records(root)['records'] if not r.get('error')])

@app.get('/api/replay-grade')
def replay_grade(root: str,path: str):
    from .market_presentation import grade_snapshot
    scope=resolve(root);target=resolve(path)
    if not target.is_relative_to(scope): raise ValueError('记录不属于当前目录')
    return grade_snapshot(snapshot(resolve(root,str(target.relative_to(scope))),scope,True))

class ReplayReview(BaseModel):
    model_config=ConfigDict(extra='forbid')
    root: str
    path: str
    grade: Literal['A','B','F']
    revision: str
    note: str=Field(default='',max_length=1000)
    problem: str=Field(default='',max_length=100)

@app.post('/api/replay-grade')
def replay_review(body: ReplayReview):
    from .market_presentation import grade_snapshot
    scope=resolve(body.root);target=resolve(body.path)
    if not target.is_relative_to(scope): raise ValueError('记录不属于当前目录')
    ident=str(target.relative_to(scope));row=snapshot(resolve(body.root,ident),scope)
    note='；'.join(v for v in [body.problem,body.note] if v)
    return grade_snapshot(review(Review(root=body.root,id=ident,uuid=row['uuid'],revision=body.revision,grade=body.grade,note=note)))
