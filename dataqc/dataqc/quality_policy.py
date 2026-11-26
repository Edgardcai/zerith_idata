"""Apply the current grading policy to measurements, including historical reports."""
import copy
from .gripper_policy import POLICY, normalize_check, project_motion
from . import prompt_policy

SOFT = {'motion', 'stationary', 'prompt', 'gripper_sequence', 'timestamps', 'fps', 'duration', 'stages', 'metadata', 'repair_records'}


def manual_grade(data, automatic):
    """Keep a saved human grade while numerical checks update processing status."""
    grade=(data.get('manual_decision') or {}).get('grade')
    return grade if grade in ('A','B','C','F') else automatic


def normalize_raw(raw):
    out = copy.deepcopy(raw)
    checks = []
    retired_penalty = 0
    for c in out.get('checks', []):
        key = c.get('key') or c.get('detail', {}).get('shared_key') if isinstance(c.get('detail'), dict) else c.get('key')
        if key == 'lift_height': continue
        before_delta = c.get('score_delta', 0)
        if key == 'gripper_sequence': normalize_check(c)
        if key == 'prompt': prompt_policy.normalize_check(c)
        if key == 'metadata': prompt_policy.normalize_metadata(c)
        retired_penalty += c.get('score_delta', 0) - before_delta
        if c.get('status') == 'fail' and (key in SOFT or str(key).startswith('arm_')):
            c.update(status='warn', score_delta=0)
        checks.append(c)
    out['checks'] = checks
    out['hard_fail'] = any(c.get('status') == 'fail' for c in checks)
    if retired_penalty and 'score' in out:
        out['score'] = min(100, max(0, out['score'] + retired_penalty))
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
    old_gripper = next((c for c in raw.get('checks', []) if c.get('key') == 'gripper_sequence'), None)
    new_gripper = next((c for c in current['checks'] if c.get('key') == 'gripper_sequence'), None)
    motion = prompt_policy.project_motion(project_motion(visual.get('motion_review') or {}))
    old_prompt = [c for c in raw.get('checks', []) if c.get('key') in ('prompt', 'metadata')]
    new_prompt = [c for c in current['checks'] if c.get('key') in ('prompt', 'metadata')]
    count_changed = old_gripper != new_gripper or old_prompt != new_prompt or motion != visual.get('motion_review', {})
    if count_changed:
        visual['motion_review'] = motion
        out['visual'] = visual
        for check in out.get('checks', []):
            check_key = check.get('key') or check.get('name') or check.get('detail', {}).get('shared_key')
            if check_key == 'gripper_sequence':
                normalize_check(check)
            if check_key == 'prompt': prompt_policy.normalize_check(check)
            if check_key == 'metadata': prompt_policy.normalize_metadata(check)
            if check.get('name') == 'motion_vlm_review':
                check.update(status='pass' if motion.get('status') == 'pass' else 'warn',
                    detail=dict(motion, shared_key='motion_vlm_review', shared_text=motion.get('summary', '')))
        cat = visual.get('category_review') or {}
        # A complete structured assessment can be re-graded from the same evidence.
        # Missing evidence, hard failures, C/F and human decisions keep their safeguards.
        if (baseline.get('quality_grade') in ('A', 'B') and {'state', 'action', 'finite'} <= keys
                and not current['hard_fail'] and visual.get('assessment_version') == 'motion_category_v5'
                and motion.get('status') in ('pass', 'review')
                and cat.get('status') in ('pass', 'skipped', 'review', 'fail') and 'task' in current):
            from .assessment import decide
            decision = decide(current, motion, cat, {}, dict(api_model=visual.get('model', '')), current['task'])
            values = dict(quality_grade='B' if decision['grade'] == 'REVIEW' else decision['grade'],
                accepted=decision['grade'] in ('A', 'B'), review_required=decision['grade'] == 'REVIEW',
                reason=decision['reason'], decision=decision)
            baseline.update(values)
            if not out.get('manual_review'): out.update(values)
            if out.get('qc_original'): out['qc_original'].update(values)
            visual['decision'] = decision
            if visual.get('verification'):
                visual['verification'].update(status='uncertain' if decision['grade'] == 'REVIEW' else 'pass', reason=decision['reason'])
            for check in out.get('checks', []):
                if check.get('name') == 'visual_matching':
                    check.update(status='warn' if decision['grade'] == 'REVIEW' else 'pass',
                        detail=dict(shared_key='visual_matching', shared_text=decision['reason']))
        out['gripper_count_policy'] = POLICY
        out['prompt_policy'] = prompt_policy.POLICY
        if 'score' in current: out['quality_score'] = current['score']
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
    out['policy_projected'] = bool(out.get('policy_projected') or count_changed or current['checks'] != raw.get('checks'))
    return out
