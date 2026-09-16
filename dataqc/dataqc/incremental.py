"""Reusable automatic results; manual choices live in a separate durable store."""
import hashlib
import json
import shutil
from pathlib import Path
from . import config
from .io import fingerprint, read_json, write_json
from .motion import RULE_VERSION


def policy(cfg):
    from .assessment import VERSION
    from .yolo_gate import cache_policy
    return dict(rules=RULE_VERSION, assessment=VERSION, category=cache_policy(cfg),
                model=cfg.get('api_model'), effort=cfg.get('reasoning_effort','none'),
                stationary=cfg['stationary_frames'])


def completed_report(cache,cfg):
    if cfg.get('qc_force'):return None
    report=read_json(Path(cache)/'manual_report.json')
    if not report or report.get('inspection_policy')!=policy(cfg):return None
    visual=report.get('visual') or {}
    if visual.get('errors') or visual.get('error'):return None
    if not report.get('raw_report'):return None
    if report.get('quality_grade')!='F' and visual.get('motion_review',{}).get('status') not in ('pass','review'):
        return None
    return report


def reused(report):
    import copy
    result=copy.deepcopy(report)
    result['execution']='cached'
    motion=result.get('visual',{}).get('motion_review')
    if motion:motion['execution']='cached'
    return result


def episode_root(path):
    path=Path(path)
    return path.parent.parent if path.is_file() and path.parent.name=='states' else path.parent if path.is_file() else path


def cli_context(source,profile,args):
    if profile.raw.get('adapter')!='zerith_columnar' or args.task or args.actions_json or args.manual_json:return None
    from integrations.zerith_rules import settings_for_profile, manual_cache
    root=episode_root(source);cfg=settings_for_profile(profile)
    cache=manual_cache(root,cfg)
    return root,cfg,cache


def reuse_cli(source,out,profile,args):
    context=cli_context(source,profile,args)
    if not context:return None
    root,cfg,cache=context;report=completed_report(cache,cfg)
    saved=read_json(cache/'cli_result.json')
    if not report or not saved or saved.get('artifact_mode')!=args.artifact_mode:return None
    original=Path(saved['output'])
    names=set(saved.get('artifacts',{}).values())|{'manifest.json'}
    if not names or any(Path(n).name!=n or not (original/n).is_file() for n in names):return None
    out.mkdir(parents=True,exist_ok=True)
    for name in names:
        if (original/name).resolve()!=(out/name).resolve():shutil.copy2(original/name,out/name)
    manifest=read_json(out/'manifest.json')
    manifest.update(processed_dir=str(out),reused_from=str(original))
    write_json(out/'manifest.json',manifest)
    write_json(out/'qc_report.json',reused(report))
    result=dict(saved,input=str(source),output=str(out),execution='cached')
    print('复用完整质检结果 · '+root.name,flush=True)
    return result


def remember_cli(source,profile,args,result):
    context=cli_context(source,profile,args)
    if context:write_json(context[2]/'cli_result.json',result)
