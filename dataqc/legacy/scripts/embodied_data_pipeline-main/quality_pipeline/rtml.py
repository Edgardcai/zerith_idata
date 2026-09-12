from __future__ import annotations

from typing import Any

from .episode_io import EpisodeData
from .profiles import RobotProfile


def build_rtml_spec(profile: RobotProfile) -> dict[str, Any]:
    processing = profile.processing.get("rtml", {})
    global_constraints = {
        "state_dim": profile.state_dim,
        "action_dim": profile.action_dim,
        "required_cameras": profile.required_camera_keys,
        "timestamp_monotonic": True,
        "max_timestamp_gap_sec": 0.3,
        "min_duration_sec": 2.0,
        "max_missing_camera_ratio": 0.02,
        "max_state_step": 0.8,
        "max_joint_step": 0.8,
    }
    global_constraints.update(processing.get("global_constraints", {}))
    return {
        "rtml_version": "agibot-0.2",
        "profile_id": profile.profile_id,
        "task": {"id": "generic_bimanual_manipulation"},
        "global_constraints": global_constraints,
        # Phase vocabulary aligns with RoboCOIN paper (arXiv:2511.17441) Appendix A
        # Table III verbs + NON_VERB_PHASES from quality_pipeline.annotations.glossary.
        "local_constraints": [
            {"name": "approach", "description": "Reach toward target or pre-manipulation pose."},
            {"name": "grasp",    "description": "Detected from gripper closing transitions."},
            {"name": "lift",     "description": "Vertical motion after a grasp."},
            {"name": "transit",  "description": "Transport phase between grasp and place."},
            {"name": "place",    "description": "Detected from gripper opening transitions at target."},
            {"name": "idle",     "description": "Gripper and arm stationary between atomic actions."},
            {"name": "end",      "description": "Terminal phase of the trajectory."},
        ],
    }


def build_rtml_report(
    episode: EpisodeData,
    profile: RobotProfile,
    qc_report: dict[str, Any],
    annotations: dict[str, Any],
) -> dict[str, Any]:
    return {
        "rtml_spec": build_rtml_spec(profile),
        "evaluation": {
            "episode_id": episode.episode_id,
            "profile_id": profile.profile_id,
            "accepted": qc_report["accepted"],
            "quality_score": qc_report["quality_score"],
            "checks": qc_report["checks"],
        },
        "local_phase_report": {
            "segments": annotations.get("segments", []),
            "segment_count": len(annotations.get("segments", [])),
        },
    }
