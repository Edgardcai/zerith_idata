"""Cascaded sliding-window kinematics per RoboCOIN §IV-B(3).

Formulas (verbatim from the paper):
    m_t = x_t - x_{t-n}                  # displacement
    v_t = || m_t - m_{t-1} ||_2 / n      # velocity (norm of disp. difference)
    a_t = (v_t - v_{t-n}) / n            # acceleration

The "summary operator" maps continuous numbers to discrete labels via
threshold bands.

This module is intentionally embodiment-agnostic. It accepts a position
trajectory as a list of vectors plus a window size `n` and threshold config.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class KinematicsConfig:
    window: int = 5
    vel_slow_thresh: float = 0.02
    vel_fast_thresh: float = 0.15
    acc_thresh: float = 0.02


@dataclass
class KinematicSeries:
    displacement: list[list[float]]
    velocity: list[float]
    acceleration: list[float]


def cascaded_kinematics(
    positions: list[list[float]],
    config: KinematicsConfig,
) -> KinematicSeries:
    """Run the 3-stage cascaded window (paper §IV-B(3))."""
    n_frames = len(positions)
    n = max(1, int(config.window))
    dim = len(positions[0]) if n_frames > 0 else 0

    displacement: list[list[float]] = [[0.0] * dim for _ in range(n_frames)]
    velocity: list[float] = [0.0] * n_frames
    acceleration: list[float] = [0.0] * n_frames

    if n_frames == 0:
        return KinematicSeries(displacement, velocity, acceleration)

    for t in range(n_frames):
        prev = max(0, t - n)
        displacement[t] = [positions[t][i] - positions[prev][i] for i in range(dim)]

    for t in range(n_frames):
        if t == 0:
            velocity[t] = 0.0
            continue
        diff = [displacement[t][i] - displacement[t - 1][i] for i in range(dim)]
        velocity[t] = math.sqrt(sum(v * v for v in diff)) / n

    for t in range(n_frames):
        prev = max(0, t - n)
        acceleration[t] = (velocity[t] - velocity[prev]) / n

    return KinematicSeries(displacement, velocity, acceleration)


def velocity_label(speed: float, config: KinematicsConfig) -> str:
    """Summary operator for speed (paper §IV-B(3) eq.)."""
    if speed < config.vel_slow_thresh:
        return "stationary"
    if speed <= config.vel_fast_thresh:
        return "slow"
    return "fast"


def acceleration_label(acc: float, config: KinematicsConfig) -> str:
    if acc > config.acc_thresh:
        return "accelerating"
    if acc < -config.acc_thresh:
        return "decelerating"
    return "constant"


def direction_label(delta: list[float]) -> str:
    """Pick the dominant axis. Convention matches paper §III-A
    (right-handed: x-forward, y-left, z-up)."""
    if not delta or max(abs(v) for v in delta) < 1e-6:
        return "stationary"
    axis_idx = max(range(len(delta)), key=lambda i: abs(delta[i]))
    sign = delta[axis_idx]
    if len(delta) >= 3:
        names_pos = ["forward", "left", "up"]
        names_neg = ["backward", "right", "down"]
    elif len(delta) == 2:
        names_pos = ["forward", "left"]
        names_neg = ["backward", "right"]
    else:
        names_pos = ["positive"]
        names_neg = ["negative"]
    if axis_idx >= len(names_pos):
        return f"axis{axis_idx}_{'+' if sign >= 0 else '-'}"
    return names_pos[axis_idx] if sign >= 0 else names_neg[axis_idx]
