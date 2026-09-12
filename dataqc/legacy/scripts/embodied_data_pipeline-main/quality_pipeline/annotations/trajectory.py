"""L1 trajectory-level annotation.

Paper §IV-B(1) describes vision-based scene description. This module:

  - parses target objects from instruction
  - infers collaboration from target count
  - extracts canonical verbs via glossary
  - optionally enriches objects with bbox from the vision pipeline
  - leaves scene_description as "not_generated" until a vision+LLM step fills it

Vision enrichment is done in pyramid.py after the detector runs on key frames.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .glossary import GLOSSARY, extract_verbs
from .schema import DetectedObject, TaskTaxonomy, TrajectoryAnnotation
from .task_preset import resolve as resolve_preset


def build_trajectory(episode: Any, profile: Any) -> TrajectoryAnnotation:
    prompt = str(episode.meta.get("prompt") or "")
    fps = int((profile.raw.get("fps") or {}).get("record") or 0)

    # ── Prompt target resolution ──────────────────────────────────────────────
    preset_info = resolve_preset(episode, profile.raw)
    targets_ordered: list[str] = preset_info["targets_ordered"]
    collaboration: str = preset_info["collaboration"]

    # Build DetectedObject list from prompt targets (bbox=None until vision runs)
    objects = [
        DetectedObject(label=t, bbox=None, source="instruction_extract")
        for t in targets_ordered
    ]

    # ── Verb / taxonomy ───────────────────────────────────────────────────────
    verbs = extract_verbs(prompt)
    dominant_cat = _dominant_category(verbs)

    taxonomy = TaskTaxonomy(
        action_category=dominant_cat,
        verbs=verbs,
        collaboration=collaboration,
        object_flexibility=["rigid"],
    )

    return TrajectoryAnnotation(
        episode_id=episode.episode_id,
        profile_id=profile.profile_id,
        robot=profile.display_name,
        fps=fps,
        n_frames=episode.n_frames,
        duration_sec=round(episode.duration_sec, 3),
        instruction=prompt,
        objects=objects,
        scene_description=str(episode.meta.get("scene_description") or "not_generated"),
        task_taxonomy=taxonomy,
        hands_used=[],
        quality={},
        annotation={
            "source": "auto",
            "version": 1,
            "reviewer": None,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "vision_pipeline": "not_run",
            "target_source": preset_info.get("match_source"),
            "preset_match": preset_info.get("preset_id"),
            "preset_match_source": preset_info.get("match_source"),
        },
        verification={
            "trajectory_level_verified": False,
            "reviewer": None,
        },
    )


def enrich_with_detections(
    trajectory: TrajectoryAnnotation,
    frame_detections: list[Any],
    profile_raw: dict[str, Any],
) -> TrajectoryAnnotation:
    """Merge YOLO detections into trajectory.objects and compute vision QC.

    Called by pyramid.py after the vision runner produces FrameDetections.
    frame_detections: list[FrameDetections]
    """
    from dataclasses import replace

    if not frame_detections:
        return trajectory

    # Gather all detections by frame_key
    by_key: dict[str, list[Any]] = {}
    for fd in frame_detections:
        by_key.setdefault(fd.frame_key, []).extend(fd.detections)

    first_dets = by_key.get("first", [])
    last_dets = by_key.get("last", by_key.get("first", []))

    # Enrich prompt targets with bbox. If no prompt targets exist, fall back to
    # first-frame detections so vision-only annotation still surfaces objects.
    if trajectory.objects:
        updated_objects = []
        for obj in trajectory.objects:
            match = _best_match(obj.label, first_dets)
            if match is not None:
                updated_objects.append(
                    DetectedObject(label=obj.label, bbox=match.bbox, source="local_yolo")
                )
            else:
                updated_objects.append(obj)
    else:
        updated_objects = _objects_from_detections(first_dets)

    # Vision QC
    target_labels = [obj.label for obj in trajectory.objects]
    target_visible = _all_visible(target_labels, first_dets)
    count_ok = _count_consistent(target_labels, first_dets)
    displaced = _targets_displaced(target_labels, first_dets, last_dets)

    quality = {
        **trajectory.quality,
        "target_visible_at_start": target_visible,
        "target_count_consistent": count_ok,
        "target_displaced": displaced,
    }

    vp_cfg = (
        profile_raw.get("processing", {})
        .get("annotations", {})
        .get("vision_pipeline", {})
    )
    detector_name = vp_cfg.get("detector", "unknown")
    weights = vp_cfg.get("weights", "")
    annotation = {
        **trajectory.annotation,
        "vision_pipeline": f"{detector_name}:{weights}",
        "vision_keyframe_detections": _serialise_frame_detections(frame_detections),
    }

    return replace(
        trajectory,
        objects=updated_objects,
        quality=quality,
        annotation=annotation,
    )


# ── helpers ───────────────────────────────────────────────────────────────────

def _dominant_category(verbs: list[str]) -> str | None:
    by_verb = {spec.verb: spec.category for spec in GLOSSARY}
    counts: dict[str, int] = {}
    for verb in verbs:
        cat = by_verb.get(verb)
        if cat:
            counts[cat] = counts.get(cat, 0) + 1
    return max(counts, key=lambda k: counts[k]) if counts else None


def _best_match(label: str, detections: list[Any]) -> Any | None:
    label_low = label.lower().strip()
    scored = []
    for d in detections:
        if d.label.lower() == label_low:
            scored.append((1.0, d))
        elif label_low in d.label.lower() or d.label.lower() in label_low:
            scored.append((0.6, d))
    if not scored:
        return None
    scored.sort(key=lambda x: (-x[0], -x[1].conf))
    return scored[0][1]


def _all_visible(target_labels: list[str], dets: list[Any]) -> bool:
    return all(_best_match(lbl, dets) is not None for lbl in target_labels)


def _count_consistent(target_labels: list[str], dets: list[Any]) -> bool:
    matched = sum(1 for lbl in target_labels if _best_match(lbl, dets) is not None)
    return matched == len(target_labels)


def _targets_displaced(
    target_labels: list[str],
    first_dets: list[Any],
    last_dets: list[Any],
    min_shift: float = 0.05,
) -> bool | None:
    """True if at least one target moved significantly between first and last frame."""
    if not first_dets or not last_dets:
        return None
    any_displaced = False
    for lbl in target_labels:
        f = _best_match(lbl, first_dets)
        la = _best_match(lbl, last_dets)
        if f is None:
            continue
        if la is None:
            any_displaced = True
            continue
        cx_f = (f.bbox[0] + f.bbox[2]) / 2
        cy_f = (f.bbox[1] + f.bbox[3]) / 2
        cx_l = (la.bbox[0] + la.bbox[2]) / 2
        cy_l = (la.bbox[1] + la.bbox[3]) / 2
        shift = ((cx_l - cx_f) ** 2 + (cy_l - cy_f) ** 2) ** 0.5
        if shift > min_shift:
            any_displaced = True
    return any_displaced


def _serialise_frame_detections(frame_detections: list[Any]) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for fd in frame_detections:
        detections = []
        for det in getattr(fd, "detections", []) or []:
            detections.append(
                {
                    "label": getattr(det, "label", ""),
                    "raw_label": getattr(det, "raw_label", ""),
                    "confidence": getattr(det, "conf", None),
                    "bbox_xyxy_norm": getattr(det, "bbox", None),
                    "mask_available": getattr(det, "mask_available", False),
                }
            )
        frames.append(
            {
                "frame_key": getattr(fd, "frame_key", ""),
                "frame_idx": getattr(fd, "frame_idx", None),
                "image_path": getattr(fd, "image_path", ""),
                "error": getattr(fd, "error", ""),
                "detections": detections,
            }
        )
    return frames


def _objects_from_detections(detections: list[Any]) -> list[DetectedObject]:
    objects: list[DetectedObject] = []
    seen: set[str] = set()
    for det in sorted(detections, key=lambda item: -float(getattr(item, "conf", 0.0) or 0.0)):
        label = str(getattr(det, "label", "")).strip()
        key = label.lower()
        if not key or key in seen:
            continue
        objects.append(
            DetectedObject(
                label=label,
                bbox=getattr(det, "bbox", None),
                source="local_yolo",
            )
        )
        seen.add(key)
    return objects
