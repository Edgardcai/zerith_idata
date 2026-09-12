"""Grade precedence: explicit human review, automated QC/VLM, then collection."""
import copy
import hashlib
import json
from pathlib import Path
import threading
from urllib.parse import urlparse, parse_qs

from dataqc import config
from dataqc.io import hdf5_path, read_json, write_json

GRADES=('A','B','C','F')
GRADE_PRIORITY=('manual','qc','collection')

def capture(root):
    """Collector review.json is distinct from QC's mutable episode metadata."""
    review=read_json(root/'review.json')
    meta=read_json(root/'meta/episode_meta.json') or read_json(root/'episode_meta.json')
    grade=review.get('grade') or meta.get('collection_grade') or meta.get('collection_quality_grade')
    source='review.json' if review.get('grade') else 'episode_meta.json'
    if not grade and not any(k in meta for k in ('manual_quality_grade','manual_failure')):
        grade=meta.get('quality_grade')
    grade=str(grade or '').upper()
    identity=str(review.get('episode_uuid') or meta.get('source_episode_id') or meta.get('episode_uuid') or root.name)
    return identity,dict(grade=grade if grade in GRADES else '',source=source if grade in GRADES else '')


def integrate_grades(app):
    scopes={};lock=threading.RLock()
    old_status=app.dataset_status
    def store_path(cfg):
        key=hashlib.sha256(str(Path(cfg['hdf5_root']).resolve()).encode()).hexdigest()
        return config.VAR/'grade-selections'/(key+'.json')
    def busy(cfg):
        with app.JOBS_LOCK:
            return any(j.get('status') not in ('completed','failed','stopped') and
                       f"hdf5:{cfg['hdf5_root']}" in j.get('resource_keys',[]) for j in app.JOBS.values())
    def enrich(status,cfg):
        status=copy.deepcopy(status);path=store_path(cfg);saved=read_json(path)
        captures=saved.setdefault('capture',{});choices=saved.setdefault('choices',{});changed=False
        changes=[];missing=0
        cfg_snapshot=app.stringify_config(cfg)
        if saved.get('config')!=cfg_snapshot:saved['config']=cfg_snapshot;changed=True
        for row in status.get('episodes',[]):
            root=Path(row.get('episode_dir') or '')
            if not root.is_dir() or not row.get('episode_dir'):continue
            key,original=capture(root)
            if key not in captures:captures[key]=original;changed=True
            original=captures[key]
            report_path=Path(row['qc_output'])/'qc_report.json' if row.get('qc_output') else None
            report=read_json(report_path) if report_path else {}
            # Reports produced before this workflow may already contain an old manual edit.
            baseline=report.get('qc_original') or (report if not report.get('manual_review') else {})
            qc=baseline.get('quality_grade') or ('F' if row.get('qc_ok') is False else '')
            token=hashlib.sha256(json.dumps([str(report_path),report_path.stat().st_mtime_ns if report_path and report_path.is_file() else 0,row.get('qc_ok')],sort_keys=True).encode()).hexdigest()
            choice=choices.get(key,{})
            if choice.get('qc_token')!=token:
                if choice.get('source')=='manual':
                    # A new report may invalidate export approval, never the human grade.
                    choice={k:v for k,v in choice.items() if k not in ('approval','export_block')}
                else:choice={}
            if choice.get('source')!='manual' and report.get('manual_review'):
                choice=dict(source='manual',grade=report.get('quality_grade',''),qc_token=token)
                choices[key]=choice;changed=True
            # Older explicit bulk selections were stored by selected source. They
            # are human choices too; retain them, including across later reports.
            if choice.get('source') in ('collection','qc') and choice.get('grade') in GRADES:
                choice=dict(choice,selected_from=choice['source'],source='manual')
                choices[key]=choice;changed=True
            manual=choice.get('grade') if choice.get('source')=='manual' else ''
            effective=manual or qc or original['grade']
            revision=hashlib.sha256(json.dumps([token,original,choice],sort_keys=True).encode()).hexdigest()
            row.update(collection_grade=original['grade'],collection_grade_source=original['source'],
                       qc_grade=qc,qc_reason=baseline.get('reason',''),qc_grade_label=qc or ('历史结论未保留' if report.get('manual_review') and not baseline else '待复核' if baseline.get('review_required') else '待质检'),quality_grade=effective,
                       manual_quality_grade=choice.get('grade','') if choice.get('source')=='manual' else '',
                       grade_source='manual' if manual else 'qc' if qc else 'collection',
                       manual_selected_from=choice.get('selected_from','manual') if manual else '',
                       grade_changed=bool(original['grade'] and qc and original['grade']!=qc),
                       grade_revision=revision,grade_key=key,qc_token=token,
                       grade_review_required=bool(baseline.get('review_required')))
            # Collection success is provenance too; don't infer it from a later manual F.
            row['collection_status']='采集失败' if original['grade']=='F' else '采集成功' if original['grade'] else '未记录'
            approval=choice.get('approval')
            row['grade_approval']=approval
            row['grade_export_block']=choice.get('export_block','')
            row['shared_quality_grade']=(approval or report).get('quality_grade') or row.get('shared_quality_grade')
            if approval:row['shared_review_required']=False
            # Keep the original QC flag while tracking the outstanding review queue.
            resolved = choice.get('source') in ('manual','collection') and bool(choice.get('grade')) and not choice.get('export_block')
            row['review_pending']=row['grade_review_required'] and not resolved
            if choice.get('export_block'):row['quality_description']+='；导出受限：'+choice['export_block']
            if row['grade_changed']:changes.append(dict(episode_id=row['episode_id'],before=original['grade'],after=qc))
            if not original['grade']:missing+=1
        if changed:write_json(path,saved)
        status.setdefault('qc_overview',{})['quality_grade_counts']=app.qc_overview_quality_grade_counts(status.get('episodes',[]))
        status['grade_comparison']=dict(changes=changes,changed_count=len(changes),missing_collection_count=missing,
                                        reviewed_count=sum(bool(r.get('qc_output')) or r.get('qc_ok') is False for r in status.get('episodes',[])),
                                        pending_count=sum(bool(r.get('review_pending')) for r in status.get('episodes',[])))
        status['grade_busy']=busy(cfg)
        return status
    def status(payload):
        cfg=app.derive_paths(payload)
        scopes[str(Path(cfg['hdf5_root']).resolve())]=cfg
        with lock:return enrich(old_status(payload),cfg)
    app.dataset_status=status

    old_replay=app.start_replay
    def replay(payload):
        cfg=app.derive_paths(payload)
        scopes[str(Path(cfg['hdf5_root']).resolve())]=cfg
        return old_replay(payload)
    app.start_replay=replay
    def context(root):
        root=Path(root).expanduser().resolve()
        if not hdf5_path(root).is_file():raise ValueError('不是可回放的 HDF5 episode')
        cfg=scopes.get(str(root.parent)) or scopes.get(str(root))
        if cfg is None:
            cfg=app.derive_paths(dict(hdf5_root=str(root.parent),dataset_name=root.parent.name,data_root=str(root.parent.parent),robot_type='zerith'))
            persisted=read_json(store_path(cfg)).get('config')
            if persisted:cfg=app.derive_paths(persisted)
        return root,cfg
    def row_snapshot(row,cfg):
        return dict(root=row['episode_dir'],episode_name=row['episode_id'],current_grade=row.get('quality_grade',''),
                    collection_grade=row.get('collection_grade',''),qc_grade=row.get('qc_grade',''),qc_grade_label=row.get('qc_grade_label',''),
                    manual_grade=row.get('manual_quality_grade',''),grade_source=row.get('grade_source'),
                    grade_changed=row.get('grade_changed',False),review_required=row.get('grade_review_required',False),
                    review_pending=row.get('review_pending',False),
                    reason=row.get('qc_reason',''),revision=row['grade_revision'],busy=busy(cfg))
    def snapshot(root):
        root,cfg=context(root);app.invalidate_status_cache()
        result=status(app.stringify_config(cfg))
        row=next((e for e in result['episodes'] if Path(e.get('episode_dir','')).resolve()==root),None)
        if row is None:raise ValueError('当前数据集中未找到此 episode')
        return row_snapshot(row,cfg),cfg
    def grade_list(root):
        _,cfg=context(root)
        result=status(app.stringify_config(cfg))
        return dict(episodes=[row_snapshot(row,cfg) for row in result.get('episodes',[])])
    def apply(cfg,requests,source='manual'):
        if source not in ('manual','collection','qc'):raise ValueError('无效等级来源')
        with lock:
            if busy(cfg):raise ValueError('该数据集正在处理，请完成后保存等级')
            app.invalidate_status_cache();current=status(app.stringify_config(cfg));rows={r['episode_id']:r for r in current['episodes']}
            pending=[];skipped=[];preserved_manual=[]
            for req in requests:
                row=rows.get(req['episode_name'])
                if not row:raise ValueError('未找到 episode：'+req['episode_name'])
                if req.get('revision')!=row['grade_revision']:raise ValueError('等级或报告已更新，请刷新后再次保存')
                if source!='manual' and row.get('manual_quality_grade') and row.get('manual_selected_from')=='manual':
                    preserved_manual.append(row['episode_id']);continue
                grade=row.get(source+'_grade','') if source!='manual' else str(req.get('grade','')).upper()
                if source!='manual' and grade not in GRADES:skipped.append(row['episode_id']);continue
                if grade not in GRADES:raise ValueError('等级仅支持 A/B/C/F')
                choice=dict(qc_token=row['qc_token'],source='manual',selected_from=source,grade=grade)
                if source!='qc' and grade in ('A','B') and (row.get('shared_review_required') or row.get('shared_quality_grade')=='F'):
                    from .zerith_rules import prepare_manual_approval
                    try:
                        approved=prepare_manual_approval(app,cfg,row['episode_id'],grade,'人工采用'+('采集等级' if source=='collection' else '筛选等级'))
                        if approved:choice['approval']=approved[1]
                    except ValueError as exc:choice['export_block']=str(exc)
                pending.append((row,choice))
            path=store_path(cfg);saved=read_json(path);choices=saved.setdefault('choices',{})
            for row,choice in pending:
                choices[row['grade_key']]=choice
            # One atomic dataset write: a bulk choice can never save only half the rows.
            write_json(path,saved);app.invalidate_status_cache()
            return dict(saved_count=len(pending),skipped=skipped,preserved_manual=preserved_manual,status=status(app.stringify_config(cfg)))
    def save(payload):
        with lock:
            before,cfg=snapshot(payload['root'])
            apply(cfg,[dict(episode_name=before['episode_name'],grade=payload.get('grade'),revision=payload.get('revision'))])
            after,_=snapshot(payload['root'])
            return dict(after,before_grade=before['current_grade'],saved_grade=payload['grade'])
    app.replay_grade_snapshot=lambda root:snapshot(root)[0]
    app.replay_grade_list=grade_list
    app.save_replay_grade=save
    app.apply_grade_choices=apply
    # Older collection endpoints use the same independent grade storage.
    def old_save(cfg,episode_name,quality_grade,reason_label='',reason_codes=None):
        current=status(app.stringify_config(cfg));row=next(r for r in current['episodes'] if r['episode_id']==episode_name)
        return apply(cfg,[dict(episode_name=episode_name,grade=quality_grade,revision=row['grade_revision'])])
    app.save_qc_report_quality_grade=old_save
    old_get=app.Handler.do_GET;old_post=app.Handler.do_POST
    def get(handler):
        parsed=urlparse(handler.path)
        if parsed.path not in ('/api/replay-grade','/api/replay-grades'):return old_get(handler)
        try:
            root=parse_qs(parsed.query).get('root',[''])[0]
            app.json_response(handler,grade_list(root) if parsed.path=='/api/replay-grades' else snapshot(root)[0])
        except (ValueError,OSError,KeyError) as exc:app.json_response(handler,dict(error=str(exc)),400)
    def post(handler):
        route=urlparse(handler.path).path
        if route not in ('/api/replay-grade','/api/grade-choices'):return old_post(handler)
        try:
            payload=json.loads(handler.rfile.read(int(handler.headers.get('Content-Length',0))))
            result=save(payload) if route=='/api/replay-grade' else apply(app.derive_paths(payload),payload['episodes'],payload.get('source','manual'))
            app.json_response(handler,result)
        except (ValueError,OSError,KeyError) as exc:app.json_response(handler,dict(error=str(exc)),409)
    app.Handler.do_GET=get;app.Handler.do_POST=post
