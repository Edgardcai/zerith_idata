#!/usr/bin/env python3
"""Dependency-light HTTP/WebSocket server for the H1 web console.

The server uses Python's standard library so the delivered ``zerith`` Python
3.10 environment needs no Flask/FastAPI installation.  It must run as exactly
one process because :class:`RobotService` is the sole H1Robot owner.
"""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import select
import socket
import struct
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .camera_service import (
    CameraService,
    CameraServiceError,
    UnknownCameraError,
    UnknownStreamError,
)
from .fake_camera import fake_camera_factory
from .fake_sdk import FakeH1Robot, FakeSdk
from .robot_service import (
    RobotCallTimeout,
    RobotCommandRejected,
    RobotConflict,
    RobotService,
    RobotServiceError,
    RobotUnavailable,
)
from .voice_gateway import VoiceGateway, VoiceGatewayError
from .voice_motion import VoiceMotionController, VoiceMotionInternalServer


STATIC_DIR = Path(__file__).with_name("static")
MAX_JSON_BODY = 64 * 1024
CONTROL_TOKEN_COOKIE = "h1_control_token"
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_CAMERA_WS_BUFFER = 4 * 1024 * 1024
MAX_VOICE_WS_BUFFER = 1024 * 1024

CAMERA_STREAM_IDS = {
    "left_wrist/rgb": 0,
    "left_wrist/depth": 1,
    "head/rgb": 2,
    "head/depth": 3,
    "right_wrist/rgb": 4,
    "right_wrist/depth": 5,
}
CAMERA_ID_STREAMS = {value: key for key, value in CAMERA_STREAM_IDS.items()}


class H1WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: "WebControlApp") -> None:
        self.app = app
        super().__init__(address, H1RequestHandler)


class CameraConnectionManager:
    """Start CameraClient for the first viewer and stop after the last one."""

    def __init__(self, camera: CameraService, *, stop_grace_s: float = 10.0) -> None:
        self.camera = camera
        self.stop_grace_s = float(stop_grace_s)
        self._lock = threading.RLock()
        self._viewers = 0
        self._stop_timer: threading.Timer | None = None

    def enter(self) -> dict[str, Any]:
        with self._lock:
            if self._stop_timer is not None:
                self._stop_timer.cancel()
                self._stop_timer = None
            if self._viewers == 0 and not self.camera.running:
                self.camera.start()
            self._viewers += 1
            return self.camera.get_status()

    def leave(self) -> None:
        with self._lock:
            self._viewers = max(0, self._viewers - 1)
            if self._viewers != 0:
                return
            timer = threading.Timer(self.stop_grace_s, self._stop_if_unused)
            timer.daemon = True
            self._stop_timer = timer
            timer.start()

    def close(self) -> None:
        with self._lock:
            if self._stop_timer is not None:
                self._stop_timer.cancel()
                self._stop_timer = None
            self._viewers = 0
        self.camera.stop()

    def status(self) -> dict[str, Any]:
        report = self.camera.get_status()
        with self._lock:
            report["web_viewers"] = self._viewers
        return report

    def _stop_if_unused(self) -> None:
        with self._lock:
            self._stop_timer = None
            if self._viewers != 0:
                return
            self.camera.stop()


class WebControlApp:
    def __init__(
        self,
        *,
        robot: RobotService | None = None,
        camera: CameraService | None = None,
        voice: VoiceGateway | None = None,
        voice_motion: VoiceMotionController | None = None,
        control_token: str | None = None,
        static_dir: Path = STATIC_DIR,
    ) -> None:
        self.robot = robot or RobotService()
        self.camera = camera or CameraService()
        self.cameras = CameraConnectionManager(self.camera)
        self.voice = voice or VoiceGateway()
        self.voice_motion = voice_motion or VoiceMotionController(self.robot)
        self.control_token = control_token
        self.static_dir = static_dir.resolve()

    def close(self) -> None:
        self.voice_motion.close()
        self.cameras.close()
        self.robot.close()

    def authorized(self, handler: BaseHTTPRequestHandler) -> bool:
        if self.control_token is None:
            return True
        supplied = handler.headers.get("X-Control-Token")
        if not supplied:
            query = parse_qs(urlsplit(handler.path).query)
            supplied = (query.get("token") or [None])[0]
        if not supplied:
            cookie_text = handler.headers.get("Cookie", "")
            cookie = SimpleCookie()
            try:
                cookie.load(cookie_text)
                morsel = cookie.get(CONTROL_TOKEN_COOKIE)
                supplied = morsel.value if morsel is not None else None
            except Exception:
                supplied = None
        return bool(supplied and secrets_compare(supplied, self.control_token))


