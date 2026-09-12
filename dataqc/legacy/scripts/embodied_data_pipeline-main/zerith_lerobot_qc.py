"""Zerith LeRobot checks using the same profile and motion rules as HDF5 QC.

Only LeRobot metadata and parquet are read. HDF5 provenance paths are
not required. Temporal checks always inspect complete selected episodes.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import math
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import lerobot_cross_platform as cp
from quality_pipeline.episode_io import EpisodeData, StateFrame
from quality_pipeline.profiles import RobotProfile, load_profile
from quality_pipeline.qc import run_quality_checks


PROFILE_PATH = Path(__file__).parent / "robot_profiles" / "zerith.yaml"
PROMPT_TEMPLATES = {
    "twohand": "Grasp {item} with the left hand and then grasp {item} with the right hand",
    "left_hand": "Grasp {item} with the left hand",
    "righthand": "Grasp {item} with the right hand",
}
PROMPT_STANDARD = "只允许物品名称变化；双臂先左后右，左臂/右臂使用对应单臂模板；大小写、动作词、连接词及手别须符合模板"
# Do not let an extra/malformed action clause be swallowed as a product name.
_PRODUCT = r"(?!\s)(?:(?!\b(?:with|then|grasp)\b)[^\r\n])+?(?<!\s)"
PROMPT_PATTERNS = {mode: re.compile(re.escape(template).replace(re.escape("{item}"), _PRODUCT))
                   for mode, template in PROMPT_TEMPLATES.items()}


def prompt_mode(path: Path) -> str | None:
    aliases = {"twohands": "twohand", "twohand": "twohand", "left_hand": "left_hand", "lefthand": "left_hand",
               "righthand": "righthand", "right_hand": "righthand"}
    return next((aliases[part] for part in reversed(path.parts) if part in aliases), None)


def prompt_format(text: str, _item_names=None) -> str:
    return next((PROMPT_TEMPLATES[mode] for mode, pattern in PROMPT_PATTERNS.items()
                 if pattern.fullmatch(text)), text)


def prompt_error(text: str, mode: str | None) -> str:
    matched = next((key for key, pattern in PROMPT_PATTERNS.items() if pattern.fullmatch(text)), None)
    if matched and (mode is None or matched == mode):
        return ""
    expected = [PROMPT_TEMPLATES[mode]] if mode else list(PROMPT_TEMPLATES.values())
    reason = "指令手别或单双臂类型与数据集目录不符" if matched else "指令不符合固定模板（检查 Grasp/grasp、with the、and then、左右手顺序及多余文字；物品名称不限）"
    return reason + "；要求：" + " / ".join(expected)


def prompt_report(descriptors: list[dict]) -> dict:
    report = cp._prompt_report(descriptors, normalizer=prompt_format)
    details = []
    for item in report["formats"]:
        item.update(status="pass", warnings=[], platform_statuses={p: "pass" for p in cp.PLATFORMS})
        for platform in cp.PLATFORMS:
            for source in item[platform]:
                reason = prompt_error(source["task"], prompt_mode(Path(source["path"])))
                if reason:
                    item["status"] = item["platform_statuses"][platform] = "fail"
                    item["warnings"].append(reason)
                    details.append({"category": "prompt", "project": "Prompt 格式", "target": item["template"],
                                    "status": "fail", "reason": reason, "datasets": [source]})
        item["warnings"] = list(dict.fromkeys(item["warnings"]))
    for dataset in report["datasets"]:
        if not dataset["tasks"]:
            details.append({"category": "prompt", "project": "Prompt 格式", "target": "缺少提示词",
                            "status": "warn", "reason": "数据集没有有效提示词，请检查 tasks.jsonl 和 episodes.jsonl",
                            "datasets": [dataset]})
    report.update(validation_mode="fixed_templates", standard=PROMPT_STANDARD,
                  status=cp._merge_status(*(d["status"] for d in details)),
                  warnings=list(dict.fromkeys(d["reason"] for d in details)), issue_details=details,
                  only_simulation=[], only_real=[],
                  platform_statuses={p: cp._merge_status(*(d["status"] for d in details
                      if any(s["platform"] == p for s in d["datasets"]))) for p in cp.PLATFORMS})
    return report


CHECK_LABELS = {
    "state_dim": "State 维度", "action_dim": "Action 维度",
    "finite_values": "有限数值", "timestamp_monotonic": "时间戳连续性",
    "fps": "帧率",
    "duration": "轨迹时长", "motion_stability": "运动稳定性",
    "action_stationary_frames": "连续静止帧", "gripper_activity": "夹爪活动",
    "task_consistency": "Prompt 元数据格式", "episode_structure": "Episode 结构",
}


def qc_profile(threshold: int) -> RobotProfile:
    if isinstance(threshold, bool) or str(threshold) not in {"20", "40", "60"}:
        raise ValueError("零次方静止帧阈值必须为 20、40 或 60")
    profile = load_profile(PROFILE_PATH)
    raw = copy.deepcopy(profile.raw)
    # Retain Zerith motion semantics, but omit checks of raw HDF5 schema/videos.
    raw["adapter"] = "zerith"
    raw["processing"]["rtml"]["global_constraints"]["max_stationary_action_frames"] = int(threshold)
    return RobotProfile(profile.path, raw)


def contract_for(profile: RobotProfile) -> dict[str, Any]:
    labels = {"left_arm": "左臂", "right_arm": "右臂", "left_gripper": "左夹爪",
              "right_gripper": "右夹爪", "lift": "升降柱", "waist_pitch": "腰部俯仰",
              "waist_yaw": "腰部偏航", "head_yaw": "头部偏航", "head_pitch": "头部俯仰",
              "base_velocity": "底盘"}
    groups, layouts = [], {}
    for vector in ("action", "state"):
        names = [""] * int(profile.raw[vector]["dim"])
        for item in profile.raw[vector]["layout"]:
            name = item["name"]
            indices = list(range(*item["slice"])) if "slice" in item else [item["index"]]
            if name.endswith("_arm"):
                expanded = [f"{name.split('_')[0]}_joint_{i + 1}" for i in range(len(indices))]
                standard = f"{labels[name]} 7 关节（rad）"
            elif name == "base_velocity":
                expanded = ["base_vx", "base_wz"]
                standard = "底盘线速度（m/s）/角速度（rad/s）"
            else:
                expanded = ["lift_m" if name == "lift" else name]
                standard = f"{labels.get(name, name)}（{item.get('unit', 'rad')}）"
            index_text = f"{indices[0]}–{indices[-1]}" if len(indices) > 2 else ','.join(map(str, indices))
            group = {"vector": vector, "part": name, "label": f"{vector.title()} {labels.get(name, name)}",
                     "indices": indices, "target": f"{vector}[{index_text}]",
                     "standard": standard, "tags": [vector], "showRange": True,
                     "range_unit": "rad" if name.endswith("_arm") else ""}
            if "gripper" in name:
                group.update(special="gripper", side=name.split("_")[0], tags=[vector, "gripper"],
                             standard=f"原生单位：打开 {item['open']} / 闭合 {item['closed']}；活动检查沿用 HDF5 规则")
            elif name == "lift":
                group.update(special="lift", tags=[vector, "lift"],
                             standard="升降柱位置（m）；profile 未规定固定高度")
            for index, canonical in zip(indices, expanded):
                names[index] = canonical
            groups.append(group)
        layouts[vector] = names
    # Merge only presentation groups; the 23-dimensional tensor layout is not
    # reordered. Waist/head each remain two independent checks within one row.
    display_groups = []
    for vector in ("action", "state"):
        parts = {g["part"]: g for g in groups if g["vector"] == vector}
        for part, label, children, standard in (
            ("waist", "腰部", ("waist_pitch", "waist_yaw"), "腰部 pitch / yaw（rad）"),
            ("head", "头部", ("head_yaw", "head_pitch"), "头部 yaw / pitch（rad）"),
        ):
            indices = [i for child in children for i in parts[child]["indices"]]
            parts[part] = {"vector": vector, "part": part, "label": f"{vector.title()} {label}",
                           "indices": indices, "target": f"{vector}[{','.join(map(str, indices))}]",
                           "standard": standard, "tags": [vector], "showRange": True, "splitRanges": True}
        parts["base_velocity"]["splitRanges"] = True
        for part in ("left_arm", "left_gripper", "right_arm", "right_gripper", "waist", "lift", "head", "base_velocity"):
            display_groups.append(parts[part])
    return {"robot_type": "zerith", "robot_label": "零次方机器人", "state_dim": profile.state_dim,
            "action_dim": profile.action_dim, "state_layout": layouts["state"], "action_layout": layouts["action"],
            "dimension_groups": display_groups, "lift_standard": "升降柱单位 m；不要求固定 200 mm",
            "gripper_standard": "State 打开/闭合 0/0.56；Action 打开/闭合 0/1.5（原生单位）",
            "stationary_threshold": profile.processing["rtml"]["global_constraints"]["max_stationary_action_frames"],
            "prompt_standard": PROMPT_STANDARD}


def dataset_path(root: Path, pattern: str, index: int, info: dict, video_key: str = "") -> Path:
    chunks = int(info.get("chunks_size", 1000))
    if chunks <= 0:
        raise ValueError("chunks_size 必须大于 0")
    relative = Path(pattern.format(episode_chunk=index // chunks, episode_index=index, video_key=video_key))
    resolved = (root / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or not resolved.is_relative_to(root):
        raise ValueError(f"LeRobot 文件路径越界: {relative}")
    return resolved


def check(name: str, errors: list[str], **detail: Any) -> dict[str, Any]:
    return {"name": name, "status": "fail" if errors else "pass", "detail": {"errors": errors, **detail}}


def finite_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: finite_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite_json(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def inspect_episode(root: Path, row: dict, info: dict, tasks: dict, mapping: dict,
                    descriptor: dict, profile: RobotProfile, item_names: list[str] | None = None) -> tuple[list[dict], int]:
    index = int(row["episode_index"])
    parquet = dataset_path(root, info["data_path"], index, info)
    frame = pd.read_parquet(parquet)
    required = {descriptor["state_key"], descriptor["action_key"], "timestamp", "frame_index", "episode_index", "task_index", "index"}
    if not required.issubset(frame.columns):
        raise ValueError(f"{parquet.name} 缺少列: {sorted(required - set(frame.columns))}")
    states = [np.asarray(v, dtype=float).tolist() for v in frame[descriptor["state_key"]]]
    actions = [np.asarray(v, dtype=float).tolist() for v in frame[descriptor["action_key"]]]
    errors = []
    if not len(frame) or int(row.get("length", -1)) != len(frame):
        errors.append(f"episodes.length={row.get('length')} 与 Parquet 帧数 {len(frame)} 不一致或为空")
    if not np.array_equal(frame["frame_index"].to_numpy(), np.arange(len(frame))):
        errors.append("frame_index 必须从 0 连续递增")
    if not bool((frame["episode_index"] == index).all()):
        errors.append("Parquet episode_index 与 metadata 不一致")
    expected_start = row.get("_global_start", 0)
    if not np.array_equal(frame["index"].to_numpy(), np.arange(expected_start, expected_start + len(frame))):
        errors.append("Parquet 全局 index 与 episode 顺序不一致")
    if any(not isinstance(v, list) or len(v) != 23 or any(isinstance(x, list) for x in v) for v in states + actions):
        raise ValueError("Parquet state/action 必须每帧为 23 维向量")
    timestamps = frame["timestamp"].to_numpy(dtype=float)
    if not np.isfinite(timestamps).all():
        errors.append("timestamp 包含 NaN/Inf")
    fps = float(info["fps"])
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("info.fps 必须为正有限数值")
    if not np.allclose(timestamps, np.arange(len(frame)) / fps, atol=1e-5, rtol=1e-5):
        errors.append("timestamp 与 frame_index / info.fps 不一致")
    task_errors = []
    raw_indices = frame["task_index"].tolist()
    task_indices = set()
    for value in raw_indices:
        if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or int(value) not in tasks:
            errors.append(f"Parquet 引用了无效 task_index: {value}")
            break
        task_indices.add(int(value))
    item_names = cp._item_names_from_episodes(root) if item_names is None else item_names
    def formats(texts):
        return {prompt_format(text) for text in texts if isinstance(text, str) and text.strip()}
    referenced = formats(tasks[i] for i in task_indices)
    listed = row.get("tasks")
    if not isinstance(listed, list) or not listed or any(not isinstance(t, str) or not t.strip() for t in listed):
        errors.append("episodes.jsonl.tasks 缺失或包含空任务")
    elif referenced and formats(listed) != referenced:
        task_errors.append("episodes.jsonl.tasks 与 Parquet task_index 引用的任务格式不一致")
    if referenced and row.get("task") and prompt_format(row["task"]) not in referenced:
        task_errors.append("episode.task 与 tasks.jsonl 的任务格式不一致")
    mapped = mapping.get(index)
    if mapped is not None:
        if not isinstance(mapped.get("task"), str) or not mapped["task"].strip():
            errors.append("mapping.task 缺失或为空")
        elif referenced and prompt_format(mapped["task"]) not in referenced:
            task_errors.append("mapping.task 与 tasks.jsonl 的任务格式不一致")
    texts = [tasks[i] for i in task_indices] + (listed if isinstance(listed, list) else [])
    texts += [row.get("task"), mapped.get("task") if mapped else None]
    task_errors.extend(dict.fromkeys(reason for text in texts if isinstance(text, str) and text.strip()
                                    if (reason := prompt_error(text, prompt_mode(root)))))
    episode = EpisodeData(root=parquet, episode_id=f"episode_{index:06d}", meta={},
                          state_frames=[StateFrame(i, float(t), s) for i, (t, s) in enumerate(zip(timestamps, states))],
                          actions=actions, camera_counts={})
    # Non-finite timestamps must not enter statistics that assume finite deltas.
    if np.isfinite(timestamps).all():
        checks = run_quality_checks(episode, profile, check_videos=False)["checks"]
    else:
        checks = [check("finite_values", ["timestamp 包含 NaN/Inf"])]
    checks.extend([check("episode_structure", errors), check("task_consistency", task_errors)])
    return checks, len(frame)


def inspect_dataset(descriptor: dict, profile: RobotProfile, max_episodes: int) -> list[dict]:
    root = Path(descriptor["path"])
    info = cp.load_json(root / "meta/info.json")
    rows = cp.episode_rows(root)
    task_rows = cp.task_rows(root)
    errors = list(descriptor["issues"])
    tasks = {}
    for row in task_rows:
        index, text = row.get("task_index"), row.get("task")
        if type(index) is not int or index < 0 or index in tasks or not isinstance(text, str) or not text.strip():
            errors.append("tasks.jsonl 存在重复/无效索引或空任务文本")
        else:
            tasks[index] = text.strip()
    if not tasks or int(info.get("total_tasks", -1)) != len(tasks):
        errors.append("total_tasks 与 tasks.jsonl 不一致或缺少任务")
    indices = [row.get("episode_index") for row in rows]
    if not rows or indices != list(range(len(rows))):
        errors.append("episodes.jsonl 索引必须从 0 连续递增且非空")
    if int(info.get("total_episodes", -1)) != len(rows):
        errors.append("total_episodes 与 episodes.jsonl 不一致")
    total = 0
    for row in rows:
        row["_global_start"] = total
        length = row.get("length")
        if type(length) is not int or length <= 0:
            errors.append(f"episode {row.get('episode_index')} 的 length 无效")
        else:
            total += length
    if int(info.get("total_frames", -1)) != total:
        errors.append("total_frames 与 episode.length 总和不一致")
    if len(list(root.glob("data/chunk-*/episode_*.parquet"))) != len(rows):
        errors.append("Parquet 文件数与 episodes.jsonl 不一致")
    mapping_file = root / "meta/episode_name_mapping.json"
    mapping = {}
    if mapping_file.is_file():
        mapping_rows = cp.load_json(mapping_file).get("episodes", [])
        mapping = {r.get("lerobot_episode_index"): r for r in mapping_rows}
        if len(mapping) != len(mapping_rows) or set(mapping) != set(indices):
            errors.append("mapping episode 索引与 episodes.jsonl 不一致")
    results = [{"episode_index": None, **check("episode_structure", errors)}]
    checked_frames = 0
    selected = cp._choose_evenly(rows, max_episodes)
    item_names = cp._item_names_from_episodes(root)
    for row in selected:
        try:
            checks, frames = inspect_episode(root, row, info, tasks, mapping, descriptor, profile, item_names)
            checked_frames += frames
        except Exception as exc:
            checks = [check("episode_structure", [f"读取或检查失败: {exc}"])]
        results.extend({"episode_index": row.get("episode_index"), **c} for c in checks)
    descriptor.update(qc_checked_episodes=len(selected), qc_checked_frames=checked_frames)
    return results


def check_reason(item: dict) -> str:
    """Bounded, human-readable cause; never expose frame/episode coordinates."""
    name, detail = item["name"], item.get("detail") or {}
    if name == "task_consistency":
        errors = ' '.join(detail.get("errors", []))
        reasons = []
        if "task_index" in errors:
            reasons.append("任务索引无效，或索引引用的任务格式与任务列表不一致")
        if "mapping" in errors:
            reasons.append("来源映射中的任务格式与 tasks.jsonl 不一致（仅忽略商品名称）")
        if "episode.task" in errors:
            reasons.append("轨迹任务格式与 tasks.jsonl 不一致（仅忽略商品名称）")
        if "固定模板" in errors:
            reasons.append("提示词不符合三种固定模板：检查大小写、动作词、连接词、手别顺序和多余文字；物品名称不限")
        if "目录不符" in errors:
            reasons.append("提示词的手别或单双臂类型与数据集目录不符")
        return "；".join(reasons) or "任务列表与 Parquet 引用关系不一致"
    if name == "action_stationary_frames":
        return f"连续静止最长 {detail.get('max_stationary_run_frames', detail.get('max_stationary_run', '?'))} 帧，超过上限 {detail.get('max_allowed_stationary_run', '?')} 帧"
    return {
        "state_dim": "State 向量长度不符合 23 维约定",
        "action_dim": "Action 向量长度不符合 23 维约定",
        "finite_values": "动作、状态或时间戳含 NaN/Inf 等非有限数值",
        "timestamp_monotonic": "时间戳存在倒退、重复或过大间隔",
        "fps": "根据 Parquet 时间戳计算的帧率低于规则要求",
        "duration": "轨迹时长超出 profile 规定范围",
        "motion_stability": "相邻帧运动跳变过大或整体关节运动量不足",
        "gripper_activity": "夹爪有效活动或开合事件不足",
        "episode_structure": "元数据、索引、帧数或向量结构不一致，或 Parquet 无法读取",
    }.get(name, "不符合对应质检规则")


def check_standard(name: str, members: list[dict]) -> str:
    def setting(key):
        value = next((c["detail"][key] for c in members if c.get("detail", {}).get(key) is not None), None)
        return f"{value:g}" if isinstance(value, (int, float)) else "按 profile"
    return {
        "state_dim": "全部受检帧 State 为 23 维",
        "action_dim": "全部受检帧 Action 为 23 维",
        "finite_values": "动作、状态及时间戳不得含 NaN / Inf",
        "episode_structure": "元数据、帧数、任务数量、连续索引与 Parquet 一致",
        "task_consistency": "各元数据须符合对应固定模板；仅忽略商品名称，保留手别和动作顺序",
        "timestamp_monotonic": f"严格递增；相邻时间戳间隔 ≤ {setting('max_allowed_gap_sec')} s",
        "fps": f"根据 Parquet 时间戳计算，帧率 ≥ {setting('min_fps')} Hz",
        "duration": f"轨迹时长 {setting('min_duration_sec')}～{setting('max_duration_sec')} s；超出范围预警",
        "motion_stability": f"State 单帧跳变 ≤ {setting('max_allowed_state_step')}；关节跳变 ≤ {setting('max_allowed_joint_step')} rad；平均关节运动 ≥ {setting('min_joint_motion_mean')}",
        "action_stationary_frames": f"连续静止 ≤ {setting('max_allowed_stationary_run')} 帧；Action 和 State 同时静止且底盘速度接近 0",
        "gripper_activity": "按原生夹爪档位检查开合/变化；至少一侧有活动，无活动则预警",
    }.get(name, "零次方 profile 规则")


def metric_summary(name: str, members: list[dict]) -> str:
    def values(key):
        return [float(c["detail"][key]) for c in members
                if isinstance(c.get("detail", {}).get(key), (int, float)) and math.isfinite(c["detail"][key])]
    def span(key, unit):
        found = values(key)
        return f"{min(found):g}～{max(found):g} {unit}" if found else "不可计算"
    def maximum(key, unit):
        found = values(key)
        return f"{max(found):g} {unit}" if found else "不可计算"
    if name == "fps":
        return "实测帧率 " + span("fps", "Hz")
    if name == "duration":
        return "实测时长 " + span("duration_sec", "s")
    if name == "timestamp_monotonic":
        return f"最大间隔 {maximum('max_gap_sec', 's')}；重复/倒退共 {sum(values('non_monotonic')):g} 处"
    if name == "action_stationary_frames":
        return "连续静止最长 " + maximum("max_stationary_run_frames", "帧")
    if name == "motion_stability":
        return f"最大 State 跳变 {maximum('max_state_step', '')}；最大关节跳变 {maximum('max_joint_step', 'rad')}；平均关节运动 {span('mean_joint_delta', '')}"
    if name == "finite_values":
        if any("bad_state_frames" not in c.get("detail", {}) for c in members):
            return "存在非有限数值，部分异常帧统计不可计算"
        return f"异常 State 帧 {sum(values('bad_state_frames')):g}；异常 Action 帧 {sum(values('bad_actions')):g}"
    if name == "state_dim":
        return f"检查 {sum(values('frames')):g} 帧；维度异常 {sum(values('bad_frames')):g} 帧"
    if name == "action_dim":
        return f"检查 {sum(values('actions')):g} 帧；维度异常 {sum(values('bad_actions')):g} 帧"
    if name == "gripper_activity":
        descriptions = []
        for side, label in (("left", "左"), ("right", "右")):
            entries = [c.get("detail", {}).get(side, {}) for c in members]
            if any(entries):
                descriptions.append(f"{label}夹爪：变化 {sum(e.get('transitions', 0) for e in entries)} 次 / 闭合 {sum(e.get('grasp_events', 0) for e in entries)} 次")
        return "；".join(descriptions)
    return ""


def summarize_checks(checks: list[dict], references: dict) -> tuple[list[dict], list[dict]]:
    rows, issues = [], []
    for name in dict.fromkeys(c["name"] for c in checks):
        members = [c for c in checks if c["name"] == name]
        category = "prompt" if name == "task_consistency" else "gripper" if name == "gripper_activity" else "temporal"
        if name in {"episode_structure", "state_dim", "action_dim", "finite_values"}:
            category = "dataset"
        bad = [c for c in members if c["status"] != "pass"]
        reasons = list(dict.fromkeys(check_reason(c) for c in bad))
        # Combine repeated stationary lengths into a single worst-case range.
        if name == "action_stationary_frames" and bad:
            worst = max(bad, key=lambda c: c["detail"].get("max_stationary_run_frames", c["detail"].get("max_stationary_run", 0)))
            reasons = [check_reason(worst)]
        status = cp._merge_status(*(c["status"] for c in members))
        affected = cp._dedupe_dataset_references([references[c["dataset_id"]] for c in bad])
        detail = {"category": category, "project": CHECK_LABELS.get(name, name), "target": "所选数据汇总",
                  "status": status, "reason": "；".join(reasons), "datasets": affected}
        row_details = []
        for severity in ("warn", "fail"):
            subset = [c for c in bad if c["status"] == severity]
            if subset:
                row_details.append({**detail, "status": severity,
                                    "datasets": cp._dedupe_dataset_references([references[c["dataset_id"]] for c in subset])})
        issues.extend(row_details)
        row = {"category": category, "kind": detail["project"], "target": "所选数据汇总", "status": status,
               "standard": check_standard(name, members), "problem": detail["reason"],
               "issue_details": row_details, "platform_statuses": {}, "counts": {}}
        for platform in cp.PLATFORMS:
            values = [c for c in members if c["platform"] == platform]
            counts = {s: sum(c["status"] == s for c in values) for s in ("pass", "warn", "fail")}
            row["counts"][platform] = counts
            row["platform_statuses"][platform] = cp._merge_status(*(c["status"] for c in values))
            row[platform] = f"{len(values)} 次检查：{counts['pass']} 通过 / {counts['warn']} 预警 / {counts['fail']} 失败" if values else "未选择"
            if values and (metrics := metric_summary(name, values)):
                row[platform] += "\n" + metrics
        rows.append(row)
    return rows, issues


def analyze_zerith_datasets(selections: list, max_episodes: int, max_frames: int, threshold: int) -> dict:
    profile = qc_profile(threshold)
    contract = contract_for(profile)
    max_episodes = max(1, min(int(max_episodes), 200))
    max_frames = max(50, min(int(max_frames), 5000))
    descriptors, arrays, checks = [], {}, []
    seen = set()
    for raw in selections:
        if not isinstance(raw, dict) or not raw.get("selected", True):
            continue
        root = Path(str(raw.get("path") or "")).expanduser().resolve()
        if root in seen:
            continue
        seen.add(root)
        item = cp.describe_dataset(root, cp._validate_platform(raw.get("platform")), "zerith")
        item.update(source_name=item["name"], logical_path=str(raw.get("logical_path") or item["grade_group_path"]),
                    source_grade=str(raw.get("source_grade") or item["source_grade"]))
        item["name"] = str(raw.get("logical_name") or item["grade_group_name"])
        item["logical_id"] = cp.dataset_id(Path(item["logical_path"]))
        descriptors.append(item)
        try:
            sample, values = cp._read_dataset_samples(item, max_episodes, max_frames)
            item.update(sample)
            arrays[item["id"]] = values
            results = inspect_dataset(item, profile, max_episodes)
        except Exception as exc:
            results = [check("episode_structure", [f"读取失败: {exc}"])]
        checks.extend({**finite_json(c), "dataset_id": item["id"], "dataset_path": item["path"], "platform": item["platform"]} for c in results)
    if not descriptors:
        raise ValueError("请至少选择一个 LeRobot 数据集")
    prompts = prompt_report(descriptors)
    dimensions = cp._dimension_report(descriptors, arrays, {v: contract[f"{v}_layout"] for v in ("state", "action")})
    references = {d["id"]: cp._dataset_reference(d) for d in descriptors}
    qc_rows, details = summarize_checks(checks, references)
    for row in dimensions:
        if row["status"] == "pass":
            continue
        group = next(g for g in contract["dimension_groups"] if g["vector"] == row["vector"] and row["index"] in g["indices"])
        affected = [d for d in descriptors if cp._definition_status([d], row["vector"], row["index"], row["name"])[0] != "pass"]
        details.append({"category": row["vector"], "project": group["label"], "target": row["name"], "status": row["status"],
                        "reason": "；".join(row["warnings"]), "datasets": [references[d["id"]] for d in (affected or descriptors)]})
    details.extend(prompts["issue_details"])
    task_checks = [c for c in checks if c["name"] == "task_consistency"]
    if any(c["status"] == "fail" for c in task_checks):
        prompts["status"] = "fail"
        prompts["warnings"].extend(dict.fromkeys(check_reason(c) for c in task_checks if c["status"] == "fail"))
        details_for_tasks = [d for d in details if d["project"] == CHECK_LABELS["task_consistency"]]
        prompts["issue_details"].extend(details_for_tasks)
        for platform in cp.PLATFORMS:
            if any(c["status"] == "fail" and c["platform"] == platform for c in task_checks):
                prompts["platform_statuses"][platform] = "fail"
    grippers, lift = [], []
    for vector in ("action", "state"):
        for side, index in (("left", 7), ("right", 15), ("lift", 16)):
            row = {"vector": vector, "side": side, "index": index, "status": "pass", "warnings": [],
                   "unit": "m" if side == "lift" else "native", "platform_statuses": {p: "pass" for p in cp.PLATFORMS}}
            for platform in cp.PLATFORMS:
                values = [arrays[d["id"]][vector][:, index] for d in descriptors if d["platform"] == platform and d["id"] in arrays and arrays[d["id"]][vector].shape[1] > index]
                joined = np.concatenate(values) if values else np.array([])
                row[platform] = cp._dimension_stats(joined) if side == "lift" else cp._binary_profile(joined)
            (lift if side == "lift" else grippers).append(row)
    failures = [d for d in details if d["status"] == "fail"]
    episodes = [r for d in descriptors for r in cp._episode_review_rows(d)]
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "contract": contract,
            "sampling": {"max_episodes_per_dataset": max_episodes, "max_frames_per_episode": max_frames,
                         "temporal_checks_full_episode": True, "video_integrity_checked": False,
                         "checked_episodes": sum(d.get("qc_checked_episodes", 0) for d in descriptors)},
            "summary": {"status": cp._merge_status(prompts["status"], *(d["status"] for d in details)),
                        "dataset_count": len({d["logical_id"] for d in descriptors}),
                        "simulation_count": len({d["logical_id"] for d in descriptors if d["platform"] == "simulation"}),
                        "real_count": len({d["logical_id"] for d in descriptors if d["platform"] == "real"}),
                        "episode_count": len(episodes), "dimension_failures": sum(d["status"] == "fail" for d in dimensions),
                        "dimension_warnings": sum(d["status"] == "warn" for d in dimensions),
                        "gripper_failures": sum(c["name"] == "gripper_activity" and c["status"] == "fail" for c in checks),
                        "lift_failures": 0, "lift_warnings": 0, "failure_detail_count": len(failures), "issue_detail_count": len(details)},
            "datasets": descriptors, "prompts": prompts, "dimensions": dimensions, "grippers": grippers, "lift": lift,
            "issue_details": details, "failure_details": failures, "episodes": episodes, "episode_checks": checks, "qc_rows": qc_rows}
