"""Human verification / manual override (RoboCOIN Appendix C).

If `annotations.manual.json` exists alongside the raw episode (or is passed
explicitly), its fields are deep-merged onto the auto-generated annotations.

Schema (all keys optional):

    {
      "trajectory": {
        "scene_description": "...",
        "task_taxonomy": {"collaboration": "high", "verbs": ["pick", "place"]},
        "verification": {"reviewer": "alice", "trajectory_level_verified": true}
      },
      "segments": [
        {"segment_id": 2, "subtask": "...", "exception": "grasping_failure",
         "verb": "grasp", "boundary_source": "manual"}
      ],
      "scene_layout": {
        "shelf": {
          "physical_levels": 5,
          "drink_rows": 3
        }
      }
    }

A segment override is matched by `segment_id`. Auto segments not mentioned
are kept as-is. Adding a brand-new segment requires also specifying
start_frame/end_frame/hand.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from .schema import SegmentAnnotation, TrajectoryAnnotation


def load_manual(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def apply_manual_trajectory(
    auto: TrajectoryAnnotation,
    manual: dict[str, Any],
) -> TrajectoryAnnotation:
    if not manual:
        return auto
    traj_patch = manual.get("trajectory") or {}
    if not isinstance(traj_patch, dict):
        return auto

    result = auto
    for key in ("instruction", "scene_description"):
        if key in traj_patch:
            result = replace(result, **{key: str(traj_patch[key])})

    if "task_taxonomy" in traj_patch and isinstance(traj_patch["task_taxonomy"], dict):
        tax = result.task_taxonomy
        patch = traj_patch["task_taxonomy"]
        new_tax = replace(
            tax,
            action_category=patch.get("action_category", tax.action_category),
            verbs=list(patch.get("verbs", tax.verbs)),
            collaboration=str(patch.get("collaboration", tax.collaboration)),
            object_flexibility=list(patch.get("object_flexibility", tax.object_flexibility)),
        )
        result = replace(result, task_taxonomy=new_tax)

    if "verification" in traj_patch and isinstance(traj_patch["verification"], dict):
        merged = {**result.verification, **traj_patch["verification"]}
        result = replace(result, verification=merged)

    if "annotation" in traj_patch and isinstance(traj_patch["annotation"], dict):
        merged = {**result.annotation, **traj_patch["annotation"], "source": "auto+manual"}
        result = replace(result, annotation=merged)
    else:
        merged = {**result.annotation, "source": "auto+manual"}
        result = replace(result, annotation=merged)

    return result


def apply_manual_segments(
    auto: list[SegmentAnnotation],
    manual: dict[str, Any],
) -> list[SegmentAnnotation]:
    if not manual:
        return auto
    patches = manual.get("segments") or []
    if not isinstance(patches, list):
        return auto

    by_id = {seg.segment_id: seg for seg in auto}
    for patch in patches:
        if not isinstance(patch, dict):
            continue
        sid = patch.get("segment_id")
        if sid is None or sid not in by_id:
            continue
        existing = by_id[sid]
        updates = {}
        for k in (
            "hand",
            "verb",
            "subtask",
            "target_object",
            "boundary_source",
            "boundary_signal",
            "exception",
            "confidence",
        ):
            if k in patch:
                updates[k] = patch[k]
        if updates:
            by_id[sid] = replace(existing, **updates)

    return [by_id[sid] for sid in sorted(by_id)]
