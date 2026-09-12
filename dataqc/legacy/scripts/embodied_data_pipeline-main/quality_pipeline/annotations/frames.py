"""L3 frame-level annotation per RoboCOIN §IV-B(3).

Implements the cascaded sliding window on:
  - left arm joints  (proxy for left end-effector kinematics)
  - right arm joints (proxy for right end-effector kinematics)
  - base xyz         (when present in profile.state_layout)

Gripper transitions and discrete state are derived per-frame.
Each frame gets a natural-language descriptor of the form:

    "left: Movement: forward, Velocity: slow, Acceleration: Constant; \\
     right: Movement: stationary, Velocity: stationary, Acceleration: Constant; \\
     left_gripper: closed, right_gripper: open"

Notes:
  - Ideal frame-level kinematics use 3D end-effector positions from FK; our
    G2 profile only exposes joints + base. Using joint-vector L2 displacement
    keeps the math identical (paper §IV-B(3) is generic over `x_t`).
  - `iter_frame_annotations` returns legacy flat dicts for back-compat with
    callers that read frame_annotations.jsonl.
"""

from __future__ import annotations

from typing import Any

from .kinematics import (
    KinematicsConfig,
    acceleration_label,
    cascaded_kinematics,
    direction_label,
    velocity_label,
)
from .schema import BaseMotion, FrameAnnotation, HandMotion


def build_frames(
    episode: Any,
    profile: Any,
    segments: list[Any] | None = None,
) -> list[FrameAnnotation]:
    cfg = _config(profile)
    segments = segments or []
    n = episode.n_frames
    if n == 0:
        return []

    arm_slices = _arm_slices(profile)
    base_slice = profile.base_position_slice()
    gripper_indices = profile.state_gripper_indices()
    gripper_cfg = profile.processing.get("segmentation", {})
    gripper_threshold = float(gripper_cfg.get("gripper_delta_threshold", 0.02))
    state_matrix = [list(f.state) for f in episode.state_frames]
    timestamps = [float(f.timestamp) for f in episode.state_frames]

    hand_series: dict[str, Any] = {}
    for side, sl in arm_slices.items():
        if sl is None:
            continue
        start, end = sl
        positions = [row[start:end] for row in state_matrix]
        hand_series[side] = cascaded_kinematics(positions, cfg)

    base_positions = None
    if base_slice is not None:
        bstart, bend = base_slice
        base_positions = [row[bstart:bend] for row in state_matrix]
    base_series = cascaded_kinematics(base_positions, cfg) if base_positions else None

    seg_by_frame: dict[int, list[int]] = {i: [] for i in range(n)}
    for seg in segments:
        for f in range(seg.start_frame, seg.end_frame + 1):
            if 0 <= f < n:
                seg_by_frame[f].append(seg.segment_id)

    out: list[FrameAnnotation] = []
    sf = episode.state_frames if hasattr(episode, "state_frames") else []
    for i in range(n):
        prev = max(0, i - cfg.window)
        frame_idx_val = int(sf[i].frame_idx) if i < len(sf) else i
        ann = FrameAnnotation(
            frame_idx=frame_idx_val,
            timestamp=timestamps[i],
            segment_ids=sorted(set(seg_by_frame.get(i, []))),
        )

        for side in ("left", "right"):
            if side not in hand_series:
                continue
            series = hand_series[side]
            disp = series.displacement[i] if i < len(series.displacement) else []
            vel = series.velocity[i] if i < len(series.velocity) else 0.0
            acc = series.acceleration[i] if i < len(series.acceleration) else 0.0
            grip_idx = gripper_indices.get(side)
            grip_val = (
                state_matrix[i][grip_idx]
                if grip_idx is not None and grip_idx < len(state_matrix[i])
                else 0.0
            )
            grip_val_prev = (
                state_matrix[prev][grip_idx]
                if grip_idx is not None and grip_idx < len(state_matrix[prev])
                else grip_val
            )
            hand = HandMotion(
                direction=direction_label(disp),
                displacement=[round(v, 6) for v in disp],
                velocity=round(vel, 6),
                velocity_label=velocity_label(vel, cfg),
                acceleration=round(acc, 6),
                acceleration_label=acceleration_label(acc, cfg),
                gripper_value=round(float(grip_val), 6),
                gripper_state=_gripper_state(grip_val, profile, side),
                gripper_transition=_gripper_transition(
                    grip_val - grip_val_prev,
                    gripper_threshold,
                    _gripper_opening_sign(profile, side),
                ),
            )
            setattr(ann, side, hand)

        if base_series is not None:
            disp = base_series.displacement[i] if i < len(base_series.displacement) else []
            vel = base_series.velocity[i] if i < len(base_series.velocity) else 0.0
            ann.base = BaseMotion(
                available=True,
                direction=direction_label(disp),
                displacement=[round(v, 6) for v in disp],
                velocity=round(vel, 6),
                velocity_label=velocity_label(vel, cfg),
            )
        else:
            ann.base = BaseMotion(available=False)

        ann.bimanual = bool(
            ann.left
            and ann.right
            and ann.left.velocity_label != "stationary"
            and ann.right.velocity_label != "stationary"
        )
        ann.natural_language = _describe(ann)
        out.append(ann)
    return out


