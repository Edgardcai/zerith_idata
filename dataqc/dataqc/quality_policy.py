"""Apply the current grading policy to measurements, including historical reports."""
import copy
from .io import parse_task, prompt_issues

SOFT = {'motion', 'stationary', 'prompt', 'gripper_sequence', 'timestamps', 'fps', 'duration', 'stages', 'metadata', 'repair_records'}


def manual_grade(data, automatic):
    """Keep a saved human grade while numerical checks update processing status."""
    grade=(data.get('manual_decision') or {}).get('grade')
    return grade if grade in ('A','B','C','F') else automatic


def normalize_raw(raw):
    out = copy.deepcopy(raw)
    checks = []
    for c in out.get('checks', []):
        key = c.get('key') or c.get('detail', {}).get('shared_key') if isinstance(c.get('detail'), dict) else c.get('key')
        if key == 'lift_height': continue
        if c.get('status') == 'fail' and (key in SOFT or str(key).startswith('arm_')):
            c.update(status='warn', score_delta=0)
        if key == 'prompt':
            detail = c.setdefault('detail', {})
            task = detail.get('original', out.get('task', ''))
            if task or 'original' in detail:
                detail['issues'] = prompt_issues(task)
                c.update(status='pass' if parse_task(task) else 'warn', score_delta=0)
        checks.append(c)
    out['checks'] = checks
    out['hard_fail'] = any(c.get('status') == 'fail' for c in checks)
    return out


def display_report(report):
    """Reinterpret saved evidence without changing source reports or human decisions."""
    out = copy.deepcopy(report)
    raw = out.get('raw_report')
    if not raw: return out
    visual=out.get('visual') or {}
    if out.get('execution')=='incomplete' or visual.get('errors') or visual.get('error'):
        out['execution']='incomplete'
        if not out.get('manual_review'):out.update(quality_grade='',accepted=False,review_required=True)
        if out.get('qc_original'):out['qc_original'].update(quality_grade='',accepted=False,review_required=True)
        return out
    current = normalize_raw(raw)
    out['raw_report'] = current
    keys = {c.get('key') for c in current['checks']}
    baseline = dict(out.get('qc_original') or (out if not out.get('manual_review') else {}))
    if baseline and {'state', 'action', 'finite'} <= keys and not current['hard_fail']:
        if baseline.get('quality_grade') == 'F':
            # Only migrate an old F. A current B's reason is evidence, not a
            # placeholder to overwrite just because it still needs review.
            retired = []
            current_by_key = {c.get('key'): c for c in current['checks']}
            for check in raw.get('checks', []):
                key = check.get('key')
                now = current_by_key.get(key)
                if check.get('status') == 'fail' and (not now or now.get('status') != 'fail'):
                    retired.append(str(check.get('label') or key))
            reason = '旧报告原判 F，按当前规则调整为 B 待复核'
            if retired:
                reason += '；不再作为硬失败的检查：' + '、'.join(dict.fromkeys(retired))
            original_reason = baseline.get('reason') or (baseline.get('decision') or {}).get('reason')
            reason += '；历史原因：' + str(original_reason) if original_reason else '；旧报告未保留具体判级原因'
            baseline.update(quality_grade='B', accepted=False, review_required=True,
                            reason=reason)
            if not out.get('manual_review'):
                out.update({k: baseline[k] for k in ('quality_grade','accepted','review_required','reason')})
            if out.get('qc_original'):
                out['qc_original'].update({k: baseline[k] for k in ('quality_grade','accepted','review_required','reason')})
    out['policy_projected'] = current['checks'] != raw.get('checks')
    return out