def secrets_compare(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(str(left), str(right))


def _normalize_voice_language(value: object) -> str:
    language = str(value).strip().lower().replace("_", "-")
    if language == "zh" or language.startswith("zh-"):
        return "zh"
    if language == "en" or language.startswith("en-"):
        return "en"
    raise RobotCommandRejected("语言只支持中文 zh-* 或英文 en-*")


class H1RequestHandler(BaseHTTPRequestHandler):
    server: H1WebServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(
            "%s - - [%s] %s\n"
            % (self.address_string(), self.log_date_time_string(), fmt % args)
        )

    @property
    def app(self) -> WebControlApp:
        return self.server.app

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/" and self.app.control_token is not None:
            supplied = (parse_qs(parsed.query).get("token") or [None])[0]
            if supplied and secrets_compare(supplied, self.app.control_token):
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", "/")
                self.send_header(
                    "Set-Cookie",
                    f"{CONTROL_TOKEN_COOKIE}={supplied}; Path=/; HttpOnly; SameSite=Strict",
                )
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

        if path in ("/", "/index.html"):
            self._serve_static("index.html")
            return
        if path.startswith("/static/"):
            self._serve_static(path.removeprefix("/static/"))
            return
        if path == "/api/config":
            if not self._require_authorized():
                return
            self._send_json(HTTPStatus.OK, self.app.robot.config())
            return
        if path == "/api/state":
            if not self._require_authorized():
                return
            state = self.app.robot.state()
            state["camera"] = self.app.cameras.status()
            self._send_json(HTTPStatus.OK, state)
            return
        if path == "/api/cameras/status":
            if not self._require_authorized():
                return
            self._send_json(HTTPStatus.OK, self.app.cameras.status())
            return
        if path == "/api/voice/status":
            if not self._require_authorized():
                return
            try:
                voice_status = self.app.voice.status()
                voice_status["motion"] = self.app.voice_motion.status()
                self._send_json(HTTPStatus.OK, voice_status)
            except VoiceGatewayError as exc:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"available": False, "state": "offline", "detail": str(exc), "messages": []},
                )
            return
        voice_audio_id = self._parse_voice_audio_path(path)
        if voice_audio_id is not None:
            if not self._require_authorized():
                return
            try:
                self._send_binary(
                    HTTPStatus.OK,
                    self.app.voice.audio(voice_audio_id),
                    "audio/wav",
                )
            except VoiceGatewayError as exc:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
            return
        if path == "/api/cameras/ws":
            if not self._require_authorized():
                return
            self._serve_camera_websocket()
            return
        if path == "/api/voice/asr/ws":
            if not self._require_authorized():
                return
            self._serve_voice_asr_websocket()
            return
        stream = self._parse_camera_mjpeg_path(path)
        if stream is not None:
            if not self._require_authorized():
                return
            self._serve_mjpeg(*stream)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._require_authorized():
            return
        try:
            body = self._read_json()
            path = urlsplit(self.path).path
            lease = self.headers.get("X-Control-Lease", "")
            if path == "/api/voice/start":
                language = _normalize_voice_language(body.get("language", "zh"))
                self._send_json(
                    HTTPStatus.ACCEPTED,
                    self.app.voice.start_session(language),
                )
                return
            if path == "/api/voice/finish-input":
                self._send_json(HTTPStatus.ACCEPTED, self.app.voice.finish_input())
                return
            if path == "/api/voice/cancel":
                self._send_json(HTTPStatus.ACCEPTED, self.app.voice.cancel())
                return
            if path == "/api/voice/text":
                text = body.get("text")
                language = _normalize_voice_language(body.get("language", "zh"))
                if not isinstance(text, str) or not text.strip():
                    raise RobotCommandRejected("文字内容不能为空")
                text = text.strip()
                if len(text) > 1000:
                    raise RobotCommandRejected("文字内容不能超过 1000 个字符")
                self._send_json(
                    HTTPStatus.ACCEPTED,
                    self.app.voice.submit_text(text, language),
                )
                return
            if path == "/api/voice/motion":
                enabled = body.get("enabled")
                if not isinstance(enabled, bool):
                    raise RobotCommandRejected("enabled 必须是布尔值")
                result = self.app.voice_motion.set_enabled(lease, enabled)
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/takeover":
                enabled = body.get("enabled")
                if not isinstance(enabled, bool):
                    raise RobotCommandRejected("enabled 必须是布尔值")
                if enabled:
                    result = self.app.robot.acquire(body.get("client_id"))
                else:
                    self.app.voice_motion.disable(
                        reason="takeover_released",
                        requested_lease=lease,
                    )
                    result = self.app.robot.release(lease)
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/heartbeat":
                self._send_json(HTTPStatus.OK, self.app.robot.heartbeat(lease))
                return
            if path == "/api/motion/joint":
                result = self.app.robot.move_joint(
                    lease,
                    body.get("motor_id"),
                    body.get("target"),
                    duration_s=body.get("duration_s"),
                    speed_scale=body.get("speed_scale"),
                )
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/motion/chassis":
                result = self.app.robot.command_chassis(
                    lease,
                    body.get("left_speed"),
                    body.get("right_speed"),
                )
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/actions/init":
                result = self.app.robot.initialize(lease)
                result.setdefault("message", "初始化完成")
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/actions/deinit":
                self.app.voice_motion.disable(
                    reason="robot_deinitialized",
                    requested_lease=lease,
                )
                result = self.app.robot.deinitialize(lease)
                result.setdefault("message", "反初始化完成")
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/actions/home":
                result = self.app.robot.move_home(
                    lease,
                    speed_scale=body.get("speed_scale", 1.0),
                )
                result.setdefault("message", "已到初始位姿并保持")
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/stop":
                self.app.voice_motion.cancel_active(lease)
                self._send_json(HTTPStatus.OK, self.app.robot.stop_motion(lease))
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except BaseException as exc:
            self._send_api_error(exc)

    def do_OPTIONS(self) -> None:  # noqa: N802
        # No CORS opt-in.  This deliberately blocks cross-origin browser control.
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET, POST")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_json(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("application/json"):
            raise RobotCommandRejected("Content-Type 必须是 application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise RobotCommandRejected("非法 Content-Length") from exc
        if length < 0 or length > MAX_JSON_BODY:
            raise RobotCommandRejected("请求体过大")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RobotCommandRejected("请求体不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise RobotCommandRejected("JSON 顶层必须是对象")
        return value

    def _serve_static(self, relative_name: str) -> None:
        if "\0" in relative_name:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid path"})
            return
        target = (self.app.static_dir / relative_name).resolve()
        try:
            target.relative_to(self.app.static_dir)
        except ValueError:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            return
        if not target.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        payload = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix == ".js":
            content_type = "text/javascript; charset=utf-8"
        elif target.suffix in (".html", ".css"):
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(payload)

    @staticmethod
    def _parse_camera_mjpeg_path(path: str) -> tuple[str, str] | None:
        prefix = "/api/cameras/"
        if not path.startswith(prefix) or not path.endswith(".mjpg"):
            return None
        parts = path[len(prefix) : -len(".mjpg")].split("/")
        if len(parts) != 2:
            return None
        camera, stream = parts
        if f"{camera}/{stream}" not in CAMERA_STREAM_IDS:
            return None
        return camera, stream

    @staticmethod
    def _parse_voice_audio_path(path: str) -> int | None:
        prefix = "/api/voice/audio/"
        if not path.startswith(prefix) or not path.endswith(".wav"):
            return None
        value = path[len(prefix) : -len(".wav")]
        if not value.isdigit():
            return None
        return int(value)

    def _serve_mjpeg(self, camera: str, stream: str) -> None:
        entered = False
        try:
            self.app.cameras.enter()
            entered = True
            self.send_response(HTTPStatus.OK)
            self.send_header(
                "Content-Type", "multipart/x-mixed-replace; boundary=frame"
            )
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self._send_security_headers()
            self.end_headers()
            for chunk in self.app.camera.iter_mjpeg(camera, stream):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        except (CameraServiceError, UnknownCameraError, UnknownStreamError) as exc:
            if not entered:
                self._send_api_error(exc)
        finally:
            if entered:
                self.app.cameras.leave()
            self.close_connection = True

    def _serve_camera_websocket(self) -> None:
        if self.headers.get("Upgrade", "").lower() != "websocket":
            self._send_json(HTTPStatus.UPGRADE_REQUIRED, {"error": "WebSocket required"})
            return
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing WebSocket key"})
            return
        accept = hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
        import base64

        accept_text = base64.b64encode(accept).decode("ascii")
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept_text)
        self.end_headers()
        self.wfile.flush()

        entered = False
        try:
            self.app.cameras.enter()
            entered = True
            self.connection.setblocking(False)
            self._camera_websocket_loop()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except BaseException as exc:
            try:
                self._ws_send_text(json.dumps({"error": str(exc)}, ensure_ascii=False))
                self._ws_send_close(1011, "camera error")
            except BaseException:
                pass
        finally:
            if entered:
                self.app.cameras.leave()
            self.close_connection = True

    def _serve_voice_asr_websocket(self) -> None:
        """Proxy browser PCM frames to the loopback-only Chinese ASR service."""
        if self.headers.get("Upgrade", "").lower() != "websocket":
            self._send_json(HTTPStatus.UPGRADE_REQUIRED, {"error": "WebSocket required"})
            return
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing WebSocket key"})
            return
        try:
            upstream = self._connect_asr_websocket()
        except (OSError, VoiceGatewayError) as exc:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
            return
        accept = hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
        import base64

        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", base64.b64encode(accept).decode("ascii"))
        self.end_headers()
        self.wfile.flush()
        try:
            self.connection.setblocking(False)
            upstream.setblocking(False)
            self._voice_asr_proxy_loop(upstream)
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass
        finally:
            upstream.close()
            self.close_connection = True

    @staticmethod
    def _connect_asr_websocket() -> socket.socket:
        import base64

        upstream = socket.create_connection(("127.0.0.1", 8770), timeout=5.0)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET /v1/stream HTTP/1.1\r\n"
            "Host: 127.0.0.1:8770\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        upstream.sendall(request)
        response = bytearray()
        while b"\r\n\r\n" not in response and len(response) < 16384:
            chunk = upstream.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
        if not response.startswith(b"HTTP/1.1 101"):
            upstream.close()
            detail = response.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
            raise VoiceGatewayError(f"中文实时识别服务连接失败：{detail or 'no response'}")
        return upstream

    def _voice_asr_proxy_loop(self, upstream: socket.socket) -> None:
        browser_in = bytearray()
        upstream_in = bytearray()
        to_browser = bytearray()
        to_upstream = bytearray()
        submitted = False
        while True:
            readers = [self.connection, upstream]
            writers = []
            if to_browser:
                writers.append(self.connection)
            if to_upstream:
                writers.append(upstream)
            readable, writable, _ = select.select(readers, writers, [], 0.1)
            if self.connection in readable:
                chunk = self.connection.recv(65536)
                if not chunk:
                    return
                browser_in.extend(chunk)
                for opcode, payload in self._ws_decode_client_frames(browser_in):
                    if opcode == 0x8:
                        to_upstream.extend(self._ws_client_frame_bytes(0x8, payload))
                        return
                    if opcode == 0x9:
                        to_browser.extend(self._ws_frame_bytes(0xA, payload))
                    elif opcode in {0x1, 0x2}:
                        to_upstream.extend(self._ws_client_frame_bytes(opcode, payload))
            if upstream in readable:
                chunk = upstream.recv(65536)
                if not chunk:
                    return
                upstream_in.extend(chunk)
                for opcode, payload in self._ws_decode_server_frames(upstream_in):
                    if opcode == 0x8:
                        to_browser.extend(self._ws_frame_bytes(0x8, payload))
                        return
                    if opcode == 0x9:
                        to_upstream.extend(self._ws_client_frame_bytes(0xA, payload))
                        continue
                    if opcode == 0x1 and not submitted:
                        try:
                            event = json.loads(payload.decode("utf-8"))
                            if event.get("type") == "final" and event.get("text"):
                                self.app.voice.submit_text(str(event["text"]), "zh")
                                event["submitted"] = True
                                payload = json.dumps(event, ensure_ascii=False).encode("utf-8")
                                submitted = True
                        except (UnicodeDecodeError, json.JSONDecodeError, VoiceGatewayError) as exc:
                            payload = json.dumps(
                                {"type": "error", "error": str(exc)}, ensure_ascii=False
                            ).encode("utf-8")
                    to_browser.extend(self._ws_frame_bytes(opcode, payload))
            if self.connection in writable and to_browser:
                sent = self.connection.send(to_browser)
                if sent:
                    del to_browser[:sent]
            if upstream in writable and to_upstream:
                sent = upstream.send(to_upstream)
                if sent:
                    del to_upstream[:sent]
            if len(to_browser) > MAX_VOICE_WS_BUFFER or len(to_upstream) > MAX_VOICE_WS_BUFFER:
                raise ValueError("voice WebSocket backpressure limit exceeded")

    @staticmethod
    def _ws_decode_server_frames(buffer: bytearray) -> list[tuple[int, bytes]]:
        frames: list[tuple[int, bytes]] = []
        while len(buffer) >= 2:
            first, second = buffer[0], buffer[1]
            opcode = first & 0x0F
            if second & 0x80:
                raise ValueError("upstream server frames must not be masked")
            length = second & 0x7F
            offset = 2
            if length == 126:
                if len(buffer) < 4:
                    break
                length = struct.unpack("!H", buffer[2:4])[0]
                offset = 4
            elif length == 127:
                if len(buffer) < 10:
                    break
                length = struct.unpack("!Q", buffer[2:10])[0]
                offset = 10
            if length > MAX_VOICE_WS_BUFFER:
                raise ValueError("upstream WebSocket frame too large")
            if len(buffer) < offset + length:
                break
            payload = bytes(buffer[offset : offset + length])
            del buffer[: offset + length]
            frames.append((opcode, payload))
        return frames

    @staticmethod
    def _ws_client_frame_bytes(opcode: int, payload: bytes) -> bytes:
        mask = os.urandom(4)
        first = 0x80 | (opcode & 0x0F)
        length = len(payload)
        if length < 126:
            header = bytes((first, 0x80 | length))
        elif length <= 0xFFFF:
            header = bytes((first, 0x80 | 126)) + struct.pack("!H", length)
        else:
            header = bytes((first, 0x80 | 127)) + struct.pack("!Q", length)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return header + mask + masked

    def _camera_websocket_loop(self) -> None:
        receive_buffer = bytearray()
        send_buffer = bytearray()
        active: set[str] = set()
        last_sequences = {name: 0 for name in CAMERA_STREAM_IDS}
        while True:
            readable, writable, _ = select.select(
                [self.connection],
                [self.connection] if send_buffer else [],
                [],
                0.005,
            )
            if readable:
                try:
                    chunk = self.connection.recv(65536)
                except BlockingIOError:
                    chunk = None
                if chunk == b"":
                    return
                if chunk:
                    receive_buffer.extend(chunk)
                frames = self._ws_decode_client_frames(receive_buffer)
                for opcode, payload in frames:
                    if opcode == 0x8:
                        return
                    if opcode == 0x9:
                        send_buffer.extend(self._ws_frame_bytes(0xA, payload))
                    elif opcode == 0x1:
                        try:
                            message = json.loads(payload.decode("utf-8"))
                            requested = message.get("streams", [])
                            if not isinstance(requested, list):
                                raise ValueError("streams must be an array")
                            active = {
                                str(value)
                                for value in requested
                                if str(value) in CAMERA_STREAM_IDS
                            }
                        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                            send_buffer.extend(
                                self._ws_frame_bytes(
                                    0x1,
                                    b'{"error":"invalid stream selection"}',
                                )
                            )

            if writable and send_buffer:
                try:
                    sent = self.connection.send(send_buffer)
                except BlockingIOError:
                    sent = None
                if sent == 0:
                    return
                if sent:
                    del send_buffer[:sent]

            # Apply backpressure in complete batches.  When a browser or Wi-Fi
            # link is slower than six 640x480 streams, intermediate frames are
            # dropped and the newest frame is queued after the buffer drains.
            # This keeps all selected streams fair and, crucially, does not
            # treat a normal non-blocking short write as a disconnect.
            if len(send_buffer) <= MAX_CAMERA_WS_BUFFER // 2:
                for name in sorted(active):
                    camera, stream = name.split("/", 1)
                    snapshot = self.app.camera.get_latest(camera, stream, copy=False)
                    if snapshot is None or snapshot.sequence <= last_sequences[name]:
                        continue
                    encoded = self.app.camera.get_encoded_jpeg(camera, stream)
                    if encoded is None or encoded.sequence <= last_sequences[name]:
                        continue
                    payload = bytes((CAMERA_STREAM_IDS[name],)) + encoded.data
                    frame = self._ws_frame_bytes(0x2, payload)
                    if len(send_buffer) + len(frame) > MAX_CAMERA_WS_BUFFER:
                        break
                    send_buffer.extend(frame)
                    last_sequences[name] = encoded.sequence

    @staticmethod
    def _ws_decode_client_frames(buffer: bytearray) -> list[tuple[int, bytes]]:
        frames: list[tuple[int, bytes]] = []
        while len(buffer) >= 2:
            first, second = buffer[0], buffer[1]
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            offset = 2
            if length == 126:
                if len(buffer) < 4:
                    break
                length = struct.unpack("!H", buffer[2:4])[0]
                offset = 4
            elif length == 127:
                if len(buffer) < 10:
                    break
                length = struct.unpack("!Q", buffer[2:10])[0]
                offset = 10
            if length > 64 * 1024:
                raise ValueError("WebSocket client frame too large")
            if not masked:
                raise ValueError("WebSocket client frames must be masked")
            if len(buffer) < offset + 4 + length:
                break
            mask = bytes(buffer[offset : offset + 4])
            offset += 4
            raw = bytes(buffer[offset : offset + length])
            del buffer[: offset + length]
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(raw))
            frames.append((opcode, payload))
        return frames

    def _ws_send_text(self, text: str) -> None:
        self._ws_send_frame(0x1, text.encode("utf-8"))

    def _ws_send_close(self, code: int, reason: str) -> None:
        self._ws_send_frame(0x8, struct.pack("!H", code) + reason.encode("utf-8")[:120])

    def _ws_send_frame(self, opcode: int, payload: bytes) -> None:
        self.connection.sendall(self._ws_frame_bytes(opcode, payload))

    @staticmethod
    def _ws_frame_bytes(opcode: int, payload: bytes) -> bytes:
        first = 0x80 | (opcode & 0x0F)
        length = len(payload)
        if length < 126:
            header = bytes((first, length))
        elif length <= 0xFFFF:
            header = bytes((first, 126)) + struct.pack("!H", length)
        else:
            header = bytes((first, 127)) + struct.pack("!Q", length)
        return header + payload

    def _require_authorized(self) -> bool:
        if self.app.authorized(self):
            return True
        self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid control token"})
        return False

    def _send_api_error(self, exc: BaseException) -> None:
        if isinstance(exc, RobotConflict):
            status = HTTPStatus.CONFLICT
        elif isinstance(exc, (RobotCommandRejected, ValueError, KeyError)):
            status = HTTPStatus.BAD_REQUEST
        elif isinstance(exc, (RobotUnavailable, CameraServiceError)):
            status = HTTPStatus.SERVICE_UNAVAILABLE
        elif isinstance(exc, VoiceGatewayError):
            status = HTTPStatus.SERVICE_UNAVAILABLE
        elif isinstance(exc, RobotCallTimeout):
            status = HTTPStatus.GATEWAY_TIMEOUT
        elif isinstance(exc, RobotServiceError):
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        else:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        self._send_json(
            status,
            {"error": str(exc), "type": type(exc).__name__},
        )

    def _send_json(self, status: int | HTTPStatus, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_binary(
        self,
        status: int | HTTPStatus,
        payload: bytes,
        content_type: str,
    ) -> None:
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' blob: data:; connect-src 'self' ws: wss:; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )


def _is_loopback_bind(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_access_policy(
    host: str,
    token: str | None,
    allow_unauthenticated_lan: bool,
) -> None:
    if (
        not _is_loopback_bind(host)
        and not token
        and not allow_unauthenticated_lan
    ):
        raise SystemExit(
            "Refusing non-loopback bind without --token, H1_WEB_CONTROL_TOKEN, "
            "or explicit --allow-unauthenticated-lan"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ZERITH H1 PRO local web console")
    parser.add_argument("--host", default="172.16.18.43")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--token",
        default=os.environ.get("H1_WEB_CONTROL_TOKEN"),
        help="required when binding to a non-loopback address",
    )
    parser.add_argument(
        "--allow-unauthenticated-lan",
        action="store_true",
        default=True,
        help="allow a non-loopback bind without a control token (default: enabled)",
    )
    parser.add_argument(
        "--simulate-robot",
        action="store_true",
        help="use an in-memory fake; never loads or controls H1Robot",
    )
    parser.add_argument(
        "--simulate-cameras",
        action="store_true",
        help="serve synthetic 640x480 RGB-D frames instead of CameraClient",
    )
    parser.add_argument("--camera-target", default="localhost:50051")
    parser.add_argument(
        "--voice-motion-port",
        type=int,
        default=8766,
        help="loopback-only voice motion bridge port",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be in 1..65535")
    if not 1 <= args.voice_motion_port <= 65535:
        raise SystemExit("--voice-motion-port must be in 1..65535")
    _validate_access_policy(
        args.host,
        args.token,
        args.allow_unauthenticated_lan,
    )

    if args.simulate_robot:
        fake_sdk = FakeSdk()
        fake_robot = FakeH1Robot()
        robot_service = RobotService(
            sdk_loader=lambda: fake_sdk,
            robot_factory=lambda _sdk: fake_robot,
        )
    else:
        robot_service = RobotService()
    camera_service = CameraService(
        grpc_target=args.camera_target,
        client_factory=fake_camera_factory if args.simulate_cameras else None,
    )
    app = WebControlApp(
        robot=robot_service,
        camera=camera_service,
        control_token=args.token,
    )
    server = H1WebServer((args.host, args.port), app)
    internal_server = VoiceMotionInternalServer(
        ("127.0.0.1", args.voice_motion_port),
        app.voice_motion,
    )
    internal_thread = threading.Thread(
        target=internal_server.serve_forever,
        kwargs={"poll_interval": 0.2},
        name="voice-motion-api",
        daemon=True,
    )
    internal_thread.start()
    display_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print(f"H1 web console: http://{display_host}:{server.server_port}/")
    if args.token:
        print("Control-token authentication is enabled.")
    elif not _is_loopback_bind(args.host):
        print("WARNING: unauthenticated LAN access is enabled.")
    print("Robot SDK stays unloaded until the takeover switch is confirmed.")
    print(f"Voice motion bridge: http://127.0.0.1:{internal_server.server_port}/")
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\nStopping web console; robot_deinit is not called automatically.")
    finally:
        server.shutdown()
        server.server_close()
        internal_server.shutdown()
        internal_server.server_close()
        internal_thread.join(timeout=2.0)
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
