from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
import queue
import threading

import httpx

from .audio import AlsaPcmStream, AlsaSpeaker, pcm16_to_wav
from .chinese_speech import ChineseSpeechClient, is_chinese_language


LOG = logging.getLogger(__name__)


class SpeechSynthesisError(RuntimeError):
    pass


class OpenAICompatibleTTS:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        fallback_model: str,
        voice: str,
        speed: float = 1.2,
        client: httpx.Client | None = None,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/audio/speech"
        self._api_key = api_key
        self._models = tuple(dict.fromkeys((model, fallback_model)))
        self._voice = voice
        self._speed = float(speed)
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(120.0, connect=8.0),
            trust_env=True,
            follow_redirects=True,
        )
        self._owns_client = client is None

    def synthesize(self, text: str) -> bytes:
        last_error = "unknown error"
        for model in self._models:
            payload: dict[str, object] = {
                "model": model,
                "voice": self._voice,
                "input": text,
                "response_format": "wav",
                "speed": self._speed,
            }
            if model == "gpt-4o-mini-tts":
                payload["instructions"] = "用自然、亲切、简洁的口吻朗读，语速较快，减少停顿。"
            try:
                response = self._client.post(
                    self._url,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=httpx.Timeout(120.0, connect=8.0),
                )
                response.raise_for_status()
                if not response.content:
                    raise SpeechSynthesisError("接口返回了空音频")
                return response.content
            except (httpx.HTTPError, SpeechSynthesisError) as exc:
                last_error = _safe_http_error(exc)
                LOG.warning("TTS model %s failed: %s", model, last_error)
        raise SpeechSynthesisError(f"语音合成失败：{last_error}")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class StreamingSpeechPlayer:
    """Collect one complete reply, synthesize it once, then play it."""

    def __init__(self, tts: OpenAICompatibleTTS, speaker: AlsaSpeaker) -> None:
        self._tts = tts
        self._speaker = speaker

    def speak(
        self,
        phrases: Iterable[str],
        on_audio_ready: Callable[[bytes], None] | None = None,
    ) -> bytes:
        # Consuming the iterator here deliberately waits for the complete LLM
        # reply.  The normal path therefore makes exactly one /audio/speech
        # request instead of one request per sentence.
        text = "".join(phrases).strip()
        if not text:
            return b""
        audio = self._tts.synthesize(text)
        if on_audio_ready is not None:
            on_audio_ready(audio)
        self._speaker.play(audio)
        return audio


class ChineseSentenceSpeechPlayer:
    """Start local Chinese synthesis as soon as the first complete sentence arrives."""

    def __init__(
        self,
        client: ChineseSpeechClient,
        speaker_device: str,
        sample_rate: int = 24000,
    ) -> None:
        self._client = client
        self._player = AlsaPcmStream(speaker_device, sample_rate)
        self._sample_rate = int(sample_rate)
        self._cancel_event = threading.Event()

    def speak(
        self,
        phrases: Iterable[str],
        on_audio_ready: Callable[[bytes], None] | None = None,
        on_audio_started: Callable[[], None] | None = None,
    ) -> bytes:
        self._cancel_event.clear()
        phrases_queue: queue.Queue[object] = queue.Queue()
        sentinel = object()
        producer_error: list[BaseException] = []

        def produce() -> None:
            try:
                for phrase in phrases:
                    if self._cancel_event.is_set():
                        break
                    phrases_queue.put(phrase)
            except BaseException as exc:
                producer_error.append(exc)
            finally:
                phrases_queue.put(sentinel)

        producer = threading.Thread(target=produce, name="xiaoda-llm-sentence-producer", daemon=True)
        producer.start()
        pcm = bytearray()
        started = False
        self._player.open()
        try:
            while True:
                phrase = phrases_queue.get()
                if phrase is sentinel:
                    break
                text = str(phrase).strip()
                if self._cancel_event.is_set():
                    break
                if not text:
                    continue
                for chunk in self._client.stream_speech(text, cancel_event=self._cancel_event):
                    if self._cancel_event.is_set():
                        break
                    if not started:
                        started = True
                        if on_audio_started is not None:
                            on_audio_started()
                    pcm.extend(chunk)
                    self._player.write(chunk)
            if self._cancel_event.is_set():
                self._player.cancel()
                return b""
            producer.join(timeout=1.0)
            if producer_error:
                raise producer_error[0]
            self._player.finish()
        except BaseException:
            self._player.cancel()
            raise
        audio = pcm16_to_wav(bytes(pcm), self._sample_rate) if pcm else b""
        if audio and on_audio_ready is not None:
            on_audio_ready(audio)
        return audio

    def cancel(self) -> None:
        self._cancel_event.set()
        self._player.cancel()
        self._client.cancel()


class LanguageRoutedSpeechPlayer:
    def __init__(
        self,
        english: StreamingSpeechPlayer,
        chinese: ChineseSentenceSpeechPlayer | None,
    ) -> None:
        self.english = english
        self.chinese = chinese

    def speak(
        self,
        phrases: Iterable[str],
        *,
        language: str,
        on_audio_ready: Callable[[bytes], None] | None = None,
        on_audio_started: Callable[[], None] | None = None,
    ) -> bytes:
        if is_chinese_language(language):
            if self.chinese is None:
                raise SpeechSynthesisError("中文本地 TTS 服务未启用")
            return self.chinese.speak(phrases, on_audio_ready, on_audio_started)
        # Frozen English behavior: collect the complete reply, one request, same voice/config.
        return self.english.speak(phrases, on_audio_ready)

    def cancel(self) -> None:
        if self.chinese is not None:
            self.chinese.cancel()


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
