from __future__ import annotations

from collections import OrderedDict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import threading
import time
from typing import Any, Protocol
from urllib.parse import urlsplit

from .service import LogStatusSink, StatusSink, VoiceState


LOG = logging.getLogger(__name__)


class VoiceWebHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class SessionController(Protocol):
    def request_session(self, language: str = "zh") -> tuple[bool, str]: ...

    def finish_input(self) -> tuple[bool, str]: ...

    def request_text(self, text: str, language: str = "zh") -> tuple[bool, str]: ...

    def cancel_current(self) -> tuple[bool, str]: ...


class VoiceWebBridge(StatusSink):
    """Thread-safe conversation view exposed only on the loopback interface."""

    def __init__(
        self,
        status_sink: StatusSink | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        max_audio_items: int = 12,
    ) -> None:
        self._status_sink = status_sink or LogStatusSink()
        self._host = host
        self._port = int(port)
        self._max_audio_items = max(1, int(max_audio_items))
        self._lock = threading.RLock()
        self._state = VoiceState.STARTING.value
        self._detail = "正在启动"
        self._sequence = 0
        self._session_id = 0
        self._language = "zh"
        self._message_id = 0
        self._audio_id = 0
        self._messages: list[dict[str, Any]] = []
        self._audio: OrderedDict[int, bytes] = OrderedDict()
        self._controller: SessionController | None = None
        self._server: VoiceWebHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        server = self._server
        return int(server.server_port if server is not None else self._port)

    def attach(self, controller: SessionController) -> None:
        self._controller = controller

    def start(self) -> None:
        if self._server is not None:
            return
        if self._host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("语音控制接口只允许监听回环地址")

        bridge = self

        class Handler(VoiceWebRequestHandler):
            api = bridge

        server = VoiceWebHTTPServer((self._host, self._port), Handler)
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="xiaoda-web-api",
            daemon=True,
        )
        self._server = server
        self._thread = thread
        thread.start()
        LOG.info("语音网页接口：http://%s:%d", self._host, server.server_port)

    def close(self) -> None:
        server, self._server = self._server, None
        thread, self._thread = self._thread, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=2.0)

    def update(self, state: VoiceState, detail: str = "") -> None:
        self._status_sink.update(state, detail)
        with self._lock:
            self._state = state.value
            self._detail = detail
            self._sequence += 1

    def session_started(self, source: str, language: str = "zh") -> None:
        with self._lock:
            self._session_id += 1
            self._language = language
            self._messages.clear()
            self._audio.clear()
            self._sequence += 1
            self._append_message_locked(
                "system",
                (
                    f"网页按钮已启动{'English' if language == 'en' else '中文'}单轮对话"
                    if source == "web"
                    else (
                        f"网页键盘已发送{'English' if language == 'en' else '中文'}文字"
                        if source == "text"
                        else "已通过唤醒词开始对话"
                    )
                ),
            )

    def message(self, role: str, text: str) -> None:
        text = str(text).strip()
        if not text:
            return
        with self._lock:
            self._append_message_locked(role, text)

    def audio(self, encoded_audio: bytes) -> None:
        if not encoded_audio:
            return
        with self._lock:
            self._audio_id += 1
            audio_id = self._audio_id
            self._audio[audio_id] = bytes(encoded_audio)
            while len(self._audio) > self._max_audio_items:
                self._audio.popitem(last=False)
            for message in reversed(self._messages):
                if message["role"] == "assistant" and "audio_id" not in message:
                    message["audio_id"] = audio_id
                    break
            self._sequence += 1

    def request_session(self, language: str = "zh") -> tuple[bool, str]:
        controller = self._controller
        if controller is None:
            return False, "语音助手尚未就绪"
        return controller.request_session(language)

    def finish_input(self) -> tuple[bool, str]:
        controller = self._controller
        if controller is None:
            return False, "语音助手尚未就绪"
        return controller.finish_input()

    def request_text(self, text: str, language: str = "zh") -> tuple[bool, str]:
        controller = self._controller
        if controller is None:
            return False, "语音助手尚未就绪"
        return controller.request_text(text, language)

    def cancel_current(self) -> tuple[bool, str]:
        controller = self._controller
        if controller is None:
            return False, "语音助手尚未就绪"
        return controller.cancel_current()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "available": True,
                "state": self._state,
                "detail": self._detail,
                "sequence": self._sequence,
                "session_id": self._session_id,
                "language": self._language,
                "messages": [dict(message) for message in self._messages],
            }

    def get_audio(self, audio_id: int) -> bytes | None:
        with self._lock:
            audio = self._audio.get(int(audio_id))
            return bytes(audio) if audio is not None else None

    def _append_message_locked(self, role: str, text: str) -> None:
        self._message_id += 1
        self._messages.append(
            {
                "id": self._message_id,
                "role": str(role),
                "text": text,
                "created_at": time.time(),
            }
        )
        if len(self._messages) > 100:
            del self._messages[:-100]
        self._sequence += 1


class VoiceWebRequestHandler(BaseHTTPRequestHandler):
    api: VoiceWebBridge
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/v1/status":
            self._send_json(HTTPStatus.OK, self.api.snapshot())
            return
        prefix = "/v1/audio/"
        if path.startswith(prefix) and path.endswith(".wav"):
            raw_id = path[len(prefix) : -len(".wav")]
            try:
                audio_id = int(raw_id)
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid audio id"})
                return
            audio = self.api.get_audio(audio_id)
            if audio is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "audio not found"})
                return
            self._send_bytes(HTTPStatus.OK, audio, "audio/wav")
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 4096:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body too large"})
            return
        payload: dict[str, Any] = {}
        if length:
            try:
                decoded = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
                return
            if not isinstance(decoded, dict):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid json object"})
                return
            payload = decoded
        if path == "/v1/start":
            language = str(payload.get("language", "zh"))
            accepted, message = self.api.request_session(language)
        elif path == "/v1/finish-input":
            accepted, message = self.api.finish_input()
        elif path == "/v1/text":
            language = str(payload.get("language", "zh"))
            text = payload.get("text", "")
            if not isinstance(text, str):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "text must be a string"})
                return
            accepted, message = self.api.request_text(text, language)
        elif path == "/v1/cancel":
            accepted, message = self.api.cancel_current()
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._send_json(
            HTTPStatus.ACCEPTED if accepted else HTTPStatus.CONFLICT,
            {"accepted": accepted, "message": message},
        )

    def _send_json(self, status: int | HTTPStatus, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, payload, "application/json; charset=utf-8")

    def _send_bytes(
        self,
        status: int | HTTPStatus,
        payload: bytes,
        content_type: str,
    ) -> None:
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass
