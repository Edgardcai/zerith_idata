"""Translate full-process checks to the existing workbench's report contract."""
import json


def presentation(row):
    items = []
    report = row.get('report') or {}
    groups = [('全流程', report.get('checks', []))]
    groups += [(p['label'], p.get('checks', [])) for p in row.get('phases', [])]
    for source, checks in groups:
        for c in checks:
            detail = c.get('detail') or {}
            text = '；'.join(map(str, detail.get('issues') or [])) or detail.get('note') or detail.get('reason') or json.dumps(detail, ensure_ascii=False)
            frames = detail.get('bad_frames') or detail.get('frames') or []
            frames = [f for f in frames if type(f) is int and f >= 0] if isinstance(frames, list) else []
            items.append(dict(label=c.get('label') or c['key'], source=source, status=c['status'],
                status_label={'pass':'通过','warn':'待复核','fail':'失败','na':'未判定'}.get(c['status'],c['status']),
                text=text, problem=c['status'] in ('warn','fail'), frames=frames))
    return dict(items=items, problem_count=sum(i['problem'] for i in items),
        summary=row.get('summary') or '尚无全流程质检报告', task='导航 → 抓取 → 运送 → 递交')


def metrics(report):
    checks = {c['key']:c.get('detail') or {} for c in report.get('checks', [])}
    result = {}
    for key, check, prop in [('fps','fps','actual'),('max_state_step','motion','max_step')]:
        if prop in checks.get(check,{}): result[key]=checks[check][prop]
    for kind in ('state','action'):
        shape=checks.get(kind,{}).get('shape') or []
        if len(shape)==2:result[kind+'_dim']=shape[1]
    videos=[c for k,c in checks.items() if k.startswith('video_')]
    if videos:result['camera_view_count']=sum(bool(c.get('decoded_frames',c.get('frames',0))) for c in videos)
    if result.get('fps') and report.get('frames'):result['duration_sec']=(report['frames']-1)/result['fps']
    return result


def grade_snapshot(row):
    return dict(root=row['path'],episode_name=row['name'],current_episode_name=row['name'],
        collection_episode_name=row['uuid'],collection_grade=row['source_grade'],qc_grade=row['auto_grade'],
        current_grade=row['effective_grade'],manual_grade=row['manual_grade'],manual_note=row.get('manual_note',''),
        manual_problem='',review_pending=row['manual_pending'],
        grade_changed=bool(row['auto_grade'] and row['source_grade'] and row['auto_grade']!=row['source_grade']),
        revision=row['revision'],qc_presentation=presentation(row),reason=row.get('summary',''),
        recording_grade_priority=False,review_required=row['manual_pending'],busy=False)
