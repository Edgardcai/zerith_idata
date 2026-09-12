from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean, pstdev
from typing import Any

from .episode_io import EpisodeData
from .profiles import RobotProfile


@dataclass(frozen=True)
class QualityCheck:
    name: str
    status: str
    score_delta: float
    detail: dict[str, Any]


def run_quality_checks(episode: EpisodeData, profile: RobotProfile, *, check_videos: bool = True) -> dict[str, Any]:
    if str(profile.raw.get("adapter") or "").lower() == "zerith_columnar" and episode.meta.get("raw_hdf5_path"):
        from integrations.zerith_rules import run_manual_checks
        return run_manual_checks(episode, profile)
    config = _quality_config(profile)
    checks: list[QualityCheck] = []
    checks.append(_check_state_dim(episode, profile))
    checks.append(_check_action_dim(episode, profile))
    checks.append(_check_finite(episode))
    if str(profile.raw.get("adapter") or "").lower() == "zerith_columnar":
        checks.append(_check_zerith_hdf5_schema(episode, profile))
        if check_videos:
            checks.append(_check_zerith_external_videos(episode, profile))
    checks.append(_check_timestamps(episode, config))
    checks.append(_check_fps(episode, config))
    if check_videos:
        checks.append(_check_cameras(episode, profile, config))
        checks.append(_check_camera_repair_frames(episode, config))
    checks.append(_check_duration(episode, config))
    checks.append(_check_state_motion(episode, profile, config))
    checks.append(_check_action_stationary_frames(episode, profile, config))
    checks.append(_check_gripper_activity(episode, profile, config))

    score = max(0.0, min(100.0, 100.0 + sum(check.score_delta for check in checks)))
    hard_fail = any(check.status == "fail" for check in checks)
    accept_threshold = float(config.get("accept_score", 70.0))
    accepted = (not hard_fail) and score >= accept_threshold
    return {
        "profile_id": profile.profile_id,
        "episode_id": episode.episode_id,
        "accepted": accepted,
        "quality_score": round(score, 2),
        "accept_score": accept_threshold,
        "checks": [check.__dict__ for check in checks],
        "summary": {
            "frames": episode.n_frames,
            "duration_sec": round(episode.duration_sec, 4),
            "fps": round(_episode_fps(episode), 3),
            "actions": len(episode.actions),
            "camera_counts": episode.camera_counts,
        },
    }


def _quality_config(profile: RobotProfile) -> dict[str, Any]:
    defaults = {
        "accept_score": 70.0,
        "max_timestamp_gap_sec": 0.3,
        "min_fps": 29.0,
        "min_duration_sec": 2.0,
        "max_missing_camera_ratio": 0.02,
        "max_state_step": 0.8,
        "max_joint_step": 0.8,
        "min_joint_motion_mean": 1e-4,
        "gripper_delta_threshold": 0.02,
        "action_stationary_epsilon": 1e-6,
        "stationary_state_epsilon": 1e-4,
        "stationary_base_velocity_epsilon": 1e-4,
        "stationary_requires_action_and_state": False,
        "max_stationary_action_frames": 15,
        "max_camera_repair_run_frames": 2,
        "fail_on_camera_repair_threshold": False,
    }
    processing = profile.processing.get("rtml", {})
    defaults.update(processing.get("global_constraints", {}))
    defaults.update(processing.get("quality", {}))
    return defaults


def _episode_fps(episode: EpisodeData) -> float:
    if episode.n_frames < 2 or episode.duration_sec <= 1e-9:
        return 0.0
    return (episode.n_frames - 1) / episode.duration_sec


def _check_state_dim(episode: EpisodeData, profile: RobotProfile) -> QualityCheck:
    bad = sum(1 for frame in episode.state_frames if len(frame.state) != profile.state_dim)
    status = "pass" if episode.state_frames and bad == 0 else "fail"
    return QualityCheck(
        "state_dim",
        status,
        0.0 if status == "pass" else -35.0,
        {"expected": profile.state_dim, "bad_frames": bad, "frames": episode.n_frames},
    )


def _check_action_dim(episode: EpisodeData, profile: RobotProfile) -> QualityCheck:
    if not episode.actions:
        return QualityCheck(
            "action_dim",
            "warn",
            -3.0,
            {"expected": profile.action_dim, "actions": 0, "note": "no action data attached"},
        )
    bad = sum(1 for action in episode.actions if len(action) != profile.action_dim)
    count_mismatch = (
        len(episode.actions) != episode.n_frames
        if str(profile.raw.get("adapter") or "").lower() == "zerith_columnar"
        else False
    )
    status = "pass" if bad == 0 and not count_mismatch else "fail"
    return QualityCheck(
        "action_dim",
        status,
        0.0 if status == "pass" else -25.0,
        {
            "expected": profile.action_dim,
            "bad_actions": bad,
            "actions": len(episode.actions),
            "expected_actions": episode.n_frames,
            "count_mismatch": count_mismatch,
        },
    )


