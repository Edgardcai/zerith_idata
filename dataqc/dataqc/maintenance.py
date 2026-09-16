"""Explicit offline rule refresh; never invokes a model or resumes a batch."""
import time
from pathlib import Path

from . import db
from .config import VAR, settings
from .io import fingerprint, read_json, write_json
from .motion import RULE_VERSION, failure_reason, warning_messages
from .robots import get_adapter
from .quality_policy import manual_grade


def switch_visual_policy(rid, current):
    """Refresh unapproved visual results without re-decoding or calling a model."""
    run = db.get_run(rid)
    if not run or run['status'] != 'paused':
        raise ValueError('切换视觉策略前请暂停任务')
    cfg = dict(run['config'])
    for key in ('api_model','reasoning_effort','max_output_tokens','vlm_token_budget','vision_version',
                'yolo_frame_offset','yolo_confidence','yolo_thresholds_path'):
        cfg[key] = current[key]
    changed = 0
    for ep in db.episodes(rid):
        data = dict(ep['data'])
        if data.get('manual_decision') or ep['status'] == 'ready':
            continue
        if not data.get('visual') and ep['status'] not in ('checking','incomplete','retry_wait'):
            continue
        if data.get('visual', {}).get('vision_version') == current['vision_version']:
            continue
        report = data.get('raw_report', {})
        fatal = [c for c in report.get('checks', []) if c['status'] == 'fail' and c['key'] not in ('prompt','stationary')]
        if fatal:
            continue
        db.audit(rid,ep['id'],'visual_policy_changed','system',ep,dict(vision_version=current['vision_version']))
        for key in ('visual','decision','yolo_report','repaired_report','repaired_root','annotation_checks','retry_at','auto_retry_count'):
            data.pop(key,None)
        db.update('episodes',ep['id'],status='queued',grade='B' if warning_messages(report) else None,
                  reason='数值报告保留，待 YOLO 左右手物品匹配',data=data,revision=ep['revision']+1)
        changed += 1
    db.audit(rid,None,'visual_policy_changed','system',run['config'],cfg)
    db.update('runs',rid,config=cfg,error='',phase='已切换 YOLO 优先 · 预警才调用 Luna',updated=time.time())
    return changed


def refresh_paused_run(rid, progress=lambda _: None):
    run = db.get_run(rid)
    if not run or run['status'] != 'paused':
        raise ValueError('只允许离线更新已暂停的任务')
    cfg = dict(run['config'])
    current = settings()
    for key in ('api_model', 'reasoning_effort', 'max_output_tokens', 'vlm_token_budget', 'vision_version'):
        cfg[key] = current[key]
    adapter = get_adapter(cfg.get('robot', 'zerith'))
    counts = {'A': 0, 'B': 0, 'C': 0, 'F': 0, 'pending': 0, 'incomplete': 0}
    for ep in db.episodes(rid):
        if db.get_run(rid)['status'] != 'paused':
            raise ValueError('任务已离开暂停状态，停止离线更新')
        cache = VAR / 'runs' / rid / f"episode_{ep['number']:06d}"
        cache.mkdir(parents=True, exist_ok=True)
        data = dict(ep['data'])
        try:
            before = fingerprint(ep['root'])
            previous = read_json(cache / 'fingerprint.json')
            if previous and previous != before:
                raise ValueError('原始文件已变化，请新建任务')
            report = adapter.check(ep['root'], cfg['stationary_frames'])
            if fingerprint(ep['root']) != before:
                raise ValueError('复检期间原始文件变化，请新建任务')
            fatal = [c for c in report['checks'] if c['status'] == 'fail' and c['key'] not in ('stationary', 'prompt')]
            warnings = warning_messages(report)
            grade = manual_grade(data,'F' if fatal else ('B' if warnings else None))
            status = 'rejected' if fatal else 'queued'
            reason = ('数值硬失败：' + failure_reason(fatal)) if fatal else ('；'.join(warnings) or '数值检查完成') + '；待动作复核'
            if data.get('manual_decision', {}).get('grade') == 'F':
                grade, status, reason = 'F', 'rejected', data['manual_decision']['reason']
            db.audit(rid, ep['id'], 'offline_rules_refresh', 'system',
                     dict(version=(data.get('raw_report') or {}).get('version'), grade=ep['grade'], reason=ep['reason'], data=data),
                     dict(version=RULE_VERSION, grade=grade, status=status, reason=reason))
            for key in ('visual', 'decision', 'repaired_report', 'repaired_root', 'annotation_checks', 'retry_at', 'auto_retry_count'):
                data.pop(key, None)
            data.update(raw_report=report, report_path=str(cache / 'raw_report.json'))
            write_json(cache / 'raw_report.json', report)
            write_json(cache / 'fingerprint.json', before)
            db.update('episodes', ep['id'], grade=grade, status=status, reason=reason, data=data, revision=ep['revision'] + 1)
            counts[grade or 'pending'] += 1
        except (ValueError, OSError) as exc:
            db.update('episodes', ep['id'], status='incomplete', grade=manual_grade(data,None), reason=str(exc))
            counts['incomplete'] += 1
        progress(dict(episode=Path(ep['root']).name, counts=counts))
    db.update('runs', rid, config=cfg, error='', updated=time.time(), phase='新规则数值复检完成 · 已暂停，继续后执行未完成复核')
    return counts
