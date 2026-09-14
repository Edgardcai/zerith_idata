"""Auditable Zerith Action/State checks; VLM consumes these measurements."""

import re
from pathlib import Path

import numpy as np

from .io import NAMES, clean, normalized_task, parse_task, read_json

RULE_VERSION = "zerith_qc_5"
EPS = 1e-7  # float32 round-trip tolerance, not a physical tolerance


def result(key, label, status, detail, issues=()):
    return dict(
        key=key,
        label=label,
        status=status,
        detail=clean(dict(detail, issues=list(issues))),
        score_delta=-25 if status == "fail" else 0,
    )


def failure_reason(checks):
    reasons = []
    for c in checks:
        if c["status"] != "fail":
            continue
        detail = c["detail"]
        description = ""
        if isinstance(detail, dict):
            description = "、".join(detail.get("issues", [])) or detail.get("error", "")
            if not description and detail.get("bad_frames"):
                description = f"异常位置 {detail['bad_frames'][:5]}"
            if not description and "decoded_frames" in detail:
                description = f"解码 {detail['decoded_frames']} 帧，应为 {detail['expected']} 帧"
            if not description and c["key"] == "fps":
                description = f"实际 {detail['actual']:.2f} fps，最低 {detail['minimum']} fps"
            if not description and c["key"] == "finite":
                description = "包含 NaN 或 Inf"
        reasons.append(c["label"] + ("：" + str(description) if description else ""))
    return "；".join(reasons)


def source_height(root):
    """Resolve the original dataset suffix, including after repair/renumbering."""
    root = Path(root)
    seen = set()
    for _ in range(8):
        if str(root) in seen:
            return dict(error="来源目录引用成环，无法确定升降高度")
        seen.add(str(root))
        prov = read_json(root / "provenance.json") or {}
        if not prov.get("source"):
            break
        root = Path(prov["source"])
    from .io import is_simulation
    if is_simulation(root):
        from .simulation import height_reference
        return height_reference(root)
    # All episodes in a dataset share the height declared by its first-level directory.
    from .config import REAL_SOURCE_ROOT
    base = REAL_SOURCE_ROOT
    candidates = [root, *root.parents]
    if root.is_relative_to(base) and root != base:
        candidates = [base / root.relative_to(base).parts[0]]
    for p in candidates:
        if re.match(r"episode(?:_|$)", p.name):
            continue
        match = re.search(r"_(-?\d+(?:\.\d+)?)$", p.name)
        if match:
            if re.search(r"_-?\d+(?:\.\d+)?$", p.name[: match.start()]):
                return dict(
                    directory=str(p),
                    error="目录末尾含多个高度数值，每组只能指定一个高度",
                )
            return dict(directory=str(p), expected_m=float(match.group(1)))
    return dict(error="目录末尾缺少升降高度，无法核对预期值")


