from __future__ import annotations

import http.client
import json
import threading
import unittest

from voice_assistant.asr import _looks_like_hallucination
from voice_assistant.audio import make_chime
from voice_assistant.llm import ConversationMemory
from voice_assistant.motion_intent import looks_motion_related, parse_fast_motion
from voice_assistant.robot_control import DisabledRobotControl, RobotCommandIntent, RobotControlError
from voice_assistant.service import (
    VoiceAssistant,
    VoiceState,
    _is_goodbye,
    _motion_acknowledgement,
)
from voice_assistant.chinese_speech import LanguageRoutedASR, is_chinese_language
from voice_assistant.text_stream import SentenceChunker
from voice_assistant.tts import LanguageRoutedSpeechPlayer, StreamingSpeechPlayer
from voice_assistant.web_api import VoiceWebBridge


class SentenceChunkerTests(unittest.TestCase):
    def test_yields_complete_sentences_and_flushes_tail(self) -> None:
        chunker = SentenceChunker(min_characters=4, max_characters=20)
        output = []
        output.extend(chunker.push("你好，我是小达。今"))
        output.extend(chunker.push("天想聊什么"))
        output.append(chunker.flush())
        self.assertEqual(output, ["你好，我是小达。", "今天想聊什么"])

    def test_long_text_is_split(self) -> None:
        chunker = SentenceChunker(min_characters=4, max_characters=8)
        output = chunker.push("一二三四五六七八九十")
        self.assertEqual(output, ["一二三四五六七八"])
        self.assertEqual(chunker.flush(), "九十")


class MemoryTests(unittest.TestCase):
    def test_memory_keeps_recent_messages(self) -> None:
        memory = ConversationMemory(max_turns=1)
        memory.add("user", "一")
        memory.add("assistant", "二")
        memory.add("user", "三")
        self.assertEqual([item.content for item in memory.messages], ["二", "三"])


class MotionIntentTests(unittest.TestCase):
    def test_clear_commands_use_deterministic_fast_path(self) -> None:
        cases = {
            "小达，请向前移动一下": "forward",
            "帮我向前移动一点点": "forward",
            "向左转": "turn_left",
            "转身": "turn_around",
            "转个身": "turn_around",
            "挥挥手": "wave",
            "握个手": "handshake",
            "please move forward": "forward",
            "shake my hand": "handshake",
            "stop moving": "stop",
            "马上停下": "stop",
            "stop now": "stop",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                parsed = parse_fast_motion(text)
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed.command, expected)

    def test_motion_acknowledgements_are_short(self) -> None:
        expected = {
            "forward": "正在前进。",
            "backward": "正在后退。",
            "turn_left": "正在左转。",
            "turn_right": "正在右转。",
            "turn_around": "正在转身。",
            "wave": "正在挥手。",
            "handshake": "正在握手。",
        }
        for command, reply in expected.items():
            with self.subTest(command=command):
                self.assertEqual(_motion_acknowledgement(command, "zh", {}), reply)

    def test_negated_motion_becomes_stop_not_positive_motion(self) -> None:
        self.assertEqual(parse_fast_motion("不要前进").command, "stop")
        self.assertEqual(parse_fast_motion("don't turn left").command, "stop")

    def test_ambiguous_motion_is_deferred_to_resolver(self) -> None:
        self.assertIsNone(parse_fast_motion("到门口那边去"))
        self.assertTrue(looks_motion_related("走到门口那边去"))
        self.assertFalse(looks_motion_related("今天天气怎么样"))

    def test_fast_path_executes_without_calling_motion_llm(self) -> None:
        class Robot:
            available = True

            def __init__(self):
                self.commands = []

            def execute(self, intent):
                self.commands.append(intent.name)
                return {"accepted": True}

        class Llm:
            def resolve_motion_intent(self, *_args, **_kwargs):
                raise AssertionError("明确指令不应该调用大模型")

        assistant = VoiceAssistant.__new__(VoiceAssistant)
        assistant.robot_control = Robot()
        assistant.llm = Llm()
        assistant._state_lock = threading.Lock()
        assistant._state = None
        assistant.status = type("Status", (), {"update": lambda *_args: None})()

        reply = assistant._try_motion("向前移动", "zh")
        self.assertEqual(assistant.robot_control.commands, ["forward"])
        self.assertEqual(reply, "正在前进。")

    def test_ambiguous_motion_uses_constrained_llm_resolver(self) -> None:
        class Robot:
            available = True

            def __init__(self):
                self.commands = []

            def execute(self, intent):
                self.commands.append(intent.name)
                return {"accepted": True}

        class Llm:
            calls = []

            def resolve_motion_intent(self, text, language):
                self.calls.append((text, language))
                return "turn_right"

        assistant = VoiceAssistant.__new__(VoiceAssistant)
        assistant.robot_control = Robot()
        assistant.llm = Llm()
        assistant._state_lock = threading.Lock()
        assistant._state = None
        assistant.status = type("Status", (), {"update": lambda *_args: None})()

        assistant._try_motion("朝右手边转过去", "zh")
        self.assertEqual(assistant.llm.calls, [("朝右手边转过去", "zh")])
        self.assertEqual(assistant.robot_control.commands, ["turn_right"])


