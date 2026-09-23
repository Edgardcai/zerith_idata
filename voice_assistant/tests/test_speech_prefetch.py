import threading
import unittest
from unittest.mock import Mock

from voice_assistant.chinese_speech import ChineseSpeechError
from voice_assistant.text_stream import SentenceChunker
from voice_assistant.tts import ChineseSentenceSpeechPlayer
from voice_assistant.llm import ConversationMemory
from voice_assistant.service import VoiceAssistant


class PrefetchTests(unittest.TestCase):
    def test_next_sentence_synthesizes_while_player_is_blocked(self):
        second_started = threading.Event()
        client = Mock()
        def stream(text, *, cancel_event):
            if text == "第二句。":
                second_started.set()
            yield b"\x01\x00" * 1024
        client.stream_speech.side_effect = stream
        player = ChineseSentenceSpeechPlayer(client, "unused")
        speaker = Mock()
        speaker.write.side_effect = lambda _pcm: self.assertTrue(second_started.wait(1), "playback blocked next synthesis")
        player._player = speaker
        ready, started = [], []
        result = player.speak(iter(["第一句。", "第二句。"]), ready.append, lambda: started.append(True))
        self.assertEqual(len(result), 44 + 4096)
        self.assertEqual(ready, [result])
        self.assertEqual(started, [True])
        speaker.open.assert_called_once()
        speaker.finish.assert_called_once()

    def test_producer_failure_propagates_without_hanging(self):
        def phrases():
            raise ValueError("LLM failed")
            yield
        player = ChineseSentenceSpeechPlayer(Mock(), "unused")
        player._player = Mock()
        with self.assertRaisesRegex(ValueError, "LLM failed"):
            player.speak(phrases())

    def test_synthesis_failure_propagates_without_hanging(self):
        client = Mock()
        client.stream_speech.side_effect = ChineseSpeechError("TTS failed")
        player = ChineseSentenceSpeechPlayer(client, "unused")
        player._player = Mock()
        with self.assertRaisesRegex(ChineseSpeechError, "TTS failed"):
            player.speak(["测试"])

    def test_cancel_while_waiting_for_text_returns_without_late_playback(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def phrases():
            entered.set()
            release.wait(2)
            yield "已经取消的文本"
        client = Mock()
        player = ChineseSentenceSpeechPlayer(client, "unused")
        player._player = Mock()
        result = []
        def run():
            try:
                result.append(player.speak(phrases()))
            finally:
                finished.set()
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            player.cancel()
            self.assertTrue(finished.wait(0.8))
            self.assertEqual(result, [b""])
            client.stream_speech.assert_not_called()
            player._player.write.assert_not_called()
        finally:
            release.set()
            thread.join(2)

    def test_first_clause_can_start_early_and_long_sentences_stay_bounded(self):
        chunker = SentenceChunker(18, 64, first_minimum=12, first_maximum=40)
        prefix = "我可以帮你介绍一下这个功能，"
        self.assertEqual(chunker.push(prefix), [prefix])
        tail = "一" * 150 + "。"
        pieces = chunker.push(tail) + [chunker.flush()]
        self.assertEqual("".join(pieces), tail)
        self.assertTrue(all(len(piece) <= 64 for piece in pieces))

    def test_cancelled_old_reply_cannot_publish_into_new_session(self):
        assistant = VoiceAssistant.__new__(VoiceAssistant)
        old_cancel = threading.Event()
        assistant._reply_cancel_event = old_cancel
        assistant.llm = Mock()
        assistant.llm.stream_reply.return_value = iter(["这是一条用于测试取消操作的语音回复。", "后续内容。"])
        assistant.observer = Mock()
        assistant.config = Mock(robot_name="小达")
        phrases = assistant._reply_phrases(ConversationMemory(), language="zh", stream_sentences=True)
        self.assertTrue(next(phrases))
        old_cancel.set()
        assistant._reply_cancel_event = threading.Event()
        phrases.close()
        assistant.observer.message.assert_not_called()
