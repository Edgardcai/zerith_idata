"""Task text remains source context; its wording and consistency do not grade data."""
import copy
import re
from .gripper_policy import refresh_motion_status

POLICY = 'prompt_qc_disabled_v1'
INSTRUCTION = ('提示词不参与质检：不得因任务文本、Grasp/Place、句式、物品名、操作手文本或'
               '文本与元数据不一致判异常、suspected或要求复核；任务原文仅作背景，不修写原文。')
METADATA_PREFIXES = ('任务冲突：', '左手目标冲突：', '右手目标冲突：', '采集目标列表', '采集目标格式')


def normalize_check(check):
    detail = check.get('detail', {})
    if not isinstance(detail, dict):
        detail = {}; check['detail'] = detail
    detail.update(issues=[], note='提示词质检已关闭，原文保留，不参与判级')
    detail.pop('normalized_candidate', None)
    check.update(status='na', score_delta=0)
    if 'shared_text' in detail:
        detail['shared_text'] = detail['note']


def normalize_metadata(check):
    detail = check.get('detail', {})
    if not isinstance(detail, dict): return
    issues = detail.get('issues', [])
    remaining = [v for v in issues if not str(v).startswith(METADATA_PREFIXES)]
    if remaining != issues:
        detail['issues'] = remaining
        if not remaining: check.update(status='pass', score_delta=0)
        if 'shared_text' in detail:
            detail['shared_text'] = check.get('label', '标注一致性') + ('：' + '；'.join(remaining) if remaining else '')


def project_motion(motion):
    out = copy.deepcopy(motion)
    evidence = out.get('evidence', {})
    changed = False
    for finding in out.get('findings', []):
        if finding.get('policy_excluded'): continue
        refs = {r for r in finding.get('evidence_ids', []) if r.startswith('check:')}
        reason = finding.get('reason', '')
        prompt_only = (('check:prompt' in refs or 'check:metadata' in refs)
            and bool(re.search(r'提示词|prompt|Grasp|Place|句式|任务文本|目标冲突|任务冲突', reason, re.I))
            and not re.search(r'跳变|静止|延迟|响应|掉落|碰撞|阶段缺失|阶段不完整', reason)
            and all(r in ('check:prompt', 'check:metadata') or evidence.get(r, {}).get('status') == 'pass' for r in refs))
        if prompt_only:
            finding.update(status='not_observable', policy_excluded=POLICY,
                           reason='提示词质检已关闭，任务文本不参与判级')
            changed = True
    if isinstance(evidence.get('check:prompt'), dict):
        normalize_check(evidence['check:prompt'])
        evidence['check:prompt']['grading_effect'] = 'none'
    if changed and out.get('status') in ('pass', 'review'):
        refresh_motion_status(out)
        out['policy_projection'] = POLICY
    return out