def _check_zerith_hdf5_schema(episode: EpisodeData, profile: RobotProfile) -> QualityCheck:
    detail = episode.meta.get("zerith_hdf5_schema")
    if not isinstance(detail, dict):
        return QualityCheck(
            "zerith_hdf5_schema",
            "fail",
            -35.0,
            {"note": "columnar HDF5 schema details are unavailable"},
        )
    mismatches = detail.get("mismatched_frame_datasets")
    mismatches = mismatches if isinstance(mismatches, list) else []
    attrs = detail.get("root_attrs") if isinstance(detail.get("root_attrs"), dict) else {}
    issues: list[str] = []
    if mismatches:
        issues.append("frame_dataset_length_mismatch")
    try:
        total_frames = int(attrs.get("total_frames"))
    except (TypeError, ValueError):
        total_frames = -1
    if total_frames != episode.n_frames:
        issues.append("total_frames_mismatch")
    try:
        control_frequency = float(attrs.get("control_frequency"))
    except (TypeError, ValueError):
        control_frequency = 0.0
    expected_frequency = float(profile.raw.get("fps", {}).get("record") or 30.0)
    if abs(control_frequency - expected_frequency) > 1e-6:
        issues.append("control_frequency_mismatch")
    action_mode = str(attrs.get("action_mode") or "").strip().lower()
    if action_mode != "absolute":
        issues.append("unexpected_action_mode")
    status = "pass" if not issues else "fail"
    return QualityCheck(
        "zerith_hdf5_schema",
        status,
        0.0 if status == "pass" else -35.0,
        {
            "frame_count": episode.n_frames,
            "frame_dataset_count": int(detail.get("frame_dataset_count") or 0),
            "mismatched_frame_datasets": mismatches,
            "total_frames": total_frames,
            "control_frequency": control_frequency,
            "expected_control_frequency": expected_frequency,
            "action_mode": action_mode,
            "issues": issues,
        },
    )


def _check_zerith_external_videos(
    episode: EpisodeData,
    profile: RobotProfile,
) -> QualityCheck:
    """Require the exact external MP4 inputs consumed by LeRobot conversion."""

    inventory = episode.meta.get("zerith_external_camera_videos")
    if not isinstance(inventory, dict):
        return QualityCheck(
            "zerith_external_videos",
            "fail",
            -35.0,
            {"expected_frames": episode.n_frames, "issues": ["video_inventory_unavailable"]},
        )

    cameras: dict[str, dict[str, Any]] = {}
    issues: list[dict[str, Any]] = []
    for camera in profile.cameras:
        raw_detail = inventory.get(camera.raw_key)
        detail = dict(raw_detail) if isinstance(raw_detail, dict) else {}
        exists = bool(detail.get("exists"))
        readable = bool(detail.get("readable"))
        try:
            decoded_frames = int(detail.get("decoded_frame_count") or 0)
        except (TypeError, ValueError):
            decoded_frames = 0
        camera_issues: list[str] = []
        if camera.required:
            if not exists:
                camera_issues.append("missing")
            elif not readable:
                camera_issues.append("unreadable")
            if decoded_frames != episode.n_frames:
                camera_issues.append("frame_count_mismatch")
        cameras[camera.raw_key] = {
            "required": bool(camera.required),
            "path": str(detail.get("path") or ""),
            "exists": exists,
            "readable": readable,
            "declared_frame_count": int(detail.get("declared_frame_count") or 0),
            "decoded_frame_count": decoded_frames,
            "expected_frames": episode.n_frames,
            "decode_error": str(detail.get("error") or ""),
            "issues": camera_issues,
        }
        if camera_issues:
            issues.append({"camera": camera.raw_key, "issues": camera_issues})

    status = "pass" if not issues else "fail"
    return QualityCheck(
        "zerith_external_videos",
        status,
        0.0 if status == "pass" else -35.0,
        {
            "expected_frames": episode.n_frames,
            "issues": issues,
            "cameras": cameras,
        },
    )


