from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import cv2
import h5py
import numpy as np

from control.web_control.camera_service import CameraService
from control.web_control.fake_camera import fake_camera_factory
from control.web_control.fake_sdk import FakeH1Robot, FakeSdk
from control.web_control.robot_service import RobotService
from control.web_control.replay_controller import REPLAY_CONFIRMATION, ReplayController
from control.web_control.server import (
    H1WebServer,
    H1RequestHandler,
    WebControlApp,
    _validate_access_policy,
    build_parser,
)


class FakeVoiceGateway:
    def __init__(self) -> None:
        self.started = 0
        self.finished = 0
        self.text_requests = []
        self.language = None
        self.audio_payload = b"RIFF-test-audio"
        self.cancelled = 0
        self.local_speech_requests = []

    def status(self):
        return {
            "available": True,
            "state": "idle",
            "detail": "请说小达唤醒",
            "sequence": 2,
            "session_id": 1,
            "messages": [{"id": 1, "role": "assistant", "text": "你好", "audio_id": 7}],
        }

    def start_session(self, language="zh"):
        self.started += 1
        self.language = language
        return {"accepted": True, "message": "已请求开始对话"}

    def finish_input(self):
        self.finished += 1
        return {"accepted": True, "message": "已结束输入"}

    def submit_text(self, text, language="zh"):
        self.text_requests.append((text, language))
        return {"accepted": True, "message": "文字已发送"}

    def cancel(self):
        self.cancelled += 1
        return {"accepted": True, "message": "已停止"}

    def audio(self, audio_id):
        if audio_id != 7:
            raise AssertionError(audio_id)
        return self.audio_payload

    def set_local_speech(self, enabled):
        self.local_speech_requests.append(enabled)
        return {"accepted": True, "message": "正在切换"}


