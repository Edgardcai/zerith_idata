"""Orchestrator: build the full 3-level annotation pyramid for one episode."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from .frames import build_frames
from .glossary import VALID_PHASES
from .manual import apply_manual_segments, apply_manual_trajectory, load_manual
from .schema import (
    DetectedObject,
    FrameAnnotation,
    SegmentAnnotation,
    TrajectoryAnnotation,
    to_jsonable,
)
from .segments import infer_segments
from .trajectory import build_trajectory, enrich_with_detections


def build_pyramid(
    episode: Any,
    profile: Any,
    *,
    manual_path: str | Path | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Return a dict {trajectory, segments, frames} of dataclass instances.

    Caller is responsible for serializing with `to_jsonable` when writing JSON.
    """
    manual: dict[str, Any] = {}
    if manual_path is not None:
        manual = load_manual(Path(manual_path).expanduser())
    if not manual:
        candidate = episode.root / "annotations.manual.json"
        if candidate.exists():
            manual = load_manual(candidate)

    # ── L1 trajectory ─────────────────────────────────────────────────────────
    trajectory = build_trajectory(episode, profile)

    # ── Vision enrichment (optional, controlled by profile) ────────────────────
    ann_cfg = profile.raw.get("processing", {}).get("annotations", {})
    vp_cfg = ann_cfg.get("vision_pipeline", {})
    vlm_cfg = ann_cfg.get("vlm", {})
    frame_dets: list[Any] = []
    if vp_cfg.get("enabled"):
        trajectory, frame_dets = _run_vision(
            trajectory,
            episode,
            profile,
            repo_root,
            manual=manual,
        )
    if vlm_cfg.get("enabled"):
        trajectory = _run_vlm(
            trajectory,
            episode,
            profile.raw,
            frame_dets,
            manual=manual or {},
        )

    # ── L2 segments: boundaries from gripper, targets from VLM hand assignment ──
    preliminary_segments = infer_segments(episode, profile, hand_targets={})
    vlm_assignments: dict[str, dict[str, Any]] = {}
    if vlm_cfg.get("enabled"):
        vlm_assignments = _run_vlm_hand_target_assignment(
            trajectory,
            episode,
            profile.raw,
            preliminary_segments,
            manual=manual or {},
        )
    hand_targets, assignment_source = _resolve_hand_targets(
        trajectory,
        preliminary_segments,
        vlm_assignments,
        require_vlm_assignment=bool(vlm_cfg.get("enabled")),
    )
    trajectory = _ensure_trajectory_objects(
        trajectory,
        [target for target in hand_targets.values() if target],
        source=assignment_source,
    )
    trajectory = _record_hand_target_assignment(
        trajectory,
        hand_targets,
        source=assignment_source,
        vlm_assignments=vlm_assignments,
    )
    segments = infer_segments(episode, profile, hand_targets=hand_targets)

    if manual:
        trajectory = apply_manual_trajectory(trajectory, manual)
        segments = apply_manual_segments(segments, manual)

    _validate_segment_phases(segments)
    trajectory = replace(
        trajectory,
        hands_used=_hands_used(segments),
    )
    frames = build_frames(episode, profile, segments=segments)

    return {
        "trajectory": trajectory,
        "segments": segments,
        "frames": frames,
    }


def build_annotations(episode: Any, profile: Any) -> dict[str, Any]:
    """Back-compat aggregate (matches old annotations.json structure).

    Old shape:
        {trajectory: {...}, segments: [...],
         frame_annotation_file, frame_annotation_count}
    """
    pyramid = build_pyramid(episode, profile)
    trajectory_dict = to_jsonable(pyramid["trajectory"])
    legacy_trajectory = {
        "episode_id": trajectory_dict["episode_id"],
        "profile_id": trajectory_dict["profile_id"],
        "robot": trajectory_dict["robot"],
        "instruction": trajectory_dict.get("instruction", ""),
        "target_objects": [obj["label"] for obj in trajectory_dict.get("objects", [])],
        "scene_description": trajectory_dict.get("scene_description", "not_generated"),
        "annotation_source": trajectory_dict.get("annotation", {}).get("source", "auto"),
    }
    return {
        "trajectory": legacy_trajectory,
        "segments": [to_jsonable(seg) for seg in pyramid["segments"]],
        "frame_annotation_file": "frame_annotations.jsonl",
        "frame_annotation_count": episode.n_frames,
    }


