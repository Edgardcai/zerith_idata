"""Local dashboard endpoints; no collection files are written by these routes."""
import csv
import io
import os
import base64
import copy
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response, RedirectResponse

from . import config
from .collection_dashboard import Inventory, aggregate, filtered, summary
from .collection_plans import PLAN_FILES, comparison
from .dashboard_sync import RemoteInventory, SCHEMA

router = APIRouter()
WEB = Path(__file__).resolve().parents[1]/'web'


@lru_cache(maxsize=1)
def inventory():
    remote=os.environ.get('DATAQC_DASHBOARD_SOURCE_URL','').strip()
    if remote:return RemoteInventory(remote, config.VAR/'collection-dashboard/remote.json')
    return Inventory(Path(os.environ.get('DATAQC_DASHBOARD_ROOT','/data/zerith_data')),
                     config.VAR/'collection-dashboard/index.json')


def filters(start='',end='',product='',scene='',grade='',issues=False):
    return dict(start=start,end=end,product=product,scene=scene,grade=grade,issues=issues)


@router.get('/collection-dashboard')
@router.get('/collection-dashboard/')
def page():
    return FileResponse(WEB/'collection-dashboard.html',headers={'Cache-Control':'no-cache'})


@router.get('/collection-dashboard/assets/{name}')
def asset(name: str):
    if name not in ('collection-dashboard.css','collection-dashboard.js'):raise HTTPException(404)
    return FileResponse(WEB/name,headers={'Cache-Control':'no-cache'})


@router.get('/api/collection-dashboard/summary')
def get_summary(start: str='',end: str='',product: str='',scene: str='',grade: str='',issues: bool=False):
    index=inventory();index.refresh()
    try:return summary(index,**filters(start,end,product,scene,grade,issues))
    except ValueError as exc:raise HTTPException(400,str(exc))


@router.post('/api/collection-dashboard/refresh')
def refresh():
    index=inventory();started=index.refresh(force=True)
    return dict(started=started,state=index.state())


@router.get('/api/collection-dashboard/snapshot')
def snapshot():
    index=inventory()
    if isinstance(index,RemoteInventory):raise HTTPException(409,'此节点为缓存看板，请使用原始采集数据源')
    index.refresh(background=False)
    with index.lock:
        state=index.state()
        if not state['ready'] or state['error']:raise HTTPException(503,state['error'] or '等待首次成功统计')
        rows=copy.deepcopy(index.rows())
    documents={}
    for batch_id,filename in PLAN_FILES:
        path=plan_root()/filename
        if path.is_file():documents[batch_id]=base64.b64encode(path.read_bytes()).decode('ascii')
    return dict(schema=SCHEMA,rows=rows,state=state,documents=documents,
                plans={basis:comparison(plan_root(),rows,basis) for basis in ('all','ab')})


def selected(start,end,product,scene,grade,issues):
    try:return filtered(inventory().rows(),**filters(start,end,product,scene,grade,issues))
    except ValueError as exc:raise HTTPException(400,str(exc))


def plan_root():
    return Path(os.environ.get('DATAQC_PLAN_ROOT', '/data/plan'))


@router.get('/api/collection-dashboard/plans')
def plans(basis: str='all'):
    index=inventory();index.refresh()
    try:
        result=index.plans(basis) if isinstance(index,RemoteInventory) else comparison(plan_root(), index.rows(), basis)
        result['state']=index.state()
        return result
    except ValueError as exc:raise HTTPException(400,str(exc))


@router.get('/api/collection-dashboard/plan-document/{batch_id}')
def plan_document(batch_id: str):
    filename=dict(PLAN_FILES).get(batch_id)
    index=inventory()
    if isinstance(index,RemoteInventory):
        document=index.document(batch_id)
        if not filename or not document:raise HTTPException(404)
        from urllib.parse import quote
        return Response(document,media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                        headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(filename)})
    if not filename or not (plan_root()/filename).is_file():raise HTTPException(404)
    return FileResponse(plan_root()/filename, filename=filename)


@router.get('/api/collection-dashboard/records')
def records(start: str='',end: str='',product: str='',scene: str='',grade: str='',issues: bool=False,
            offset: int=Query(0,ge=0),limit: int=Query(50,ge=1,le=200),basis: str='all'):
    rows=grade_basis(selected(start,end,product,scene,grade,issues),basis)
    rows=sorted(rows,key=lambda r:(r['date'],r['path']),reverse=True)
    return dict(total=len(rows),offset=offset,limit=limit,rows=rows[offset:offset+limit])