def _check_finite(episode: EpisodeData) -> QualityCheck:
    bad_state = sum(
        1
        for frame in episode.state_frames
        if (not math.isfinite(frame.timestamp)) or any(not math.isfinite(v) for v in frame.state)
    )
    bad_action = sum(1 for action in episode.actions if any(not math.isfinite(v) for v in action))
    status = "pass" if bad_state == 0 and bad_action == 0 else "fail"
    return QualityCheck(
        "finite_values",
        status,
        0.0 if status == "pass" else -30.0,
        {"bad_state_frames": bad_state, "bad_actions": bad_action},
    )


def _check_timestamps(episode: EpisodeData, config: dict[str, Any]) -> QualityCheck:
    ts = [frame.timestamp for frame in episode.state_frames]
    if len(ts) < 2:
        return QualityCheck("timestamp_monotonic", "fail", -25.0, {"frames": len(ts)})
    frame_deltas = [
        (prev.frame_idx, cur.frame_idx, cur.timestamp - prev.timestamp)
        for prev, cur in zip(episode.state_frames, episode.state_frames[1:])
    ]
    deltas = [delta for _, _, delta in frame_deltas]
    non_monotonic = sum(1 for d in deltas if d <= 0.0)
    max_gap_start_frame, max_gap_end_frame, max_gap = max(
        frame_deltas,
        key=lambda item: item[2],
    )
    mean_dt = mean(deltas)
    hz = (1.0 / mean_dt) if mean_dt > 1e-9 else 0.0
    gap_limit = float(config["max_timestamp_gap_sec"])
    if non_monotonic > 0 or max_gap > gap_limit:
        status, penalty = "fail", -25.0
    else:
        status, penalty = "pass", 0.0
    return QualityCheck(
        "timestamp_monotonic",
        status,
        penalty,
        {
            "non_monotonic": non_monotonic,
            "mean_dt_sec": round(mean_dt, 6),
            "std_dt_sec": round(pstdev(deltas), 6) if len(deltas) > 1 else 0.0,
            "estimated_hz": round(hz, 3),
            "max_gap_sec": round(max_gap, 6),
            "max_gap_start_frame": max_gap_start_frame,
            "max_gap_end_frame": max_gap_end_frame,
            "max_allowed_gap_sec": gap_limit,
        },
    )


def _check_fps(episode: EpisodeData, config: dict[str, Any]) -> QualityCheck:
    min_fps = float(config["min_fps"])
    fps = _episode_fps(episode)
    status = "pass" if fps >= min_fps else "fail"
    return QualityCheck(
        "fps",
        status,
        0.0 if status == "pass" else -20.0,
        {
            "fps": round(fps, 3),
            "min_fps": min_fps,
            "frames": episode.n_frames,
            "duration_sec": round(episode.duration_sec, 6),
        },
    )


def _check_cameras(
    episode: EpisodeData,
    profile: RobotProfile,
    config: dict[str, Any],
) -> QualityCheck:
    details: dict[str, Any] = {}
    failures = 0
    frames = max(episode.n_frames, 1)
    max_missing_ratio = float(config["max_missing_camera_ratio"])
    required_view_count = 0
    present_required_view_count = 0
    for camera in profile.cameras:
        count = int(episode.camera_counts.get(camera.raw_key, 0))
        missing = max(0, episode.n_frames - count)
        extra = max(0, count - episode.n_frames)
        ratio = missing / frames
        if camera.required:
            required_view_count += 1
            if count > 0:
                present_required_view_count += 1
            if count <= 0 or ratio > max_missing_ratio or extra > 0:
                failures += 1
        details[camera.raw_key] = {
            "required": camera.required,
            "count": count,
            "missing": missing,
            "extra": extra,
            "missing_ratio": round(ratio, 4),
            "max_missing_ratio": max_missing_ratio,
        }
    details["_summary"] = {
        "expected_required_view_count": required_view_count,
        "present_required_view_count": present_required_view_count,
    }
    if failures:
        status, penalty = "fail", -30.0
    else:
        status, penalty = "pass", 0.0
    return QualityCheck("camera_completeness", status, penalty, details)


