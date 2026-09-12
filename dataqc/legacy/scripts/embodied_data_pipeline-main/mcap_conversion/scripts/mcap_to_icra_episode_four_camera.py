#!/usr/bin/env python3
"""Four-camera defaults for the shared ICRA episode converter."""

from __future__ import annotations

import sys
from pathlib import Path

from mcap_to_icra_episode import main


PIPELINE_ROOT = Path(__file__).resolve().parents[2]
FOUR_CAMERA_PROFILE = PIPELINE_ROOT / "robot_profiles" / "aloha_four_camera.yaml"
FOUR_CAMERA_TOPIC_YAML = (
    PIPELINE_ROOT / "mcap_conversion" / "topic_configs" / "aloha_four_camera_data_params.yaml"
)


def has_option(argv: list[str], option: str) -> bool:
    return option in argv or any(value.startswith(f"{option}=") for value in argv)


def four_camera_args(argv: list[str]) -> list[str]:
    result = list(argv)
    defaults = (
        ("--profile", str(FOUR_CAMERA_PROFILE)),
        ("--alohaYaml", str(FOUR_CAMERA_TOPIC_YAML)),
        ("--cameraLayout", "four_camera"),
    )
    for option, value in defaults:
        if not has_option(result, option):
            result.extend([option, value])
    return result


if __name__ == "__main__":
    raise SystemExit(main(four_camera_args(sys.argv[1:])))
