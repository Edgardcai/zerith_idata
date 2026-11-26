"""Read-only recorded-phase inspection of order recordings; no grasp-only rules."""
import hashlib
import json
import os
from pathlib import Path
import time

import numpy as np
from .io import load, clean, parse_task
from .motion import closure_events

VERSION = 'market_recorded_phases_v2'
# Data identity stays stable across rule revisions, preserving manual reviews.
SIGNATURE_VERSION = 'market_four_phase_v1'
EXPORT_LAYOUT = 'grade_directories_v1'
PHASES = ('navigation', 'grasp', 'transport', 'handover')
LABELS = dict(zip(PHASES, ('导航', '抓取', '运送', '递交'))) | {'return': '返回'}
GRADES = ('A', 'B', 'F')


def read(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def check(key, label, status, detail):
    return dict(key=key, label=label, status=status, detail=clean(detail))


def grade(checks):
    return 'F' if any(c['status'] == 'fail' for c in checks) else 'B' if any(c['status'] == 'warn' for c in checks) else 'A'


def signature(path):
    """File identity/size/nanosecond times; manual grade alone never invalidates QC."""
    path = Path(path)
    values = []
    for name in ('episode.hdf5', 'episode_meta.json', 'collection_task.json', 'subtasks.json',
                 'execution_result.json', 'timing_quality.json',
                 'videos/rs/cam_high.mp4', 'videos/rs/cam_left_wrist.mp4', 'videos/rs/cam_right_wrist.mp4'):
        try:
            st = (path / name).stat()
            values.append((name, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns))
        except OSError:
            values.append((name, None))
    recognition = read(path / 'review.json').get('product_recognition')
    return hashlib.sha256(json.dumps([SIGNATURE_VERSION, values, recognition], sort_keys=True).encode()).hexdigest()


def discover(root, excluded=None):
    root = Path(root)
    if root.is_symlink() or root.name.endswith('.partial'):
        if excluded is not None:
            excluded.append(dict(id='.', reason='符号链接或尚未完成收尾的 .partial 目录'))
        return []
    found = []
    for current, dirs, files in os.walk(root, followlinks=False):
        if excluded is not None:
            excluded.extend(dict(id=str((Path(current)/d).relative_to(root)),
                                 reason='尚未完成收尾的 .partial 目录，不参与转换')
                            for d in sorted(dirs) if d.endswith('.partial') and not d.startswith('.'))
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not d.endswith('.partial')
                         and d not in ('lerobot', 'qc_reports', 'videos', 'product_recognition')
                         and not (Path(current) / d).is_symlink())
        if 'episode.hdf5' not in files:
            continue
        path = Path(current)
        dirs[:] = []
        if (path / 'episode.hdf5').is_symlink():
            continue
        meta = read(path / 'episode_meta.json')
        steps = meta.get('step_index') or []
        inference = meta.get('inference') or {}
        if not isinstance(inference, dict): inference = {}
        if not isinstance(steps, list): steps = []
        if (any(isinstance(s, dict) and s.get('phase') in PHASES for s in steps)
                or inference.get('orderId') or any(p.startswith('market_recordings') for p in path.relative_to(root).parts)
                or root.name.startswith('market_recordings')):
            found.append(path)
    return sorted(found)


def stages(meta, frames=None, transitions=None):
    rows = []
    issues = []
    raw = meta.get('step_index')
    if not isinstance(raw, list):
        raw = []
    cursor = 0
    for index, s in enumerate(raw):
        if not isinstance(s, dict):
            issues.append(f'第 {index + 1} 阶段格式错误'); continue
        phase = s.get('phase', '')
        if not isinstance(phase, str) or not phase.strip():
            issues.append(f'第 {index + 1} 阶段名称无效')
            phase = ''
        b, last = s.get('start_frame_id'), s.get('end_frame_id')
        valid = type(b) is int and type(last) is int and 0 <= b <= last and (frames is None or last < frames)
        if not valid:
            issues.append(f'第 {index + 1} 阶段帧范围无效')
        elif b != cursor:
            issues.append(f'第 {index + 1} 阶段与前段不连续')
        if valid:
            cursor = last + 1
            if s.get('frame_count') not in (None, last - b + 1):
                issues.append(f'第 {index + 1} 阶段帧数与边界不一致')
        rows.append(dict(index=index, phase=phase, label=LABELS.get(phase, phase or '未知阶段'),
                         start=b if valid else None, end=last + 1 if valid else None,
                         prompt=str(s.get('prompt') or ''), outcome=s.get('outcome'),
                         items=s.get('items') or [], valid=valid))
    if not rows:
        issues.append('没有实际阶段标注')
    if frames is not None and cursor != frames:
        issues.append('阶段边界未覆盖全部数据帧')
    if transitions is not None and transitions != [s['end'] for s in rows if s['valid']]:
        issues.append('HDF5 与 episode_meta.json 的阶段边界不一致')
    return rows, issues