def _check_camera_repair_frames(
    episode: EpisodeData,
    config: dict[str, Any],
) -> QualityCheck:
    threshold = int(config.get("max_camera_repair_run_frames", 2))
    entries = _camera_sync_entries(episode.meta)
    if not entries:
        return QualityCheck(
            "camera_repair_frames",
            "pass",
            0.0,
            {
                "available": False,
                "max_allowed_consecutive_repair_frames": threshold,
                "warnings": [],
            },
        )

    warnings: list[dict[str, Any]] = []
    camera_details: list[dict[str, Any]] = []
    max_run = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        camera_name = str(entry.get("name") or "")
        view_name = _camera_view_name(episode.meta, camera_name)
        repair_run = _to_int(entry.get("max_consecutive_repair_or_reuse_frames"), 0)
        generated_run = _to_int(entry.get("max_consecutive_generated_fill_frames"), 0)
        reused_run = _to_int(entry.get("max_consecutive_reused_existing_frames"), 0)
        max_run = max(max_run, repair_run)
        detail = {
            "camera": view_name,
            "camera_color_name": camera_name,
            "source_frame_count": _to_int(entry.get("source_frame_count"), 0),
            "synced_frame_count": _to_int(entry.get("synced_frame_count"), 0),
            "generated_fill_count": _to_int(entry.get("generated_fill_count"), 0),
            "interpolated_fill_count": _to_int(entry.get("interpolated_fill_count"), 0),
            "nearest_fill_count": _to_int(entry.get("nearest_fill_count"), 0),
            "reused_existing_frame_count": _to_int(entry.get("reused_existing_frame_count"), 0),
            "repair_or_reuse_frame_count": _to_int(entry.get("repair_or_reuse_frame_count"), 0),
            "max_consecutive_repair_or_reuse_frames": repair_run,
            "max_consecutive_repair_or_reuse_start": _to_int(
                entry.get("max_consecutive_repair_or_reuse_start"),
                -1,
            ),
            "max_consecutive_repair_or_reuse_end": _to_int(
                entry.get("max_consecutive_repair_or_reuse_end"),
                -1,
            ),
            "max_consecutive_generated_fill_frames": generated_run,
            "max_consecutive_generated_fill_start": _to_int(
                entry.get("max_consecutive_generated_fill_start"),
                -1,
            ),
            "max_consecutive_generated_fill_end": _to_int(
                entry.get("max_consecutive_generated_fill_end"),
                -1,
            ),
            "max_consecutive_reused_existing_frames": reused_run,
            "max_consecutive_reused_existing_start": _to_int(
                entry.get("max_consecutive_reused_existing_start"),
                -1,
            ),
            "max_consecutive_reused_existing_end": _to_int(
                entry.get("max_consecutive_reused_existing_end"),
                -1,
            ),
        }
        camera_details.append(detail)
        if repair_run > threshold:
            warnings.append(detail)

    fail_on_threshold = bool(config.get("fail_on_camera_repair_threshold", False))
    status = "fail" if warnings and fail_on_threshold else "warn" if warnings else "pass"
    return QualityCheck(
        "camera_repair_frames",
        status,
        -30.0 if status == "fail" else -3.0 if status == "warn" else 0.0,
        {
            "available": True,
            "max_allowed_consecutive_repair_frames": threshold,
            "fail_on_threshold": fail_on_threshold,
            "max_consecutive_repair_or_reuse_frames": max_run,
            "warnings": warnings,
            "cameras": camera_details,
        },
    )


def _camera_sync_entries(meta: dict[str, Any]) -> list[dict[str, Any]]:
    summary = meta.get("camera_sync_summary")
    if isinstance(summary, dict):
        entries = summary.get("camera_color")
        if isinstance(entries, list):
            return [entry for entry in entries if isinstance(entry, dict)]
    entries = []
    for video_info in meta.get("available_videos") or []:
        if not isinstance(video_info, dict):
            continue
        sync_stats = video_info.get("sync_stats")
        if isinstance(sync_stats, dict):
            entries.append(sync_stats)
    return entries


def _camera_view_name(meta: dict[str, Any], camera_color_name: str) -> str:
    for video_info in meta.get("available_videos") or []:
        if not isinstance(video_info, dict):
            continue
        if str(video_info.get("camera_color_name") or "") != camera_color_name:
            continue
        file_name = str(video_info.get("file") or "").strip()
        if file_name:
            return file_name.rsplit(".", 1)[0]
    fallback = {
        "left": "hand_left_color",
        "front": "head_color",
        "right": "hand_right_color",
    }
    return fallback.get(camera_color_name, camera_color_name)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _check_duration(episode: EpisodeData, config: dict[str, Any]) -> QualityCheck:
    duration = episode.duration_sec
    min_duration = float(config["min_duration_sec"])
    max_duration_raw = config.get("max_duration_sec")
    max_duration = float(max_duration_raw) if max_duration_raw is not None else None
    if duration < min_duration:
        reason = "too_short"
    elif max_duration is not None and duration > max_duration:
        reason = "too_long"
    else:
        reason = ""
    status = "warn" if reason else "pass"
    return QualityCheck(
        "duration",
        status,
        0.0 if status == "pass" else -5.0,
        {
            "duration_sec": round(duration, 4),
            "min_duration_sec": min_duration,
            "max_duration_sec": max_duration,
            "reason": reason,
        },
    )


