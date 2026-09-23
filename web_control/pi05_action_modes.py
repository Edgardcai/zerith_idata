"""Hardware-independent execution ranges for the 23-D Zerith wire protocol."""

from dataclasses import dataclass
import math


EXECUTION_MODES = ("arms", "arms_lift", "full")
DEFAULT_EXECUTION_MODE = "arms"


def validate_execution_mode(value: str) -> str:
    if not isinstance(value, str) or value not in EXECUTION_MODES:
        raise ValueError(f"execution_mode must be one of {list(EXECUTION_MODES)!r}")
    return value


def model_dimensions(mode: str) -> int:
    return {"arms": 16, "arms_lift": 17, "full": 23}[validate_execution_mode(mode)]


def apply_execution_mode(action, initial_state, mode: str) -> list[float]:
    """Keep disabled position axes at one session snapshot; hold base stopped."""
    count = model_dimensions(mode)
    if len(action) != 23 or len(initial_state) != 23:
        raise ValueError("action and initial_state must contain 23 values")
    result = list(action)
    if count < 23:
        result[count:21] = initial_state[count:21]
        result[21:23] = [0.0, 0.0]
    return result


@dataclass(frozen=True)
class ChassisKinematics:
    """Explicit measured geometry and SDK wheel signs; no guessed defaults."""

    wheel_radius_m: float
    wheel_track_m: float
    left_sign: int
    right_sign: int

    def __post_init__(self):
        for value in (self.wheel_radius_m, self.wheel_track_m):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("chassis radius and track must be finite positive metres")
        for value in (self.left_sign, self.right_sign):
            if type(value) is not int or value not in (-1, 1):
                raise ValueError("chassis wheel signs must be integers -1 or 1")

    def wheel_speeds(self, linear: float, angular: float) -> tuple[float, float]:
        left = self.left_sign * (linear - angular * self.wheel_track_m / 2) / self.wheel_radius_m
        right = self.right_sign * (linear + angular * self.wheel_track_m / 2) / self.wheel_radius_m
        if not all(map(math.isfinite, (left, right))):
            raise ValueError("converted wheel speeds must be finite")
        return left, right
