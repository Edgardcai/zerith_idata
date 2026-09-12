"""Dataclasses for the 3-level annotation pyramid.

Schema is documented in docs/annotation_schema.md and locked by
tests/test_annotations.py.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

PYRAMID_SCHEMA_VERSION = 1


@dataclass
class DetectedObject:
    label: str
    bbox: list[float] | None = None
    source: str = "instruction_extract"


@dataclass
class TaskTaxonomy:
    """RoboCOIN §III-B taxonomy projected onto a single episode."""
    action_category: str | None = None
    verbs: list[str] = field(default_factory=list)
    collaboration: str = "unknown"        # low | high | unknown
    object_flexibility: list[str] = field(default_factory=list)


@dataclass
class TrajectoryAnnotation:
    """L1 — RoboCOIN §III-C Trajectory-level concepts."""
    episode_id: str
    profile_id: str
    robot: str
    fps: int
    n_frames: int
    duration_sec: float
    instruction: str
    objects: list[DetectedObject] = field(default_factory=list)
    scene_description: str = "not_generated"
    task_taxonomy: TaskTaxonomy = field(default_factory=TaskTaxonomy)
    hands_used: list[str] = field(default_factory=list)
    quality: dict[str, Any] = field(default_factory=dict)
    annotation: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=dict)
    schema_version: int = PYRAMID_SCHEMA_VERSION


@dataclass
class SegmentQuality:
    gripper_consistent: bool = True
    duration_in_range: bool = True
    velocity_in_range: bool = True


@dataclass
class SegmentAnnotation:
    """L2 — RoboCOIN §III-C Segment-level subtask.

    Per paper §III-C, segments MAY temporally overlap (bimanual). The
    `hand` field plus `overlaps_with` make this explicit and machine-readable.
    """
    segment_id: int
    start_frame: int
    end_frame: int
    start_time: float
    end_time: float
    hand: str                                # left | right | both | base | none
    verb: str                                # from glossary.VALID_PHASES
    subtask: str                             # short natural-language instruction
    target_object: str | None = None
    boundary_source: str = "auto"
    boundary_signal: str | None = None       # gripper_close | gripper_open | stationary | task_end | velocity_jump | trajectory_start | trajectory_end
    confidence: float = 0.7
    exception: str | None = None             # grasping_failure | dropped | retry | ...
    quality: SegmentQuality = field(default_factory=SegmentQuality)
    overlaps_with: list[int] = field(default_factory=list)


@dataclass
class HandMotion:
    """Per-hand kinematics for a single frame (paper §IV-B(3))."""
    direction: str
    displacement: list[float]
    velocity: float
    velocity_label: str
    acceleration: float
    acceleration_label: str
    gripper_value: float
    gripper_state: str                       # closed | open | partial | unknown
    gripper_transition: str                  # closing | opening | stationary


@dataclass
class BaseMotion:
    available: bool
    direction: str = "stationary"
    displacement: list[float] = field(default_factory=list)
    velocity: float = 0.0
    velocity_label: str = "stationary"


@dataclass
class FrameAnnotation:
    """L3 — RoboCOIN §III-C Frame-level kinematics."""
    frame_idx: int
    timestamp: float
    segment_ids: list[int] = field(default_factory=list)
    left: HandMotion | None = None
    right: HandMotion | None = None
    base: BaseMotion | None = None
    natural_language: str = ""
    bimanual: bool = False


def to_jsonable(obj: Any) -> Any:
    """Recursive dataclass → dict, dropping None for cleaner JSON."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_jsonable(v) for k, v in asdict(obj).items() if v is not None}
    if isinstance(obj, list):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items() if v is not None}
    return obj
