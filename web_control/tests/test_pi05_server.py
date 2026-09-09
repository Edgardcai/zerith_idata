from __future__ import annotations

import http.client
import json
import threading
import unittest

from control.web_control.camera_service import CameraService
from control.web_control.fake_camera import fake_camera_factory
from control.web_control.fake_sdk import FakeH1Robot, FakeSdk
from control.web_control.pi05_executor import REQUIRED_CONFIRMATION
from control.web_control.robot_service import RobotService
from control.web_control.server import H1WebServer, WebControlApp


class _FakePi05:
    def __init__(self) -> None:
        self.phase = "idle"
        self.probe_calls = 0
        self.dry_runs: list[tuple[str, str]] = []
        self.dry_run_plans: list[dict[str, object]] = []
        self.reconnects: list[tuple[str, int]] = []
        self.disconnect_reasons: list[str] = []
        self.starts: list[tuple[str, str, str, int, float, float]] = []
        self.start_plans: list[dict[str, object]] = []
        self.stop_reasons: list[str] = []
        self.closed = False

    def status(self):
        return {
            "endpoint": "192.168.1.154:9973",
            "phase": self.phase,
            "active": self.phase in ("running", "stopping"),
            "fault": None,
            "metadata_ok": self.probe_calls > 0,
            "dry_run_ok": bool(self.dry_runs),
        }

    def probe(self):
        self.probe_calls += 1
        return {"health": "OK", "metadata": {"robot": "zerith_h1_pro"}, "status": self.status()}

    def reconnect(self, host, port):
        self.reconnects.append((host, port))
        self.probe_calls += 1
        return {"health": "OK", "metadata": {"robot": "zerith_h1_pro"}, "status": self.status()}

    def disconnect(self, *, reason="operator_disconnect", **_kwargs):
        self.disconnect_reasons.append(reason)
        self.phase = "idle"
        return self.status()

    def dry_run(
        self,
        prompt,
        lease,
        *,
        inference_mode="custom",
        active_hand=None,
        right_prompt=None,
    ):
        if not lease:
            raise ValueError("lease required")
        self.phase = "dry_run_ready"
        self.dry_runs.append((prompt, lease))
        self.dry_run_plans.append(
            {
                "inference_mode": inference_mode,
                "active_hand": active_hand,
                "right_prompt": right_prompt,
            }
        )
        return {"ok": True, "chunk_length": 50, "action_dim": 23}

    def start(
        self,
        prompt,
        lease,
        *,
        confirmation,
        steps_per_chunk=30,
        control_rate_hz=30,
        joint_speed_deg_s=30,
        inference_mode="custom",
        active_hand=None,
        right_prompt=None,
    ):
        if confirmation != REQUIRED_CONFIRMATION:
            raise ValueError("confirmation required")
        self.phase = "running"
        self.starts.append(
            (
                prompt,
                lease,
                confirmation,
                steps_per_chunk,
                control_rate_hz,
                joint_speed_deg_s,
            )
        )
        self.start_plans.append(
            {
                "inference_mode": inference_mode,
                "active_hand": active_hand,
                "right_prompt": right_prompt,
            }
        )
        return self.status()

    def stop(self, *, reason="operator_stop", **_kwargs):
        self.stop_reasons.append(reason)
        self.phase = "idle"
        return self.status()

    def reset_fault(self):
        self.phase = "idle"
        return self.status()

    def close(self):
        self.closed = True


class _FakeVoice:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def status(self):
        return {"available": True, "state": "idle", "messages": []}

    def audio(self, audio_id):
        self.calls.append(("audio", audio_id))
        return b"RIFF"

    def start_session(self, language):
        self.calls.append(("start", language))
        return {"ok": True}

    def finish_input(self):
        self.calls.append(("finish",))
        return {"ok": True}

    def cancel(self):
        self.calls.append(("cancel",))
        return {"ok": True}

    def submit_text(self, text, language):
        self.calls.append(("text", text, language))
        return {"ok": True}


class _FakeVoiceMotion:
    def __init__(self) -> None:
        self.enabled = False
        self.calls: list[tuple] = []

    def status(self):
        return {"available": True, "enabled": self.enabled}

    def set_enabled(self, lease, enabled):
        self.enabled = enabled
        self.calls.append(("set_enabled", lease, enabled))
        return self.status()

    def disable(self, *, reason, requested_lease=""):
        self.enabled = False
        self.calls.append(("disable", reason, requested_lease))

    def cancel_active(self, lease):
        self.calls.append(("cancel_active", lease))

    def close(self):
        self.calls.append(("close",))


