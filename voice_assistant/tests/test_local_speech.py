from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from voice_assistant.chinese_speech import ChineseSpeechError, LanguageRoutedASR
from voice_assistant.local_speech import LocalSpeechServices, UNITS
from voice_assistant.tts import LanguageRoutedSpeechPlayer, StreamingSpeechPlayer


class LocalSpeechTests(unittest.TestCase):
    def manager(self):
        return LocalSpeechServices("http://127.0.0.1:8770", "http://127.0.0.1:8771")

    def test_monitor_never_starts_services(self):
        manager = self.manager()
        with patch.object(manager, "_systemctl", return_value="inactive\ninactive\n") as ctl, \
             patch.object(manager, "_healthy", return_value=False):
            manager._refresh()
        ctl.assert_called_once_with("show", "--property=ActiveState", "--value", *UNITS)
        self.assertEqual(manager.snapshot()["phase"], "off")
        self.assertFalse(manager.ready())

    def test_start_waits_for_both_health_checks_and_stop_clears_readiness(self):
        manager = self.manager()
        with patch.object(manager, "_systemctl", return_value="active\nactive\n") as ctl, \
             patch.object(manager, "_healthy", side_effect=[True, False, True, True]):
            self.assertTrue(manager.set_enabled(True)[0])
            self.assertEqual(manager.snapshot()["phase"], "starting")
            manager._refresh()
            self.assertFalse(manager.ready())
            manager._refresh()
            self.assertTrue(manager.ready())
            self.assertTrue(manager.set_enabled(False)[0])
            self.assertFalse(manager.ready())
            self.assertEqual(manager.snapshot()["phase"], "stopping")
        self.assertIn(unittest.mock.call("--no-block", "start", *UNITS), ctl.call_args_list)
        self.assertIn(unittest.mock.call("--no-block", "stop", *UNITS), ctl.call_args_list)
        self.assertFalse(any("enable" in c.args for c in ctl.call_args_list))

    def test_load_timeout_stops_both_services(self):
        manager = self.manager()
        with patch.object(manager, "_systemctl", return_value="active\nactive\n") as ctl, \
             patch.object(manager, "_healthy", return_value=False):
            manager.set_enabled(True)
            manager._starting_at -= 181
            manager._refresh()
        ctl.assert_called_with("--no-block", "stop", *UNITS)
        self.assertEqual(manager.snapshot()["phase"], "error")
        self.assertFalse(manager.ready())

    def test_command_failure_is_reported(self):
        manager = self.manager()
        with patch.object(manager, "_systemctl", side_effect=RuntimeError("unit missing")):
            self.assertEqual(manager.set_enabled(True), (False, "unit missing"))
        self.assertEqual(manager.snapshot()["error"], "unit missing")

    def test_chinese_asr_uses_cloud_when_off_loading_or_failed(self):
        cloud = Mock()
        cloud.transcribe.return_value = "你好，小达。"
        local = Mock()
        audio = object()
        for ready in (False, True):
            local.transcribe.side_effect = ChineseSpeechError("connection refused")
            router = LanguageRoutedASR(cloud, local, lambda: ready)
            self.assertEqual(router.transcribe(audio, language="zh-CN"), "你好，小达。")
        cloud.transcribe.assert_called_with(audio, 16000, language="zh")
        self.assertEqual(local.transcribe.call_count, 1)
        self.assertEqual(LanguageRoutedASR(cloud, None).transcribe(audio), "你好，小达。")

    def test_cloud_chinese_tts_retains_audio_and_playback_callbacks(self):
        class Cloud:
            def speak(self, phrases, on_audio_ready):
                self.text = "".join(phrases)
                on_audio_ready(b"RIFF-chinese")
                return b"RIFF-chinese"
        cloud, local = Cloud(), Mock()
        audio, started = [], []
        router = LanguageRoutedSpeechPlayer(cloud, local, lambda: False)
        result = router.speak(iter(["你好，", "我是小达。"]), language="zh",
                              on_audio_ready=audio.append,
                              on_audio_started=lambda: started.append(True))
        self.assertEqual(cloud.text, "你好，我是小达。")
        self.assertEqual(result, b"RIFF-chinese")
        self.assertEqual(audio, [result])
        self.assertEqual(started, [True])
        local.speak.assert_not_called()

    def test_cancel_during_cloud_synthesis_discards_audio(self):
        speaker, tts = Mock(), Mock()
        player = StreamingSpeechPlayer(tts, speaker)
        def synthesize(_text):
            player.cancel()
            return b"RIFF-cancelled"
        tts.synthesize.side_effect = synthesize
        callback = Mock()
        self.assertEqual(player.speak(["你好"], callback), b"")
        speaker.cancel.assert_called_once()
        speaker.play.assert_not_called()
        callback.assert_not_called()

    def test_router_cancels_cloud_and_local_playback(self):
        cloud, local = Mock(), Mock()
        LanguageRoutedSpeechPlayer(cloud, local).cancel()
        cloud.cancel.assert_called_once()
        local.cancel.assert_called_once()


if __name__ == "__main__":
    unittest.main()