def inspect_phases(d):
    n = d['n']; s = d['state']; a = d['action']; t = d['t']
    layout, issues = stages(d['meta'], n, d['transitions'])
    mask = d.get('action_valid')
    all_checks = [check('phase_layout', '实际阶段边界与帧覆盖', 'warn' if issues else 'pass', dict(issues=issues))]
    sequence = tuple(row['phase'] for row in layout)
    standard = sequence in (PHASES, PHASES + ('return',))
    all_checks.append(check('phase_workflow', '流程完整性与重试', 'pass' if standard else 'warn',
                            dict(sequence=list(sequence), issues=[] if standard else
                                 ['非标准流程，可能包含缺失、重试或额外阶段；按实际标注保留全部帧导出'])))
    if d['meta'].get('complete') is not True:
        all_checks.append(check('complete', '录制结束状态', 'warn', dict(issues=['录制未声明完整结束'])))
    results = []
    for row in layout:
        b, e, phase = row['start'], row['end'], row['phase']
        checks = []
        if not row['valid']:
            checks.append(check('range', '阶段帧范围', 'warn', dict(issues=['阶段范围无法用于检查与回放'])))
        else:
            checks.append(check('outcome', '阶段执行记录', 'pass' if row['outcome'] == 'completed' else 'warn',
                                dict(outcome=row['outcome'], note='completed 表示执行记录结束，不证明物理任务成功')))
            step = np.abs(np.diff(s[b:e, :16], axis=0))
            # The two gripper columns use different units from arm joint angles.
            if len(step):
                jumps = np.flatnonzero(np.max(step[:, [*range(7), *range(8, 15)]], axis=1) > .8) + b + 1
                checks.append(check('continuity', '手臂轨迹连续性', 'warn' if len(jumps) else 'pass', dict(bad_frames=jumps[:100])))
            if phase in ('navigation', 'transport', 'return'):
                base = s[b:e, 21:23]
                observed = d['attrs'].get('chassis_recorded') is True and not d['attrs'].get('chassis_zero_filled')
                peak = np.max(np.abs(base), axis=0).tolist() if len(base) else []
                tail = np.max(np.abs(base[-min(15, len(base)):]), axis=0) if len(base) else np.zeros(2)
                checks.append(check('base', '底盘运动与阶段末速度', 'na' if not observed else 'warn' if np.any(tail > [.05, .1]) else 'pass',
                                    dict(peak_velocity=peak, tail_velocity=tail, observed=observed,
                                         note='末段线速度参考 0.05 m/s、角速度 0.1 rad/s；无目标位姿不判定导航到位误差')))
                checks.append(check('arrival', '到达目标位置', 'na', dict(note='当前检查未取得独立目标位姿和定位误差，需人工核对')))
            if phase in ('grasp', 'transport', 'handover'):
                targets = {i.get('hand'): i.get('target') for i in row['items'] if isinstance(i, dict) and i.get('hand') in ('left', 'right')}
                if not targets and phase == 'grasp': targets = parse_task(row['prompt']) or {}
                if not targets:
                    checks.append(check('targets', '操作手与商品标注', 'warn', dict(issues=['没有可用的操作手/商品标注'])))
                for hand, col in [('left', 7), ('right', 15)]:
                    if hand not in targets: continue
                    valid = np.isfinite(a[b:e, col]).all() and (mask is None or mask[b:e, col].all())
                    if not valid:
                        checks.append(check('gripper_'+hand, hand+' 夹爪', 'warn', dict(issues=['阶段内有未观测指令，无法完整判断夹爪动作']))); continue
                    # Include previous frame so transitions exactly at a boundary are counted.
                    start = max(0, b - 1)
                    closed = [f + start for f in closure_events(a[start:e, col], .1, .8) if f + start >= b]
                    opened = [f + start for f in closure_events(-a[start:e, col], -.8, -.1) if f + start >= b]
                    feedback = [f + start for f in closure_events(s[start:e, col], .08, .1) if f + start >= b]
                    warnings = []
                    if phase == 'grasp':
                        if len(closed) != 1: warnings.append(f'抓取阶段闭合 {len(closed)} 次，请核对重试或已有持物')
                        if len(feedback) != 1: warnings.append(f'抓取反馈闭合 {len(feedback)} 次，需复核')
                        if len(closed) == len(feedback) == 1 and not -2 <= feedback[0]-closed[0] <= 15: warnings.append('闭合指令与反馈时间差异常')
                    elif phase == 'transport':
                        if opened or np.any(a[b:e, col] <= .1): warnings.append('运送阶段存在张开指令，需核对是否提前释放')
                    elif phase == 'handover' and not opened:
                        warnings.append('递交阶段未观察到明确张开转换，需确认实际交接')
                    checks.append(check('gripper_'+hand, ('左手' if hand == 'left' else '右手')+'夹爪时序', 'warn' if warnings else 'pass',
                                        dict(target=targets[hand], close_frames=closed, open_frames=opened, feedback_close_frames=feedback, issues=warnings)))
            checks.append(check('physical', '视频人工确认', 'na', dict(note={
                'navigation': '核对实际到位、障碍与碰撞', 'grasp': '核对商品、抓稳及抽出过程',
                'transport': '核对持物与掉落', 'handover': '核对递交对象、释放与真实接收',
                'return': '核对返回初始位置、障碍与碰撞'}.get(phase, '核对额外阶段'))))
        results.append(dict(row, checks=checks, auto_grade=grade(checks), physical_review_required=True))
    for phase in PHASES:
        if phase not in [r['phase'] for r in layout]:
            results.append(dict(index=None, phase=phase, label=LABELS[phase], start=None, end=None, valid=False, missing=True,
                                prompt='', checks=[check('missing', '阶段缺失', 'warn', dict(issues=['没有记录此阶段']))], auto_grade='B', physical_review_required=True))
    return results, all_checks


