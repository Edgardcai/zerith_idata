"""Use explicit tabletop collection metadata; never infer a pose from measurements."""
import copy
import math
import re
from pathlib import Path

POLICY = 'recorded_tabletop_pose_and_stages_v1'
ANGLE_FIELDS = ('waist_pitch_rad', 'waist_yaw_rad', 'head_yaw_rad', 'head_pitch_rad')
CHANGED_CHECKS = {'posture_state', 'posture_action', 'gripper_sequence'}


def reference(d):
    targets = d.get('collection', {}).get('targets', {})
    if not isinstance(targets, dict) or targets.get('prompt_mode') != 'tabletop':
        return None
    pose = targets.get('fixed_pose', {}).get('config', {})
    angles = [pose.get(k) for k in ANGLE_FIELDS]
    if not all(type(v) in (int, float) and math.isfinite(v) for v in angles):
        angles = None
    prompts = targets.get('stage_prompts', [])
    hands = []
    operations = []
    for prompt in prompts if isinstance(prompts, list) else []:
        names = re.findall(r'\b(left|right) hand\b', str(prompt), re.I)
        operation = re.match(r'\s*(Place|Grasp)\b', str(prompt), re.I)
        if len(names) != 1 or operation is None:
            hands = []; break
        hands.append(names[0].lower()); operations.append(operation[1].lower())
    # This is the collector's declared tabletop layout, not a task-text grade.
    if hands != ['left', 'right', 'left', 'right'] or operations != ['place', 'place', 'grasp', 'grasp']:
        hands = []
    return dict(policy=POLICY, target_rad=angles, hands=hands,
                operations=operations if hands else [], transitions=d['transitions'])


def correct(d, report):
    ref = reference(d)
    if (ref is None or report.get('recorded_task_policy') == ref or d['n'] < 2
            or any(d[k].shape != (d['n'], 23) for k in ('state', 'action'))):
        return report
    from .motion import arm_and_posture_checks, gripper_check
    out = copy.deepcopy(report)
    replacements = {}
    if ref['target_rad'] is not None:
        state, action = d['state'].copy(), d['action'].copy()
        state[:, 17:21] -= ref['target_rad']; action[:, 17:21] -= ref['target_rad']
        for check in arm_and_posture_checks(state, action, d['t']):
            if not check['key'].startswith('posture_'):
                continue
            check['label'] = check['label'].replace('零位', '采集目标姿态')
            detail = check['detail']; detail['reference'] = ref
            detail['range_reference'] = 'deviation_from_recorded_target'
            for channel, target in zip(detail['channels'], ref['target_rad']):
                channel['target_rad'] = target
                for key in ('mean_rad', 'q01_rad', 'q99_rad'):
                    channel[key.replace('_rad', '_deviation_rad')] = channel[key]
                    if channel[key] is not None: channel[key] += target
            detail['issues'] = [f"{c['joint']} 偏离采集目标 {c['target_rad']:g} rad，超过原 ±0.02 rad 范围"
                                for c in detail['channels'] if c['status'] == 'warn']
            replacements[check['key']] = check
    if ref['hands']:
        check = gripper_check(d['state'], d['action'], d['task'], d['transitions'],
                              feedback_available=d.get('gripper_feedback_available', True),
                              expected_hands=ref['hands'])
        for stage, operation in zip(check['detail'].get('stages') or [], ref['operations']):
            stage['operation'] = operation
        check['detail']['stage_source'] = 'collection_task.targets.stage_prompts + subtask_transitions'
        replacements[check['key']] = check
    out['checks'] = [replacements.get(c['key'], c) for c in out['checks']]
    out['recorded_task_policy'] = ref
    return out


def project_review(old, payload, signature, cfg):
    """Reconcile complete cached findings against corrected measured checks."""
    from . import motion_review
    raw = dict(summary='已按采集目标姿态及桌面四阶段重算，真实预警保留',
               findings=copy.deepcopy(old['findings']))
    changed = []
    for finding in raw['findings']:
        refs = [r for r in finding.get('evidence_ids', []) if r.startswith('check:')]
        if not any(r[6:] in CHANGED_CHECKS for r in refs) or finding['criterion'].endswith('风险'):
            continue
        checks = [payload['evidence'].get(r) for r in refs]
        if not checks or any(c is None for c in checks):
            continue
        original = copy.deepcopy(finding)
        warnings = [c for c in checks if c['status'] in ('warn', 'fail')]
        if not warnings:
            finding.update(status='pass', reason='所引指标已按采集目标姿态及四阶段标注重算通过')
        else:
            reasons = [reason for c in warnings for reason in c.get('detail', {}).get('issues', [])]
            finding.update(status='suspected', reason='；'.join(reasons) or '所引指标仍有预警，保留人工复核')
        finding.pop('policy_excluded', None)
        changed.append(dict(before=original, after=copy.deepcopy(finding)))
    result = motion_review.validated_result(raw, payload, signature, cfg, old.get('elapsed_seconds', 0))
    result.update(execution='cached_policy_adjusted', recorded_task_projection=dict(
        policy=POLICY, original_signature=old['signature'], changes=changed))
    if 'batch' in old: result['batch'] = old['batch']
    return result


def upgrade_cached(root, cfg, cache, report):
    from .io import read_json, write_json, load
    from . import motion_review
    root, cache = Path(root), Path(cache)
    targets = read_json(root / 'collection_task.json').get('targets', {})
    if not isinstance(targets, dict) or targets.get('prompt_mode') != 'tabletop':
        return report
    d = load(root); updated = correct(d, report)
    if updated is report:
        return report
    motion_path = cache / 'motion/motion_report.json'
    old = read_json(motion_path)
    if old.get('status') in ('pass', 'review') and old.get('signature') == motion_review.prepare(root, report, cfg)[1]:
        payload, signature = motion_review.prepare(root, updated, cfg)
        result = project_review(old, payload, signature, cfg)
        write_json(cache / 'motion/recorded-task-history' / (old['signature'] + '.json'), old)
        write_json(motion_path, result)
    write_json(cache / 'recorded-task-original-checks.json', dict(policy=POLICY,
        checks=[c for c in report['checks'] if c['key'] in CHANGED_CHECKS]))
    return updated
