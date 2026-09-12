"""L2 segment-level annotation, per RoboCOIN §IV-B(2).

Boundary signals from the paper:
  1. Gripper stationary period (transition between atomic actions)
  2. Gripper opening transition
  3. Gripper closing transition
  4. Task end moment
  (optional: sudden velocity / acceleration changes)

Per §III-C, segments MAY temporally overlap (bimanual). This module produces
one segment stream per hand (`left`, `right`) and computes `overlaps_with`
across hands.

Output is a list of SegmentAnnotation. The pyramid module is responsible for
serializing to segments.jsonl.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .schema import SegmentAnnotation, SegmentQuality

GRIPPER_CLOSE = "gripper_close"
GRIPPER_OPEN = "gripper_open"
GRIPPER_STATIONARY = "gripper_stationary"
TASK_END = "task_end"
TRAJECTORY_START = "trajectory_start"
TRAJECTORY_END = "trajectory_end"


def infer_segments(
    episode: Any,
    profile: Any,
    targets_ordered: list[str] | None = None,
    hand_targets: dict[str, str | None] | None = None,
) -> list[SegmentAnnotation]:
    """Build per-hand segment list.

    Args:
        targets_ordered: Legacy fallback list, interpreted as [left, right].
        hand_targets: Explicit mapping from hand name to target object. This is
            the preferred path when VLM hand-object assignment is available.
    """
    if episode.n_frames == 0:
        return []
    cfg = profile.processing.get("segmentation", {})
    threshold = float(cfg.get("gripper_delta_threshold", 0.02))
    min_frames = int(cfg.get("min_segment_frames", 10))
    stationary_min = int(cfg.get("stationary_min_frames", max(5, min_frames // 2)))

    gripper_indices = profile.state_gripper_indices()

    if hand_targets is None:
        if targets_ordered is None:
            targets_ordered = []
            t = _trajectory_target(episode)
            if t:
                targets_ordered = [t]
        # Legacy fallback: index 0 -> left, index 1 -> right.
        hand_target: dict[str, str | None] = {
            "left": targets_ordered[0] if len(targets_ordered) > 0 else None,
            "right": targets_ordered[1] if len(targets_ordered) > 1 else None,
        }
    else:
        hand_target = {
            "left": hand_targets.get("left"),
            "right": hand_targets.get("right"),
        }
        targets_ordered = [
            target for target in (hand_target.get("left"), hand_target.get("right"))
            if target
        ]

    fallback_target = targets_ordered[0] if targets_ordered else None
    out: list[SegmentAnnotation] = []
    next_id = 0

    if not gripper_indices:
        return [_single_segment(episode, fallback_target, next_id)]

    for side, idx in gripper_indices.items():
        target_object = hand_target.get(side, fallback_target)
        opening_sign = _gripper_opening_sign(profile, side)
        boundaries = _detect_per_hand_boundaries(
            episode, idx, threshold, stationary_min, opening_sign
        )
        per_hand = _boundaries_to_segments(
            boundaries=boundaries,
            episode=episode,
            hand=side,
            target_object=target_object,
            min_frames=min_frames,
            start_id=next_id,
        )
        next_id += len(per_hand)
        out.extend(per_hand)

    if not out:
        return [_single_segment(episode, target_object, 0)]

    collapsed = _collapse_bimanual_pick_place(out, episode)
    if collapsed is not None:
        out = collapsed
    else:
        out.sort(key=lambda s: (s.start_frame, s.hand))
        out = [replace(seg, segment_id=i) for i, seg in enumerate(out)]
    _annotate_overlaps(out)
    return out


def _detect_per_hand_boundaries(
    episode: Any,
    gripper_idx: int,
    threshold: float,
    stationary_min: int,
    opening_sign: float = 1.0,
) -> list[tuple[int, str]]:
    """Return sorted list of (frame_idx, signal) for one hand."""
    frames = episode.state_frames
    n = len(frames)
    if n < 2:
        return [(0, TRAJECTORY_START)]

    signals: list[tuple[int, str]] = [(0, TRAJECTORY_START)]
    last_sign = 0
    stationary_run = 0
    for i in range(1, n):
        if gripper_idx >= len(frames[i].state) or gripper_idx >= len(frames[i - 1].state):
            continue
        delta = (frames[i].state[gripper_idx] - frames[i - 1].state[gripper_idx]) * opening_sign
        if delta > threshold:
            if last_sign != 1:
                signals.append((i, GRIPPER_OPEN))
                last_sign = 1
            stationary_run = 0
        elif delta < -threshold:
            if last_sign != -1:
                signals.append((i, GRIPPER_CLOSE))
                last_sign = -1
            stationary_run = 0
        else:
            stationary_run += 1
            if stationary_run == stationary_min and last_sign != 0:
                signals.append((i, GRIPPER_STATIONARY))
                last_sign = 0
    signals.append((n - 1, TASK_END))
    return signals


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


def _boundaries_to_segments(
    boundaries: list[tuple[int, str]],
    episode: Any,
    hand: str,
    target_object: str | None,
    min_frames: int,
    start_id: int,
) -> list[SegmentAnnotation]:
    frames = episode.state_frames
    n = len(frames)
    if n == 0 or len(boundaries) < 2:
        return []

    merged: list[tuple[int, str]] = [boundaries[0]]
    for frame_idx, signal in boundaries[1:]:
        if frame_idx - merged[-1][0] < min_frames and signal not in (TASK_END,):
            continue
        merged.append((frame_idx, signal))
    if merged[-1][0] != n - 1:
        merged.append((n - 1, TRAJECTORY_END))

    segs: list[SegmentAnnotation] = []
    for i in range(len(merged) - 1):
        start, sig_start = merged[i]
        end, _sig_end = merged[i + 1]
        if end - start < 1:
            continue
        verb, subtask = _label_for(sig_start, hand, target_object, i)
        segs.append(
            SegmentAnnotation(
                segment_id=start_id + len(segs),
                start_frame=start,
                end_frame=end,
                start_time=float(frames[start].timestamp),
                end_time=float(frames[end].timestamp),
                hand=hand,
                verb=verb,
                subtask=subtask,
                target_object=target_object,
                boundary_source="auto_gripper",
                boundary_signal=sig_start,
                confidence=_confidence(sig_start),
                exception=None,
                quality=SegmentQuality(),
                overlaps_with=[],
            )
        )
    return segs


def _label_for(
    signal: str,
    hand: str,
    target_object: str | None,
    segment_index: int,
) -> tuple[str, str]:
    obj = target_object or "target"
    if signal == TRAJECTORY_START:
        return "approach", f"{hand} arm approaches {obj}"
    if signal == GRIPPER_CLOSE:
        return "grasp", f"{hand} arm grasps {obj}"
    if signal == GRIPPER_OPEN:
        return "place", f"{hand} arm releases or places {obj}"
    if signal == GRIPPER_STATIONARY:
        return "idle", f"{hand} arm holds steady"
    if signal == TASK_END:
        return "end", "task end"
    if segment_index == 0:
        return "approach", f"{hand} arm approaches {obj}"
    return "transit", f"{hand} arm transports {obj}"


def _collapse_bimanual_pick_place(
    segments: list[SegmentAnnotation],
    episode: Any,
) -> list[SegmentAnnotation] | None:
    """Collapse per-hand gripper phases into the expected bimanual task steps.

    Primitive segmentation produces per-hand idle intervals. For the drink
    bimanual pick-place task, the useful L2 subtasks are the semantic steps:
    approach/grasp per hand, one shared carry-to-cart segment, then place per
    hand.
    """
    by_hand = {hand: [s for s in segments if s.hand == hand] for hand in ("left", "right")}
    if not all(by_hand.values()):
        return None

    hand_info: dict[str, dict[str, SegmentAnnotation | str]] = {}
    for hand, hand_segments in by_hand.items():
        approach = _first_by_verb(hand_segments, "approach")
        grasp = _first_by_verb(hand_segments, "grasp")
        place = _first_by_verb(hand_segments, "place")
        target = next((s.target_object for s in hand_segments if s.target_object), None)
        if approach is None or grasp is None or place is None or target is None:
            return None
        hand_info[hand] = {
            "approach": approach,
            "grasp": grasp,
            "place": place,
            "target": target,
        }

    grasp_order = sorted(
        ("left", "right"),
        key=lambda h: _seg(hand_info[h]["grasp"]).start_frame,
    )
    place_order = sorted(
        ("left", "right"),
        key=lambda h: _seg(hand_info[h]["place"]).start_frame,
    )

    out: list[SegmentAnnotation] = []
    for hand in grasp_order:
        target = str(hand_info[hand]["target"])
        approach = _seg(hand_info[hand]["approach"])
        grasp = _seg(hand_info[hand]["grasp"])
        approach_end = min(max(approach.start_frame + 1, grasp.start_frame), max(0, episode.n_frames - 1))
        out.append(_copy_segment(
            approach,
            segment_id=len(out),
            end_frame=approach_end,
            end_time=_timestamp(episode, approach_end),
            subtask=f"{hand} arm approaches {target}",
            target_object=target,
        ))
        out.append(_copy_segment(
            grasp,
            segment_id=len(out),
            subtask=f"{hand} arm grasps {target}",
            target_object=target,
        ))

    second_grasp = _seg(hand_info[grasp_order[-1]]["grasp"])
    first_place = _seg(hand_info[place_order[0]]["place"])
    last_frame = max(0, episode.n_frames - 1)
    carry_start = min(second_grasp.end_frame, last_frame)
    carry_end = min(max(carry_start + 1, first_place.start_frame), last_frame)
    targets = [str(hand_info[hand]["target"]) for hand in grasp_order]
    out.append(SegmentAnnotation(
        segment_id=len(out),
        start_frame=carry_start,
        end_frame=carry_end,
        start_time=_timestamp(episode, carry_start),
        end_time=_timestamp(episode, carry_end),
        hand="both",
        verb="transit",
        subtask="Hold the items and walk to the cart",
        target_object=" and ".join(targets),
        boundary_source="auto_gripper",
        boundary_signal=GRIPPER_STATIONARY,
        confidence=0.7,
        exception=None,
        quality=SegmentQuality(),
        overlaps_with=[],
    ))

    for hand in place_order:
        target = str(hand_info[hand]["target"])
        place = _seg(hand_info[hand]["place"])
        out.append(_copy_segment(
            place,
            segment_id=len(out),
            subtask=f"{hand} arm places {target} into the cart",
            target_object=target,
        ))

    return out


def _seg(value: SegmentAnnotation | str) -> SegmentAnnotation:
    if not isinstance(value, SegmentAnnotation):
        raise TypeError(f"expected SegmentAnnotation, got {type(value)!r}")
    return value


def _first_by_verb(segments: list[SegmentAnnotation], verb: str) -> SegmentAnnotation | None:
    matches = [s for s in segments if s.verb == verb]
    return min(matches, key=lambda s: s.start_frame) if matches else None


def _copy_segment(seg: SegmentAnnotation, **changes: Any) -> SegmentAnnotation:
    return replace(seg, overlaps_with=[], **changes)


def _timestamp(episode: Any, frame_idx: int) -> float:
    frames = episode.state_frames
    if not frames:
        return 0.0
    idx = max(0, min(int(frame_idx), len(frames) - 1))
    return float(frames[idx].timestamp)


def _confidence(signal: str) -> float:
    return {
        GRIPPER_CLOSE: 0.85,
        GRIPPER_OPEN: 0.85,
        GRIPPER_STATIONARY: 0.6,
        TRAJECTORY_START: 0.7,
        TASK_END: 0.7,
    }.get(signal, 0.5)


def _single_segment(episode: Any, target_object: str | None, seg_id: int) -> SegmentAnnotation:
    frames = episode.state_frames
    last = max(0, len(frames) - 1)
    return SegmentAnnotation(
        segment_id=seg_id,
        start_frame=0,
        end_frame=last,
        start_time=float(frames[0].timestamp) if frames else 0.0,
        end_time=float(frames[last].timestamp) if frames else 0.0,
        hand="none",
        verb="approach",
        subtask=f"approach {target_object}" if target_object else "single segment trajectory",
        target_object=target_object,
        boundary_source="single_segment",
        boundary_signal=TRAJECTORY_START,
        confidence=0.4,
    )


def _trajectory_target(episode: Any) -> str | None:
    prompt = str(episode.meta.get("prompt") or "")
    import re
    match = re.search(r"target\s*:\s*(.+?)(?:\.|$)", prompt, flags=re.IGNORECASE)
    if not match:
        return None
    first = re.split(r"\s+and\s+|,", match.group(1))[0].strip()
    return first or None


def _annotate_overlaps(segments: list[SegmentAnnotation]) -> None:
    for i, a in enumerate(segments):
        overlaps: list[int] = []
        for j, b in enumerate(segments):
            if i == j or a.hand == b.hand:
                continue
            if a.start_frame < b.end_frame and b.start_frame < a.end_frame:
                overlaps.append(b.segment_id)
        a.overlaps_with = overlaps
