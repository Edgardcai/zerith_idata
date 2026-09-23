from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
import queue
import threading
import time

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
        if not self._api_key:
            raise SpeechSynthesisError("未配置云端语音合成；请启用本地中文语音并等待模型就绪，英文需要另配语音接口")
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
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()
        cancel = getattr(self._speaker, "cancel", None)
        if cancel is not None:
            cancel()

    def speak(
        self,
        phrases: Iterable[str],
        on_audio_ready: Callable[[bytes], None] | None = None,
    ) -> bytes:
        self._cancel_event.clear()
        # Consuming the iterator here deliberately waits for the complete LLM
        # reply.  The normal path therefore makes exactly one /audio/speech
        # request instead of one request per sentence.
        text = "".join(phrases).strip()
        if not text or self._cancel_event.is_set():
            return b""
        audio = self._tts.synthesize(text)
        if self._cancel_event.is_set():
            return b""
        if on_audio_ready is not None:
            on_audio_ready(audio)
        self._speaker.play(audio)
        return audio


class ChineseSentenceSpeechPlayer:
    """Read LLM text, prefetch PCM, and play audio independently with bounded queues."""

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
        self.last_metrics: dict[str, float | None] = {}

    def speak(
        self,
        phrases: Iterable[str],
        on_audio_ready: Callable[[bytes], None] | None = None,
        on_audio_started: Callable[[], None] | None = None,
    ) -> bytes:
        # Each run owns its event, so cancelling an old worker cannot affect the next reply.
        stop = threading.Event()
        self._cancel_event = stop
        phrases_queue: queue.Queue[object] = queue.Queue(maxsize=8)
        audio_queue: queue.Queue[object] = queue.Queue(maxsize=48)
        sentinel = object()
        producer_error: list[BaseException] = []
        synthesis_error: list[BaseException] = []
        synthesis_done = threading.Event()
        begun = time.monotonic()

        def put(target: queue.Queue[object], item: object) -> bool:
            while not stop.is_set():
                try:
                    target.put(item, timeout=0.05)
                    return True
                except queue.Full:
                    continue
            return False

        def produce() -> None:
            iterator = iter(phrases)
            try:
                while not stop.is_set():
                    try:
                        phrase = next(iterator)
                    except StopIteration:
                        break
                    if not put(phrases_queue, phrase):
                        break
            except BaseException as exc:
                producer_error.append(exc)
            finally:
                close = getattr(iterator, "close", None)
                if close is not None:
                    try:
                        close()
                    except BaseException as exc:
                        producer_error.append(exc)
                put(phrases_queue, sentinel)

        def synthesize() -> None:
            try:
                while not stop.is_set():
                    try:
                        phrase = phrases_queue.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    if phrase is sentinel:
                        if producer_error:
                            raise producer_error[0]
                        break
                    text = str(phrase).strip()
                    if not text:
                        continue
                    for chunk in self._client.stream_speech(text, cancel_event=stop):
                        if not put(audio_queue, chunk):
                            break
            except BaseException as exc:
                synthesis_error.append(exc)
            finally:
                synthesis_done.set()
                put(audio_queue, sentinel)

        producer = threading.Thread(target=produce, name="xiaoda-llm-sentence-producer", daemon=True)
        synthesis = threading.Thread(target=synthesize, name="xiaoda-tts-prefetch", daemon=True)
        producer.start()
        synthesis.start()
        pcm = bytearray()
        first_audio_ms = None
        try:
            while not stop.is_set():
                try:
                    chunk = audio_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                if chunk is sentinel:
                    break
                if not chunk:
                    continue
                if first_audio_ms is None:
                    first_audio_ms = (time.monotonic() - begun) * 1000
                    self._player.open()
                    if on_audio_started is not None:
                        on_audio_started()
                pcm.extend(chunk)
                self._player.write(chunk)
            if stop.is_set():
                self._player.cancel()
                return b""
            if synthesis_error:
                raise synthesis_error[0]
            self._player.finish()
        except BaseException:
            self._player.cancel()
            if stop.is_set():
                return b""
            raise
        finally:
            stop.set()
            if not synthesis_done.is_set():
                self._client.cancel()
            synthesis.join(timeout=1.0)
            producer.join(timeout=0.2)
            self.last_metrics = {
                "first_audio_ms": first_audio_ms,
                "total_ms": (time.monotonic() - begun) * 1000,
                "audio_ms": len(pcm) / (self._sample_rate * 2) * 1000,
            }
            LOG.info("中文语音播放指标：%s", self.last_metrics)
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
        local_ready: Callable[[], bool] | None = None,
        cloud_fallback: bool = True,
    ) -> None:
        self.cloud_fallback = cloud_fallback
        self.english = english
        self.chinese = chinese
        self.local_ready = local_ready or (lambda: chinese is not None)

    def speak(
        self,
        phrases: Iterable[str],
        *,
        language: str,
        on_audio_ready: Callable[[bytes], None] | None = None,
        on_audio_started: Callable[[], None] | None = None,
    ) -> bytes:
        if is_chinese_language(language):
            if self.chinese is not None and self.local_ready():
                # The producer consumes the LLM iterator on another thread. Do not
                # replay it after a stream failure: it may already have spoken audio.
                return self.chinese.speak(phrases, on_audio_ready, on_audio_started)
            if not self.cloud_fallback:
                raise SpeechSynthesisError("本地语音尚未就绪，请启用本地语音并等待加载完成")
            def cloud_audio_ready(audio: bytes) -> None:
                if on_audio_ready is not None:
                    on_audio_ready(audio)
                if on_audio_started is not None:
                    on_audio_started()
            return self.english.speak(phrases, cloud_audio_ready)
        # Frozen English behavior: collect the complete reply, one request, same voice/config.
        return self.english.speak(phrases, on_audio_ready)

    def cancel(self) -> None:
        self.english.cancel()
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
