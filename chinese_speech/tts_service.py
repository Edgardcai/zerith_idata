from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Iterator

from .text import float_audio_to_pcm, resample_audio, split_chinese_text


LOG = logging.getLogger("chinese-tts-service")


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


class QwenTTSBackend:
    """Official qwen-tts wrapper kept resident on the configured device."""

    output_sample_rate = 24000

    def __init__(self) -> None:
        self.model_name = os.environ.get(
            "CHINESE_TTS_MODEL", "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
        )
        self.local_model = os.environ.get("CHINESE_TTS_LOCAL_MODEL", "").strip()
        self.default_speaker = os.environ.get("CHINESE_TTS_SPEAKER", "Serena")
        self.device = os.environ.get("CHINESE_TTS_DEVICE", "cuda")
        self.dtype_name = os.environ.get("CHINESE_TTS_DTYPE", "bfloat16")
        self.use_flash_attention = _env_bool("CHINESE_TTS_FLASH_ATTENTION", True)
        self.model: Any = None
        self.attention = "unloaded"
        self.load_seconds = 0.0

    def load(self) -> None:
        import torch
        from qwen_tts import Qwen3TTSModel

        started = time.monotonic()
        dtype = {
            "bfloat16": torch.bfloat16,
            "float16": torch.float16,
            "float32": torch.float32,
        }.get(self.dtype_name)
        if dtype is None:
            raise RuntimeError(f"unsupported dtype: {self.dtype_name}")
        device_map = "cuda:0" if self.device == "cuda" else self.device
        kwargs: dict[str, Any] = {"device_map": device_map, "dtype": dtype}
        if self.use_flash_attention and self.device.startswith("cuda"):
            kwargs["attn_implementation"] = "flash_attention_2"
        source = self.local_model if self.local_model and Path(self.local_model).is_dir() else self.model_name
        try:
            self.model = Qwen3TTSModel.from_pretrained(source, **kwargs)
            self.attention = str(kwargs.get("attn_implementation", "sdpa"))
        except (ImportError, RuntimeError) as exc:
            if "attn_implementation" not in kwargs:
                raise
            LOG.warning("FlashAttention 2 unavailable, falling back to SDPA: %s", exc)
            kwargs.pop("attn_implementation")
            self.model = Qwen3TTSModel.from_pretrained(source, **kwargs)
            self.attention = "sdpa"
        self.load_seconds = time.monotonic() - started

    def synthesize(self, text: str, speaker: str) -> tuple[object, int]:
        if self.model is None:
            raise RuntimeError("model is not loaded")
        waveforms, sample_rate = self.model.generate_custom_voice(
            text=text,
            language="Chinese",
            speaker=speaker,
        )
        return waveforms[0], int(sample_rate)


class ChineseTTSApplication:
    def __init__(self, backend: QwenTTSBackend) -> None:
        self.backend = backend
        self.model_lock = threading.Lock()
        self.cancel_lock = threading.Lock()
        self.cancelled: set[str] = set()
        self.last_metrics: dict[str, Any] = {}

    def cancel(self, request_id: str) -> None:
        with self.cancel_lock:
            self.cancelled.add(request_id)

    def is_cancelled(self, request_id: str) -> bool:
        with self.cancel_lock:
            return request_id in self.cancelled

    def clear(self, request_id: str) -> None:
        with self.cancel_lock:
            self.cancelled.discard(request_id)

    def pcm_chunks(self, request_id: str, text: str, speaker: str) -> Iterator[bytes]:
        started = time.monotonic()
        first_audio_ms: float | None = None
        segment_count = 0
        try:
            for segment in split_chinese_text(text, 15, 80):
                if self.is_cancelled(request_id):
                    return
                with self.model_lock:
                    if self.is_cancelled(request_id):
                        return
                    audio, sample_rate = self.backend.synthesize(segment, speaker)
                if sample_rate != self.backend.output_sample_rate:
                    audio = resample_audio(audio, sample_rate, self.backend.output_sample_rate)
                pcm = float_audio_to_pcm(audio, sample_rate=self.backend.output_sample_rate)
                segment_count += 1
                for offset in range(0, len(pcm), 8192):
                    if self.is_cancelled(request_id):
                        return
                    if first_audio_ms is None:
                        first_audio_ms = (time.monotonic() - started) * 1000
                    yield pcm[offset : offset + 8192]
        finally:
            self.last_metrics = {
                "request_id": request_id,
                "first_audio_ms": first_audio_ms,
                "total_ms": (time.monotonic() - started) * 1000,
                "segments": segment_count,
                "cancelled": self.is_cancelled(request_id),
            }
            self.clear(request_id)


class ChineseTTSServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: ChineseTTSApplication) -> None:
        self.app = app
        super().__init__(address, ChineseTTSHandler)


class ChineseTTSHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self._response_started = False

    @property
    def app(self) -> ChineseTTSApplication:
        return self.server.app  # type: ignore[attr-defined,no-any-return]

    def log_message(self, pattern: str, *args: Any) -> None:
        LOG.debug(pattern, *args)

    def do_GET(self) -> None:  # noqa: N802
        self._response_started = False
        if self.path != "/health":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        payload: dict[str, Any] = {
            "ok": self.app.backend.model is not None,
            "model": self.app.backend.model_name,
            "model_source": (
                self.app.backend.local_model
                if self.app.backend.local_model and Path(self.app.backend.local_model).is_dir()
                else self.app.backend.model_name
            ),
            "speaker": self.app.backend.default_speaker,
            "device": self.app.backend.device,
            "dtype": self.app.backend.dtype_name,
            "attention": self.app.backend.attention,
            "sample_rate": self.app.backend.output_sample_rate,
            "load_seconds": self.app.backend.load_seconds,
            "last_metrics": self.app.last_metrics,
        }
        try:
            import torch
            if torch.cuda.is_available():
                payload["gpu_allocated_mib"] = round(torch.cuda.memory_allocated() / 1048576, 1)
                payload["gpu_reserved_mib"] = round(torch.cuda.memory_reserved() / 1048576, 1)
        except ImportError:
            pass
        self._json(HTTPStatus.OK, payload)

    def do_POST(self) -> None:  # noqa: N802
        self._response_started = False
        try:
            payload = self._read_json()
            if self.path == "/v1/cancel":
                request_id = str(payload.get("request_id", "")).strip()
                if request_id:
                    self.app.cancel(request_id)
                self._json(HTTPStatus.OK, {"cancelled": bool(request_id)})
                return
            if self.path != "/v1/speech":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            request_id = str(payload.get("request_id", "")).strip()
            text = str(payload.get("text", "")).strip()
            language = str(payload.get("language", "Chinese"))
            speaker = str(payload.get("speaker") or self.app.backend.default_speaker)
            if not request_id or not text:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "request_id and text are required"})
                return
            if language.lower() != "chinese":
                self._json(HTTPStatus.BAD_REQUEST, {"error": "Chinese language is required"})
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "audio/L16")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Audio-Sample-Rate", str(self.app.backend.output_sample_rate))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self._response_started = True
            try:
                for chunk in self.app.pcm_chunks(request_id, text, speaker):
                    self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                    self.wfile.write(chunk + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                self.app.cancel(request_id)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            LOG.exception("TTS request failed")
            if not self._response_started and not self.wfile.closed:
                try:
                    self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)[:300]})
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self.close_connection = True

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > 256 * 1024:
            raise ValueError("invalid body length")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON object required")
        return value

    def _json(self, status: int | HTTPStatus, value: object) -> None:
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._response_started = True
        self.wfile.write(raw)


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Qwen3 Chinese TTS service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8771)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        parser.error("Chinese TTS must listen on loopback")
    logging.basicConfig(level=os.environ.get("CHINESE_SPEECH_LOG_LEVEL", "INFO"))
    backend = QwenTTSBackend()
    LOG.info("Loading %s on %s", backend.model_name, backend.device)
    backend.load()
    server = ChineseTTSServer((args.host, args.port), ChineseTTSApplication(backend))
    LOG.info("Chinese TTS ready at http://%s:%d", args.host, args.port)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
