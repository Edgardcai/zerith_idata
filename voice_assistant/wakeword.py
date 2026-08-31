from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

from .audio import AlsaMicrophone, AudioError, pcm16_to_float32


LOG = logging.getLogger(__name__)


class WakeWordError(RuntimeError):
    pass


class SherpaWakeWord:
    def __init__(
        self,
        model_dir: Path,
        microphone_device: str,
        sample_rate: int = 16000,
        threshold: float = 0.38,
        score: float = 1.35,
        num_threads: int = 1,
    ) -> None:
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise WakeWordError("缺少 sherpa-onnx，请先运行安装脚本") from exc

        encoder = model_dir / "encoder-epoch-13-avg-2-chunk-8-left-64.int8.onnx"
        decoder = model_dir / "decoder-epoch-13-avg-2-chunk-8-left-64.onnx"
        joiner = model_dir / "joiner-epoch-13-avg-2-chunk-8-left-64.int8.onnx"
        tokens = model_dir / "tokens.txt"
        keywords = model_dir / "keywords_xiaoda.txt"
        missing = [p for p in (encoder, decoder, joiner, tokens, keywords) if not p.is_file()]
        if missing:
            raise WakeWordError("缺少唤醒模型文件：" + ", ".join(str(p) for p in missing))

        self._spotter = sherpa_onnx.KeywordSpotter(
            encoder=str(encoder),
            decoder=str(decoder),
            joiner=str(joiner),
            tokens=str(tokens),
            keywords_file=str(keywords),
            num_threads=num_threads,
            max_active_paths=4,
            num_trailing_blanks=1,
            keywords_score=score,
            keywords_threshold=threshold,
            provider="cpu",
        )
        self._device = microphone_device
        self._sample_rate = sample_rate

    def detect_samples(self, samples: np.ndarray, sample_rate: int | None = None) -> str | None:
        """Decode a finite sample array; used for model commissioning and tests."""
        rate = sample_rate or self._sample_rate
        stream = self._spotter.create_stream()
        stream.accept_waveform(rate, np.asarray(samples, dtype=np.float32))
        stream.accept_waveform(rate, np.zeros(int(rate * 0.7), dtype=np.float32))
        stream.input_finished()
        while self._spotter.is_ready(stream):
            self._spotter.decode_stream(stream)
            result = self._spotter.get_result(stream)
            if result:
                return str(result)
        return None

    def wait(
        self,
        stop_event: threading.Event | None = None,
        trigger_event: threading.Event | None = None,
    ) -> str | None:
        stop_event = stop_event or threading.Event()
        stream = self._spotter.create_stream()
        with AlsaMicrophone(self._device, self._sample_rate) as microphone:
            while not stop_event.is_set():
                if trigger_event is not None and trigger_event.is_set():
                    return "__manual__"
                try:
                    chunk = microphone.read()
                except AudioError:
                    if stop_event.is_set():
                        return None
                    raise
                if trigger_event is not None and trigger_event.is_set():
                    return "__manual__"
                stream.accept_waveform(self._sample_rate, pcm16_to_float32(chunk))
                while self._spotter.is_ready(stream):
                    self._spotter.decode_stream(stream)
                    result = self._spotter.get_result(stream)
                    if result:
                        self._spotter.reset_stream(stream)
                        LOG.info("检测到唤醒词：%s", result)
                        return str(result)
        return None