def iter_frame_rows(
    episode: Any,
    profile: Any,
    segments: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """JSON-ready dict rows for frames.jsonl."""
    from .schema import to_jsonable
    return [to_jsonable(ann) for ann in build_frames(episode, profile, segments)]


def iter_frame_annotations(
    episode: Any,
    profile: Any,
) -> list[dict[str, Any]]:
    """Back-compat: flat per-frame rows matching the old frame_annotations.jsonl
    consumers in tools/ and docs. New consumers should use iter_frame_rows."""
    rows: list[dict[str, Any]] = []
    for ann in build_frames(episode, profile, segments=None):
        rows.append(_legacy_row(ann))
    return rows


def _legacy_row(ann: FrameAnnotation) -> dict[str, Any]:
    grippers: dict[str, dict[str, Any]] = {}
    if ann.left is not None:
        grippers["left"] = {
            "value": ann.left.gripper_value,
            "transition": ann.left.gripper_transition,
            "delta": 0.0,
        }
    if ann.right is not None:
        grippers["right"] = {
            "value": ann.right.gripper_value,
            "transition": ann.right.gripper_transition,
            "delta": 0.0,
        }
    base = {
        "available": bool(ann.base and ann.base.available),
    }
    if ann.base and ann.base.available:
        base.update(
            {
                "delta": ann.base.displacement,
                "direction": ann.base.direction,
                "speed": ann.base.velocity,
                "speed_label": ann.base.velocity_label,
            }
        )
    joint_speed = 0.0
    joint_acc_label = "constant"
    if ann.left is not None:
        joint_speed = max(joint_speed, ann.left.velocity)
        joint_acc_label = ann.left.acceleration_label
    if ann.right is not None:
        joint_speed = max(joint_speed, ann.right.velocity)
    speed_label = "stationary"
    if ann.left is not None:
        speed_label = ann.left.velocity_label
    return {
        "frame_idx": ann.frame_idx,
        "timestamp": ann.timestamp,
        "base_motion": base,
        "joint_motion": {
            "speed": joint_speed,
            "speed_label": speed_label,
            "acceleration_label": joint_acc_label,
        },
        "grippers": grippers,
        "segment_ids": ann.segment_ids,
        "natural_language": ann.natural_language,
    }


def _arm_slices(profile: Any) -> dict[str, tuple[int, int] | None]:
    out: dict[str, tuple[int, int] | None] = {"left": None, "right": None}
    for item in profile.state_layout():
        name = str(item.get("name") or "")
        if "slice" not in item:
            continue
        start, end = item["slice"]
        if name.startswith("left_arm"):
            out["left"] = (int(start), int(end))
        elif name.startswith("right_arm"):
            out["right"] = (int(start), int(end))
    return out


def _gripper_state(value: float, profile: Any, side: str) -> str:
    """Map a gripper value to {open, closed, partial, unknown} using profile.
    Tolerant: if open/closed bounds are missing, returns 'unknown'."""
    layout = profile.state_layout()
    for item in layout:
        name = str(item.get("name") or "")
        if not name.endswith("_gripper"):
            continue
        if (side == "left" and "left" in name) or (side == "right" and "right" in name):
            open_v = item.get("open")
            closed_v = item.get("closed")
            if open_v is None or closed_v is None:
                return "unknown"
            try:
                open_f = float(open_v)
                closed_f = float(closed_v)
            except (TypeError, ValueError):
                return "unknown"
            band = abs(open_f - closed_f) * 0.15 or 0.02
            if abs(value - open_f) <= band:
                return "open"
            if abs(value - closed_f) <= band:
                return "closed"
            return "partial"
    return "unknown"


def _gripper_transition(delta: float, threshold: float, opening_sign: float = 1.0) -> str:
    delta *= opening_sign
    if delta > threshold:
        return "opening"
    if delta < -threshold:
        return "closing"
    return "stationary"


def _gripper_opening_sign(profile: Any, side: str) -> float:
    for item in profile.state_layout():
        name = str(item.get("name") or "")
        if "gripper" not in name or side not in name:
            continue
        try:
            open_value = float(item["open"])
            closed_value = float(item["closed"])
        except (KeyError, TypeError, ValueError):
            return 1.0
        return 1.0 if open_value >= closed_value else -1.0
    return 1.0


def _describe(ann: FrameAnnotation) -> str:
    """Render the paper-style summary sentence (§IV-B(3))."""
    parts: list[str] = []
    for side in ("left", "right"):
        h: HandMotion | None = getattr(ann, side)
        if h is None:
            continue
        parts.append(
            f"{side}: Movement: {h.direction}, Velocity: {h.velocity_label}, "
            f"Acceleration: {h.acceleration_label}"
        )
    grips: list[str] = []
    for side in ("left", "right"):
        h = getattr(ann, side)
        if h is None:
            continue
        grips.append(f"{side}_gripper: {h.gripper_state}")
    if grips:
        parts.append(", ".join(grips))
    if ann.base and ann.base.available and ann.base.velocity_label != "stationary":
        parts.append(f"base: {ann.base.direction} ({ann.base.velocity_label})")
    return "; ".join(parts)


def _config(profile: Any) -> KinematicsConfig:
    cfg = profile.processing.get("frame_annotations", {})
    return KinematicsConfig(
        window=int(cfg.get("window", 5)),
        vel_slow_thresh=float(cfg.get("vel_slow_thresh", 0.02)),
        vel_fast_thresh=float(cfg.get("vel_fast_thresh", 0.15)),
        acc_thresh=float(cfg.get("acc_thresh", 0.02)),
    )
