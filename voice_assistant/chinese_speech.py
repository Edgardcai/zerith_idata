from __future__ import annotations

from collections.abc import Iterator
import logging
import threading
import uuid

import httpx
import numpy as np

from .audio import float32_to_wav


LOG = logging.getLogger(__name__)


class ChineseSpeechError(RuntimeError):
    pass


def is_chinese_language(language: str | None) -> bool:
    value = str(language or "").strip().lower().replace("_", "-")
    return value == "zh" or value.startswith("zh-")


def normalize_session_language(language: str | None) -> str:
    value = str(language or "zh").strip().lower().replace("_", "-")
    if is_chinese_language(value):
        return "zh"
    if value == "en" or value.startswith("en-"):
        return "en"
    raise ValueError("语言只支持中文 zh-* 或英文 en-*")


class ChineseSpeechClient:
    """Loopback-only client for the isolated local Chinese speech services."""

    def __init__(
        self,
        asr_base_url: str,
        tts_base_url: str,
        *,
        speaker: str = "Serena",
        final_timeout_ms: int = 1500,
        sample_rate: int = 24000,
    ) -> None:
        self.asr_base_url = asr_base_url.rstrip("/")
        self.tts_base_url = tts_base_url.rstrip("/")
        self.speaker = speaker
        self.final_timeout_ms = int(final_timeout_ms)
        self.sample_rate = int(sample_rate)
        self._client = httpx.Client(
            timeout=httpx.Timeout(120.0, connect=1.0, write=10.0, pool=2.0),
            trust_env=False,
        )
        self._active_lock = threading.Lock()
        self._active_request_id: str | None = None

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        if samples.size == 0:
            return ""
        try:
            response = self._client.post(
                f"{self.asr_base_url}/v1/transcribe",
                files={"file": ("utterance.wav", float32_to_wav(samples, sample_rate), "audio/wav")},
                data={"language": "zh", "timeout_ms": str(self.final_timeout_ms)},
                timeout=httpx.Timeout(max(5.0, self.final_timeout_ms / 1000 + 3.0), connect=1.0),
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("中文语音识别服务返回格式错误")
            metrics = payload.get("metrics")
            LOG.info(
                "中文 ASR：source=%s audio_ms=%s final_ms=%s",
                payload.get("source", "unknown"),
                metrics.get("audio_ms") if isinstance(metrics, dict) else "unknown",
                metrics.get("final_after_endpoint_ms") if isinstance(metrics, dict) else "unknown",
            )
            return str(payload.get("text", "")).strip()
        except (httpx.HTTPError, ValueError) as exc:
            raise ChineseSpeechError(f"中文语音识别服务不可用：{_safe_error(exc)}") from exc

    def stream_speech(
        self,
        text: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[bytes]:
        request_id = uuid.uuid4().hex
        with self._active_lock:
            self._active_request_id = request_id
        try:
            with self._client.stream(
                "POST",
                f"{self.tts_base_url}/v1/speech",
                json={
                    "request_id": request_id,
                    "text": text,
                    "language": "Chinese",
                    "speaker": self.speaker,
                    "format": "pcm_s16le",
                },
                timeout=httpx.Timeout(120.0, connect=1.0, read=120.0),
            ) as response:
                response.raise_for_status()
                returned_rate = int(response.headers.get("X-Audio-Sample-Rate", self.sample_rate))
                if returned_rate != self.sample_rate:
                    raise ChineseSpeechError(
                        f"中文 TTS 采样率不匹配：期望 {self.sample_rate}，收到 {returned_rate}"
                    )
                for chunk in response.iter_bytes(16384):
                    if cancel_event is not None and cancel_event.is_set():
                        self.cancel()
                        return
                    if chunk:
                        yield chunk
        except (httpx.HTTPError, ValueError) as exc:
            if cancel_event is not None and cancel_event.is_set():
                return
            raise ChineseSpeechError(f"中文语音合成服务不可用：{_safe_error(exc)}") from exc
        finally:
            with self._active_lock:
                if self._active_request_id == request_id:
                    self._active_request_id = None

    def cancel(self) -> None:
        with self._active_lock:
            request_id = self._active_request_id
        if not request_id:
            return
        try:
            self._client.post(
                f"{self.tts_base_url}/v1/cancel",
                json={"request_id": request_id},
                timeout=0.5,
            )
        except httpx.HTTPError:
            LOG.debug("中文 TTS 取消通知失败", exc_info=True)

    def close(self) -> None:
        self.cancel()
        self._client.close()


class LanguageRoutedASR:
    def __init__(self, english_asr: object, chinese: ChineseSpeechClient | None) -> None:
        self.english_asr = english_asr
        self.chinese = chinese

    def transcribe(
        self,
        samples: np.ndarray,
        sample_rate: int = 16000,
        language: str = "zh",
    ) -> str:
        if is_chinese_language(language):
            if self.chinese is None:
                raise ChineseSpeechError("中文本地语音链路未启用")
            return self.chinese.transcribe(samples, sample_rate)
        # Frozen English path: same object, models, payload, config and fallback order.
        return self.english_asr.transcribe(samples, sample_rate, language="en")


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            payload = exc.response.json()
            return str(payload.get("error") or payload.get("detail") or f"HTTP {exc.response.status_code}")[:240]
        except ValueError:
            return f"HTTP {exc.response.status_code}"
    return str(exc)[:240]
