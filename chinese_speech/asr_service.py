from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
import io
import json
import logging
import os
from pathlib import Path
import time
import threading
from typing import Any
import wave

import numpy as np


LOG = logging.getLogger("chinese-asr-service")
PACKAGE_DIR = Path(__file__).resolve().parent


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def _first_text(result: object) -> str:
    if isinstance(result, list) and result and isinstance(result[0], dict):
        return str(result[0].get("text", "")).strip()
    return ""


def _read_pcm_wav(raw: bytes) -> bytes:
    with wave.open(io.BytesIO(raw), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2 or source.getframerate() != 16000:
            raise ValueError("audio must be 16 kHz mono PCM16 WAV")
        return source.readframes(source.getnframes())


@dataclass
class StreamState:
    online_cache: dict[str, Any] = field(default_factory=dict)
    vad_cache: dict[str, Any] = field(default_factory=dict)
    all_pcm: bytearray = field(default_factory=bytearray)
    pending_pcm: bytearray = field(default_factory=bytearray)
    partial_parts: list[str] = field(default_factory=list)
    finalized: bool = False
    cancelled: bool = False
    started_at: float = field(default_factory=time.monotonic)
    first_partial_ms: float | None = None


class ChineseASRBackend:
    sample_rate = 16000
    # Paraformer uses 60 ms units.  Four current frames plus two look-ahead
    # frames keeps partial updates within the requested 200-400 ms cadence.
    chunk_ms = 240
    chunk_size = [0, 4, 2]

    def __init__(self) -> None:
        configured = os.environ.get("CHINESE_ASR_STREAM_MODEL", "paraformer-online")
        self.stream_model_name = (
            "paraformer-zh-streaming" if configured == "paraformer-online" else configured
        )
        self.final_model_name = os.environ.get(
            "CHINESE_ASR_FINAL_MODEL", "Qwen/Qwen3-ASR-0.6B"
        )
        self.final_local_model = os.environ.get("CHINESE_ASR_FINAL_LOCAL_MODEL", "").strip()
        self.final_timeout_ms = int(os.environ.get("CHINESE_ASR_FINAL_TIMEOUT_MS", "1500"))
        self.endpoint_ms = int(os.environ.get("CHINESE_ASR_ENDPOINT_MS", "500"))
        self.device = os.environ.get("CHINESE_ASR_DEVICE", "cuda")
        self.hotword_file = Path(os.environ.get("CHINESE_ASR_HOTWORDS_FILE", PACKAGE_DIR / "hotwords.txt"))
        self.correction_file = Path(
            os.environ.get("CHINESE_ASR_CORRECTIONS_FILE", PACKAGE_DIR / "corrections.json")
        )
        self.online: Any = None
        self.vad: Any = None
        self.punc: Any = None
        self.qwen: Any = None
        self.final_enabled = _env_bool("CHINESE_ASR_FINAL_ENABLED", True)
        self.hotwords: list[str] = []
        self.corrections: dict[str, str] = {}
        self.load_seconds = 0.0
        self.online_lock = asyncio.Lock()
        self.vad_lock = asyncio.Lock()
        self.punc_lock = asyncio.Lock()
        # A timed-out asyncio waiter cannot stop CUDA work already running in a
        # worker thread. Keep a real thread lock around that work so a later
        # sentence never launches a second Qwen review concurrently.
        self.qwen_lock = threading.Lock()
        self.last_metrics: dict[str, Any] = {}

    def load(self) -> None:
        from funasr import AutoModel

        started = time.monotonic()
        self.hotwords = [
            line.strip()
            for line in self.hotword_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ] if self.hotword_file.is_file() else []
        if self.correction_file.is_file():
            value = json.loads(self.correction_file.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                self.corrections = {str(key): str(replacement) for key, replacement in value.items()}
        ngpu = 1 if self.device.startswith("cuda") else 0
        self.online = AutoModel(
            model=self.stream_model_name,
            device=self.device,
            ngpu=ngpu,
            disable_pbar=True,
            disable_log=True,
        )
        self.vad = AutoModel(
            model="fsmn-vad",
            device=self.device,
            ngpu=ngpu,
            max_end_silence_time=self.endpoint_ms,
            disable_pbar=True,
            disable_log=True,
        )
        self.punc = AutoModel(
            model="ct-punc",
            device=self.device,
            ngpu=ngpu,
            disable_pbar=True,
            disable_log=True,
        )
        if self.final_enabled and self.final_model_name and self.device.startswith("cuda"):
            try:
                import torch
                from qwen_asr import Qwen3ASRModel

                dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
                qwen_source = (
                    self.final_local_model
                    if self.final_local_model and Path(self.final_local_model).is_dir()
                    else self.final_model_name
                )
                self.qwen = Qwen3ASRModel.from_pretrained(
                    qwen_source,
                    dtype=dtype,
                    device_map="cuda:0",
                    max_inference_batch_size=1,
                    max_new_tokens=256,
                )
            except Exception:
                LOG.exception("Qwen3-ASR final review disabled after load failure")
                self.qwen = None
        self.load_seconds = time.monotonic() - started

    async def online_chunk(self, state: StreamState, pcm: bytes, *, final: bool = False) -> str:
        if not pcm and not final:
            return ""
        status = {
            "cache": state.online_cache,
            "is_final": final,
            "chunk_size": self.chunk_size,
            "encoder_chunk_look_back": 4,
            "decoder_chunk_look_back": 1,
            "itn": True,
            "hotword": " ".join(self.hotwords),
        }
        async with self.online_lock:
            result = await asyncio.to_thread(self.online.generate, input=pcm, **status)
        text = _first_text(result)
        if text:
            state.partial_parts.append(text)
        return "".join(state.partial_parts).strip()

    async def vad_chunk(self, state: StreamState, pcm: bytes) -> bool:
        status = {
            "cache": state.vad_cache,
            "is_final": False,
            "chunk_size": max(60, round(len(pcm) / 2 / self.sample_rate * 1000)),
        }
        async with self.vad_lock:
            result = await asyncio.to_thread(self.vad.generate, input=pcm, **status)
        try:
            segments = result[0].get("value", [])
            return any(len(segment) > 1 and segment[1] != -1 for segment in segments)
        except (IndexError, AttributeError, TypeError):
            return False

    async def punctuate(self, text: str) -> str:
        if not text:
            return ""
        async with self.punc_lock:
            result = await asyncio.to_thread(self.punc.generate, input=text)
        return _first_text(result) or text

    async def review(self, pcm: bytes, fallback: str) -> tuple[str, str]:
        if self.qwen is None or not pcm:
            return fallback, "paraformer"

        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0

        def run_qwen_sync() -> str:
            with self.qwen_lock:
                results = self.qwen.transcribe(
                    audio=(audio, self.sample_rate),
                    context=" ".join(self.hotwords),
                    language="Chinese",
                    return_time_stamps=False,
                )
            return str(results[0].text).strip() if results else ""

        try:
            reviewed = await asyncio.wait_for(
                asyncio.to_thread(run_qwen_sync), timeout=self.final_timeout_ms / 1000
            )
            return (reviewed, "qwen3-asr") if reviewed else (fallback, "paraformer")
        except asyncio.TimeoutError:
            LOG.warning("Qwen3-ASR final review timed out after %d ms", self.final_timeout_ms)
            return fallback, "paraformer"
        except Exception as exc:
            LOG.warning("Qwen3-ASR final review fallback: %s", exc)
            return fallback, "paraformer"

    def correct(self, text: str) -> str:
        for source, replacement in self.corrections.items():
            text = text.replace(source, replacement)
        return text.strip()

    async def feed(self, state: StreamState, pcm: bytes) -> tuple[str | None, bool]:
        state.all_pcm.extend(pcm)
        state.pending_pcm.extend(pcm)
        ended, partial = False, None
        if len(state.pending_pcm) >= self.sample_rate * 2 * self.chunk_ms // 1000:
            current = bytes(state.pending_pcm)
            state.pending_pcm.clear()
            partial = await self.online_chunk(state, current)
            if partial and state.first_partial_ms is None:
                state.first_partial_ms = (time.monotonic() - state.started_at) * 1000
        ended = await self.vad_chunk(state, pcm)
        return partial, ended

    async def finalize(self, state: StreamState) -> dict[str, Any]:
        if state.finalized:
            return {"type": "final", "text": "", "is_final": True, "source": "duplicate"}
        state.finalized = True
        if state.pending_pcm:
            await self.online_chunk(state, bytes(state.pending_pcm), final=True)
            state.pending_pcm.clear()
        else:
            await self.online_chunk(state, b"", final=True)
        paraformer = self.correct(await self.punctuate("".join(state.partial_parts).strip()))
        final_started = time.monotonic()
        text, source = await self.review(bytes(state.all_pcm), paraformer)
        text = self.correct(text or paraformer)
        metrics = {
            "first_partial_ms": state.first_partial_ms,
            "final_after_endpoint_ms": (time.monotonic() - final_started) * 1000,
            "audio_ms": len(state.all_pcm) / 2 / self.sample_rate * 1000,
            "source": source,
        }
        self.last_metrics = metrics
        return {"type": "final", "text": text, "is_final": True, "source": source, "metrics": metrics}


def create_app(backend: ChineseASRBackend) -> Any:
    from aiohttp import web

    app = web.Application(client_max_size=40 * 1024 * 1024)

    async def health(_request: Any) -> Any:
        payload = {
            "ok": backend.online is not None,
            "stream_model": backend.stream_model_name,
            "final_model": backend.final_model_name,
            "final_model_source": (
                backend.final_local_model
                if backend.final_local_model and Path(backend.final_local_model).is_dir()
                else backend.final_model_name
            ),
            "final_enabled": backend.qwen is not None,
            "endpoint_ms": backend.endpoint_ms,
            "hotwords": len(backend.hotwords),
            "load_seconds": backend.load_seconds,
            "last_metrics": backend.last_metrics,
        }
        try:
            import torch
            if torch.cuda.is_available():
                payload["gpu_allocated_mib"] = round(torch.cuda.memory_allocated() / 1048576, 1)
                payload["gpu_reserved_mib"] = round(torch.cuda.memory_reserved() / 1048576, 1)
        except ImportError:
            pass
        return web.json_response(payload)

    async def transcribe(request: Any) -> Any:
        reader = await request.multipart()
        raw = b""
        while part := await reader.next():
            if part.name == "file":
                chunks = bytearray()
                while chunk := await part.read_chunk():
                    chunks.extend(chunk)
                raw = bytes(chunks)
        if not raw:
            raise web.HTTPBadRequest(text="audio file is required")
        try:
            pcm = _read_pcm_wav(raw)
        except (ValueError, wave.Error) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        state = StreamState()
        chunk_bytes = backend.sample_rate * 2 * backend.chunk_ms // 1000
        for offset in range(0, len(pcm), chunk_bytes):
            await backend.online_chunk(state, pcm[offset : offset + chunk_bytes])
            state.all_pcm.extend(pcm[offset : offset + chunk_bytes])
        result = await backend.finalize(state)
        return web.json_response(result)

    async def stream(request: Any) -> Any:
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1024 * 1024)
        await ws.prepare(request)
        state = StreamState()
        await ws.send_json({"type": "ready", "sample_rate": 16000, "format": "pcm_s16le"})
        async for message in ws:
            if message.type == web.WSMsgType.BINARY:
                if state.finalized or state.cancelled:
                    continue
                partial, ended = await backend.feed(state, bytes(message.data))
                if partial:
                    await ws.send_json({"type": "partial", "text": partial, "is_final": False})
                if ended:
                    await ws.send_json(await backend.finalize(state))
            elif message.type == web.WSMsgType.TEXT:
                try:
                    command = json.loads(message.data)
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "error": "invalid JSON"})
                    continue
                action = command.get("type") if isinstance(command, dict) else None
                if action in {"finish", "end"} and not state.finalized:
                    await ws.send_json(await backend.finalize(state))
                elif action == "cancel":
                    state.cancelled = True
                    await ws.send_json({"type": "cancelled"})
                    await ws.close()
            elif message.type == web.WSMsgType.ERROR:
                break
        return ws

    app.router.add_get("/health", health)
    app.router.add_post("/v1/transcribe", transcribe)
    app.router.add_get("/v1/stream", stream)
    return app


def main() -> int:
    from aiohttp import web

    parser = argparse.ArgumentParser(description="Local FunASR + Qwen3-ASR Chinese service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        parser.error("Chinese ASR must listen on loopback")
    logging.basicConfig(level=os.environ.get("CHINESE_SPEECH_LOG_LEVEL", "INFO"))
    backend = ChineseASRBackend()
    LOG.info("Loading FunASR and Qwen final reviewer")
    backend.load()
    web.run_app(create_app(backend), host=args.host, port=args.port, access_log=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
