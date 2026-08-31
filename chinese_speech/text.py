from __future__ import annotations

import html
import re


_URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_CODE_BLOCK = re.compile(r"```(?:\w+)?\s*(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]+)`")
_DATE = re.compile(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b")
_RMB = re.compile(r"(?:￥|¥)\s*(\d+(?:\.\d+)?)")
_USD = re.compile(r"\$\s*(\d+(?:\.\d+)?)")
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_SPACE = re.compile(r"\s+")


def normalize_tts_text(text: str) -> str:
    """Remove formatting that a speech model should not pronounce literally."""
    value = html.unescape(str(text))
    value = _CODE_BLOCK.sub(lambda match: match.group(1), value)
    value = _MARKDOWN_LINK.sub(lambda match: match.group(1), value)
    value = _INLINE_CODE.sub(lambda match: match.group(1), value)
    value = _URL.sub("链接", value)
    value = _DATE.sub(lambda match: f"{match.group(1)}年{int(match.group(2))}月{int(match.group(3))}日", value)
    value = _RMB.sub(r"\1元", value)
    value = _USD.sub(r"\1美元", value)
    value = _PERCENT.sub(r"百分之\1", value)
    for source, spoken in {
        "km/h": "公里每小时", "m/s": "米每秒", "kg": "千克", "cm": "厘米",
        "mm": "毫米", "℃": "摄氏度",
    }.items():
        value = value.replace(source, spoken)
    value = value.replace("#", "").replace("*", "").replace("_", " ")
    value = value.replace("|", "，").replace("~", "").replace("^", "")
    return _SPACE.sub(" ", value).strip()


def split_chinese_text(text: str, minimum: int = 15, maximum: int = 80) -> list[str]:
    """Prefer full sentence boundaries, then long commas, without tiny middle chunks."""
    value = normalize_tts_text(text)
    if not value:
        return []
    chunks: list[str] = []
    buffer = ""
    for character in value:
        buffer += character
        hard = character in "。！？!?；;\n"
        soft = character in "，,：:"
        if (hard and len(buffer) >= minimum) or (soft and len(buffer) >= maximum // 2) or len(buffer) >= maximum:
            chunks.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        tail = buffer.strip()
        if chunks and len(tail) < minimum and len(chunks[-1]) + len(tail) <= maximum:
            chunks[-1] += tail
        else:
            chunks.append(tail)
    return [chunk for chunk in chunks if chunk]


def float_audio_to_pcm(audio: object, *, fade_ms: int = 5, sample_rate: int = 24000) -> bytes:
    import numpy as np

    waveform = np.asarray(audio, dtype=np.float32).reshape(-1)
    if not waveform.size:
        return b""
    waveform = np.nan_to_num(waveform, copy=False)
    waveform = np.clip(waveform, -1.0, 1.0)
    fade = min(int(sample_rate * fade_ms / 1000), waveform.size // 2)
    if fade:
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        waveform[:fade] *= ramp
        waveform[-fade:] *= ramp[::-1]
    return (waveform * 32767.0).astype("<i2").tobytes()


def resample_audio(audio: object, source_rate: int, target_rate: int) -> object:
    import numpy as np

    waveform = np.asarray(audio, dtype=np.float32).reshape(-1)
    if source_rate == target_rate or not waveform.size:
        return waveform
    output_count = max(1, round(waveform.size * target_rate / source_rate))
    source_x = np.arange(waveform.size, dtype=np.float64)
    target_x = np.linspace(0, waveform.size - 1, output_count, dtype=np.float64)
    return np.interp(target_x, source_x, waveform).astype(np.float32)
