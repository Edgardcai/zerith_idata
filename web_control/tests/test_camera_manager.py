from __future__ import annotations

import threading
import unittest

from control.web_control.server import CameraConnectionManager


class _BlockingStartCamera:
    def __init__(self) -> None:
        self._running = False
        self.start_entered = threading.Event()
        self.allow_start = threading.Event()

    @property
    def running(self) -> bool:
        return self._running

    def start(self):
        self.start_entered.set()
        if not self.allow_start.wait(2.0):
            raise TimeoutError("test did not release camera start")
        self._running = True
        return self.get_status()

    def stop(self):
        self._running = False
        return self.get_status()

    def get_status(self):
        return {"running": self._running}


class CameraConnectionManagerTests(unittest.TestCase):
    def test_status_does_not_block_behind_vendor_camera_start(self) -> None:
        camera = _BlockingStartCamera()
        manager = CameraConnectionManager(camera, stop_grace_s=60.0)
        entered: list[dict] = []
        worker = threading.Thread(target=lambda: entered.append(manager.enter()))
        worker.start()
        self.assertTrue(camera.start_entered.wait(0.5))

        # This used to deadlock because enter() held the manager lock while
        # CameraClient.start() was blocked in a vendor RPC.
        report = manager.status()
        self.assertFalse(report["running"])
        self.assertEqual(report["web_viewers"], 1)

        camera.allow_start.set()
        worker.join(1.0)
        self.assertFalse(worker.is_alive())
        self.assertTrue(entered[0]["running"])
        manager.leave()
        manager.close()


if __name__ == "__main__":
    unittest.main()