class SpeechPlayerTests(unittest.TestCase):
    def test_complete_reply_uses_one_tts_request_and_one_playback(self) -> None:
        class FakeTTS:
            calls = []

            def synthesize(self, text):
                self.calls.append(text)
                return b"RIFF-audio"

        class FakeSpeaker:
            calls = []

            def play(self, audio):
                self.calls.append(audio)

        tts = FakeTTS()
        speaker = FakeSpeaker()
        player = StreamingSpeechPlayer(tts, speaker)
        ready = []
        audio = player.speak(iter(["第一句。", "第二句。"]), ready.append)

        self.assertEqual(tts.calls, ["第一句。第二句。"])
        self.assertEqual(ready, [b"RIFF-audio"])
        self.assertEqual(speaker.calls, [b"RIFF-audio"])
        self.assertEqual(audio, b"RIFF-audio")

    def test_language_router_keeps_english_asr_object_and_call_shape(self) -> None:
        class EnglishASR:
            calls = []

            def transcribe(self, samples, sample_rate, language):
                self.calls.append((samples, sample_rate, language))
                return "english"

        class ChineseASR:
            calls = []

            def transcribe(self, samples, sample_rate):
                self.calls.append((samples, sample_rate))
                return "中文"

        samples = object()
        english, chinese = EnglishASR(), ChineseASR()
        router = LanguageRoutedASR(english, chinese)
        self.assertEqual(router.transcribe(samples, 16000, "en-US"), "english")
        self.assertEqual(english.calls, [(samples, 16000, "en")])
        self.assertEqual(router.transcribe(samples, 16000, "zh-Hans"), "中文")
        self.assertEqual(chinese.calls, [(samples, 16000)])
        self.assertTrue(is_chinese_language("zh-CN"))

    def test_language_router_keeps_english_tts_player_behavior(self) -> None:
        class EnglishPlayer:
            calls = []

            def speak(self, phrases, on_audio_ready=None):
                text = "".join(phrases)
                self.calls.append((text, on_audio_ready))
                return b"english-audio"

        class ChinesePlayer:
            def speak(self, *_args, **_kwargs):
                raise AssertionError("English must not enter the Chinese player")

            def cancel(self):
                return None

        english = EnglishPlayer()
        router = LanguageRoutedSpeechPlayer(english, ChinesePlayer())
        callback = lambda _audio: None
        result = router.speak(
            iter(["same ", "English"]),
            language="en-US",
            on_audio_ready=callback,
            on_audio_started=lambda: None,
        )
        self.assertEqual(result, b"english-audio")
        self.assertEqual(english.calls, [("same English", callback)])


class VoiceWebBridgeTests(unittest.TestCase):
    def test_loopback_api_starts_session_and_serves_status_and_audio(self) -> None:
        class QuietStatus:
            def update(self, _state, _detail=""):
                return None

        class Controller:
            def request_session(self, language="zh"):
                self.language = language
                return True, "已请求开始对话"

            def finish_input(self):
                return True, "已结束输入"

            def request_text(self, text, language="zh"):
                self.text_request = (text, language)
                return True, "文字已发送"

            def cancel_current(self):
                self.cancelled = True
                return True, "已停止"

        bridge = VoiceWebBridge(QuietStatus(), port=0)
        controller = Controller()
        bridge.attach(controller)
        bridge.start()
        try:
            bridge.session_started("web", "en")
            bridge.message("assistant", "你好")
            bridge.audio(b"RIFF-test")
            connection = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=2)
            connection.request("GET", "/v1/status")
            response = connection.getresponse()
            status = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(status["messages"][-1]["audio_id"], 1)
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=2)
            connection.request(
                "POST",
                "/v1/start",
                body=b'{"language":"en"}',
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            result = json.loads(response.read())
            self.assertEqual(response.status, 202)
            self.assertTrue(result["accepted"])
            self.assertEqual(controller.language, "en")
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=2)
            connection.request("POST", "/v1/finish-input", body=b"{}")
            response = connection.getresponse()
            result = json.loads(response.read())
            self.assertEqual(response.status, 202)
            self.assertTrue(result["accepted"])
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=2)
            connection.request(
                "POST",
                "/v1/text",
                body=json.dumps({"text": "向前移动", "language": "zh"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            result = json.loads(response.read())
            self.assertEqual(response.status, 202)
            self.assertTrue(result["accepted"])
            self.assertEqual(controller.text_request, ("向前移动", "zh"))
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=2)
            connection.request("POST", "/v1/cancel", body=b"{}")
            response = connection.getresponse()
            self.assertEqual(response.status, 202)
            self.assertTrue(json.loads(response.read())["accepted"])
            self.assertTrue(controller.cancelled)
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=2)
            connection.request("GET", "/v1/audio/1.wav")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"RIFF-test")
            connection.close()
        finally:
            bridge.close()


