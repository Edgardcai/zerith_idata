from __future__ import annotations

import io
import math
import struct
import subprocess
import threading
import wave
from dataclasses import dataclass

import numpy as np


class AudioError(RuntimeError):
    pass


@dataclass
class AlsaMicrophone:
    device: str
    sample_rate: int = 16000
    chunk_seconds: float = 0.1

    def __post_init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self.frames_per_chunk = int(self.sample_rate * self.chunk_seconds)

    def __enter__(self) -> "AlsaMicrophone":
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def open(self) -> None:
        if self._process is not None:
            return
        self._process = subprocess.Popen(
            [
                "arecord",
                "-q",
                "-D",
                self.device,
                "-t",
                "raw",
                "-f",
                "S16_LE",
                "-c",
                "1",
                "-r",
                str(self.sample_rate),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def read(self, frames: int | None = None) -> bytes:
        if self._process is None or self._process.stdout is None:
            raise AudioError("麦克风尚未打开")
        expected = (frames or self.frames_per_chunk) * 2
        chunks: list[bytes] = []
        received = 0
        while received < expected:
            chunk = self._process.stdout.read(expected - received)
            if not chunk:
                detail = ""
                if self._process.stderr is not None:
                    detail = self._process.stderr.read().decode("utf-8", errors="replace").strip()
                raise AudioError(f"麦克风数据流中断：{detail or 'arecord 已退出'}")
            chunks.append(chunk)
            received += len(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.stdout is not None:
            process.stdout.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)


@dataclass(frozen=True)
class AlsaSpeaker:
    device: str

    def play(self, encoded_audio: bytes) -> None:
        if not encoded_audio:
            return
        audio = encoded_audio if encoded_audio[:4] == b"RIFF" else self._decode_to_wav(encoded_audio)
        process = subprocess.run(
            ["aplay", "-q", "-D", self.device],
            input=audio,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
        if process.returncode:
            detail = process.stderr.decode("utf-8", errors="replace").strip()
            raise AudioError(f"扬声器播放失败：{detail}")

    @staticmethod
    def _decode_to_wav(encoded_audio: bytes) -> bytes:
        process = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-f",
                "wav",
                "pipe:1",
            ],
            input=encoded_audio,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
        if process.returncode:
            detail = process.stderr.decode("utf-8", errors="replace").strip()
            raise AudioError(f"音频解码失败：{detail}")
        return process.stdout

    def chime(self) -> None:
        self.play(make_chime())


class AlsaPcmStream:
    """One long-lived aplay process fed with raw PCM as chunks become available."""

    def __init__(self, device: str, sample_rate: int = 24000) -> None:
        self.device = device
        self.sample_rate = int(sample_rate)
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None

    def open(self) -> None:
        with self._lock:
            if self._process is not None:
                return
            self._process = subprocess.Popen(
                [
                    "aplay", "-q", "-D", self.device,
                    "-t", "raw", "-f", "S16_LE", "-c", "1", "-r", str(self.sample_rate),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

    def write(self, pcm: bytes) -> None:
        if not pcm:
            return
        with self._lock:
            process = self._process
            if process is None or process.stdin is None:
                raise AudioError("PCM 播放器尚未启动")
            try:
                process.stdin.write(pcm)
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise AudioError("PCM 播放流已中断") from exc

    def finish(self) -> None:
        with self._lock:
            process, self._process = self._process, None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            returncode = process.wait(timeout=120)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
            raise AudioError("PCM 播放超时")
        if returncode:
            detail = process.stderr.read().decode("utf-8", errors="replace").strip() if process.stderr else ""
            raise AudioError(f"PCM 播放失败：{detail or returncode}")

    def cancel(self) -> None:
        with self._lock:
            process, self._process = self._process, None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=0.15)


def pcm16_to_float32(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def float32_to_wav(samples: np.ndarray, sample_rate: int = 16000) -> bytes:
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2").tobytes()
    return pcm16_to_wav(pcm, sample_rate)


def pcm16_to_wav(pcm: bytes, sample_rate: int = 16000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return output.getvalue()


def make_chime(sample_rate: int = 16000) -> bytes:
    pcm = bytearray()
    for frequency in (740.0, 980.0):
        duration = 0.065
        count = int(sample_rate * duration)
        for i in range(count):
            envelope = min(1.0, i / (sample_rate * 0.008), (count - i) / (sample_rate * 0.02))
            value = int(32767 * 0.14 * envelope * math.sin(2 * math.pi * frequency * i / sample_rate))
            pcm.extend(struct.pack("<h", value))
        pcm.extend(b"\x00\x00" * int(sample_rate * 0.018))
    return pcm16_to_wav(bytes(pcm), sample_rate)