def grade_basis(rows,basis):
    if basis not in ('all','ab'):raise HTTPException(400,'未知采集口径')
    return [r for r in rows if r['grade'] in ('A','B')] if basis=='ab' else rows


def csv_text(headers, rows):
    output=io.StringIO();writer=csv.writer(output);writer.writerow(headers)
    for row in rows:
        # Neutralize spreadsheet formulas in product names, prompts and paths.
        writer.writerow(["'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v for v in row])
    return '\ufeff'+output.getvalue()


@router.get('/api/collection-dashboard/export.csv')
def export(kind: str='products',start: str='',end: str='',product: str='',scene: str='',grade: str='',issues: bool=False,search: str='',batch: str='',basis: str='all'):
    if kind=='plans':
        result=plans(basis)
        if batch and batch not in dict(PLAN_FILES):raise HTTPException(400,'未知采集批次')
        items=[p for p in result['plans'] if p['status']=='ready' and (not batch or p['id']==batch)]
        headers=['批次','商品','左手计划','左手实际','左手缺口','右手计划','右手实际','右手缺口','配额达成率','涉及原始记录','数量口径']
        values=[[p['name'],r['name'],r['left_target'],r['left_actual'],r['left_gap'],r['right_target'],r['right_actual'],r['right_gap'],
                 f"{r['completion']:.2%}",r['actual_records'],'全部采集' if basis=='all' else '仅 A/B 等级'] for p in items for r in p['products']]
        return Response(csv_text(headers,values),media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition':'attachment; filename="collection-plans.csv"'})
    rows=selected(start,end,product,scene,grade,issues)
    if kind=='records':
        rows=grade_basis(rows,basis)
        headers=['采集日期','日期依据','原始记录路径','左手目标','右手目标','采用等级','等级来源','采集等级','自动/VLM等级','人工等级','人工备注','场景','原始标识','重复副本数','待核对说明']
        sources=dict(manual='人工',qc='自动/VLM',collection='采集',unrated='未分级')
        values=[[r['date'],r['date_source'],r['path'],r['left'],r['right'],r['grade'],sources[r['grade_source']],
                 r['collection_grade'],r['qc_grade'],r['manual_quality_grade'],r['manual_note'],r['scene'],r['identity'],
                 r['duplicate_count'],'；'.join(r['issues'])] for r in rows]
    else:
        products,daily=aggregate(rows)
        if kind=='products':
            products=[r for r in products if search.strip().casefold() in r['name'].casefold()]
            headers=['商品','涉及原始记录数','左手目标记录数','右手目标记录数','A','B','C','F','未分级','首次采集','最近采集']
            values=[[r['name'],r['total'],r['left'],r['right'],*[r['grades'][g] for g in ('A','B','C','F','UNRATED')],r['first'],r['last']] for r in products]
        elif kind=='daily':
            headers=['采集日期','原始记录数','左手目标记录数','右手目标记录数','商品数',*[v for g in ('A','B','C','F','未分级') for v in (g+'数量',g+'占比')]]
            values=[[r['date'],r['total'],r['left'],r['right'],r['products'],*[v for g in ('A','B','C','F','UNRATED') for v in (r['grades'][g], f"{r['grades'][g]/r['total']:.4%}")]] for r in daily]
        else:raise HTTPException(400,'未知导出类型')
    return Response(csv_text(headers,values),media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition':f'attachment; filename="collection-{kind}.csv"'})


@router.get('/api/collection-dashboard/video/{record_id}/{camera}')
def video(record_id: str,camera: str):
    if camera not in ('cam_high','cam_left_wrist','cam_right_wrist'):raise HTTPException(404)
    index=inventory();row=next((r for r in index.rows() if r['id']==record_id),None)
    if row is None:raise HTTPException(404,'记录不存在或索引已更新')
    if isinstance(index,RemoteInventory):
        return RedirectResponse(index.remote_url+f'/api/collection-dashboard/video/{record_id}/{camera}')
    path=(index.root/row['path']/'videos/rs'/f'{camera}.mp4').resolve()
    if not path.is_relative_to(index.root) or not path.is_file():raise HTTPException(404,'该视角视频不存在')
    return FileResponse(path,media_type='video/mp4')
