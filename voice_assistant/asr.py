from __future__ import annotations

import logging
import re

import httpx
import numpy as np

from .audio import float32_to_wav


LOG = logging.getLogger(__name__)


class SpeechRecognitionError(RuntimeError):
    pass


class OpenAICompatibleASR:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = "whisper-1",
        client: httpx.Client | None = None,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/audio/transcriptions"
        self._api_key = api_key
        self._models = tuple(dict.fromkeys((model, "gpt-4o-transcribe-diarize", "whisper-1")))
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(90.0, connect=8.0),
            trust_env=True,
            follow_redirects=True,
        )
        self._owns_client = client is None

    def transcribe(
        self,
        samples: np.ndarray,
        sample_rate: int = 16000,
        language: str = "zh",
    ) -> str:
        if samples.size == 0:
            return ""
        if language not in {"zh", "en"}:
            raise ValueError(f"unsupported ASR language: {language}")
        audio = float32_to_wav(samples, sample_rate)
        last_error = "unknown error"
        for model in self._models:
            try:
                response_format = "verbose_json" if model == "whisper-1" else "json"
                data = {
                    "model": model,
                    "language": language,
                    "response_format": response_format,
                    "temperature": "0",
                }
                if "diarize" not in model:
                    data["prompt"] = (
                        "这是机器人小达的中文对话，可能包含机器人、关节、相机等词。"
                        if language == "zh"
                        else "This is a clear English conversation with the robot assistant XiaoDa."
                    )
                response = self._client.post(
                    self._url,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    data=data,
                    files={"file": ("utterance.wav", audio, "audio/wav")},
                    timeout=httpx.Timeout(90.0, connect=8.0),
                )
                response.raise_for_status()
                payload = response.json()
                text = str(payload.get("text", "")).strip().replace("小達", "小达")
                if _looks_like_hallucination(text, payload, samples.size / sample_rate):
                    LOG.warning("ASR rejected a likely hallucination (%d characters)", len(text))
                    return ""
                LOG.debug("ASR model %s returned %d characters", model, len(text))
                return text
            except (httpx.HTTPError, ValueError) as exc:
                last_error = _safe_http_error(exc)
                LOG.warning("ASR model %s failed: %s", model, last_error)
        raise SpeechRecognitionError(f"语音识别失败：{last_error}")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def _safe_http_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            payload = exc.response.json()
            message = payload.get("error", {}).get("message") or payload.get("message")
            if message:
                return str(message)[:240]
        except ValueError:
            pass
        return f"HTTP {exc.response.status_code}"
    return str(exc)[:240]


def _looks_like_hallucination(text: str, payload: dict[str, object], duration: float) -> bool:
    if not text:
        return False
    normalized = re.sub(r"[\s，。！？,.!?]", "", text)
    common = (
        "请不吝点赞订阅转发打赏",
        "字幕由Amara.org社区提供",
        "感谢观看",
        "明镜与点点栏目",
    )
    if any(phrase.lower() in normalized.lower() for phrase in common):
        return True
    if duration < 1.0 and len(normalized) > 18:
        return True

    segments = payload.get("segments")
    if isinstance(segments, list) and segments:
        no_speech = []
        log_probs = []
        compression = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            if isinstance(segment.get("no_speech_prob"), (int, float)):
                no_speech.append(float(segment["no_speech_prob"]))
            if isinstance(segment.get("avg_logprob"), (int, float)):
                log_probs.append(float(segment["avg_logprob"]))
            if isinstance(segment.get("compression_ratio"), (int, float)):
                compression.append(float(segment["compression_ratio"]))
        if no_speech and log_probs and max(no_speech) > 0.7 and min(log_probs) < -1.0:
            return True
        if compression and max(compression) > 2.8:
            return True
    return False
