"""Incremental audio decoding for the pinned qwen-tts 0.1.1 CustomVoice model.

The public generator returns a complete waveform. Its talker exposes one codec
frame in each forward result, and the 12 Hz tokenizer has a causal decoder.
Decode prefixes and emit only new samples, retaining all context within a text
segment. Never apply a fade at internal chunk boundaries.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


class SynthesisCancelled(Exception):
    """Cooperative cancellation between codec generation steps."""


class QwenCodecStreamer:
    def __init__(self, model: Any, frames_per_chunk: int = 8) -> None:
        if not 4 <= frames_per_chunk <= 32:
            raise ValueError("streaming chunk size must be in 4..32 codec frames")
        self.model = model
        self.frames_per_chunk = frames_per_chunk

    def synthesize(
        self,
        text: str,
        speaker: str,
        emit: Callable[[Any, int], None],
        cancelled: Callable[[], bool],
        language: str = "Chinese",
    ) -> None:
        import torch

        frames: list[Any] = []
        emitted_samples = 0
        core = self.model.model
        eos = core.config.talker_config.codec_eos_token_id

        def check_cancelled() -> None:
            if cancelled():
                raise SynthesisCancelled()

        def on_forward(_module: Any, _args: Any, output: Any) -> None:
            nonlocal emitted_samples
            check_cancelled()
            codes = output.hidden_states[-1]
            if codes is None or codes[0, 0].item() == eos:
                return
            frames.append(codes.detach())
            if len(frames) % self.frames_per_chunk:
                return
            waveforms, rate = core.speech_tokenizer.decode(
                [{"audio_codes": torch.cat(frames, dim=0)}]
            )
            check_cancelled()
            waveform = waveforms[0]
            emit(waveform[emitted_samples:], int(rate))
            emitted_samples = len(waveform)

        check_cancelled()
        # The caller holds the model lock throughout generation and emission.
        handle = core.talker.register_forward_hook(on_forward)
        try:
            with torch.inference_mode():
                waveforms, rate = self.model.generate_custom_voice(
                    text=text, language=language, speaker=speaker,
                )
                check_cancelled()
                tail = waveforms[0][emitted_samples:]
                if len(tail):
                    emit(tail, int(rate))
        finally:
            # Also remove the hook after cancellation or a disconnected client.
            handle.remove()
