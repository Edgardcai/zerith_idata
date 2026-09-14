"""Independent trajectory review and optional, conjunctive category verification."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time
from . import motion_review, yolo_gate, vision
from .io import load, parse_task, normalized_task, write_json, read_json
from .motion import RULE_VERSION, warning_grade, failure_reason

VERSION='motion_category_v4'


def category(root,report,cfg,cache,progress):
    if not cfg.get('vlm_enabled',False):
        return dict(status='skipped',enabled=False,hands=[],yolo=dict(status='na',enabled=False,hands=[],warnings=[]),evidence={})
    gate=yolo_gate.yolo_match(root,report,cfg,cache,progress)
    evidence={}
    # Every required hand must be independently checked, including YOLO passes.
    reviewed=yolo_gate.review_warnings(root,gate,cfg,cache,evidence,progress,all_hands=True)['hands']
    status='fail' if any(h['status']=='fail' for h in reviewed) else 'pass' if gate['status']=='pass' and all(h['status']=='pass' for h in reviewed) else 'review'
    return dict(status=status,enabled=True,hands=reviewed,yolo=gate,evidence=evidence)


def inspect(root,report,cfg,cache,progress=lambda _:None,*,motion_outcome=None):
    started=time.perf_counter();cache=Path(cache);cache.mkdir(parents=True,exist_ok=True)
    enabled=cfg.get('vlm_enabled',False);d=load(root)
    # Separate request ledgers/caches and partition the per-episode budget.
    branch_cfg=cfg|dict(vlm_token_budget=cfg.get('vlm_token_budget',250000)//(2 if enabled else 1))
    errors={};results={};retry_after=0;api_unavailable=False
    def motion_call():
        if motion_outcome is None:
            return motion_review.inspect(root,report,branch_cfg,cache/'motion',progress)
        from .batch_motion import unwrap
        result=unwrap(motion_outcome)
        if result['signature'] != motion_review.prepare(root,report,branch_cfg)[1]:
            raise ValueError('批量结果与当前数据或规则不一致，请重新质检')
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=dict(motion=pool.submit(motion_call),
                     category=pool.submit(category,root,report,branch_cfg,cache/'category',progress))
        for name,future in futures.items():
            try:results[name]=future.result()
            except Exception as exc:
                from .worker import Paused
                if isinstance(exc,Paused):raise
                errors[name]=str(exc)
                if isinstance(exc,vision.APIUnavailable):api_unavailable=True
                if isinstance(exc,vision.RateLimited):retry_after=max(retry_after,exc.retry_after)
    motion=results.get('motion',dict(status='incomplete',summary=errors.get('motion',''),findings=[]))
    cat=results.get('category',dict(status='incomplete',enabled=enabled,hands=[],yolo=read_json(cache/'category/yolo_match.json') or dict(status='na',hands=[],warnings=[]),evidence={}))
    targets=parse_task(normalized_task(d['task'])) or {}
    gripper=next((c for c in report['checks'] if c['key']=='gripper_sequence'),{})
    stages=[dict(s,item=targets.get(s['hand'],'')) for s in gripper.get('detail',{}).get('stages') or []]
    fatal=[c for c in report['checks'] if c['status']=='fail' and c['key'] not in ('stationary','prompt')]
    reasons=[]
    if fatal:grade='F';reasons.append(failure_reason(fatal))
    elif cat['status']=='fail':grade='F';reasons.append('图像 VLM 有证据确认商品或操作手不匹配')
    elif errors or motion['status']!='pass' or cat['status'] not in ('pass','skipped'):
        grade='REVIEW'
        if errors:reasons.extend(k+' 分析未完成：'+v for k,v in errors.items())
        if motion['status']=='review':reasons.append('动作指标存在可疑现象，需要人工复核')
        if cat['status']=='review':reasons.append('类别识别未同时满足每手 YOLO 至少2/3及图像 VLM通过')
    else:grade='A';reasons.append('动作指标分析完成；类别双检通过' if enabled else '动作指标分析完成；类别识别未启用')
    if (not targets or not stages or any(c['key']=='stationary' and c['status']=='fail' for c in report['checks'])) and grade!='F':
        grade='REVIEW';reasons.append('任务/阶段标注或超长静止段需要人工复核')
    references=[c for c in report['checks'] if isinstance(c.get('detail'),dict) and c['detail'].get('requires_review')]
    if references and grade!='F':
        grade='REVIEW';reasons.extend('；'.join(c['detail'].get('issues',[])) for c in references)
    decision=warning_grade(dict(grade=grade,reason='；'.join(reasons),corrected_prompt=normalized_task(d['task']),
        stages=stages,safe_trim_ids=[],findings=[],category_qc=dict(policy=VERSION,enabled=enabled,
        yolo_enabled=enabled,vlm_enabled=enabled,complete=cat['status']=='pass',model=cfg['api_model']),
        motion_qc=dict(version=motion_review.VERSION,status=motion['status'],input_type='metrics_and_trajectory_only')),report)
    if decision['grade'] in ('A','B'):vision.validate_decision(decision,d,[])
    result=dict(pipeline='yolo_first',assessment_version=VERSION,rule_version=RULE_VERSION,
        vision_version=cfg.get('vision_version',VERSION),matching_policy=yolo_gate.cache_policy(cfg),model=cfg['api_model'],
        decision=decision,motion_review=motion,category_review=cat,errors=errors,retry_after=retry_after,api_unavailable=api_unavailable,
        yolo_enabled=enabled,vlm_enabled=enabled,vlm_called=enabled and 'category' not in errors,
        motion_vlm_called='motion' not in errors,yolo=cat['yolo'],hand_checks=cat['hands'],evidence=cat['evidence'],
        verification=dict(status='fail' if grade=='F' else 'uncertain' if grade=='REVIEW' else 'pass',reason=decision['reason']),
        coverage=dict(mode='metrics_and_optional_category',total_frames=d['n'],full_motion_review=False),
        timing=dict(total_seconds=time.perf_counter()-started))
    write_json(cache/'visual_report.json',result)
    return result
