import cv2
import h5py
import numpy as np

from .io import (
    CAMS,
    Path,
    clean,
    gripper_events,
    load,
    normalized_task,
    parse_task,
    prompt_issues,
    stationary_spans,
    video_path,
)
from .motion import (
    RULE_VERSION,
    arm_and_posture_checks,
    contextual_checks,
    failure_reason,
)

LABELS = {
    "schema": "HDF5 结构",
    "state": "State 维度",
    "action": "Action 维度",
    "finite": "数值有效性",
    "timestamps": "时间戳",
    "fps": "实际帧率",
    "duration": "轨迹时长",
    "motion": "State 跳变",
    "joint": "关节运动",
    "stationary": "连续静止",
    "gripper": "夹爪活动",
    "gripper_feedback": "独立夹爪反馈",
    "prompt": "提示词格式",
    "metadata": "标注一致性",
    "stages": "阶段标注",
    "repair_records": "连续补帧 / 复用",
}


def numeric_checks(s, a, t, threshold=40):
    n = len(s)
    checks = []

    def add(k, status, detail, penalty=0):
        checks.append(
            dict(
                key=k,
                label=LABELS.get(
                    k,
                    next(
                        (
                            label
                            + " · "
                            + cam.replace("cam_high", "头部")
                            .replace("cam_left_wrist", "左腕")
                            .replace("cam_right_wrist", "右腕")
                            for prefix, label in [
                                ("hdf5_", "内嵌图像完整性"),
                                ("video_", "外部视频完整性"),
                                ("depth_", "深度图像完整性"),
                                ("quality_", "视觉质量"),
                            ]
                            for cam in CAMS
                            if k == prefix + cam
                        ),
                        k,
                    ),
                ),
                status=status,
                detail=clean(detail),
                score_delta=-penalty if status not in ("pass", "na") else 0,
            )
        )

    add(
        "state",
        "pass" if s.shape == (n, 23) and n > 0 else "fail",
        {"shape": s.shape},
        35,
    )
    add(
        "action",
        "pass" if a.shape == (n, 23) and n > 0 else "fail",
        {"shape": a.shape},
        25,
    )
    finite = all(np.isfinite(v).all() for v in [s, a, t])
    invalid={key:np.argwhere(~np.isfinite(v)).tolist() for key,v in [('state',s),('action',a),('timestamp',t)]}
    add("finite", "pass" if finite else "fail", {"finite": bool(finite),"invalid":invalid}, 30)
    if n < 2:
        add("timestamps", "fail", {"frames": n, "minimum": 2}, 25)
    if len(t) != n:
        add("timestamps", "fail", {"timestamp_frames": len(t), "expected": n}, 25)
    if n < 2 or s.shape != (n, 23) or a.shape != (n, 23) or len(t) != n or not finite:
        return checks
    dt = np.diff(t)
    fps = (n - 1) / (t[-1] - t[0]) if t[-1] > t[0] else 0
    bad = np.flatnonzero((dt <= 0) | (dt > 0.1 + 1e-7)) + 1
    gaps = [dict(previous_frame=int(i-1), frame=int(i),
                 previous_seconds=float(t[i-1]-t[0]), seconds=float(t[i]-t[0]),
                 interval_seconds=float(dt[i-1])) for i in bad]
    issues = [f"第 {g['previous_frame']}→{g['frame']} 帧，间隔 {g['interval_seconds']:.6f} 秒"
              for g in gaps[:6]]
    if len(gaps) > 6:
        issues.append(f"另有 {len(gaps)-6} 处，详情可查看全部位置")
    add("timestamps", "warn" if gaps else "pass", dict(
        max_gap_seconds=float(max(dt)), min_gap_seconds=float(min(dt)),
        normal_interval_seconds=1/30, warning_limit_seconds=0.1,
        bad_frames=bad.tolist(), gaps=gaps, issues=issues,
        note="时间间隔异常仅预警，默认 B；原始时钟保留在来源信息中"), 0)
    add("fps", "pass" if fps >= 29 else "warn", dict(
        actual=fps, minimum=29,
        issues=[] if fps >= 29 else [f"实际 {fps:.2f} fps，低于参考 29 fps，按预警处理"]), 0)
    add(
        "duration",
        "pass" if 2 <= t[-1] - t[0] <= 40 else "warn",
        {"seconds": t[-1] - t[0]},
        3,
    )
    ds = abs(np.diff(s, axis=0))
    j = ds[:, list(range(7)) + list(range(8, 15))]
    d = j.max(axis=1)
    add(
        "motion",
        "pass" if d.max() <= 0.8 else "warn",
        {"max_step": d.max(), "bad_frames": (np.where(d > 0.8)[0] + 1).tolist()},
        25,
    )
    add(
        "joint",
        "pass" if j.max() <= 0.8 and j.mean() >= 0.0001 else "warn",
        {"max_rad": j.max(), "mean_rad": j.mean()},
        4,
    )
    checks.extend(arm_and_posture_checks(s, a, t))
    runs = stationary_spans(s, a)
    long = [dict(start=b, end=e, frames=e - b) for b, e in runs if e - b > threshold]
    add(
        "stationary",
        "warn" if long else "pass",
        {
            "threshold": threshold,
            "max_frames": max([e - b for b, e in runs], default=0),
            "intervals": long,
        },
        15,
    )
    activity = {
        hand: dict(
            state_range=float(np.ptp(s[:, col])),
            action_range=float(np.ptp(a[:, col])),
            events=[e for e in gripper_events(a) if e["hand"] == hand],
        )
        for hand, col in [("left", 7), ("right", 15)]
    }
    add(
        "gripper",
        "pass"
        if any(
            v["state_range"] > 0.05 or v["action_range"] > 0.05
            for v in activity.values()
        )
        else "warn",
        activity,
        2,
    )
    return checks


