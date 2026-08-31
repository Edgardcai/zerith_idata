from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_MODEL_ROOT = PACKAGE_DIR / "models"
DEFAULT_KWS_MODEL = DEFAULT_MODEL_ROOT / "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value else default


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ApiProfile:
    name: str
    base_url: str
    api_key: str = field(repr=False)
    trust_environment_proxy: bool = True

    @property
    def responses_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/responses"

    @property
    def redacted(self) -> str:
        return f"{self.name} ({self.base_url}, key={'set' if self.api_key else 'missing'})"


@dataclass(frozen=True)
class VoiceConfig:
    robot_name: str
    api_file: Path
    llm_model: str
    reasoning_effort: str
    llm_profiles: tuple[ApiProfile, ...]
    audio_api_base: str
    audio_api_key: str = field(repr=False)
    asr_model: str = "gpt-4o-transcribe-diarize"
    tts_model: str = "gpt-4o-mini-tts"
    tts_fallback_model: str = "tts-1"
    tts_voice: str = "coral"
    tts_speed: float = 1.35
    microphone_device: str = "pipewire"
    speaker_device: str = "pipewire"
    sample_rate: int = 16000
    kws_model_dir: Path = DEFAULT_KWS_MODEL
    vad_model: Path = DEFAULT_MODEL_ROOT / "silero_vad.onnx"
    kws_threshold: float = 0.38
    kws_score: float = 1.35
    kws_threads: int = 1
    first_turn_timeout: float = 12.0
    followup_timeout: float = 15.0
    max_utterance_seconds: float = 18.0
    end_silence_seconds: float = 0.42
    session_turn_limit: int = 20
    robot_control_host: str = "127.0.0.1"
    robot_control_port: int = 8766
    chinese_speech_enabled: bool = True
    chinese_asr_base_url: str = "http://127.0.0.1:8770"
    chinese_tts_base_url: str = "http://127.0.0.1:8771"
    chinese_asr_stream_model: str = "paraformer-online"
    chinese_asr_final_model: str = "Qwen/Qwen3-ASR-0.6B"
    chinese_asr_final_timeout_ms: int = 1500
    chinese_asr_endpoint_ms: int = 500
    chinese_tts_model: str = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
    chinese_tts_speaker: str = "Serena"
    chinese_tts_device: str = "cuda"
    chinese_tts_dtype: str = "bfloat16"

    @classmethod
    def load(cls, api_file: str | Path | None = None) -> "VoiceConfig":
        path = Path(api_file or _env("XIAODA_API_FILE", "/home/robot/api.txt"))
        parsed = _read_loose_api_config(path)

        model = _env("XIAODA_LLM_MODEL", parsed["model"] or "gpt-5.5")
        local_base = _env("XIAODA_LLM_BASE_URL", parsed["local_base"])
        local_key = _env("XIAODA_LLM_API_KEY", parsed["local_key"])
        remote_base = _env("XIAODA_FALLBACK_BASE_URL", parsed["remote_base"])
        remote_key = _env("XIAODA_FALLBACK_API_KEY", parsed["remote_key"])

        local_profile: ApiProfile | None = None
        if local_base and local_key:
            local_profile = ApiProfile(
                name="local",
                base_url=local_base,
                api_key=local_key,
                # RFC1918 services must not be sent through the workstation proxy.
                trust_environment_proxy=False,
            )
        remote_profile: ApiProfile | None = None
        if remote_base and remote_key:
            remote_v1 = remote_base.rstrip("/")
            if not remote_v1.endswith("/v1"):
                remote_v1 += "/v1"
            remote_profile = ApiProfile(
                name="aihubmix",
                base_url=remote_v1,
                api_key=remote_key,
                trust_environment_proxy=True,
            )

        prefer_local = os.environ.get("XIAODA_PREFER_LOCAL", "").strip().lower() in {"1", "true", "yes"}
        ordered = (local_profile, remote_profile) if prefer_local else (remote_profile, local_profile)
        profiles = [profile for profile in ordered if profile is not None]

        audio_key = _env("XIAODA_AUDIO_API_KEY", remote_key)
        audio_base = _env("XIAODA_AUDIO_BASE_URL", remote_base).rstrip("/")
        if audio_base and not audio_base.endswith("/v1"):
            audio_base += "/v1"

        return cls(
            robot_name=_env("XIAODA_ROBOT_NAME", "小达"),
            api_file=path,
            llm_model=model,
            reasoning_effort=_env("XIAODA_REASONING_EFFORT", "none"),
            llm_profiles=tuple(profiles),
            audio_api_base=audio_base,
            audio_api_key=audio_key,
            asr_model=_env("XIAODA_ASR_MODEL", "gpt-4o-transcribe-diarize"),
            tts_model=_env("XIAODA_TTS_MODEL", "gpt-4o-mini-tts"),
            tts_fallback_model=_env("XIAODA_TTS_FALLBACK_MODEL", "tts-1"),
            tts_voice=_env("XIAODA_TTS_VOICE", "coral"),
            tts_speed=_env_float("XIAODA_TTS_SPEED", 1.35),
            microphone_device=_env("XIAODA_MIC_DEVICE", "pipewire"),
            speaker_device=_env("XIAODA_SPEAKER_DEVICE", "pipewire"),
            kws_model_dir=Path(_env("XIAODA_KWS_MODEL_DIR", str(DEFAULT_KWS_MODEL))),
            vad_model=Path(_env("XIAODA_VAD_MODEL", str(DEFAULT_MODEL_ROOT / "silero_vad.onnx"))),
            kws_threshold=_env_float("XIAODA_KWS_THRESHOLD", 0.38),
            kws_score=_env_float("XIAODA_KWS_SCORE", 1.35),
            kws_threads=_env_int("XIAODA_KWS_THREADS", 1),
            first_turn_timeout=_env_float("XIAODA_FIRST_TURN_TIMEOUT", 12.0),
            followup_timeout=_env_float("XIAODA_FOLLOWUP_TIMEOUT", 15.0),
            max_utterance_seconds=_env_float("XIAODA_MAX_UTTERANCE_SECONDS", 18.0),
            end_silence_seconds=_env_float("XIAODA_END_SILENCE_SECONDS", 0.42),
            robot_control_host=_env("XIAODA_ROBOT_CONTROL_HOST", "127.0.0.1"),
            robot_control_port=_env_int("XIAODA_ROBOT_CONTROL_PORT", 8766),
            chinese_speech_enabled=_env_bool("CHINESE_SPEECH_ENABLED", True),
            chinese_asr_base_url=_env("CHINESE_ASR_BASE_URL", "http://127.0.0.1:8770"),
            chinese_tts_base_url=_env("CHINESE_TTS_BASE_URL", "http://127.0.0.1:8771"),
            chinese_asr_stream_model=_env("CHINESE_ASR_STREAM_MODEL", "paraformer-online"),
            chinese_asr_final_model=_env("CHINESE_ASR_FINAL_MODEL", "Qwen/Qwen3-ASR-0.6B"),
            chinese_asr_final_timeout_ms=_env_int("CHINESE_ASR_FINAL_TIMEOUT_MS", 1500),
            chinese_asr_endpoint_ms=_env_int("CHINESE_ASR_ENDPOINT_MS", 500),
            chinese_tts_model=_env("CHINESE_TTS_MODEL", "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"),
            chinese_tts_speaker=_env("CHINESE_TTS_SPEAKER", "Serena"),
            chinese_tts_device=_env("CHINESE_TTS_DEVICE", "cuda"),
            chinese_tts_dtype=_env("CHINESE_TTS_DTYPE", "bfloat16"),
        )

    def validate(self, require_models: bool = True) -> list[str]:
        errors: list[str] = []
        if not self.llm_profiles:
            errors.append("没有可用的 LLM API 配置")
        if not self.audio_api_base or not self.audio_api_key:
            errors.append("没有可用的语音 API 配置")
        if not 0.25 <= self.tts_speed <= 4.0:
            errors.append("TTS 语速必须在 0.25 到 4.0 之间")
        if not 0.25 <= self.end_silence_seconds <= 2.0:
            errors.append("句尾静音时长必须在 0.25 到 2.0 秒之间")
        if not 1 <= self.kws_threads <= 4:
            errors.append("唤醒模型线程数必须在 1 到 4 之间")
        if self.robot_control_host not in {"127.0.0.1", "::1", "localhost"}:
            errors.append("语音运动控制必须使用回环地址")
        if not 1 <= self.robot_control_port <= 65535:
            errors.append("语音运动控制端口必须在 1..65535")
        if self.chinese_speech_enabled:
            if not 250 <= self.chinese_asr_endpoint_ms <= 2000:
                errors.append("中文 ASR 句尾静音必须在 250..2000ms")
            if not 100 <= self.chinese_asr_final_timeout_ms <= 10000:
                errors.append("中文 ASR 复核超时必须在 100..10000ms")
            if self.chinese_tts_dtype not in {"bfloat16", "float16", "float32"}:
                errors.append("中文 TTS dtype 只支持 bfloat16/float16/float32")
        if require_models:
            required = [
                self.kws_model_dir / "tokens.txt",
                self.kws_model_dir / "keywords_xiaoda.txt",
                self.vad_model,
            ]
            errors.extend(f"缺少模型文件：{path}" for path in required if not path.is_file())
        return errors


def _read_loose_api_config(path: Path) -> dict[str, str]:
    """Read api.txt without trying to parse its intentionally bare key lines."""
    if not path.is_file():
        return {
            "model": "",
            "local_base": "",
            "local_key": "",
            "remote_base": "https://aihubmix.com",
            "remote_key": "",
        }

    lines = path.read_text(encoding="utf-8").splitlines()
    model = ""
    local_base = ""
    remote_base = "https://aihubmix.com"
    keys: list[str] = []

    assignment = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*["\'](.*)["\']\s*$')
    for raw in lines:
        line = raw.strip()
        match = assignment.match(line)
        if match:
            name, value = match.groups()
            if name == "model" and not model:
                model = value
            elif name == "base_url" and not local_base:
                local_base = value
            continue
        candidate = line.strip('"\'')
        if candidate.startswith("sk-") and " " not in candidate:
            keys.append(candidate)
        elif candidate.startswith("https://aihubmix.com"):
            remote_base = candidate

    return {
        "model": model,
        "local_base": local_base,
        "local_key": keys[0] if keys else "",
        "remote_base": remote_base,
        "remote_key": keys[1] if len(keys) > 1 else (keys[0] if not local_base and keys else ""),
    }