class ServerTests(unittest.TestCase):
    def test_deployed_bind_defaults(self) -> None:
        args = build_parser().parse_args([])
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 8080)
        self.assertTrue(args.allow_unauthenticated_lan)

    def setUp(self) -> None:
        self.sdk = FakeSdk()
        self.fake_robot = FakeH1Robot()
        robot = RobotService(
            sdk_loader=lambda: self.sdk,
            robot_factory=lambda _sdk: self.fake_robot,
            lease_seconds=2.0,
            trajectory_rate_hz=250,
            state_rate_hz=20,
            home_duration_s=0.02,
            home_lift_timeout_s=0.2,
        )
        camera = CameraService(
            client_factory=fake_camera_factory,
            poll_interval_s=0.005,
        )
        self.voice = FakeVoiceGateway()
        self.app = WebControlApp(
            robot=robot,
            camera=camera,
            voice=self.voice,
            replay=ReplayController(robot, alignment_duration_s=0),
        )
        self.server = H1WebServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.app.close()
        self.thread.join(2.0)

    def request(self, method: str, path: str, body=None, lease=None, headers=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=10
        )
        headers = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if lease:
            headers["X-Control-Lease"] = lease
        connection.request(method, path, payload, headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, response.getheaders(), raw

    def post(self, path: str, body: dict, lease=None):
        status, _, raw = self.request("POST", path, body, lease)
        value = json.loads(raw)
        self.assertEqual(status, 200, value)
        return value

    def test_static_config_and_complete_fake_robot_lifecycle(self) -> None:
        status, _, html = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"ZERITH H1", html)
        status, _, raw = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        config = json.loads(raw)
        self.assertEqual(len(config["motors"]), 21)
        self.assertEqual(config["motion_speed"]["default"], 1.0)

        takeover = self.post(
            "/api/takeover",
            {"enabled": True, "client_id": "lifecycle-page"},
        )
        lease = takeover["lease_id"]
        self.assertEqual(takeover["state"]["takeover_client_id"], "lifecycle-page")
        self.post("/api/actions/init", {}, lease)
        self.post(
            "/api/motion/joint",
            {"motor_id": 7, "target": 0.2, "duration_s": 0.01},
            lease,
        )
        self.post("/api/motion/chassis", {"left_speed": 1, "right_speed": 1}, lease)
        self.post("/api/stop", {}, lease)
        home = self.post("/api/actions/home", {}, lease)
        self.assertEqual(home["targets"]["2"], 0.4)
        self.assertTrue(
            all(home["targets"][str(motor_id)] == 0.0 for motor_id in range(3, 23))
        )

        self.fake_robot.states[2].Position_Actual = 0.57
        self.fake_robot.states[7].Position_Actual = 0.25
        self.fake_robot.states[14].Position_Actual = 1.5
        arm_home = self.post(
            "/api/actions/arm-home",
            {"speed_scale": 2.0},
            lease,
        )
        self.assertEqual(arm_home["lift_held"], 0.57)
        self.assertEqual(arm_home["targets"]["2"], 0.57)
        self.assertTrue(
            all(
                arm_home["targets"][str(motor_id)] == 0.0
                for motor_id in range(3, 23)
            )
        )
        self.post("/api/actions/deinit", {}, lease)
        self.post("/api/takeover", {"enabled": False}, lease)

    def test_camera_layout_fits_three_columns_and_robot_mic_is_default(self) -> None:
        status, _, css = self.request("GET", "/static/style.css")
        self.assertEqual(status, 200)
        self.assertIn(b"repeat(3, minmax(0, 1fr))", css)
        self.assertNotIn(b"repeat(3, 640px)", css)
        self.assertIn(b"aspect-ratio: 4 / 3", css)
        self.assertIn(b"color-scheme: light", css)
        self.assertIn(b"--accent: #324376", css)
        self.assertIn(b".pi05-primary-safety-actions", css)

        status, _, html = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("机器人独立麦克风".encode(), html)
        self.assertIn(b'id="pi05ArmHomeButton"', html)
        self.assertIn("机械臂归位".encode(), html)
        for element_id in (
            b"pi05TaskSingle",
            b"pi05TaskDual",
            b"pi05SingleItem",
            b"pi05SingleHand",
            b"pi05DualLeftItem",
            b"pi05DualRightItem",
            b"pi05DualContinuous",
            b"pi05DualSeparate",
            b"pi05PromptPreview",
            b"pi05TaskStage",
            b"pi05LeftGripperProgress",
        ):
            self.assertIn(b'id="' + element_id + b'"', html)
        self.assertIn(b'id="replayPage"', html)
        self.assertIn("真机回放".encode(), html)
        self.assertIn(b'id="replayDirectoryOptions"', html)
        self.assertIn(b'id="replayDirectoryRefreshButton"', html)
        self.assertIn(b'id="replaySpeed" type="range" min="0.5" max="2"', html)
        self.assertIn(b'<meta name="theme-color" content="#ffffff">', html)

        status, _, javascript = self.request("GET", "/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"await startRobotMicrophoneSession(language);", javascript)
        self.assertIn(b'"arm-home"', javascript)
        self.assertIn(b'"/api/replay/start"', javascript)
        self.assertIn(b'inference_mode: "dual_separate"', javascript)
        self.assertIn(b'right_prompt:', javascript)
        for product in (
            "Taro Milk",
            "Coca-Cola",
            "NEVER Coconut Latte",
            "If coconut",
            "Pocky Chocolate",
        ):
            self.assertIn(product.encode(), javascript)

    def test_hdf5_replay_scan_start_and_status_routes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            episode_dir = os.path.join(directory, "episode-001")
            os.makedirs(episode_dir)
            episode_path = os.path.join(episode_dir, "episode.hdf5")
            count = 4
            with h5py.File(episode_path, "w") as file:
                file.attrs["episode_id"] = "episode-001"
                file.attrs["task_name"] = "server replay"
                file.attrs["control_frequency"] = 30
                file.attrs["action_mode"] = "absolute"
                file.create_dataset("action/arm/position", data=np.zeros((count, 14)))
                file.create_dataset("action/effector/position", data=np.zeros((count, 2)))
                file.create_dataset(
                    "action/waist/position",
                    data=np.tile(np.asarray((0.4, 0.0, 0.0)), (count, 1)),
                )
                file.create_dataset("action/head/position", data=np.zeros((count, 2)))
                file.create_dataset("action/base/velocity", data=np.zeros((count, 2)))

            scan = self.post("/api/replay/scan", {"dataset_dir": directory})
            self.assertEqual(scan["count"], 1)
            self.assertEqual(scan["episodes"][0]["path"], "episode-001/episode.hdf5")

            takeover = self.post(
                "/api/takeover",
                {"enabled": True, "client_id": "replay-page"},
            )
            lease = takeover["lease_id"]
            self.post("/api/actions/init", {}, lease)
            status, _, raw = self.request(
                "POST",
                "/api/replay/start",
                {
                    "dataset_dir": directory,
                    "episode": "episode-001/episode.hdf5",
                    "source": "action",
                    "mode": "full",
                    "speed": 1.0,
                    "confirmation": REPLAY_CONFIRMATION,
                },
                lease,
            )
            self.assertEqual(status, 202, raw)
            deadline = time.monotonic() + 2.0
            replay_status = {}
            while time.monotonic() < deadline:
                code, _, payload = self.request("GET", "/api/replay/status")
                self.assertEqual(code, 200)
                replay_status = json.loads(payload)
                if replay_status["phase"] == "completed":
                    break
                time.sleep(0.02)
            self.assertEqual(replay_status["phase"], "completed")
            self.assertEqual(replay_status["progress"], count)

            state_code, _, state_raw = self.request("GET", "/api/state")
            self.assertEqual(state_code, 200)
            self.assertEqual(json.loads(state_raw)["replay"]["phase"], "completed")
            self.post("/api/actions/deinit", {}, lease)
            self.post("/api/takeover", {"enabled": False}, lease)

    def test_hdf5_replay_directory_discovery_route(self) -> None:
        expected = {
            "root": "/data",
            "directories": ["/data/example"],
            "count": 1,
            "inspected_episode_files": 1,
            "invalid_count": 0,
            "truncated": False,
        }
        with patch.object(self.app.replay, "directories", return_value=expected):
            status, _, raw = self.request("GET", "/api/replay/directories")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), expected)

    def test_voice_status_start_and_audio_are_proxied(self) -> None:
        status, _, raw = self.request("GET", "/api/voice/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["messages"][0]["text"], "你好")

        status, _, raw = self.request("POST", "/api/voice/start", {"language": "en"})
        self.assertEqual(status, 202)
        result = json.loads(raw)
        self.assertTrue(result["accepted"])
        self.assertEqual(self.voice.started, 1)
        self.assertEqual(self.voice.language, "en")

        status, _, raw = self.request("POST", "/api/voice/finish-input", {})
        self.assertEqual(status, 202)
        self.assertTrue(json.loads(raw)["accepted"])
        self.assertEqual(self.voice.finished, 1)

        status, _, raw = self.request(
            "POST",
            "/api/voice/text",
            {"text": "  向前移动  ", "language": "zh"},
        )
        self.assertEqual(status, 202)
        self.assertTrue(json.loads(raw)["accepted"])
        self.assertEqual(self.voice.text_requests, [("向前移动", "zh")])

        status, _, raw = self.request("POST", "/api/voice/cancel", {})
        self.assertEqual(status, 202)
        self.assertTrue(json.loads(raw)["accepted"])
        self.assertEqual(self.voice.cancelled, 1)

        status, headers, raw = self.request("GET", "/api/voice/audio/7.wav")
        self.assertEqual(status, 200)
        self.assertEqual(dict(headers)["Content-Type"], "audio/wav")
        self.assertEqual(raw, self.voice.audio_payload)

    def test_local_speech_requires_boolean_and_proxies_explicit_requests(self) -> None:
        for enabled in (True, False):
            status, _, raw = self.request("POST", "/api/voice/local-speech", {"enabled": enabled})
            self.assertEqual(status, 202)
            self.assertTrue(json.loads(raw)["accepted"])
        self.assertEqual(self.voice.local_speech_requests, [True, False])
        for body in ({}, {"enabled": "true"}, {"enabled": 1}, {"enabled": None}):
            status, _, _ = self.request("POST", "/api/voice/local-speech", body)
            self.assertEqual(status, 400)
        self.assertEqual(self.voice.local_speech_requests, [True, False])

    def test_voice_text_rejects_invalid_input(self) -> None:
        for body in (
            {"text": ""},
            {"text": "   "},
            {"text": 123},
            {"text": "你好", "language": "fr"},
            {"text": "字" * 1001},
        ):
            with self.subTest(body=body):
                status, _, _raw = self.request("POST", "/api/voice/text", body)
                self.assertEqual(status, 400)

    def test_voice_motion_requires_takeover_and_explicit_opt_in(self) -> None:
        status, _, raw = self.request(
            "POST",
            "/api/voice/motion",
            {"enabled": True},
        )
        self.assertEqual(status, 409, raw)

        takeover = self.post(
            "/api/takeover",
            {"enabled": True, "client_id": "voice-motion-page"},
        )
        lease = takeover["lease_id"]
        self.post("/api/actions/init", {}, lease)
        enabled = self.post("/api/voice/motion", {"enabled": True}, lease)
        self.assertTrue(enabled["enabled"])

        status, _, raw = self.request("GET", "/api/voice/status")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["motion"]["enabled"])

        disabled = self.post("/api/voice/motion", {"enabled": False}, lease)
        self.assertFalse(disabled["enabled"])
        self.post("/api/actions/deinit", {}, lease)
        self.post("/api/takeover", {"enabled": False}, lease)

    def test_second_page_takeover_is_rejected(self) -> None:
        first = self.post(
            "/api/takeover",
            {"enabled": True, "client_id": "page-a"},
        )
        status, _, raw = self.request(
            "POST",
            "/api/takeover",
            {"enabled": True, "client_id": "page-b"},
        )
        self.assertEqual(status, 409)
        self.assertIn("其他页面", json.loads(raw)["error"])
        status, _, raw = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["takeover_client_id"], "page-a")
        self.post("/api/takeover", {"enabled": False}, first["lease_id"])

    def test_websocket_carries_multiple_full_size_streams(self) -> None:
        client = socket.create_connection(
            ("127.0.0.1", self.server.server_port), timeout=5
        )
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            request = (
                "GET /api/cameras/ws HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.server.server_port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
            client.sendall(request)
            response = b""
            while b"\r\n\r\n" not in response:
                response += client.recv(4096)
            self.assertIn(b"101 Switching Protocols", response)
            selected = json.dumps(
                {"streams": ["left_wrist/rgb", "head/depth"]}
            ).encode()
            client.sendall(self._masked_frame(1, selected))
            seen = set()
            while len(seen) < 2:
                opcode, payload = self._recv_frame(client)
                if opcode != 2:
                    continue
                seen.add(payload[0])
                image = cv2.imdecode(
                    np.frombuffer(payload[1:], np.uint8), cv2.IMREAD_COLOR
                )
                self.assertEqual(image.shape, (480, 640, 3))
            self.assertEqual(seen, {0, 3})
        finally:
            client.close()

    def test_chinese_asr_websocket_proxies_pcm_and_submits_final_text(self) -> None:
        proxy_side, fake_asr_side = socket.socketpair()
        release = threading.Event()
        received = bytearray()

        def fake_asr():
            release.wait(3)
            received.extend(fake_asr_side.recv(65536))
            fake_asr_side.sendall(
                H1RequestHandler._ws_frame_bytes(
                    1, json.dumps({"type": "partial", "text": "向前"}).encode()
                )
            )
            fake_asr_side.sendall(
                H1RequestHandler._ws_frame_bytes(
                    1,
                    json.dumps({"type": "final", "text": "向前移动"}).encode(),
                )
            )

        thread = threading.Thread(target=fake_asr, daemon=True)
        thread.start()
        client = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=5)
        try:
            with patch.object(H1RequestHandler, "_connect_asr_websocket", return_value=proxy_side):
                key = base64.b64encode(os.urandom(16)).decode()
                request = (
                    "GET /api/voice/asr/ws HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{self.server.server_port}\r\n"
                    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {key}\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n"
                ).encode()
                client.sendall(request)
                response = b""
                while b"\r\n\r\n" not in response:
                    response += client.recv(4096)
                self.assertIn(b"101 Switching Protocols", response)
                release.set()
                client.sendall(self._masked_frame(2, b"\x00\x00" * 1600))
                events = []
                while len(events) < 2:
                    opcode, payload = self._recv_frame(client)
                    if opcode == 1:
                        events.append(json.loads(payload))
                self.assertEqual([event["type"] for event in events], ["partial", "final"])
                self.assertTrue(events[-1]["submitted"])
                self.assertEqual(self.voice.text_requests[-1], ("向前移动", "zh"))
                self.assertTrue(received)
                self.assertTrue(received[1] & 0x80, "proxy must mask frames sent as a WS client")
        finally:
            client.close()
            fake_asr_side.close()
            thread.join(2)

    def test_websocket_survives_camera_backpressure(self) -> None:
        client = socket.create_connection(
            ("127.0.0.1", self.server.server_port), timeout=8
        )
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            request = (
                "GET /api/cameras/ws HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.server.server_port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
            client.sendall(request)
            response = b""
            while b"\r\n\r\n" not in response:
                response += client.recv(4096)
            self.assertIn(b"101 Switching Protocols", response)

            deadline = time.monotonic() + 3.0
            while self.app.camera._client is None:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            rng = np.random.default_rng(42)
            noisy = rng.integers(0, 256, (480, 640, 3), dtype=np.uint8)
            fake_client = self.app.camera._client
            for camera_name in tuple(fake_client._rgb):
                fake_client._rgb[camera_name] = noisy

            selected = json.dumps(
                {"streams": ["left_wrist/rgb", "head/rgb", "right_wrist/rgb"]}
            ).encode()
            client.sendall(self._masked_frame(1, selected))
            time.sleep(0.75)

            ping_payload = b"still-alive"
            client.sendall(self._masked_frame(9, ping_payload))
            seen = set()
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                opcode, payload = self._recv_frame(client)
                if opcode == 2:
                    seen.add(payload[0])
                elif opcode == 10 and payload == ping_payload:
                    break
                elif opcode == 8:
                    self.fail("camera WebSocket closed under normal backpressure")
            else:
                self.fail("camera WebSocket did not answer ping after backpressure")
            self.assertEqual(seen, {0, 2, 4})
        finally:
            client.close()

    def test_control_token_gates_api_and_moves_from_query_to_cookie(self) -> None:
        self.app.control_token = "test-control-token"
        status, _, raw = self.request("GET", "/api/config")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(raw)["error"], "invalid control token")

        status, response_headers, raw = self.request(
            "GET", "/?token=test-control-token"
        )
        self.assertEqual(status, 303)
        self.assertEqual(raw, b"")
        headers = dict(response_headers)
        self.assertEqual(headers["Location"], "/")
        cookie = headers["Set-Cookie"].split(";", 1)[0]

        status, _, raw = self.request(
            "GET", "/api/config", headers={"Cookie": cookie}
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["sdk_version"], "1.3.9")

    def test_non_loopback_without_token_requires_explicit_opt_in(self) -> None:
        with self.assertRaises(SystemExit):
            _validate_access_policy("172.16.18.43", None, False)
        _validate_access_policy("172.16.18.43", None, True)
        _validate_access_policy("172.16.18.43", "token", False)
        _validate_access_policy("127.0.0.1", None, False)

    @staticmethod
    def _masked_frame(opcode: int, payload: bytes) -> bytes:
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = bytes((0x80 | opcode, 0x80 | length))
        else:
            header = bytes((0x80 | opcode, 0x80 | 126)) + struct.pack("!H", length)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return header + mask + masked

    @staticmethod
    def _recv_exact(client: socket.socket, length: int) -> bytes:
        data = b""
        while len(data) < length:
            chunk = client.recv(length - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    @classmethod
    def _recv_frame(cls, client: socket.socket):
        first, second = cls._recv_exact(client, 2)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", cls._recv_exact(client, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", cls._recv_exact(client, 8))[0]
        return first & 0x0F, cls._recv_exact(client, length)


if __name__ == "__main__":
    unittest.main()
