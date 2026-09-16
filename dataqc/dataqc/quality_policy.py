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
    current = normalize_raw(raw)
    out['raw_report'] = current
    keys = {c.get('key') for c in current['checks']}
    baseline = dict(out.get('qc_original') or (out if not out.get('manual_review') else {}))
    if baseline and {'state', 'action', 'finite'} <= keys and not current['hard_fail']:
        if baseline.get('quality_grade') == 'F' or baseline.get('review_required'):
            baseline.update(quality_grade='B', accepted=False, review_required=True,
                            reason='按当前规则为 B，保留问题供人工复核；自动检查未发现保留的硬失败')
            if not out.get('manual_review'):
                out.update({k: baseline[k] for k in ('quality_grade','accepted','review_required','reason')})
            if out.get('qc_original'):
                out['qc_original'].update({k: baseline[k] for k in ('quality_grade','accepted','review_required','reason')})
    out['policy_projected'] = current['checks'] != raw.get('checks')
    return out
