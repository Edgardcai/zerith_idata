"""Hierarchical 3-level annotations following RoboCOIN / CoRobot.

Reference: Wu et al., "RoboCOIN: An Open-Sourced Bimanual Robotic Data
Collection for Integrated Manipulation", arXiv:2511.17441, §III-C and §IV-B.

Layers:
    L1 trajectory  - global scene + task concept
    L2 segments    - atomic subtasks per hand, may overlap (bimanual)
    L3 frames      - cascaded sliding window kinematics + NL descriptor

Public re-exports:
    build_pyramid           build all three levels
    build_annotations       legacy aggregate (back-compat shim)
    iter_frame_annotations  legacy flat frame rows (back-compat shim)
"""

from __future__ import annotations

from .frames import iter_frame_annotations, iter_frame_rows
from .pyramid import build_annotations, build_pyramid
from .segments import infer_segments
from .task_preset import resolve as resolve_task_preset

__all__ = [
    "build_annotations",
    "build_pyramid",
    "infer_segments",
    "iter_frame_annotations",
    "iter_frame_rows",
    "resolve_task_preset",
]
