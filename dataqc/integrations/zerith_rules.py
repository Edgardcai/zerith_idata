"""Legacy report adapter. Measurements and visual policy come from dataqc."""
import hashlib
import json
import os
import copy
from pathlib import Path

from dataqc import config
from dataqc.io import fingerprint, read_json, write_json, load, hdf5_path
from dataqc.motion import RULE_VERSION, failure_reason, warning_messages
from dataqc.robots import get_adapter
from dataqc.worker import visual_decision


def fatal_checks(report):
    return [c for c in report['checks'] if c['status']=='fail' and c['key'] not in ('stationary','prompt')]


def assess(root, cfg, cache, progress=lambda text: print(text, flush=True)):
    """Manual invocation of exactly the same adapter and decision as the worker."""
    adapter=get_adapter('zerith')
    from dataqc.batch_prepare import measure
    report,_=measure(root,cfg,Path(cache),progress)
    fatal=fatal_checks(report)
    visual={}
    if fatal:
        decision=dict(grade='F',reason='判 F：'+failure_reason(fatal))
    else:
        try:
            from dataqc.batch_motion import episode_key
            outcomes=os.environ.get('DATAQC_MOTION_OUTCOMES')
            outcome=read_json(Path(outcomes)/(episode_key(root)+'.json')) if outcomes else None
            if outcomes and not outcome:
                outcome=dict(error='集中审查缺少当前 episode 结果，请重试')
            visual=adapter.inspect(root,report,cfg,cache,progress,motion_outcome=outcome)
            decision=visual_decision(visual,report)
        except Exception as exc:
            decision=dict(grade='REVIEW',reason='VLM 质检未完成，需重试或人工确认：'+str(exc))
            visual=dict(yolo=read_json(Path(cache)/'yolo_match.json'),error=str(exc))
    return report,visual,decision


ALIASES={'state':'state_dim','action':'action_dim','finite':'finite_values',
         'timestamps':'timestamp_monotonic','motion':'motion_stability',
         'stationary':'action_stationary_frames','gripper':'gripper_activity'}


def settings_for_profile(profile):
    cfg=config.settings()
    # Freeze each collection job's switch/model across all subprocess workers.
    policy=json.loads(os.environ.get('DATAQC_CATEGORY_POLICY','{}'))
    for key in ('vlm_enabled','api_model','motion_batch_size','motion_batch_concurrency','qc_force','qc_refresh_token'):
        if key in policy:
            cfg[key]=policy[key]
    if type(cfg.get('vlm_enabled',True)) is not bool:
        raise ValueError('vlm_enabled 必须是布尔值')
    # Explicit old UI/CLI overrides remain supported. Its default follows settings.
    quality=profile.processing.get('rtml',{}).get('global_constraints',{})
    cfg['stationary_frames']=int(quality.get('max_stationary_action_frames',cfg['stationary_frames']))
    return cfg


def manual_cache(root,cfg):
    signature=hashlib.sha256(json.dumps(dict(root=str(root),source=fingerprint(root),rules=RULE_VERSION,stationary=cfg['stationary_frames']),sort_keys=True).encode()).hexdigest()
    cache=config.VAR/'manual'/signature
    cache.mkdir(parents=True,exist_ok=True)
    return cache


def run_manual_checks(episode,profile):
    root=Path(episode.root)
    if not hdf5_path(root).is_file():
        raise ValueError('零次方共享质检需要有效的真机或仿真 HDF5 目录')
    cfg=settings_for_profile(profile)
    cache=manual_cache(root,cfg)
    from dataqc.incremental import completed_report, reused, policy
    previous=completed_report(cache,cfg)
    if previous:return reused(previous)
    raw,visual,decision=assess(root,cfg,cache)
    write_json(cache/'raw_report.json',raw)
    grade=decision['grade']
    warnings=warning_messages(raw)
    if grade=='REVIEW':
        display_grade='B'
    else:
        display_grade=grade
    checks=[]
    for check in raw['checks']:
        detail=dict(check['detail']) if isinstance(check['detail'],dict) else dict(issues=[str(v) for v in check['detail']])
        key=check['key']
        if key in ('state','action'):
            detail.update(expected=23,frames=episode.n_frames,actions=len(episode.actions),bad_frames=0 if check['status']=='pass' else episode.n_frames)
        if key=='fps':detail['fps']=detail.get('actual')
        if key=='duration':detail['duration_sec']=detail.get('seconds')
        if key=='motion':detail['max_state_step']=detail.get('max_step')
        if key=='stationary':detail.update(max_stationary_run_frames=detail.get('max_frames'),stationary_frames=sum(v['frames'] for v in detail.get('intervals',[])))
        detail['shared_key']=key
        detail['shared_text']=check['label']+('：'+'；'.join(detail['issues']) if detail.get('issues') else '')
        checks.append(dict(name=ALIASES.get(key,key),label=check['label'],status=check['status'],score_delta=check['score_delta'],detail=detail))
    if visual.get('motion_review'):
        m=visual['motion_review']
        checks.append(dict(name='motion_vlm_review',label='VLM 动作指标分析',status='pass' if m['status']=='pass' else 'warn',
            score_delta=0,detail=dict(shared_key='motion_vlm_review',shared_text=m.get('summary',''),**m)))
    gate=visual.get('yolo',{})
    if gate.get('enabled', True) and gate.get('hands'):
        checks.append(dict(name='yolo_matching',label='YOLO 左右手匹配',status=gate['status'],score_delta=0,detail=dict(shared_key='yolo_matching',shared_text='；'.join(h['reason'] for h in gate['hands']),**gate)))
    checks.append(dict(name='visual_matching',label='综合质检结论',status='fail' if grade=='F' and not fatal_checks(raw) else 'warn' if grade=='REVIEW' else 'na' if visual.get('verification',{}).get('status')=='skipped' else 'pass',score_delta=0,detail=dict(shared_key='visual_matching',shared_text=decision['reason'])))
    result=dict(profile_id=profile.profile_id,episode_id=episode.episode_id,accepted=grade in ('A','B'),quality_score=raw.get('score',100),accept_score=70,
                checks=checks,summary=dict(frames=episode.n_frames,duration_sec=episode.duration_sec,fps=episode.n_frames/episode.duration_sec if episode.duration_sec else 0,actions=len(episode.actions),camera_counts=episode.camera_counts),
                quality_grade=display_grade,review_required=grade=='REVIEW',reason=decision['reason'],rules_version=RULE_VERSION,
                raw_report=raw,visual=visual,decision=decision,source_fingerprint=fingerprint(root),cache=str(cache))
    result['inspection_policy']=policy(cfg)
    result['execution']='completed'
    result['qc_original']={key:result[key] for key in ('quality_grade','accepted','review_required','reason','decision')}
    from dataqc.reporting import presentation
    result['presentation']=presentation(result)
    write_json(cache/'manual_report.json',result)
    return result


