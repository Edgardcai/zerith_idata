"""Synthetic 640x480 RGB-D CameraClient used by tests and demo mode."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

import cv2
import numpy as np


@dataclass
class _Stream:
    type: str
    width: int = 640
    height: int = 480
    fps: float = 30.0

    def HasField(self, name: str) -> bool:
        return False


@dataclass
class _Camera:
    camera_name: str

    @property
    def streams(self) -> list[_Stream]:
        return [_Stream("color"), _Stream("depth")]


class _State:
    camera_configs = [
        _Camera("rs/cam_left_wrist"),
        _Camera("rs/cam_high"),
        _Camera("rs/cam_right_wrist"),
    ]


class FakeCameraClient:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started = False
        self._sequence = 0
        self._last_frame_time = 0.0
        self._rgb: dict[str, np.ndarray] = {}
        self._depth: dict[str, np.ndarray] = {}
        colours = {
            "rs/cam_left_wrist": (180, 90, 40),
            "rs/cam_high": (55, 160, 80),
            "rs/cam_right_wrist": (65, 90, 200),
        }
        for name, colour in colours.items():
            image = np.full((480, 640, 3), colour, dtype=np.uint8)
            cv2.rectangle(image, (12, 12), (627, 467), (235, 235, 235), 3)
            cv2.putText(
                image,
                name,
                (55, 245),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.9,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )
            self._rgb[name] = image
            horizontal = np.linspace(300, 6000, 640, dtype=np.uint16)
            self._depth[name] = np.repeat(horizontal[None, :], 480, axis=0)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def get_state(self, camera_names: Any = None, timeout: float = 5.0) -> _State:
        if not self.started:
            raise RuntimeError("fake camera is stopped")
        return _State()

    def _timestamp(self) -> float:
        now = time.monotonic()
        # CameraService polls every ~30 ms.  Quantising gives realistic latest-
        # frame duplicate behaviour instead of inventing a frame per getter.
        return int(now * 30.0) / 30.0

    def get_latest_frame(self, camera_name: str):
        if not self.started:
            return None
        return self._rgb[camera_name], self._timestamp()

    def get_latest_depth(self, camera_name: str):
        if not self.started:
            return None
        return self._depth[camera_name], self._timestamp()


def fake_camera_factory(**kwargs: Any) -> FakeCameraClient:
    return FakeCameraClient(**kwargs)


__all__ = ["FakeCameraClient", "fake_camera_factory"]