def _check_state_motion(
    episode: EpisodeData,
    profile: RobotProfile,
    config: dict[str, Any],
) -> QualityCheck:
    if len(episode.state_frames) < 2:
        return QualityCheck("motion_stability", "fail", -20.0, {"frames": episode.n_frames})
    max_state_step = 0.0
    max_state_step_start_frame: int | None = None
    max_state_step_end_frame: int | None = None
    for prev, cur in zip(episode.state_frames, episode.state_frames[1:]):
        if len(prev.state) != len(cur.state):
            continue
        state_step = max(abs(b - a) for a, b in zip(prev.state, cur.state))
        if state_step > max_state_step:
            max_state_step = state_step
            max_state_step_start_frame = prev.frame_idx
            max_state_step_end_frame = cur.frame_idx
    joint_indices = profile.joint_state_indices()
    joint_deltas: list[float] = []
    max_joint_step = 0.0
    max_joint_step_start_frame: int | None = None
    max_joint_step_end_frame: int | None = None
    for prev, cur in zip(episode.state_frames, episode.state_frames[1:]):
        for idx in joint_indices:
            if idx < len(prev.state) and idx < len(cur.state):
                delta = abs(cur.state[idx] - prev.state[idx])
                if delta > max_joint_step:
                    max_joint_step = delta
                    max_joint_step_start_frame = prev.frame_idx
                    max_joint_step_end_frame = cur.frame_idx
                joint_deltas.append(delta)
    mean_joint_delta = mean(joint_deltas) if joint_deltas else 0.0
    if max_state_step > float(config["max_state_step"]):
        status, penalty = "fail", -25.0
    elif max_joint_step > float(config["max_joint_step"]):
        status, penalty = "warn", -8.0
    elif mean_joint_delta < float(config["min_joint_motion_mean"]):
        status, penalty = "warn", -4.0
    else:
        status, penalty = "pass", 0.0
    return QualityCheck(
        "motion_stability",
        status,
        penalty,
        {
            "max_state_step": round(max_state_step, 6),
            "max_state_step_start_frame": max_state_step_start_frame,
            "max_state_step_end_frame": max_state_step_end_frame,
            "max_allowed_state_step": float(config["max_state_step"]),
            "max_joint_step": round(max_joint_step, 6),
            "max_joint_step_start_frame": max_joint_step_start_frame,
            "max_joint_step_end_frame": max_joint_step_end_frame,
            "max_allowed_joint_step": float(config["max_joint_step"]),
            "mean_joint_delta": round(mean_joint_delta, 8),
            "min_joint_motion_mean": float(config["min_joint_motion_mean"]),
        },
    )


def _check_gripper_activity(
    episode: EpisodeData,
    profile: RobotProfile,
    config: dict[str, Any],
) -> QualityCheck:
    state_indices = profile.state_gripper_indices()
    action_indices = profile.action_gripper_indices()
    sides = sorted(set(state_indices) | set(action_indices))
    if not sides or (not episode.state_frames and not episode.actions):
        return QualityCheck("gripper_activity", "warn", -2.0, {"note": "no gripper indices"})
    threshold = float(config["gripper_delta_threshold"])
    gripper_layout = _state_gripper_layout(profile)
    action_gripper_layout = _action_gripper_layout(profile)
    detail: dict[str, Any] = {}
    active_sides = 0
    for side in sides:
        layout = gripper_layout.get(side, {})
        action_layout = action_gripper_layout.get(side, layout)
        open_value = float(layout.get("open", 0.1))
        closed_value = float(layout.get("closed", 0.0))
        action_open_value = float(action_layout.get("open", open_value))
        action_closed_value = float(action_layout.get("closed", closed_value))
        state_values = (
            [frame.state[state_indices[side]] for frame in episode.state_frames if state_indices[side] < len(frame.state)]
            if side in state_indices
            else []
        )
        action_values = (
            [action[action_indices[side]] for action in episode.actions if action_indices[side] < len(action)]
            if side in action_indices
            else []
        )
        state_stats = _gripper_activity_stats(
            state_values,
            open_value=open_value,
            closed_value=closed_value,
            threshold=threshold,
        )
        action_stats = _gripper_activity_stats(
            action_values,
            open_value=action_open_value,
            closed_value=action_closed_value,
            threshold=threshold,
        )
        primary_source = "action" if action_values else "state"
        primary_stats = action_stats if action_values else state_stats
        if (
            int(primary_stats["transitions"]) > 0
            or int(state_stats["transitions"]) > 0
            or int(action_stats["transitions"]) > 0
        ):
            active_sides += 1
        detail[side] = {
            **primary_stats,
            "source": primary_source,
            "grasp_events": primary_stats["close_events"],
            "threshold": threshold,
            "open_value": open_value,
            "closed_value": closed_value,
            "action_open_value": action_open_value,
            "action_closed_value": action_closed_value,
            "state": state_stats,
            "action": action_stats,
        }
    status = "pass" if active_sides else "warn"
    return QualityCheck(
        "gripper_activity",
        status,
        0.0 if status == "pass" else -3.0,
        detail,
    )