def write_pyramid(out_dir: Path, pyramid: dict[str, Any]) -> dict[str, str]:
    """Write trajectory.json + segments.jsonl + frames.jsonl. Returns artifact map."""
    import json

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    traj = to_jsonable(pyramid["trajectory"])
    (out_dir / "trajectory.json").write_text(
        json.dumps(traj, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    seg_path = out_dir / "segments.jsonl"
    with seg_path.open("w", encoding="utf-8") as f:
        for seg in pyramid["segments"]:
            f.write(json.dumps(to_jsonable(seg), ensure_ascii=False, separators=(",", ":")) + "\n")

    frm_path = out_dir / "frames.jsonl"
    with frm_path.open("w", encoding="utf-8") as f:
        for frm in pyramid["frames"]:
            f.write(json.dumps(to_jsonable(frm), ensure_ascii=False, separators=(",", ":")) + "\n")

    return {
        "trajectory": "trajectory.json",
        "segments": "segments.jsonl",
        "frames": "frames.jsonl",
    }


def _validate_segment_phases(segments: list[SegmentAnnotation]) -> None:
    bad = [s.segment_id for s in segments if s.verb not in VALID_PHASES]
    if bad:
        raise ValueError(
            f"segment(s) {bad} have verbs outside VALID_PHASES "
            f"(see annotations.glossary.VALID_PHASES)"
        )


def _hands_used(segments: list[SegmentAnnotation]) -> list[str]:
    hands = sorted({s.hand for s in segments if s.hand in {"left", "right", "both"}})
    return hands


def _run_vision(
    trajectory: TrajectoryAnnotation,
    episode: Any,
    profile: Any,
    repo_root: Path | None,
    *,
    manual: dict[str, Any] | None = None,
) -> tuple[TrajectoryAnnotation, list[Any]]:
    """Run the vision detector on key frames and return detections."""
    del manual
    try:
        from .vision.factory import create_detector
        from .vision.runner import run_on_episode
    except ImportError:
        return trajectory, []

    frame_dets: list[Any] = []
    detector = None
    try:
        detector = create_detector(profile.raw, repo_root=repo_root)
        frame_dets = run_on_episode(detector, episode, profile.raw)
        errors = [
            str(getattr(frame_det, "error", ""))
            for frame_det in frame_dets
            if getattr(frame_det, "error", "")
        ]
        if errors:
            import warnings
            warnings.warn(
                "Vision detector failed on key frame(s): " + "；".join(errors),
                stacklevel=2,
            )
        trajectory = enrich_with_detections(trajectory, frame_dets, profile.raw)
    except Exception as exc:
        import warnings
        warnings.warn(f"Vision detector failed, skipping: {exc}", stacklevel=2)
        trajectory = replace(
            trajectory,
            annotation={
                **trajectory.annotation,
                "vision_pipeline": "failed",
                "vision_error": f"{type(exc).__name__}: {exc}",
            },
        )
    finally:
        if detector is not None:
            try:
                detector.close()
            except Exception:
                pass

    return trajectory, frame_dets


def _run_vlm(
    trajectory: TrajectoryAnnotation,
    episode: Any,
    profile_raw: dict[str, Any],
    frame_dets: list[Any],
    *,
    manual: dict[str, Any] | None = None,
) -> TrajectoryAnnotation:
    """Call VLM on the first key frame and store scene_description."""
    from dataclasses import replace as _replace
    try:
        from .vision.vlm import VLMClient
        from .vision.runner import _image_path  # type: ignore[attr-defined]
    except ImportError as exc:
        import warnings
        warnings.warn(f"VLM import failed: {exc}", stacklevel=2)
        return trajectory

    vp_cfg = (
        profile_raw.get("processing", {})
        .get("annotations", {})
        .get("vision_pipeline", {})
    )
    camera = str(vp_cfg.get("camera", "head_color"))
    first_image = _image_path(episode, camera, 0)
    if first_image is None or not first_image.exists():
        return trajectory

    # First-frame detections (if YOLO ran)
    first_dets: list[Any] = []
    for fd in frame_dets:
        if fd.frame_key == "first":
            first_dets = fd.detections
            break

    try:
        profile_for_vlm = _profile_with_scene_layout(
            profile_raw,
            episode,
            manual or {},
            detections=first_dets,
        )
        target_labels = [obj.label for obj in trajectory.objects]
        target_dets = _filter_detections_for_targets(first_dets, target_labels)
        client = VLMClient.from_profile(profile_for_vlm)
        desc = client.describe_scene(
            image_path=first_image,
            detections=target_dets,
            instruction=trajectory.instruction,
            target_labels=target_labels,
        )
        client.close()
    except Exception as exc:
        import warnings
        warnings.warn(f"VLM scene description failed: {exc}", stacklevel=2)
        return trajectory

    vlm_cfg = (
        profile_for_vlm.get("processing", {})
        .get("annotations", {})
        .get("vlm", {})
    )
    scene_layout = vlm_cfg.get("scene_layout") if isinstance(vlm_cfg.get("scene_layout"), dict) else {}
    layout_source = scene_layout.get("source") or (
        scene_layout.get("shelf", {}).get("source")
        if isinstance(scene_layout.get("shelf"), dict)
        else None
    )
    annotation = {
        **trajectory.annotation,
        "vlm_model": vlm_cfg.get("model", "unknown"),
        "vlm_provider": "aihubmix",
    }
    if layout_source:
        annotation["scene_layout_source"] = layout_source
    return _replace(trajectory, scene_description=desc, annotation=annotation)


def _run_vlm_hand_target_assignment(
    trajectory: TrajectoryAnnotation,
    episode: Any,
    profile_raw: dict[str, Any],
    preliminary_segments: list[SegmentAnnotation],
    *,
    manual: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Ask the VLM which target object each grasping hand touched."""
    try:
        from .vision.runner import _image_path  # type: ignore[attr-defined]
        from .vision.vlm import VLMClient
    except ImportError as exc:
        import warnings
        warnings.warn(f"VLM hand-target assignment import failed: {exc}", stacklevel=2)
        return {}

    events = _grasp_events_for_vlm(
        episode,
        profile_raw,
        preliminary_segments,
        image_path_fn=_image_path,
    )
    if not events:
        return {}

    try:
        profile_for_vlm = _profile_with_scene_layout(profile_raw, episode, manual or {})
        client = VLMClient.from_profile(profile_for_vlm)
        assignments = client.identify_hand_targets(
            events,
            instruction=trajectory.instruction,
            candidate_labels=_candidate_target_labels(trajectory),
        )
        _attach_hand_target_event_metadata(assignments, events)
        client.close()
        return assignments
    except Exception as exc:
        import warnings
        warnings.warn(f"VLM hand-target assignment failed: {exc}", stacklevel=2)
        return {}


def _attach_hand_target_event_metadata(
    assignments: dict[str, dict[str, Any]],
    events: list[dict[str, Any]],
) -> None:
    event_by_hand = {
        str(event.get("hand")): event
        for event in events
        if str(event.get("hand")) in {"left", "right"}
    }
    for hand, details in assignments.items():
        if hand not in {"left", "right"} or not isinstance(details, dict):
            continue
        event = event_by_hand.get(hand)
        if not event:
            continue
        details["sample_frame_idx"] = event.get("frame_idx")
        details["grasp_start_frame"] = event.get("grasp_start_frame")
        details["sample_source"] = event.get("sample_source")
        images = event.get("images") if isinstance(event.get("images"), list) else []
        details["sample_cameras"] = [
            str(image.get("camera"))
            for image in images
            if isinstance(image, dict) and image.get("camera")
        ]


def _grasp_events_for_vlm(
    episode: Any,
    profile_raw: dict[str, Any],
    preliminary_segments: list[SegmentAnnotation],
    *,
    image_path_fn: Any,
) -> list[dict[str, Any]]:
    ann_cfg = profile_raw.get("processing", {}).get("annotations", {})
    assign_cfg = ann_cfg.get("hand_target_assignment", {})
    cameras = assign_cfg.get("cameras") if isinstance(assign_cfg, dict) else None
    if not isinstance(cameras, list) or not cameras:
        vp_cfg = ann_cfg.get("vision_pipeline", {})
        cameras = [str(vp_cfg.get("camera", "head_color"))]
    wrist_cameras = assign_cfg.get("wrist_cameras") if isinstance(assign_cfg, dict) else {}
    if not isinstance(wrist_cameras, dict):
        wrist_cameras = {}
    max_events_per_hand = int(assign_cfg.get("max_events_per_hand") or 1) if isinstance(assign_cfg, dict) else 1

    events: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for seg in preliminary_segments:
        if seg.verb != "grasp" or seg.hand not in {"left", "right"}:
            continue
        if counts.get(seg.hand, 0) >= max_events_per_hand:
            continue
        frame_idx = _hand_target_sample_frame(seg, episode, assign_cfg)
        images: list[dict[str, str]] = []
        event_cameras = _event_cameras_for_hand(seg.hand, cameras, wrist_cameras)
        for camera in event_cameras:
            image_path = image_path_fn(episode, str(camera), frame_idx)
            if image_path is None or not image_path.exists():
                continue
            images.append({"camera": str(camera), "path": str(image_path)})
        if not images:
            continue
        counts[seg.hand] = counts.get(seg.hand, 0) + 1
        events.append(
            {
                "event_id": f"{seg.hand}_{seg.start_frame}_{frame_idx}",
                "hand": seg.hand,
                "frame_idx": frame_idx,
                "grasp_start_frame": seg.start_frame,
                "sample_source": "post_grasp",
                "images": images,
            }
        )
    return events


def _hand_target_sample_frame(
    seg: SegmentAnnotation,
    episode: Any,
    assign_cfg: dict[str, Any],
) -> int:
    strategy = str(assign_cfg.get("sample_frame") or "post_grasp").strip().lower()
    n_frames = int(getattr(episode, "n_frames", 0) or 0)
    last_frame = max(0, n_frames - 1)
    if strategy in {"grasp_start", "start", "close"}:
        frame_idx = seg.start_frame
    elif strategy in {"grasp_end", "end"}:
        frame_idx = seg.end_frame
    else:
        offset = int(assign_cfg.get("post_grasp_frame_offset") or 10)
        frame_idx = min(seg.start_frame + max(1, offset), seg.end_frame)
    return max(0, min(int(frame_idx), last_frame))


def _event_cameras_for_hand(
    hand: str,
    cameras: list[Any],
    wrist_cameras: dict[str, Any],
) -> list[str]:
    out: list[str] = []
    wrist_camera = wrist_cameras.get(hand)
    if wrist_camera:
        out.append(str(wrist_camera))
    for camera in cameras:
        camera_name = str(camera)
        if camera_name not in out:
            out.append(camera_name)
    return out


def _resolve_hand_targets(
    trajectory: TrajectoryAnnotation,
    preliminary_segments: list[SegmentAnnotation],
    vlm_assignments: dict[str, dict[str, Any]],
    *,
    require_vlm_assignment: bool = False,
) -> tuple[dict[str, str | None], str]:
    candidates = _candidate_target_labels(trajectory)
    fallback = _fallback_hand_targets(candidates, preliminary_segments)
    vlm_targets = {
        hand: details.get("target_object")
        for hand, details in vlm_assignments.items()
        if hand in {"left", "right"} and details.get("target_object")
    }
    if require_vlm_assignment and not vlm_targets and len(candidates) > 1:
        return {"left": None, "right": None}, "vlm_assignment_missing"
    merged = _merge_hand_targets(fallback, vlm_targets, candidates)
    source = "vlm_hand_assignment" if any(vlm_targets.values()) else "prompt_fallback"
    return merged, source


def _candidate_target_labels(trajectory: TrajectoryAnnotation) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for obj in trajectory.objects:
        key = obj.label.lower().strip()
        if key and key not in seen:
            labels.append(obj.label)
            seen.add(key)
    return labels


def _fallback_hand_targets(
    candidates: list[str],
    preliminary_segments: list[SegmentAnnotation],
) -> dict[str, str | None]:
    if not candidates:
        return {}
    grasp_hands: list[str] = []
    for seg in preliminary_segments:
        if seg.verb == "grasp" and seg.hand in {"left", "right"} and seg.hand not in grasp_hands:
            grasp_hands.append(seg.hand)
    if len(candidates) == 1:
        hands = grasp_hands or ["left"]
        return {hand: candidates[0] for hand in hands}
    return {
        "left": candidates[0] if len(candidates) > 0 else None,
        "right": candidates[1] if len(candidates) > 1 else None,
    }


def _merge_hand_targets(
    fallback: dict[str, str | None],
    vlm_targets: dict[str, str | None],
    candidates: list[str],
) -> dict[str, str | None]:
    result: dict[str, str | None] = {"left": fallback.get("left"), "right": fallback.get("right")}
    for hand, target in vlm_targets.items():
        if target:
            result[hand] = target

    if len(candidates) >= 2 and vlm_targets:
        used = {target for target in vlm_targets.values() if target}
        remaining = [target for target in candidates if target not in used]
        for hand in ("left", "right"):
            if hand in vlm_targets:
                continue
            if result.get(hand) in used and remaining:
                result[hand] = remaining.pop(0)
    return result


def _ensure_trajectory_objects(
    trajectory: TrajectoryAnnotation,
    labels: list[str],
    *,
    source: str,
) -> TrajectoryAnnotation:
    existing = {obj.label.lower().strip() for obj in trajectory.objects}
    objects = list(trajectory.objects)
    for label in labels:
        key = label.lower().strip()
        if key and key not in existing:
            objects.append(DetectedObject(label=label, bbox=None, source=source))
            existing.add(key)
    return replace(trajectory, objects=objects)


def _record_hand_target_assignment(
    trajectory: TrajectoryAnnotation,
    hand_targets: dict[str, str | None],
    *,
    source: str,
    vlm_assignments: dict[str, dict[str, Any]],
) -> TrajectoryAnnotation:
    annotation = {
        **trajectory.annotation,
        "hand_target_assignment_source": source,
        "hand_target_assignments": {
            hand: target for hand, target in hand_targets.items() if hand in {"left", "right"}
        },
    }
    if vlm_assignments:
        annotation["hand_target_assignment_vlm"] = vlm_assignments
    return replace(trajectory, annotation=annotation)


def _filter_detections_for_targets(detections: list[Any], target_labels: list[str]) -> list[Any]:
    if not target_labels:
        return detections
    selected: list[Any] = []
    used_ids: set[int] = set()
    for target in target_labels:
        best = None
        best_conf = float("-inf")
        for det in detections:
            if id(det) in used_ids:
                continue
            label = str(getattr(det, "label", "")).lower().strip()
            if not _label_matches_target(label, target):
                continue
            conf = float(getattr(det, "conf", 0.0) or 0.0)
            if conf > best_conf:
                best = det
                best_conf = conf
        if best is not None:
            selected.append(best)
            used_ids.add(id(best))
    return selected


def _label_matches_target(label_low: str, target: str) -> bool:
    target_low = target.lower().strip()
    return bool(
        label_low
        and target_low
        and (
            label_low == target_low
            or target_low in label_low
            or label_low in target_low
        )
    )


def _profile_with_scene_layout(
    profile_raw: dict[str, Any],
    episode: Any,
    manual: dict[str, Any],
    *,
    detections: list[Any] | None = None,
) -> dict[str, Any]:
    """Merge per-episode/manual scene layout into the VLM profile config.

    Priority is profile defaults < auto-inferred layout < manual scene_layout
    < episode.meta.scene_layout.
    This keeps the robot profile useful as a stable default while letting a changed
    shelf layout be handled per recording.
    """
    import copy

    result = copy.deepcopy(profile_raw)
    ann_cfg = result.setdefault("processing", {}).setdefault("annotations", {})
    vlm_cfg = ann_cfg.setdefault("vlm", {})
    merged_layout = copy.deepcopy(vlm_cfg.get("scene_layout") or ann_cfg.get("scene_layout") or {})

    if _should_auto_infer_layout(merged_layout, ann_cfg) and detections:
        try:
            from .vision.layout import infer_scene_layout

            inferred_layout = infer_scene_layout(detections, merged_layout)
        except Exception:
            inferred_layout = {}
        if inferred_layout:
            merged_layout = _deep_merge(merged_layout, inferred_layout)

    manual_layout = manual.get("scene_layout") if isinstance(manual, dict) else None
    if isinstance(manual_layout, dict):
        merged_layout = _deep_merge(merged_layout, manual_layout)

    meta_layout = getattr(episode, "meta", {}).get("scene_layout")
    if isinstance(meta_layout, dict):
        merged_layout = _deep_merge(merged_layout, meta_layout)

    if merged_layout:
        vlm_cfg["scene_layout"] = merged_layout
    return result


def _should_auto_infer_layout(scene_layout: dict[str, Any], ann_cfg: dict[str, Any]) -> bool:
    if scene_layout.get("auto_infer") is True:
        return True
    if ann_cfg.get("auto_scene_layout") is True:
        return True
    shelf = scene_layout.get("shelf") if isinstance(scene_layout.get("shelf"), dict) else {}
    if shelf.get("auto_infer") is True:
        return True
    return not bool(scene_layout)


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# Keep these names importable from the top-level `quality_pipeline.annotations`
__all__ = [
    "build_pyramid",
    "build_annotations",
    "write_pyramid",
    "FrameAnnotation",
    "SegmentAnnotation",
    "TrajectoryAnnotation",
]
