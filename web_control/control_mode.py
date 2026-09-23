"""One control-mode contract for the 8080 service and its clients.

The SDK's gravity-compensated mode uses the existing low-level setters.
Selecting a different mode is a lifecycle operation, never a per-action choice.
"""

from typing import Any

CONTROL_MODE_ID = 3
CONTROL_MODE_NAME = "GRAVITY_COMPENSATION_LEVEL"
CONTROL_MODE_LABEL = "重力补偿"


def sdk_control_mode(sdk: Any) -> Any:
    """Require explicit SDK support; never silently fall back to LOW_LEVEL."""
    mode = getattr(sdk.MotorControlMode, CONTROL_MODE_NAME, None)
    if mode is None or int(getattr(mode, "value", mode)) != CONTROL_MODE_ID:
        raise RuntimeError(f"SDK 不支持预期的 {CONTROL_MODE_NAME}({CONTROL_MODE_ID})")
    return mode