class KeyboardInputTests(unittest.TestCase):
    def test_text_request_is_queued_only_while_idle(self) -> None:
        updates = []
        assistant = VoiceAssistant.__new__(VoiceAssistant)
        assistant._state_lock = threading.Lock()
        assistant._state = VoiceState.IDLE
        assistant._pending_text = None
        assistant._manual_wake_event = threading.Event()
        assistant.status = type(
            "Status", (), {"update": lambda _self, state, detail="": updates.append((state, detail))}
        )()

        accepted, _message = assistant.request_text("  向前移动  ", "zh")
        self.assertTrue(accepted)
        self.assertTrue(assistant._manual_wake_event.is_set())
        self.assertEqual(assistant._take_pending_text(), ("向前移动", "zh"))
        rejected, _message = assistant.request_text("再来一条", "zh")
        self.assertFalse(rejected)
        self.assertTrue(updates)

    def test_keyboard_motion_uses_the_existing_safe_motion_pipeline(self) -> None:
        class Robot:
            available = True

            def __init__(self):
                self.commands = []

            def execute(self, intent):
                self.commands.append(intent.name)
                return {"accepted": True}

        class Observer:
            def __init__(self):
                self.messages = []

            def message(self, role, text):
                self.messages.append((role, text))

        class Speech:
            def __init__(self):
                self.phrases = []

            def speak(self, phrases, **_kwargs):
                self.phrases.extend(phrases)
                return b""

        class Llm:
            def resolve_motion_intent(self, *_args, **_kwargs):
                raise AssertionError("明确指令不应调用动作解析模型")

        assistant = VoiceAssistant.__new__(VoiceAssistant)
        assistant._state_lock = threading.Lock()
        assistant._state = VoiceState.STARTING
        assistant.status = type("Status", (), {"update": lambda *_args: None})()
        assistant.robot_control = Robot()
        assistant.llm = Llm()
        assistant.observer = Observer()
        assistant.speech = Speech()
        assistant._reply_cancel_event = threading.Event()

        assistant.run_text_session("向前移动", "zh")

        self.assertEqual(assistant.robot_control.commands, ["forward"])
        self.assertEqual(assistant.observer.messages[0], ("user", "向前移动"))
        self.assertEqual(assistant.observer.messages[1][1], "正在前进。")
        self.assertEqual(assistant.current_state, VoiceState.IDLE)


class SafetyTests(unittest.TestCase):
    def test_robot_control_is_disabled(self) -> None:
        control = DisabledRobotControl()
        self.assertFalse(control.available)
        self.assertEqual(control.tool_definitions(), [])
        with self.assertRaises(RobotControlError):
            control.execute(RobotCommandIntent("move", {"meters": 1}))

    def test_goodbye_detection(self) -> None:
        self.assertTrue(_is_goodbye("好的，先这样吧。"))
        self.assertFalse(_is_goodbye("再介绍一下这个功能"))

    def test_chime_is_wav(self) -> None:
        self.assertEqual(make_chime()[:4], b"RIFF")

    def test_common_asr_hallucination_is_rejected(self) -> None:
        self.assertTrue(
            _looks_like_hallucination(
                "请不吝点赞 订阅 转发 打赏支持明镜与点点栏目",
                {},
                3.0,
            )
        )
        self.assertFalse(_looks_like_hallucination("小达，请介绍一下自己", {}, 2.0))


if __name__ == "__main__":
    unittest.main()
