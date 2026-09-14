"""LeRobot replay/review and reused cross-platform analysis, without VLM calls."""
import hashlib
import importlib
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
import pyarrow.parquet as pq
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from . import db
from .config import EXPORTS, VAR, REAL_SOURCE_ROOT, SIM_SOURCE_ROOT
from .io import NAMES, clean, read_json, write_json
from .export import jsonl, rows, stats

router = APIRouter(prefix="/api")
LEGACY = Path(__file__).resolve().parents[1] / 'legacy/scripts/embodied_data_pipeline-main'
DEFAULT_ROOTS = [EXPORTS, REAL_SOURCE_ROOT, SIM_SOURCE_ROOT, EXPORTS.parent / 'lerobot']


def legacy():
    if str(LEGACY) not in sys.path:
        sys.path.insert(0, str(LEGACY))
    return importlib.import_module('lerobot_cross_platform')


def init():
    with db.connect() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS library_datasets(id TEXT PRIMARY KEY,path TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS library_reviews(dataset_id TEXT NOT NULL,episode_index INTEGER NOT NULL,grade TEXT NOT NULL,excluded INTEGER NOT NULL,reason TEXT NOT NULL,actor TEXT NOT NULL,revision INTEGER NOT NULL,updated REAL NOT NULL,PRIMARY KEY(dataset_id,episode_index));''')


def inside(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise HTTPException(422, '数据集中的路径越界')
    return path


def describe(root):
    root = Path(root).expanduser().resolve()
    info = read_json(root / 'meta/info.json')
    if not str(info.get('codebase_version', '')).startswith('v2'):
        raise HTTPException(422, '请选择 LeRobot v2 数据集（含 meta/info.json）')
    ident = hashlib.sha256(str(root).encode()).hexdigest()[:16]
    init()
    with db.connect() as c:
        c.execute('INSERT OR IGNORE INTO library_datasets(id,path) VALUES(?,?)',(ident,str(root)))
    mapping = read_json(root / 'meta/episode_name_mapping.json')
    sample = next(iter(mapping.get('episodes', [])), {})
    source = sample.get('source_original') or sample.get('source_root') or ''
    name = Path(source).parent.name if source else root.name
    if source:
        kind = {'full': '完整数据', 'left': '左手', 'right': '右手'}.get(root.name, '复筛副本' if '/reviewed/' in str(root) else root.name)
        name += ' / ' + kind
        if sample.get('grade') in ('A', 'B', 'F'):
            name += ' / ' + sample['grade'] + ' 级'
    return dict(id=ident,root=str(root),name=name,episodes=info.get('total_episodes',0),frames=info.get('total_frames',0),
                robot=info.get('robot_type','unknown'), cameras=[k for k,v in info.get('features',{}).items() if v.get('dtype')=='video'])


def dataset(ident):
    init()
    with db.connect() as c:
        row=c.execute('SELECT path FROM library_datasets WHERE id=?',(ident,)).fetchone()
    if not row:raise HTTPException(404,'数据集不存在，请刷新目录')
    root=Path(row['path']);return root, read_json(root/'meta/info.json')


def scan(roots):
    found={};errors=[]
    for value in roots:
        root=Path(value).expanduser().resolve()
        if not root.is_dir():continue
        for current, dirs, files in os.walk(root,followlinks=False):
            dirs[:]=[d for d in dirs if not d.startswith('.') and not d.endswith('.partial') and d not in ['videos','observation','states','node_modules']]
            if (Path(current)/'meta/info.json').is_file():
                try:
                    d=describe(current);found[d['id']]=d
                except (ValueError,OSError,HTTPException) as e:errors.append(dict(root=current,reason=str(e)))
                dirs[:]=[]
            if len(found)>=500:break
    return dict(datasets=sorted(found.values(),key=lambda v:v['root']),errors=errors)


def data_path(root,info,index,key=None):
    pattern=info.get('video_path' if key else 'data_path')
    if not pattern:raise HTTPException(422,'缺少数据路径模板')
    return inside(root,pattern.format(episode_chunk=index//int(info.get('chunks_size',1000)),episode_index=index,video_key=key))


def episode_meta(root,index):
    ep=next((x for x in rows(root/'meta/episodes.jsonl') if x['episode_index']==index),None)
    if ep is None:raise HTTPException(404,'记录不存在')
    return ep


def review_rows(ident):
    init()
    with db.connect() as c:
        return {r['episode_index']:dict(r) for r in c.execute('SELECT * FROM library_reviews WHERE dataset_id=?',(ident,))}


def trajectory(s,a,t,task,transitions, fps=30, names=None):
    n=len(s)
    if s.ndim!=2 or a.ndim!=2 or len(a)!=n or len(t)!=n:
        raise HTTPException(422,'State / Action / 时间戳帧数不一致')
    names=names or (NAMES if s.shape[1]==23 else [f'joint_{i}'for i in range(s.shape[1])])
    return clean(dict(frames=list(range(n)),state=s,action=a,timestamps=t,
                      names=names,total_frames=n,task=task,transitions=transitions,fps=fps))


@router.get('/library/datasets')
def catalog():
    return scan(DEFAULT_ROOTS)


class Scan(BaseModel):
    root:str


@router.post('/library/discover')
def discover_library(body:Scan):
    if not Path(body.root).expanduser().is_dir():raise HTTPException(422,'目录不存在')
    return scan([body.root])


@router.get('/library/{ident}/episodes')
def list_episodes(ident:str):
    root,info=dataset(ident);reviews=review_rows(ident)
    return dict(dataset=describe(root),episodes=[dict(e,review=reviews.get(e['episode_index']),
                    grade=reviews.get(e['episode_index'],{}).get('grade',e.get('quality_grade','')),
                    excluded=bool(reviews.get(e['episode_index'],{}).get('excluded',False)))for e in rows(root/'meta/episodes.jsonl')])


@router.get('/library/{ident}/episodes/{index}')
def replay(ident:str,index:int):
    root,info=dataset(ident);ep=episode_meta(root,index);data=pq.read_table(data_path(root,info,index)).to_pydict()
    state_key,action_key=legacy().vector_feature_keys(info)
    s=np.asarray(data[state_key],dtype=float);a=np.asarray(data[action_key],dtype=float)
    tasks={r['task_index']:r['task'] for r in rows(root/'meta/tasks.jsonl')}
    tids=data.get('task_index',[0]);task=tasks.get(tids[0],'')
    mapping=read_json(root/'meta/episode_name_mapping.json').get('episodes',[])
    m=next((v for v in mapping if v.get('episode_index',v.get('lerobot_episode_index'))==index),{})
    b,e=m.get('source_range',[0,len(s)])
    stages=[dict(v,start=max(0,v['start']-b),end=min(len(s),v['end']-b)) for v in m.get('stages',[]) if v['end']>b and v['start']<e]
    names=info.get('features',{}).get(state_key,{}).get('names')
    if isinstance(names,dict):names=next(iter(names.values()),None)
    if not isinstance(names,list) or len(names)!=s.shape[1]:names=None
    return clean(dict(id=ident,index=index,root=str(root),episode=ep,review=review_rows(ident).get(index),
        trajectory=trajectory(s,a,np.asarray(data.get('timestamp',np.arange(len(s))/info.get('fps',30))),task,[v['end'] for v in stages],info.get('fps',30),names),
        stages=stages,mapping=m,cameras=describe(root)['cameras']))


@router.get('/library/{ident}/episodes/{index}/video/{key:path}')
def video(ident:str,index:int,key:str):
    root,info=dataset(ident);episode_meta(root,index)
    if info.get('features',{}).get(key,{}).get('dtype')!='video':raise HTTPException(404,'相机不存在')
    p=data_path(root,info,index,key)
    if not p.is_file():raise HTTPException(404,'视频不存在')
    return FileResponse(p,media_type='video/mp4')


class Annotation(BaseModel):
    model_config=ConfigDict(extra='forbid')
    episode_index:int=Field(ge=0)
    grade:Literal['A','B','F']
    excluded:bool=False
    reason:str=Field(min_length=1,max_length=2000)
    actor:str=Field(min_length=1,max_length=80)
    revision:int=Field(ge=0)


class Annotations(BaseModel):
    annotations:list[Annotation]=Field(min_length=1,max_length=10000)


@router.post('/library/{ident}/review')
def save_reviews(ident:str,body:Annotations):
    root,info=dataset(ident);valid={r['episode_index']for r in rows(root/'meta/episodes.jsonl')}
    indices=[a.episode_index for a in body.annotations]
    if len(set(indices))!=len(indices) or not set(indices)<=valid:raise HTTPException(422,'记录不存在或重复')
    now=time.time()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        for a in body.annotations:
            old=c.execute('SELECT revision FROM library_reviews WHERE dataset_id=? AND episode_index=?',(ident,a.episode_index)).fetchone()
            if (old['revision'] if old else 0)!=a.revision:raise HTTPException(409,'复筛记录已更新，请刷新后重试')
            c.execute('INSERT OR REPLACE INTO library_reviews VALUES(?,?,?,?,?,?,?,?)',
                      (ident,a.episode_index,a.grade,int(a.excluded or a.grade=='F'),a.reason,a.actor,a.revision+1,now))
    db.audit(None,None,'lerobot_review',body.annotations[0].actor,None,body.model_dump())
    return dict(reviews=list(review_rows(ident).values()))


def aggregate_stats(stat_rows):
    out={}
    for key in set(k for r in stat_rows for k in r['stats']):
        values=[r['stats'][key] for r in stat_rows if key in r['stats']]
        w=np.array([v['count'][0] for v in values]);means=np.array([v['mean']for v in values]);std=np.array([v['std']for v in values])
        mean=np.average(means,axis=0,weights=w)
        out[key]=dict(mean=mean,std=np.sqrt(np.average(std**2+(means-mean)**2,axis=0,weights=w)),min=np.min([v['min']for v in values],axis=0),max=np.max([v['max']for v in values],axis=0),count=[int(w.sum())])
    return out


@router.post('/library/{ident}/export')
def export_reviewed(ident:str):
    root,info=dataset(ident);reviews=review_rows(ident)
    if not reviews:raise HTTPException(422,'请先保存人工复筛结果')
    annotations=[dict(episode_index=k,quality_grade=v['grade'],exclude=bool(v['excluded']),reason=v['reason'])for k,v in reviews.items()]
    target=EXPORTS/'reviewed'/f'{root.name}-{uuid.uuid4().hex[:10]}'
    stage=target.with_name(target.name+'.partial')
    try:
        for ep in rows(root/'meta/episodes.jsonl'):
            i = ep['episode_index']
            for key in [None] + [k for k,v in info['features'].items() if v.get('dtype') == 'video']:
                if not data_path(root,info,i,key).is_file():
                    raise ValueError('源数据缺少 Parquet 或视频文件')
        result=legacy().rebuild_reviewed_dataset(root,stage,annotations)
        meta=read_json(stage/'meta/info.json');maps=read_json(stage/'meta/episode_name_mapping.json');epstats=rows(stage/'meta/episodes_stats.jsonl') if (stage/'meta/episodes_stats.jsonl').exists() else []
        stats_by={v['episode_index']:v['stats']for v in epstats};rebuilt=[];count=0
        for ep in rows(stage/'meta/episodes.jsonl'):
            i=ep['episode_index'];data=pq.read_table(data_path(stage,meta,i)).to_pydict();n=len(data['episode_index']);count+=n
            v=dict(stats_by.get(i,{}))
            for k,values in data.items():
                try:
                    arr=np.asarray(values,dtype=float)
                    if arr.ndim<=2 and np.isfinite(arr).all():v[k]=stats(arr)
                except (ValueError,TypeError):pass
            rebuilt.append(dict(episode_index=i,stats=v))
            for key in [k for k,v in meta['features'].items()if v.get('dtype')=='video']:
                path=data_path(stage,meta,i,key)
                if not path.is_file():raise ValueError('复筛输出缺少视频：'+key)
            m=maps['episodes'][i];m['episode_index']=i;m['grade']=ep.get('quality_grade',m.get('grade',''))
        if count!=meta['total_frames']:raise ValueError('复筛输出帧数不一致')
        jsonl(stage/'meta/episodes_stats.jsonl',rebuilt);write_json(stage/'meta/stats.json',aggregate_stats(rebuilt))
        maps['reviewed_lerobot_dataset']=str(target);write_json(stage/'meta/episode_name_mapping.json',maps)
        record=read_json(stage/'meta/manual_review.json');record['output_dataset']=str(target);write_json(stage/'meta/manual_review.json',record)
        # Old quality reports describe the source, not the reviewed copy.
        old=stage/'qc_report.json'
        if old.exists():old.rename(stage/'source_qc_report.json')
        write_json(stage/'review_validation.json',dict(passed=True,frames=count,episodes=len(rebuilt),source=str(root),excluded=[k for k,v in reviews.items()if v['excluded']]))
        os.replace(stage,target)
    except Exception as e:
        if stage.exists():shutil.rmtree(stage)
        if isinstance(e, HTTPException):raise
        if isinstance(e, (ValueError,OSError)):raise HTTPException(422,str(e))
        raise
    result['output']=str(target);result['dataset']=describe(target)
    db.audit(None,None,'lerobot_review_export','operator',None,result)
    return result


class Selection(BaseModel):
    path:str
    platform:Literal['simulation','real']


class Compare(BaseModel):
    datasets:list[Selection]=Field(min_length=1,max_length=100)
    robot_type:Literal['zerith','aloha']='zerith'
    max_episodes:int=Field(default=20,ge=1,le=200)
    max_frames:int=Field(default=1000,ge=50,le=5000)
    stationary_threshold:Literal[20,40,60]=40


@router.post('/compare/analyze')
def compare(body:Compare):
    for item in body.datasets:describe(item.path)
    try:
        result=legacy().analyze_datasets([v.model_dump()for v in body.datasets],body.max_episodes,body.max_frames,body.robot_type,body.stationary_threshold)
    except (ValueError,OSError)as e:raise HTTPException(422,str(e))
    ident=uuid.uuid4().hex[:16];write_json(VAR/'comparisons'/f'{ident}.json',result)
    return clean(dict(result,id=ident))


@router.get('/compare/reports/{ident}')
def comparison_report(ident:str):
    if len(ident)!=16 or any(c not in '0123456789abcdef'for c in ident):raise HTTPException(404)
    p=VAR/'comparisons'/f'{ident}.json'
    if not p.is_file():raise HTTPException(404)
    return FileResponse(p,media_type='application/json',filename=f'comparison-{ident}.json')