def inspect(path, progress=lambda _: None, previous=None):
    from .checks import raw_checks
    before = signature(path)
    progress('读取实际阶段数据')
    d = load(path)
    if d['meta'].get('outcome') in (None, '', 'recording'):
        raise ValueError('录制未结束，暂不检查')
    phase_results, checks = inspect_phases(d)
    # Reuse full image/video decoding and structural checks, never the grasp-only verdict.
    # Only phase rules changed: unchanged v1 media/numeric evidence is reusable.
    reuse = (isinstance(previous, dict) and previous.get('version') == 'market_four_phase_v1'
             and previous.get('signature') == before)
    raw = previous if reuse else raw_checks(path, progress=progress)
    keep = {'state', 'action', 'finite', 'timestamps', 'fps', 'motion', 'schema', 'action_coverage', 'chassis_recording', 'repair_records'}
    checks = [c for c in raw['checks'] if c['key'] in keep or c['key'].startswith(('hdf5_', 'depth_', 'video_', 'quality_'))] + checks
    review = read(Path(path) / 'review.json')
    recognition = review.get('product_recognition') or {}
    if review.get('episode_uuid') and review.get('episode_uuid') != d['meta'].get('episode_id'):
        recognition = {}
        checks.append(check('review_identity', '评级来源标识', 'warn', dict(issues=['review.json 与录制 UUID 不一致，未采用原评级和 YOLO 结果'])))
    if not isinstance(recognition, dict): recognition = {}
    for phase in phase_results:
        if phase['phase'] == 'grasp':
            phase['yolo'] = recognition
            if recognition.get('status') == 'completed' and recognition.get('grade') == 'F':
                phase['checks'].append(check('yolo', '已有 YOLO 商品识别', 'warn', dict(reason=recognition.get('reason'), note='自动视觉疑似失败，需人工复核')))
                phase['auto_grade'] = grade(phase['checks'])
    if before != signature(path): raise ValueError('检查期间源数据发生变化，请重新质检')
    auto = grade(checks + [c for p in phase_results for c in p['checks']])
    return clean(dict(version=VERSION, signature=before, created=time.time(), frames=d['n'],
                      reused_structure_report=previous.get('created') if reuse else None,
                      auto_grade=auto, hard_fail=any(c['status']=='fail' for c in checks), checks=checks, phases=phase_results,
                      scope='结构、全帧媒体解码、数值及阶段动作；物理成功由人工视频复核'))