def raw_checks(root, threshold=40, progress=lambda _: None):
    checks = []

    def add(k, status, detail, penalty=0):
        checks.append(
            dict(
                key=k,
                label=LABELS.get(
                    k,
                    next(
                        (
                            label
                            + " · "
                            + cam.replace("cam_high", "头部")
                            .replace("cam_left_wrist", "左腕")
                            .replace("cam_right_wrist", "右腕")
                            for prefix, label in [
                                ("hdf5_", "内嵌图像完整性"),
                                ("video_", "外部视频完整性"),
                                ("depth_", "深度图像完整性"),
                                ("quality_", "视觉质量"),
                            ]
                            for cam in CAMS
                            if k == prefix + cam
                        ),
                        k,
                    ),
                ),
                status=status,
                detail=clean(detail),
                score_delta=-penalty if status not in ("pass", "na") else 0,
            )
        )

    try:
        d = load(root)
    except Exception as e:
        add("schema", "fail", {"error": str(e)}, 35)
        return summary(checks, 0)
    n = d["n"]
    simulation = bool(d.get("source_format"))
    if simulation:
        from .simulation import numeric
        checks.extend(numeric(d, threshold))
    else:
        checks.extend(numeric_checks(d["state"], d["action"], d["t"], threshold))
    checks.extend(contextual_checks(d, root))
    if simulation:
        add('schema', 'pass', {'format': d['source_format'], 'frames': n, 'vector_components_checked': True})
        for cam in CAMS:
            add('hdf5_'+cam, 'na', {'note': '此仿真格式仅保存外部视频，完整性在视频项目检查'})
        if not d.get('gripper_feedback_available',True):
            add('gripper_feedback', 'na', {'note': 'State 夹爪复制 Action，无法作为独立物理反馈；指令次数和阶段仍检查'})
    else:
        with h5py.File(Path(root) / "episode.hdf5") as f:
            mismatches = []

            def visit(k, v):
                if (
                    isinstance(v, h5py.Dataset)
                    and k.startswith(("action/", "observation/", "timestamp/"))
                    and (not v.shape or v.shape[0] != n)
                ):
                    mismatches.append(k)

            f.visititems(visit)
            attrs = d["attrs"]
            problems = []
            if mismatches:
                problems.append({"length_mismatch": mismatches})
            for key, expect in [
                ("total_frames", n),
                ("control_frequency", 30),
                ("action_mode", "absolute"),
            ]:
                if attrs.get(key) != expect:
                    problems.append({key: attrs.get(key), "expected": expect})
            add("schema", "fail" if mismatches else "pass", {"length_mismatch": mismatches}, 35)
            add("metadata", "warn" if problems else "pass", {"issues": [str(p) for p in problems if "length_mismatch" not in p]}, 0)
            for cam in CAMS:
                progress("完整解码 " + cam)
                key = f"observation/images/rs/{cam}/color"
                bad = []
                if key not in f:
                    bad = ["missing"]
                else:
                    for i, buf in enumerate(f[key]):
                        image = cv2.imdecode(
                            np.asarray(buf, dtype=np.uint8), cv2.IMREAD_COLOR
                        )
                        if image is None:
                            bad.append(i)
                add(
                    "hdf5_" + cam,
                    "fail" if bad or key not in f or len(f[key]) != n else "pass",
                    {"bad_frames": bad, "expected_frames": n},
                    30,
                )
                if bool(attrs.get("depth_recorded")):
                    dk = f"observation/images/rs/{cam}/depth"
                    db = []
                    if dk not in f:
                        db = ["missing"]
                    else:
                        for i, buf in enumerate(f[dk]):
                            img = cv2.imdecode(
                                np.asarray(buf, dtype=np.uint8), cv2.IMREAD_UNCHANGED
                            )
                            if img is None:
                                db.append(i)
                    add(
                        "depth_" + cam,
                        "fail" if db or dk not in f or len(f[dk]) != n else "pass",
                        {"bad_frames": db},
                        30,
                    )
    visual = {}
    for cam in CAMS:
        from .io import open_video
        cap = open_video(video_path(root, cam))
        count = 0
        bad = []
        blur = []
        freeze = []
        prev = None
        while True:
            ok, img = cap.read()
            if not ok:
                break
            gray = cv2.cvtColor(cv2.resize(img, (160, 120)), cv2.COLOR_BGR2GRAY)
            mean = float(gray.mean())
            if mean < 5 or mean > 250:
                bad.append(count)
            if cv2.Laplacian(gray, cv2.CV_64F).var() < 8:
                blur.append(count)
            if prev is not None and np.mean(abs(gray.astype(float) - prev)) < 0.05:
                freeze.append(count)
            prev = gray.astype(float)
            count += 1
        cap.release()
        add(
            "video_" + cam,
            "pass" if count == n and n > 0 else "fail",
            {"decoded_frames": count, "expected": n},
            35,
        )
        visual[cam] = dict(dark_or_overexposed=bad, blur=blur, possible_freeze=freeze)
        add(
            "quality_" + cam,
            "warn" if len(bad) > n * 0.05 or len(blur) > n * 0.2 else "pass",
            {
                "unusable_frames": len(bad),
                "blur_frames": len(blur),
                "possible_freeze": len(freeze),
            },
            3,
        )
    task = d["task"]
    targets = parse_task(task)
    norm = normalized_task(task)
    add(
        "prompt",
        "pass" if targets else "warn",
        {"original": task, "normalized_candidate": norm if parse_task(norm) else None, "issues": prompt_issues(task)},
        5,
    )
    mism = list(d.get("metadata_issues", []))
    ct = d["collection"]
    ctask = ct.get("config", {}).get("task_name")
    if ctask and ctask != task:
        if parse_task(normalized_task(ctask)) != parse_task(normalized_task(task)) or not targets:
            mism.append(f"任务冲突：HDF5={task}；采集配置={ctask}")
    if targets:
        for h, v in ct.get("targets", {}).items():
            if h in ("left", "right") and v and targets.get(h) != v:
                mism.append(f"{'左手' if h=='left' else '右手'}目标冲突：提示词={targets.get(h)}；采集配置={v}")
    add("metadata", "warn" if mism else "pass", {"issues": mism}, 3)
    tr = d["transitions"]
    ends = [int(x) for x in tr if 0 < int(x) <= n]
    valid = bool(ends) and ends == sorted(set(ends)) and ends[-1] == n
    add("stages", "pass" if valid else "warn", {"boundaries": tr, "frames": n}, 3)
    if simulation:
        checks[-1]["detail"]["issues"] = d["stage_issues"]
    repair = d["meta"].get("camera_sync_summary", {}).get("camera_color", [])
    if not repair:
        repair = [
            v["sync_stats"]
            for v in d["meta"].get("available_videos", [])
            if isinstance(v, dict) and isinstance(v.get("sync_stats"), dict)
        ]
    max_run = max(
        [int(v.get("max_consecutive_repair_or_reuse_frames", 0)) for v in repair],
        default=0,
    )
    add(
        "repair_records",
        "na" if not repair else "warn" if max_run > 2 else "pass",
        {
            "records": repair,
            "maximum_consecutive": max_run,
            "threshold": 2,
            "note": "无记录不检测" if not repair else "连续修复 / 复用超过 2 帧告警",
        },
        3,
    )
    report = summary(checks, n)
    if simulation:
        report['source_format']=d['source_format']
        report['source_semantics']=dict(timestamp=d['timestamp_policy'],gripper_feedback_available=d['gripper_feedback_available'],
            original_task=d['original_task'],original_stages=d['original_stages'],stage_issues=d['stage_issues'])
    report.update(
        task=task, events=gripper_events(d["action"]), transitions=tr, visual=visual
    )
    return report


def summary(checks, n):
    return dict(
        version=RULE_VERSION,
        frames=n,
        checks=checks,
        score=max(0, 100 + sum(c.get("score_delta", 0) for c in checks)),
        hard_fail=any(c["status"] == "fail" for c in checks),
        failure_reason=failure_reason(checks),
    )
