from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from voice_assistant.config import VoiceConfig, _read_loose_api_config


class ConfigTests(unittest.TestCase):
    def test_reads_loose_api_file_without_exposing_keys_in_repr(self) -> None:
        content = '''model = "gpt-5.5"
[model_providers.OpenAI]
base_url = "http://192.168.1.244:8000"
sk-local-secret
https://aihubmix.com/
sk-remote-secret
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api.txt"
            path.write_text(content, encoding="utf-8")
            parsed = _read_loose_api_config(path)
            self.assertEqual(parsed["model"], "gpt-5.5")
            self.assertEqual(parsed["local_key"], "sk-local-secret")
            self.assertEqual(parsed["remote_key"], "sk-remote-secret")
            with patch.dict(os.environ, {}, clear=True):
                config = VoiceConfig.load(path)
            rendered = repr(config)
            self.assertNotIn("sk-local-secret", rendered)
            self.assertNotIn("sk-remote-secret", rendered)
            self.assertEqual(config.llm_profiles[0].responses_url, "https://aihubmix.com/v1/responses")
            self.assertEqual(config.llm_profiles[1].responses_url, "http://192.168.1.244:8000/responses")
            self.assertEqual(config.tts_speed, 1.35)
            self.assertEqual(config.end_silence_seconds, 0.42)
            self.assertEqual(config.kws_threads, 1)

    def test_combined_provider_and_json_auth_uses_only_provider_key(self) -> None:
        content = '''model = "gpt-6-astra"
base_url = "http://192.168.1.244:8000"
{"OPENAI_API_KEY": "new-provider-secret"}
'''
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / "api.txt"
            path.write_text(content)
            config = VoiceConfig.load(path)
        self.assertEqual(config.reasoning_effort, "low")
        self.assertEqual(len(config.llm_profiles), 1)
        self.assertEqual(config.llm_profiles[0].api_key, "new-provider-secret")
        self.assertEqual(config.llm_profiles[0].responses_url, "http://192.168.1.244:8000/responses")
        self.assertEqual(config.audio_api_key, "")
        self.assertNotIn("new-provider-secret", repr(config))
        self.assertEqual(config.validate(require_models=False), [])

    def test_cloud_audio_required_when_local_speech_disabled(self) -> None:
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / "api.txt"
            path.write_text('base_url = "http://localhost:8000"\nOPENAI_API_KEY = "provider-secret"')
            config = VoiceConfig.load(path)
        self.assertEqual(config.llm_profiles[0].api_key, "provider-secret")
        self.assertIn("没有可用的语音 API 配置", replace(config, chinese_speech_enabled=False).validate(False))

    def test_name_defaults_to_xiaoda(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            config = VoiceConfig.load(Path(directory) / "missing.txt")
        self.assertEqual(config.robot_name, "小达")

    def test_tts_speed_can_be_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"XIAODA_TTS_SPEED": "1.35"},
            clear=True,
        ):
            config = VoiceConfig.load(Path(directory) / "missing.txt")
        self.assertEqual(config.tts_speed, 1.35)

    def test_chinese_speech_environment_is_independent_from_english(self) -> None:
        environment = {
            "CHINESE_SPEECH_ENABLED": "true",
            "CHINESE_ASR_FINAL_TIMEOUT_MS": "1400",
            "CHINESE_ASR_ENDPOINT_MS": "500",
            "CHINESE_TTS_SPEAKER": "Vivian",
        }
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, environment, clear=True):
            config = VoiceConfig.load(Path(directory) / "missing.txt")
        self.assertTrue(config.chinese_speech_enabled)
        self.assertEqual(config.chinese_asr_stream_model, "paraformer-online")
        self.assertEqual(config.chinese_asr_final_timeout_ms, 1400)
        self.assertEqual(config.chinese_tts_speaker, "Vivian")
        self.assertEqual(config.tts_model, "gpt-4o-mini-tts")
        self.assertEqual(config.tts_voice, "coral")


if __name__ == "__main__":
    unittest.main()