class Pi05ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        sdk = FakeSdk()
        robot = RobotService(
            sdk_loader=lambda: sdk,
            robot_factory=lambda _sdk: FakeH1Robot(),
            lease_seconds=2.0,
            trajectory_rate_hz=250,
            state_rate_hz=20,
        )
        camera = CameraService(client_factory=fake_camera_factory, poll_interval_s=0.005)
        self.pi05 = _FakePi05()
        self.voice = _FakeVoice()
        self.voice_motion = _FakeVoiceMotion()
        self.app = WebControlApp(
            robot=robot,
            camera=camera,
            voice=self.voice,
            voice_motion=self.voice_motion,
            pi05=self.pi05,
        )
        self.server = H1WebServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.app.close()
        self.thread.join(2.0)

    def request(self, method: str, path: str, body=None, lease: str | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if lease:
            headers["X-Control-Lease"] = lease
        connection.request(method, path, payload, headers)
        response = connection.getresponse()
        value = json.loads(response.read())
        status = response.status
        connection.close()
        return status, value

    def test_state_and_explicit_status_expose_pi05(self) -> None:
        status, value = self.request("GET", "/api/pi05/status")
        self.assertEqual(status, 200)
        self.assertEqual(value["phase"], "idle")
        status, value = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(value["pi05"]["endpoint"], "192.168.1.154:9973")

    def test_probe_dry_run_start_and_global_stop_routes(self) -> None:
        status, value = self.request(
            "POST",
            "/api/pi05/reconnect",
            {"host": "192.168.1.155", "port": 9988},
        )
        self.assertEqual((status, value["health"]), (200, "OK"))
        self.assertEqual(self.pi05.reconnects, [("192.168.1.155", 9988)])

        status, value = self.request("POST", "/api/pi05/probe", {})
        self.assertEqual((status, value["health"]), (200, "OK"))

        status, takeover = self.request(
            "POST",
            "/api/takeover",
            {"enabled": True, "client_id": "pi05-test"},
        )
        self.assertEqual(status, 200)
        lease = takeover["lease_id"]
        status, _ = self.request("POST", "/api/actions/init", {}, lease)
        self.assertEqual(status, 200)

        prompt = "把物体放入盒子"
        status, value = self.request(
            "POST", "/api/pi05/dry-run", {"prompt": prompt}, lease
        )
        self.assertEqual((status, value["chunk_length"]), (200, 50))
        status, value = self.request(
            "POST",
            "/api/pi05/start",
            {
                "prompt": prompt,
                "steps_per_chunk": 30,
                "control_rate_hz": 25,
                "joint_speed_deg_s": 20,
                "confirmation": REQUIRED_CONFIRMATION,
            },
            lease,
        )
        self.assertEqual(status, 202)
        self.assertEqual(value["phase"], "running")
        self.assertEqual(self.pi05.starts[-1][3:], (30, 25, 20))
        self.assertEqual(
            self.pi05.start_plans[-1],
            {
                "inference_mode": "custom",
                "active_hand": None,
                "right_prompt": None,
            },
        )

    def test_prompt_plan_fields_are_forwarded_to_dry_run_and_start(self) -> None:
        status, takeover = self.request(
            "POST",
            "/api/takeover",
            {"enabled": True, "client_id": "pi05-plan-test"},
        )
        self.assertEqual(status, 200)
        lease = takeover["lease_id"]

        left_prompt = "Grasp Coca-Cola with the left hand"
        right_prompt = "Grasp Vita Coconut with the right hand"
        task = {
            "prompt": left_prompt,
            "inference_mode": "dual_separate",
            "active_hand": None,
            "right_prompt": right_prompt,
        }
        status, _ = self.request("POST", "/api/pi05/dry-run", task, lease)
        self.assertEqual(status, 200)
        self.assertEqual(
            self.pi05.dry_run_plans[-1],
            {
                "inference_mode": "dual_separate",
                "active_hand": None,
                "right_prompt": right_prompt,
            },
        )

        status, _ = self.request(
            "POST",
            "/api/pi05/start",
            {
                **task,
                "confirmation": REQUIRED_CONFIRMATION,
            },
            lease,
        )
        self.assertEqual(status, 202)
        self.assertEqual(self.pi05.start_plans[-1], self.pi05.dry_run_plans[-1])

        # Pi STOP is deliberately available to any authenticated console,
        # even if the page that started the run disappeared with its lease.
        status, value = self.request("POST", "/api/pi05/stop", {})
        self.assertEqual((status, value["phase"]), (200, "idle"))
        self.assertIn("web_operator_stop", self.pi05.stop_reasons)

        status, value = self.request("POST", "/api/pi05/disconnect", {})
        self.assertEqual((status, value["phase"]), (200, "idle"))
        self.assertEqual(
            self.pi05.disconnect_reasons,
            ["web_operator_disconnect"],
        )

    def test_start_rejects_missing_confirmation(self) -> None:
        status, _value = self.request(
            "POST",
            "/api/pi05/start",
            {
                "prompt": "test",
                "steps_per_chunk": 30,
                "confirmation": "确认",
            },
            "lease",
        )
        self.assertEqual(status, 400)

    def test_running_phase_blocks_every_ordinary_control_route(self) -> None:
        self.pi05.phase = "running"
        blocked = (
            ("POST", "/api/motion/joint", {"motor_id": 7, "target": 0.0}),
            (
                "POST",
                "/api/motion/chassis",
                {"left_speed": 0.0, "right_speed": 0.01},
            ),
            ("POST", "/api/actions/init", {}),
            ("POST", "/api/actions/deinit", {}),
            ("POST", "/api/actions/home", {}),
            ("POST", "/api/actions/arm-home", {}),
            ("POST", "/api/voice/start", {"language": "zh"}),
            ("POST", "/api/voice/finish-input", {}),
            ("POST", "/api/voice/text", {"text": "向前走", "language": "zh"}),
            ("POST", "/api/voice/motion", {"enabled": True}),
            (
                "POST",
                "/api/takeover",
                {"enabled": True, "client_id": "blocked-client"},
            ),
            ("GET", "/api/voice/asr/ws", None),
        )
        for method, path, body in blocked:
            with self.subTest(path=path):
                status, value = self.request(method, path, body, "unused-lease")
                self.assertEqual(status, 409)
                self.assertEqual(value["type"], "RobotConflict")
                self.assertIn("Pi0.5", value["error"])

        self.assertEqual(self.voice.calls, [])
        self.assertEqual(self.voice_motion.calls, [])

    def test_stopping_fault_and_unknown_phases_fail_closed(self) -> None:
        for phase in ("stopping", "fault", "future_phase", None):
            with self.subTest(phase=phase):
                self.pi05.phase = phase
                status, value = self.request(
                    "POST",
                    "/api/motion/joint",
                    {"motor_id": 7, "target": 0.0},
                    "unused-lease",
                )
                self.assertEqual(status, 409)
                self.assertEqual(value["type"], "RobotConflict")

    def test_reads_and_safety_exit_routes_remain_available(self) -> None:
        status, takeover = self.request(
            "POST",
            "/api/takeover",
            {"enabled": True, "client_id": "safe-exit-test"},
        )
        self.assertEqual(status, 200)
        lease = takeover["lease_id"]

        self.pi05.phase = "running"
        for path in (
            "/api/config",
            "/api/state",
            "/api/pi05/status",
            "/api/cameras/status",
            "/api/voice/status",
        ):
            with self.subTest(path=path):
                status, _value = self.request("GET", path)
                self.assertEqual(status, 200)

        status, value = self.request("POST", "/api/heartbeat", {}, lease)
        self.assertEqual((status, value["ok"]), (200, True))
        status, value = self.request("POST", "/api/voice/cancel", {})
        self.assertEqual((status, value["ok"]), (202, True))
        status, value = self.request(
            "POST", "/api/voice/motion", {"enabled": False}, lease
        )
        self.assertEqual(status, 200)
        self.assertFalse(value["enabled"])

        # Probe, STOP and fault reset have their own Pi state validation, but
        # the HTTP gate must never prevent them from reaching the executor.
        self.pi05.phase = "running"
        status, value = self.request("POST", "/api/pi05/probe", {})
        self.assertEqual((status, value["health"]), (200, "OK"))
        status, value = self.request("POST", "/api/pi05/stop", {})
        self.assertEqual((status, value["phase"]), (200, "idle"))
        self.pi05.phase = "fault"
        status, value = self.request("POST", "/api/pi05/reset-fault", {})
        self.assertEqual((status, value["phase"]), (200, "idle"))

        # Releasing takeover is also a safety exit.  It first requests Pi STOP
        # and is allowed even if the reported phase is unknown.
        self.pi05.phase = "future_phase"
        status, value = self.request(
            "POST", "/api/takeover", {"enabled": False}, lease
        )
        self.assertEqual(status, 200)
        self.assertFalse(value["takeover"])

    def test_exact_double_zero_chassis_and_global_stop_remain_available(self) -> None:
        status, takeover = self.request(
            "POST",
            "/api/takeover",
            {"enabled": True, "client_id": "zero-chassis-test"},
        )
        self.assertEqual(status, 200)
        lease = takeover["lease_id"]
        status, _value = self.request("POST", "/api/actions/init", {}, lease)
        self.assertEqual(status, 200)

        self.pi05.phase = "running"
        status, _value = self.request(
            "POST",
            "/api/motion/chassis",
            {"left_speed": 0, "right_speed": -0.0},
            lease,
        )
        self.assertEqual(status, 200)

        for left, right in (("0", 0), (False, 0), (0, 1e-300)):
            with self.subTest(left=left, right=right):
                self.pi05.phase = "running"
                status, value = self.request(
                    "POST",
                    "/api/motion/chassis",
                    {"left_speed": left, "right_speed": right},
                    lease,
                )
                self.assertEqual(status, 409)
                self.assertEqual(value["type"], "RobotConflict")

        self.pi05.phase = "running"
        status, value = self.request("POST", "/api/stop", {}, lease)
        self.assertEqual((status, value["ok"]), (200, True))
        self.assertIn("global_software_stop", self.pi05.stop_reasons)


if __name__ == "__main__":
    unittest.main()