def arm_and_posture_checks(s, a, t):
    checks = []
    for source, arr in [("state", s), ("action", a)]:
        for hand, columns in [("left", range(7)), ("right", range(8, 15))]:
            delta = abs(np.diff(arr[:, list(columns)], axis=0))
            rows, cols = np.where(delta > 0.8 + EPS)
            jumps = [
                dict(
                    frame=int(r + 1),
                    joint=NAMES[columns[c]],
                    previous=float(arr[r, columns[c]]),
                    value=float(arr[r + 1, columns[c]]),
                    delta_rad=float(delta[r, c]),
                )
                for r, c in zip(rows, cols)
            ]
            issues = []
            if jumps:
                issues.append(
                    f"{len(jumps)} 处关节单步变化超过 0.8 rad，首处第 {jumps[0]['frame']} 帧 {jumps[0]['joint']}"
                )
            checks.append(
                result(
                    f"arm_{source}_{hand}",
                    f"{source.title()} · {'左' if hand == 'left' else '右'}臂连续性",
                    "fail" if issues else "pass",
                    dict(
                        max_step_rad=float(delta.max()),
                        jump_limit_rad=0.8,
                        max_gap_seconds=float(np.diff(t).max()),
                        gap_limit_seconds=0.1,
                        bad_frames=sorted(set(v["frame"] for v in jumps)),
                        jumps=jumps,
                        note="小幅抖动不判失败；缺帧依据帧数和时间戳，重复关节值本身不证明丢帧",
                    ),
                    issues,
                )
            )
        channels = []
        issues = []
        for col in range(17, 21):
            v = arr[:, col]
            mean = float(v.mean())
            q01, q99 = np.quantile(v, [0.01, 0.99]).tolist()
            ok = abs(mean) <= 0.02 + EPS and max(abs(q01), abs(q99)) <= 0.02 + EPS
            channels.append(
                dict(
                    joint=NAMES[col],
                    mean_rad=mean,
                    q01_rad=q01,
                    q99_rad=q99,
                    status="pass" if ok else "warn",
                )
            )
            if not ok:
                issues.append(
                    f"{NAMES[col]} 均值 {mean:.5f}、Q01 {q01:.5f}、Q99 {q99:.5f} rad 超出范围"
                )
        checks.append(
            result(
                f"posture_{source}",
                f"{source.title()} · 腰部 / 头部零位",
                "warn" if issues else "pass",
                dict(
                    channels=channels,
                    mean_range_rad=[-0.02, 0.02],
                    quantile_range_rad=[-0.02, 0.02],
                ),
                issues,
            )
        )
    return checks


def lift_check(s, a, root):
    expected = source_height(root)
    if "error" in expected:
        # Missing reference is explicitly incomplete; never infer it from observations.
        return result(
            "lift_height", "升降柱高度", "fail", expected, [expected["error"]]
        )
    height = expected["expected_m"]
    channels, issues = {}, []
    for source, arr in [("state", s), ("action", a)]:
        values = arr[:, 16]
        bad = np.flatnonzero(abs(values - height) > 0.02 + EPS).tolist()
        channels[source] = dict(
            mean_m=float(values.mean()),
            min_m=float(values.min()),
            max_m=float(values.max()),
            max_deviation_m=float(abs(values - height).max()),
            bad_frames=bad,
        )
        if bad:
            issues.append(
                f"{source.title()} 升降柱应为 {height:g}±0.02 m，{len(bad)} 帧超差，首处第 {bad[0]} 帧（{values[bad[0]]:.5f} m）"
            )
    return result(
        "lift_height",
        "升降柱高度",
        "fail" if issues else "pass",
        dict(expected, tolerance_m=0.02, channels=channels),
        issues,
    )


def closure_events(values, open_limit, close_limit):
    """Schmitt trigger with two-frame confirmation; held at frame 0 is not a closure."""
    closed = bool(values[0] >= close_limit)
    armed = False
    events = []
    pending = None
    for i, value in enumerate(values):
        target = (
            False if value <= open_limit else True if value >= close_limit else None
        )
        if target is None or (target == closed and (closed or armed)):
            pending = None
            continue
        if pending is None or pending[0] != target:
            pending = (target, i)
            continue
        frame = pending[1]
        if target and armed:
            events.append(frame)
        if not target:
            armed = True
        closed = target
        pending = None
    return events