def _gripper_activity_stats(
    values: list[float],
    *,
    open_value: float,
    closed_value: float,
    threshold: float,
) -> dict[str, Any]:
    span = (max(values) - min(values)) if values else 0.0
    delta_transitions = sum(1 for a, b in zip(values, values[1:]) if abs(b - a) >= threshold)
    states = _gripper_open_closed_states(
        values,
        open_value=open_value,
        closed_value=closed_value,
        hysteresis=threshold,
    )
    transitions = sum(1 for a, b in zip(states, states[1:]) if a != b)
    close_events = sum(1 for a, b in zip(states, states[1:]) if a != "closed" and b == "closed")
    open_events = sum(1 for a, b in zip(states, states[1:]) if a != "open" and b == "open")
    return {
        "range": round(span, 6),
        "transitions": transitions,
        "close_events": close_events,
        "open_events": open_events,
        "closed_frames": sum(1 for state in states if state == "closed"),
        "open_frames": sum(1 for state in states if state == "open"),
        "initial_state": states[0] if states else "",
        "final_state": states[-1] if states else "",
        "midpoint": round((open_value + closed_value) / 2.0, 6),
        "delta_transitions": delta_transitions,
    }


def _state_gripper_layout(profile: RobotProfile) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in profile.state_layout():
        name = str(item.get("name") or "")
        if "gripper" not in name or "index" not in item:
            continue
        side = "left" if "left" in name else "right" if "right" in name else name
        out[side] = item
    return out


def _action_gripper_layout(profile: RobotProfile) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in profile.action_layout():
        name = str(item.get("name") or "")
        if "gripper" not in name or "index" not in item:
            continue
        side = "left" if "left" in name else "right" if "right" in name else name
        out[side] = item
    return out


def _gripper_open_closed_states(
    values: list[float],
    *,
    open_value: float,
    closed_value: float,
    hysteresis: float,
) -> list[str]:
    if not values:
        return []
    midpoint = (open_value + closed_value) / 2.0
    half_band = max(0.0, hysteresis) / 2.0
    closed_is_lower = closed_value < open_value

    def classify(value: float) -> str:
        if closed_is_lower:
            return "closed" if value <= midpoint else "open"
        return "closed" if value >= midpoint else "open"

    states = [classify(values[0])]
    current = states[0]
    for value in values[1:]:
        if closed_is_lower:
            if current == "open" and value <= midpoint - half_band:
                current = "closed"
            elif current == "closed" and value >= midpoint + half_band:
                current = "open"
        else:
            if current == "open" and value >= midpoint + half_band:
                current = "closed"
            elif current == "closed" and value <= midpoint - half_band:
                current = "open"
        states.append(current)
    return states


