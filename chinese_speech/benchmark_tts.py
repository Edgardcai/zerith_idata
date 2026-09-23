"""Measure the robot PCM playback path silently; start local TTS before running."""
from __future__ import annotations

import argparse
import json
import time

from voice_assistant.audio import AlsaPcmStream
from voice_assistant.chinese_speech import ChineseSpeechClient
from voice_assistant.tts import ChineseSentenceSpeechPlayer


PHRASES = [
    "你好，我是小达，很高兴和你聊天。",
    "你可以直接告诉我想了解的问题，我会尽量用简单清楚的话来回答。",
    "如果有需要，也可以让我把刚才的内容再解释一遍。",
]


class SilentMeasuredPlayer(AlsaPcmStream):
    """Keep device timing/backpressure while replacing speech samples with zeros."""
    def __init__(self, device: str):
        super().__init__(device, 24000)
        self.first_write: float | None = None
        self.bytes_written = 0

    def write(self, pcm: bytes) -> None:
        if self.first_write is None:
            self.first_write = time.monotonic()
        self.bytes_written += len(pcm)
        super().write(bytes(len(pcm)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--speaker-device", default="pipewire")
    parser.add_argument("--label", default="current")
    args = parser.parse_args()
    client = ChineseSpeechClient("http://127.0.0.1:8770", "http://127.0.0.1:8771")
    player = ChineseSentenceSpeechPlayer(client, args.speaker_device)
    measured = SilentMeasuredPlayer(args.speaker_device)
    player._player = measured
    begin = time.monotonic()
    try:
        audio = player.speak(iter(PHRASES))
        elapsed = time.monotonic() - begin
        if measured.first_write is None:
            raise RuntimeError("TTS produced no audio")
        duration = measured.bytes_written / 48000
        first = measured.first_write - begin
        print(json.dumps({
            "label": args.label, "first_audio_s": round(first, 3),
            "elapsed_s": round(elapsed, 3), "audio_s": round(duration, 3),
            "extra_wait_s": round(elapsed - first - duration, 3),
            "wav_bytes": len(audio),
        }, ensure_ascii=False))
    finally:
        player.cancel()
        client.close()


if __name__ == "__main__":
    main()
