from __future__ import annotations

import time
import threading
from pathlib import Path

import numpy as np

from .audio import AlsaMicrophone, AudioError, pcm16_to_float32


class VadError(RuntimeError):
    pass


class SileroUtteranceRecorder:
    def __init__(
        self,
        model: Path,
        microphone_device: str,
        sample_rate: int = 16000,
        min_silence_seconds: float = 0.42,
        max_utterance_seconds: float = 18.0,
    ) -> None:
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise VadError("缺少 sherpa-onnx，请先运行安装脚本") from exc
        if not model.is_file():
            raise VadError(f"缺少 VAD 模型：{model}")

        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = str(model)
        config.silero_vad.threshold = 0.5
        config.silero_vad.min_silence_duration = min_silence_seconds
        config.silero_vad.min_speech_duration = 0.18
        config.silero_vad.max_speech_duration = max_utterance_seconds
        config.sample_rate = sample_rate

        self._sherpa = sherpa_onnx
        self._config = config
        self._device = microphone_device
        self._sample_rate = sample_rate
        self._window_size = config.silero_vad.window_size
        self._max_seconds = max_utterance_seconds

    def record(
        self,
        initial_timeout: float,
        stop_event: threading.Event | None = None,
        finish_event: threading.Event | None = None,
    ) -> np.ndarray | None:
        stop_event = stop_event or threading.Event()
        finish_event = finish_event or threading.Event()
        vad = self._sherpa.VoiceActivityDetector(self._config, buffer_size_in_seconds=self._max_seconds + 5)
        pending = np.empty(0, dtype=np.float32)
        start = time.monotonic()
        speech_started: float | None = None

        with AlsaMicrophone(self._device, self._sample_rate) as microphone:
            while True:
                if stop_event.is_set():
                    return None
                if finish_event.is_set():
                    return self._flush(vad)
                try:
                    chunk = microphone.read()
                except AudioError:
                    if stop_event.is_set():
                        return None
                    raise
                pending = np.concatenate((pending, pcm16_to_float32(chunk)))
                while len(pending) >= self._window_size:
                    vad.accept_waveform(pending[: self._window_size])
                    pending = pending[self._window_size :]

                if finish_event.is_set():
                    return self._flush(vad)

                now = time.monotonic()
                if vad.is_speech_detected() and speech_started is None:
                    speech_started = now

                if not vad.empty():
                    segment = np.asarray(vad.front.samples, dtype=np.float32).copy()
                    vad.pop()
                    return segment

                if speech_started is None and now - start >= initial_timeout:
                    return None
                if speech_started is not None and now - speech_started >= self._max_seconds:
                    vad.flush()
                    if not vad.empty():
                        segment = np.asarray(vad.front.samples, dtype=np.float32).copy()
                        vad.pop()
                        return segment
                    return None

    @staticmethod
    def _flush(vad: object) -> np.ndarray | None:
        vad.flush()
        if vad.empty():
            return None
        segment = np.asarray(vad.front.samples, dtype=np.float32).copy()
        vad.pop()
        return segment
