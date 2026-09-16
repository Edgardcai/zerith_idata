"""Complete deterministic checks before scheduling any motion model requests."""
from . import db, batch_motion
from .io import fingerprint, read_json, write_json
from .motion import RULE_VERSION, failure_reason, warning_messages
from .robots import get_adapter
from .quality_policy import manual_grade


def measure(root, cfg, cache, progress):
    cache.mkdir(parents=True, exist_ok=True)
    before = fingerprint(root)
    previous = read_json(cache / 'fingerprint.json')
    if previous and previous != before:
        raise ValueError('原始数据自本次任务开始后发生变化，请新建任务')
    write_json(cache / 'fingerprint.json', before)
    report = read_json(cache / 'raw_report.json')
    refresh=cfg.get('qc_refresh_token')
    if not report or report.get('version') != RULE_VERSION or (cfg.get('qc_force') and report.get('refresh_token')!=refresh):
        report = get_adapter(cfg.get('robot', 'zerith')).check(root, cfg['stationary_frames'], progress)
        if fingerprint(root) != before: raise ValueError('传统质检期间原始数据发生变化')
        if refresh:report['refresh_token']=refresh
        write_json(cache / 'raw_report.json', report)
    return report, before


def prepare_run(run, work, progress):
    cfg = run['config']; items = []
    for ep in db.episodes(run['id']):
        if ep['status'] in ('ready', 'rejected', 'review', 'incomplete', 'retry_wait'): continue
        progress(f"传统质检 · 记录 {ep['number']:06d}")
        cache = work / f"episode_{ep['number']:06d}"; data = ep['data']
        try:
            report, before = measure(ep['root'], cfg, cache, progress)
            data.update(raw_report=report, report_path=str(cache / 'raw_report.json'))
            fatal = [c for c in report['checks'] if c['status'] == 'fail' and c['key'] not in ('stationary', 'prompt')]
            if fatal:
                db.update('episodes', ep['id'], status='rejected', grade=manual_grade(data,'F'), reason='数值硬失败：' + failure_reason(fatal), data=data)
                continue
            db.update('episodes', ep['id'], status='checking', data=data,
                      grade=manual_grade(data,'B' if warning_messages(report) else None), reason='传统质检完成，等待集中审查')
            if data.get('manual_decision') is None and (run['mode'] != 'manual' or cfg.get('assist_manual', True)):
                items.append(dict(root=ep['root'], report=report, cache=cache / 'motion', fingerprint_expected=before))
        except Exception as exc:
            from .worker import Paused
            if isinstance(exc, Paused): raise
            db.update('episodes', ep['id'], status='incomplete', reason=str(exc), data=data)
    branch_cfg = cfg | dict(vlm_token_budget=cfg.get('vlm_token_budget', 250000) // (2 if cfg.get('vlm_enabled', False) else 1))
    return batch_motion.review_many(items, branch_cfg, work / 'motion_batches', progress) if items else {}
