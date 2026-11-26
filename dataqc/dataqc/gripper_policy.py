"""Retire closure-count requirements without suppressing stage or feedback issues."""
import copy
import re

POLICY = 'gripper_counts_informational_v1'
INSTRUCTION = ('夹爪闭合次数仅作统计，不要求每手一次；零次、多次或初始已闭合均不得因此判异常、'
               'suspected或要求复核。阶段归属和可匹配的反馈延迟仍按原指标检查。')
NOTE = '闭合次数仅作统计，不限制次数，不参与判级；初始已闭合不计作一次闭合'
COUNT_ISSUE = re.compile(r'^[左右]爪 (?:Action|State) 闭合 \d+ 次，应为 \d+ 次$')


def normalize_check(check):
    """Mutate one copied measurement; preserve every non-count issue."""
    detail = check.get('detail', {})
    if not isinstance(detail, dict):
        return
    issues = detail.get('issues', [])
    remaining = [issue for issue in issues if not COUNT_ISSUE.fullmatch(str(issue))]
    retired = len(remaining) != len(issues)
    for channel in detail.get('channels', {}).values():
        channel.pop('expected_closures', None)
    detail.update(count_grading=False, note=NOTE)
    check['label'] = '夹爪阶段 / 反馈'
    if retired:
        detail['issues'] = remaining
        if not remaining:
            detail['bad_frames'] = []
            check.update(status='pass', score_delta=0)
    if 'shared_text' in detail:
        detail['shared_text'] = check['label'] + ('：' + '；'.join(remaining) if remaining else '')


def count_only_finding(finding):
    if finding.get('criterion') != '夹爪配合':
        return False
    reason = finding.get('reason', '')
    checks = {e for e in finding.get('evidence_ids', []) if e.startswith('check:')}
    return (checks == {'check:gripper_sequence'} and '闭合' in reason
            and bool(re.search(r'次数|\d+\s*次|少于|未达|不足|缺预期|无预期', reason))
            and not re.search(r'延迟|超时|落在|阶段外|错位|掉落|碰撞|顺序|(?:响应|反馈)(?:异常|缺失|不符|滞后)', reason))


def refresh_motion_status(motion):
    active = [f for f in motion.get('findings', []) if not f.get('policy_excluded')]
    motion['status'] = 'review' if any(f.get('status') == 'suspected' or
        (not f.get('criterion', '').endswith('风险') and f.get('status') != 'pass') for f in active) else 'pass'
    motion['summary'] = ('动作指标仍有其他可疑项，已排除停用的质检规则' if motion['status'] == 'review'
                         else '动作指标未发现其他异常，已排除停用的质检规则')


def project_motion(motion):
    """Project grounded count-only findings; keep other model findings intact."""
    out = copy.deepcopy(motion)
    changed = False
    for finding in out.get('findings', []):
        if not finding.get('policy_excluded') and count_only_finding(finding):
            finding.update(status='not_observable', policy_excluded=POLICY,
                           reason='闭合次数仅作统计，已取消次数要求，不参与判级')
            changed = True
    evidence = out.get('evidence', {}).get('check:gripper_sequence')
    if isinstance(evidence, dict):
        normalize_check(evidence)
        evidence['grading_effect'] = 'B' if evidence.get('status') == 'warn' else 'none'
    if changed and out.get('status') in ('pass', 'review'):
        refresh_motion_status(out)
        out['policy_projection'] = POLICY
    return out
