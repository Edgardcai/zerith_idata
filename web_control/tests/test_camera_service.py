from __future__ import annotations

import time
import unittest

import cv2
import numpy as np

from control.web_control.camera_service import CameraService
from control.web_control.fake_camera import FakeCameraClient


class CameraServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.created: list[FakeCameraClient] = []

        def factory(**kwargs):
            client = FakeCameraClient(**kwargs)
            self.created.append(client)
            return client

        self.service = CameraService(
            client_factory=factory,
            poll_interval_s=0.005,
            stale_after_s=0.5,
        )

    def tearDown(self) -> None:
        self.service.stop()

    def test_default_off_then_discovers_all_six_original_streams(self) -> None:
        self.assertFalse(self.service.enabled)
        self.assertEqual(self.created, [])
        self.service.start()
        self.assertEqual(len(self.created), 1)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            status = self.service.get_status()
            if all(
                self.service.get_latest(camera, stream) is not None
                for camera in ("left_wrist", "head", "right_wrist")
                for stream in ("rgb", "depth")
            ):
                break
            time.sleep(0.01)
        for camera in ("left_wrist", "head", "right_wrist"):
            rgb = self.service.get_latest(camera, "rgb")
            depth = self.service.get_latest(camera, "depth")
            self.assertIsNotNone(rgb)
            self.assertIsNotNone(depth)
            self.assertEqual(rgb.image.shape, (480, 640, 3))
            self.assertEqual(rgb.image.dtype, np.uint8)
            self.assertEqual(depth.image.shape, (480, 640))
            self.assertEqual(depth.image.dtype, np.uint16)

    def test_rgb_and_depth_jpeg_remain_640_by_480(self) -> None:
        self.service.start()
        for stream in ("rgb", "depth"):
            snapshot = self.service.wait_for_frame("head", stream, timeout=1.0)
            self.assertIsNotNone(snapshot)
            payload = self.service.get_jpeg("head", stream)
            decoded = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
            self.assertEqual(decoded.shape, (480, 640, 3))

    def test_stop_releases_client_and_clears_frames(self) -> None:
        self.service.start()
        self.service.wait_for_frame("head", "rgb", timeout=1.0)
        status = self.service.stop()
        self.assertFalse(status["enabled"])
        self.assertFalse(status["running"])
        self.assertFalse(self.created[0].started)
        self.assertIsNone(self.service.get_latest("head", "rgb"))


if __name__ == "__main__":
    unittest.main()