def _check_action_stationary_frames(
    episode: EpisodeData,
    profile: RobotProfile,
    config: dict[str, Any],
) -> QualityCheck:
    """Count frames where configured action/state motion is near zero and the base is stopped."""
    action_rows = _stationary_action_rows(episode)
    state_rows = _stationary_state_rows(episode)
    if len(action_rows) < 2:
        return QualityCheck(
            "action_stationary_frames",
            "warn",
            -2.0,
            {"actions": len(action_rows), "note": "not enough action rows to compare"},
        )

    epsilon = float(config["action_stationary_epsilon"])
    state_epsilon = float(config["stationary_state_epsilon"])
    base_velocity_epsilon = float(config["stationary_base_velocity_epsilon"])
    requires_action_and_state = bool(config.get("stationary_requires_action_and_state", False))
    max_allowed = int(config["max_stationary_action_frames"])
    using_hdf5_stationary_vectors = bool(episode.stationary_action_vectors and episode.stationary_state_vectors)
    pair_count = min(len(action_rows), len(state_rows)) - 1
    stationary_frames = 0
    stationary_pairs = 0
    max_stationary_run = 0
    max_stationary_run_start_pair: int | None = None
    max_stationary_run_end_pair: int | None = None
    over_threshold_runs = 0
    current_run = 0
    current_run_start_pair: int | None = None
    unchanged_action_frames = 0
    max_unchanged_action_run = 0
    current_unchanged_action_run = 0
    compared = 0
    max_action_delta = 0.0
    max_state_delta = 0.0
    max_base_velocity_abs = 0.0

    for pair_idx in range(pair_count):
        prev = action_rows[pair_idx]
        cur = action_rows[pair_idx + 1]
        if len(prev) != len(cur):
            continue
        compared += 1
        metrics = _stationary_pair_metrics(
            profile,
            prev,
            cur,
            state_rows[pair_idx],
            state_rows[pair_idx + 1],
            using_hdf5_stationary_vectors,
        )
        action_delta = metrics["action_delta"]
        state_delta = metrics["state_delta"]
        base_velocity_abs = metrics["base_velocity_abs"]
        stationary_source = metrics["stationary_source"]
        max_action_delta = max(max_action_delta, action_delta)
        max_state_delta = max(max_state_delta, state_delta)
        max_base_velocity_abs = max(max_base_velocity_abs, base_velocity_abs)
        action_unchanged = action_delta <= epsilon
        if action_unchanged:
            unchanged_action_frames += 1
            current_unchanged_action_run += 1
            max_unchanged_action_run = max(
                max_unchanged_action_run,
                current_unchanged_action_run,
            )
        else:
            current_unchanged_action_run = 0
        motion_stationary = (
            action_unchanged and state_delta <= state_epsilon
            if requires_action_and_state
            else action_unchanged or state_delta <= state_epsilon
        )
        if motion_stationary and base_velocity_abs <= base_velocity_epsilon:
            if current_run_start_pair is None:
                current_run_start_pair = pair_idx
            stationary_pairs += 1
            current_run += 1
            if current_run > max_stationary_run:
                max_stationary_run = current_run
                max_stationary_run_start_pair = current_run_start_pair
                max_stationary_run_end_pair = pair_idx
        else:
            if current_run:
                stationary_frames += current_run + 1
            if current_run + 1 > max_allowed:
                over_threshold_runs += 1
            current_run = 0
            current_run_start_pair = None

    if current_run:
        stationary_frames += current_run + 1
    if current_run + 1 > max_allowed:
        over_threshold_runs += 1

    max_stationary_run_frames = max_stationary_run + 1 if max_stationary_run else 0
    status = "pass" if max_stationary_run_frames <= max_allowed else "fail"
    max_run_start_frame = (
        max_stationary_run_start_pair
        if max_stationary_run_start_pair is not None
        else None
    )
    max_run_end_frame = (
        max_stationary_run_end_pair + 1
        if max_stationary_run_end_pair is not None
        else None
    )
    return QualityCheck(
        "action_stationary_frames",
        status,
        0.0 if status == "pass" else -15.0,
        {
            "stationary_frames": stationary_frames,
            "stationary_pairs": stationary_pairs,
            "unchanged_action_frames": unchanged_action_frames,
            "max_stationary_run": max_stationary_run,
            "max_stationary_run_frames": max_stationary_run_frames,
            "max_stationary_run_start_frame": max_run_start_frame,
            "max_stationary_run_end_frame": max_run_end_frame,
            "max_stationary_run_action_rows": max_stationary_run_frames,
            "over_threshold_stationary_runs": over_threshold_runs,
            "max_unchanged_action_run": max_unchanged_action_run,
            "max_allowed_stationary_run": max_allowed,
            "epsilon": epsilon,
            "state_stationary_epsilon": state_epsilon,
            "requires_action_and_state": requires_action_and_state,
            "base_velocity_epsilon": base_velocity_epsilon,
            "compared_action_pairs": compared,
            "stationary_state_dim": len(state_rows[0]) if state_rows else 0,
            "stationary_action_dim": len(action_rows[0]) if action_rows else 0,
            "stationary_source": stationary_source if compared else (
                "hdf5_full_motion_fields" if using_hdf5_stationary_vectors else "profile_state_action"
            ),
            "stationary_ratio": round(stationary_pairs / compared, 6) if compared else 0.0,
            "max_action_delta": round(max_action_delta, 8),
            "max_state_delta": round(max_state_delta, 8),
            "max_base_velocity_abs": round(max_base_velocity_abs, 8),
        },
    )


def _stationary_state_rows(episode: EpisodeData) -> list[list[float]]:
    if episode.stationary_state_vectors and len(episode.stationary_state_vectors) >= len(episode.state_frames):
        return episode.stationary_state_vectors
    return [frame.state for frame in episode.state_frames]