def gripper_check(s, a, task, transitions, stages=None, feedback_available=True):
    n = len(s)
    targets = parse_task(normalized_task(task))
    hands = list(targets) if targets else ["left", "right"]
    if stages is None:
        ends = list(transitions)
        if (
            len(ends) == len(hands)
            and ends
            and ends[-1] == n
            and all(isinstance(e, (int, np.integer)) for e in ends)
            and all(b < e for b, e in zip([0] + ends[:-1], ends))
        ):
            stages = [
                dict(hand=h, start=b, end=e)
                for h, b, e in zip(hands, [0] + ends[:-1], ends)
            ]
    valid_stages = bool(stages) and [st["hand"] for st in stages] == hands
    if valid_stages:
        valid_stages = (
            stages[0]["start"] == 0
            and stages[-1]["end"] == n
            and all(
                st["start"] < st["end"]
                and (i == 0 or stages[i - 1]["end"] == st["start"])
                for i, st in enumerate(stages)
            )
        )
    issues, channels, bad_frames = [], {}, []
    for hand, col in [("left", 7), ("right", 15)]:
        action = closure_events(a[:, col], 0.1, 0.8)
        state = closure_events(s[:, col], 0.05, 0.1) if feedback_available else []
        expected = 1 if hand in hands else 0
        channels[hand] = dict(
            action_close_frames=action,
            state_close_frames=state,
            expected_closures=expected,
        )
        for name, events in ([("Action", action), ("State", state)] if feedback_available else [("Action", action)]):
            if len(events) != expected:
                issues.append(
                    f"{'左' if hand == 'left' else '右'}爪 {name} 闭合 {len(events)} 次，应为 {expected} 次"
                )
                bad_frames.extend(events)
        # Allow physical feedback up to 0.5 s later (and two frames timestamp skew).
        if feedback_available and len(action) == len(state) == expected:
            for ac, sc in zip(action, state):
                if not -2 <= sc - ac <= 15:
                    issues.append(
                        f"{hand} Action 第 {ac} 帧闭合，State 第 {sc} 帧响应，超出允许延迟"
                    )
                    bad_frames.extend([ac, sc])
        if valid_stages:
            for name, events in ([("Action", action), ("State", state)] if feedback_available else [("Action", action)]):
                for frame in events:
                    st = next(st for st in stages if st["start"] <= frame < st["end"])
                    if st["hand"] != hand:
                        issues.append(
                            f"{hand} {name} 第 {frame} 帧闭合落在 {st['hand']} 阶段"
                        )
                        bad_frames.append(frame)
    status = "fail" if issues else "pass"
    if not valid_stages:
        issues.append("阶段标注缺失或不完整，需补全后核对闭合阶段")
        if status == "pass":
            status = "warn"
    return result(
        "gripper_sequence",
        "夹爪次数 / 阶段 / 反馈",
        status,
        dict(
            channels=channels,
            stages=stages,
            bad_frames=sorted(set(bad_frames)),
            thresholds=dict(
                action_open=0.1,
                action_close=0.8,
                state_open=0.05,
                state_close=0.1,
                confirm_frames=2,
                max_response_frames=15,
            ),
            note="初始已闭合不计作一次闭合；闭合为数值增大，State 接触后不要求达到 0.56",
        ),
        issues,
    )


def contextual_checks(d, root, stages=None):
    if (
        any(
            d[k].shape != (d["n"], 23) or not np.isfinite(d[k]).all()
            for k in ("state", "action")
        )
        or d["n"] < 2
    ):
        return []
    return [
        gripper_check(d["state"], d["action"], d["task"], d["transitions"], stages, d.get("gripper_feedback_available", True)),
        lift_check(d["state"], d["action"], root),
    ]


def warning_messages(report):
    out = []
    for c in report.get("checks", []):
        if c["status"] != "warn":
            continue
        d = c.get("detail", {})
        issues = d.get("issues", []) if isinstance(d, dict) else []
        out.append(c["label"] + ("：" + "、".join(issues) if issues else ""))
    return list(dict.fromkeys(out))


def warning_grade(decision, report):
    """Warnings default to B; uncertain visual findings still require review."""
    out = dict(decision)
    warnings = warning_messages(report)
    if warnings and out.get("grade") in ("A", "B"):
        out["grade"] = "B"
        text = "预警默认 B：" + "；".join(warnings)
        if text not in out.get("reason", ""):
            out["reason"] = (out.get("reason", "") + "；" + text).strip("；")
    return out