def _stationary_action_rows(episode: EpisodeData) -> list[list[float]]:
    if episode.stationary_action_vectors and len(episode.stationary_action_vectors) >= len(episode.actions):
        return episode.stationary_action_vectors
    return episode.actions


def _action_layout_indices(profile: RobotProfile, name: str) -> list[int]:
    return _layout_indices(profile.action_layout(), name)


def _state_layout_indices(profile: RobotProfile, name: str) -> list[int]:
    return _layout_indices(profile.state_layout(), name)


def _layout_indices(layout: list[dict[str, Any]], name: str) -> list[int]:
    for item in layout:
        if str(item.get("name") or "") != name:
            continue
        if "slice" in item:
            start, end = item["slice"]
            return list(range(int(start), int(end)))
        if "index" in item:
            return [int(item["index"])]
    return []


def _is_aloha_profile(profile: RobotProfile) -> bool:
    adapter = str(profile.raw.get("adapter") or "").lower()
    profile_id = str(profile.profile_id or "").lower()
    return adapter == "aloha" or profile_id == "aloha"


def _is_zerith_profile(profile: RobotProfile) -> bool:
    adapter = str(profile.raw.get("adapter") or "").lower()
    profile_id = str(profile.profile_id or "").lower()
    return adapter in {"zerith", "zerith_columnar"} or profile_id == "zerith"


def _aloha_extended_stationary_mode(profile: RobotProfile, using_hdf5_stationary_vectors: bool) -> bool:
    if using_hdf5_stationary_vectors or not _is_aloha_profile(profile):
        return False
    return bool(_state_layout_indices(profile, "base_velocity") or _action_layout_indices(profile, "base_velocity"))


def _valid_indices(indices: list[int], length: int) -> list[int]:
    return [idx for idx in indices if 0 <= idx < length]


def _max_delta(prev: list[float], cur: list[float], indices: list[int] | None = None) -> float:
    if indices is None:
        if len(prev) != len(cur):
            return math.inf
        return max((abs(b - a) for a, b in zip(prev, cur)), default=0.0)
    usable = _valid_indices(indices, min(len(prev), len(cur)))
    return max((abs(cur[idx] - prev[idx]) for idx in usable), default=0.0)


def _max_abs(row: list[float], indices: list[int]) -> float:
    return max((abs(row[idx]) for idx in _valid_indices(indices, len(row))), default=0.0)


def _stationary_pair_metrics(
    profile: RobotProfile,
    prev_action: list[float],
    cur_action: list[float],
    prev_state: list[float],
    cur_state: list[float],
    using_hdf5_stationary_vectors: bool,
) -> dict[str, Any]:
    if _is_zerith_profile(profile):
        state_base_velocity_indices = _state_layout_indices(profile, "base_velocity")
        action_base_velocity_indices = _action_layout_indices(profile, "base_velocity")
        return {
            "action_delta": _max_delta(prev_action, cur_action),
            "state_delta": _max_delta(prev_state, cur_state),
            "base_velocity_abs": max(
                _max_abs(cur_state, state_base_velocity_indices),
                _max_abs(cur_action, action_base_velocity_indices),
            ),
            "stationary_source": "zerith_state_action_base_velocity",
        }
    if _aloha_extended_stationary_mode(profile, using_hdf5_stationary_vectors):
        arm_state_indices = list(range(min(14, len(prev_state), len(cur_state))))
        arm_action_indices = list(range(min(14, len(prev_action), len(cur_action))))
        state_base_velocity_indices = _state_layout_indices(profile, "base_velocity")
        action_base_velocity_indices = _action_layout_indices(profile, "base_velocity")
        return {
            "action_delta": _max_delta(prev_action, cur_action, arm_action_indices),
            "state_delta": _max_delta(prev_state, cur_state, arm_state_indices),
            "base_velocity_abs": max(
                _max_abs(cur_state, state_base_velocity_indices),
                _max_abs(cur_action, action_base_velocity_indices),
            ),
            "stationary_source": "aloha_arm14_base_velocity_zero",
        }
    action_base_velocity_indices = [] if using_hdf5_stationary_vectors else _action_layout_indices(profile, "base_velocity")
    return {
        "action_delta": _max_delta(prev_action, cur_action),
        "state_delta": _max_delta(prev_state, cur_state),
        "base_velocity_abs": _max_abs(cur_action, action_base_velocity_indices),
        "stationary_source": "hdf5_full_motion_fields" if using_hdf5_stationary_vectors else "profile_state_action",
    }
