from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Iterator
from enum import Enum
from typing import Protocol

import httpx

from .asr import OpenAICompatibleASR
from .audio import AlsaSpeaker
from .chinese_speech import (
    ChineseSpeechClient,
    LanguageRoutedASR,
    is_chinese_language,
    normalize_session_language,
)
from .config import VoiceConfig
from .llm import ConversationMemory, LlmError, ResponsesConversation
from .motion_intent import looks_motion_related, parse_fast_motion
from .robot_control import DisabledRobotControl, RobotControlPort
from .robot_control import RobotCommandIntent, RobotControlError
from .text_stream import SentenceChunker
from .tts import (
    ChineseSentenceSpeechPlayer,
    LanguageRoutedSpeechPlayer,
    OpenAICompatibleTTS,
    StreamingSpeechPlayer,
)
from .vad import SileroUtteranceRecorder
from .wakeword import SherpaWakeWord


LOG = logging.getLogger(__name__)


class VoiceState(str, Enum):
    STARTING = "starting"
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    ACTING = "acting"
    SYNTHESIZING = "synthesizing"
    SPEAKING = "speaking"
    ERROR = "error"
    STOPPED = "stopped"


class StatusSink(Protocol):
    def update(self, state: VoiceState, detail: str = "") -> None: ...


class VoiceObserver(Protocol):
    def session_started(self, source: str, language: str = "zh") -> None: ...

    def message(self, role: str, text: str) -> None: ...

    def audio(self, encoded_audio: bytes) -> None: ...


class LogStatusSink:
    def update(self, state: VoiceState, detail: str = "") -> None:
        LOG.info("状态=%s%s", state.value, f"，{detail}" if detail else "")


class VoiceAssistant:
    def __init__(
        self,
        config: VoiceConfig,
        status: StatusSink | None = None,
        robot_control: RobotControlPort | None = None,
        observer: VoiceObserver | None = None,
    ) -> None:
        errors = config.validate(require_models=True)
        if errors:
            raise RuntimeError("；".join(errors))
        self.config = config
        self.status = status or LogStatusSink()
        self.robot_control = robot_control or DisabledRobotControl()
        self.observer = observer
        self.stop_event = threading.Event()
        self._manual_wake_event = threading.Event()
        self._finish_input_event = threading.Event()
        self._cancel_input_event = threading.Event()
        self._reply_cancel_event = threading.Event()
        self._state_lock = threading.Lock()
        self._state = VoiceState.STARTING
        self._requested_language = "zh"
        self._pending_text: tuple[str, str] | None = None

        self.speaker = AlsaSpeaker(config.speaker_device)
        self.wakeword = SherpaWakeWord(
            config.kws_model_dir,
            config.microphone_device,
            config.sample_rate,
            config.kws_threshold,
            config.kws_score,
            config.kws_threads,
        )
        self.recorder = SileroUtteranceRecorder(
            config.vad_model,
            config.microphone_device,
            config.sample_rate,
            max_utterance_seconds=config.max_utterance_seconds,
            min_silence_seconds=config.end_silence_seconds,
        )
        self._audio_http = httpx.Client(
            timeout=httpx.Timeout(150.0, connect=8.0, write=30.0, pool=10.0),
            trust_env=True,
            follow_redirects=True,
        )
        english_asr = OpenAICompatibleASR(
            config.audio_api_base,
            config.audio_api_key,
            config.asr_model,
            client=self._audio_http,
        )
        self._chinese_speech = (
            ChineseSpeechClient(
                config.chinese_asr_base_url,
                config.chinese_tts_base_url,
                speaker=config.chinese_tts_speaker,
                final_timeout_ms=config.chinese_asr_final_timeout_ms,
            )
            if config.chinese_speech_enabled
            else None
        )
        self.asr = LanguageRoutedASR(english_asr, self._chinese_speech)
        self.llm = ResponsesConversation(
            config.llm_profiles,
            config.llm_model,
            config.robot_name,
            config.reasoning_effort,
            shared_client=self._audio_http,
            shared_base_url=config.audio_api_base,
            shared_api_key=config.audio_api_key,
        )
        tts = OpenAICompatibleTTS(
            config.audio_api_base,
            config.audio_api_key,
            config.tts_model,
            config.tts_fallback_model,
            config.tts_voice,
            config.tts_speed,
            client=self._audio_http,
        )
        english_speech = StreamingSpeechPlayer(tts, self.speaker)
        chinese_speech = (
            ChineseSentenceSpeechPlayer(self._chinese_speech, config.speaker_device)
            if self._chinese_speech is not None
            else None
        )
        self.speech = LanguageRoutedSpeechPlayer(english_speech, chinese_speech)

    def stop(self) -> None:
        self._reply_cancel_event.set()
        self.speech.cancel()
        self.stop_event.set()
        self._finish_input_event.set()
        self._manual_wake_event.set()

    def close(self) -> None:
        if self._chinese_speech is not None:
            self._chinese_speech.close()
        self._audio_http.close()

    def request_session(self, language: str = "zh") -> tuple[bool, str]:
        """Request a normal microphone session from the loopback web API."""
        try:
            language = normalize_session_language(language)
        except ValueError as exc:
            return False, str(exc)
        with self._state_lock:
            state = self._state
            if state is not VoiceState.IDLE:
                return False, f"小达当前状态为 {state.value}"
            self._state = VoiceState.STARTING
            self._requested_language = language
            self._finish_input_event.clear()
            self._cancel_input_event.clear()
            self.status.update(VoiceState.STARTING, f"正在启动{_language_label(language)}语音输入")
            self._manual_wake_event.set()
        return True, f"已启动{_language_label(language)}单轮对话"

    def finish_input(self) -> tuple[bool, str]:
        """Ask the active recorder to flush immediately and start recognition."""
        with self._state_lock:
            if self._state is not VoiceState.LISTENING:
                return False, "当前不在录音"
            self._finish_input_event.set()
        return True, "已结束输入，正在识别"

    def request_text(self, text: str, language: str = "zh") -> tuple[bool, str]:
        """Queue one keyboard message for the same reply and motion pipeline as ASR."""
        text = str(text).strip()
        if not text:
            return False, "文字内容不能为空"
        if len(text) > 1000:
            return False, "文字内容不能超过 1000 个字符"
        try:
            language = normalize_session_language(language)
        except ValueError as exc:
            return False, str(exc)
        with self._state_lock:
            state = self._state
            if state is not VoiceState.IDLE:
                return False, f"小达当前状态为 {state.value}"
            self._state = VoiceState.STARTING
            self._pending_text = (text, language)
            self.status.update(VoiceState.STARTING, "已收到键盘输入，正在处理")
            self._manual_wake_event.set()
        return True, "文字已发送，正在处理"

    def cancel_current(self) -> tuple[bool, str]:
        """Stop synthesis/playback and ask an active recorder to finish promptly."""
        self._reply_cancel_event.set()
        self._cancel_input_event.set()
        self.speech.cancel()
        self._finish_input_event.set()
        with self._state_lock:
            if self._state in {VoiceState.IDLE, VoiceState.STOPPED}:
                return True, "当前没有需要停止的语音"
        return True, "已请求停止当前语音"

    @property
    def current_state(self) -> VoiceState:
        with self._state_lock:
            return self._state

    def begin_session(self, source: str, language: str = "zh") -> None:
        if self.observer is not None:
            self.observer.session_started(source, language)

    def run_forever(self) -> None:
        self._update_status(VoiceState.IDLE, f'请说“{self.config.robot_name}”唤醒')
        while not self.stop_event.is_set():
            try:
                detected = self.wakeword.wait(self.stop_event, self._manual_wake_event)
                if not detected or self.stop_event.is_set():
                    break
                self._manual_wake_event.clear()
                text_request = self._take_pending_text()
                if text_request is not None:
                    text, language = text_request
                    self.begin_session("text", language)
                    self.run_text_session(text, language)
                    continue
                source = "web" if detected == "__manual__" else "wakeword"
                language = self._consume_requested_language() if source == "web" else "zh"
                self.begin_session(source, language)
                self.speaker.chime()
                self.run_session(
                    max_turns=1 if source == "web" else None,
                    language=language,
                )
            except Exception as exc:
                LOG.exception("语音循环异常")
                self._update_status(VoiceState.ERROR, str(exc))
                if self.stop_event.wait(1.0):
                    break
                self._update_status(VoiceState.IDLE, f'请说“{self.config.robot_name}”唤醒')
        self._update_status(VoiceState.STOPPED)

    def run_web_only_forever(self) -> None:
        """Keep the bridge online without opening the microphone for wake words."""
        idle_detail = "网页按钮待命（语音唤醒已关闭）"
        self._update_status(VoiceState.IDLE, idle_detail)
        while not self.stop_event.is_set():
            if not self._manual_wake_event.wait(0.2):
                continue
            if self.stop_event.is_set():
                break
            self._manual_wake_event.clear()
            text_request = self._take_pending_text()
            if text_request is not None:
                text, language = text_request
                try:
                    self.begin_session("text", language)
                    self.run_text_session(text, language)
                except Exception as exc:
                    LOG.exception("网页文字对话异常")
                    self._update_status(VoiceState.ERROR, str(exc))
                    if self.stop_event.wait(1.0):
                        break
                    self._update_status(VoiceState.IDLE, idle_detail)
                continue
            language = self._consume_requested_language()
            try:
                self.begin_session("web", language)
                self.speaker.chime()
                self.run_session(max_turns=1, language=language)
            except Exception as exc:
                LOG.exception("网页语音对话异常")
                self._update_status(VoiceState.ERROR, str(exc))
                if self.stop_event.wait(1.0):
                    break
                self._update_status(VoiceState.IDLE, idle_detail)
        self._update_status(VoiceState.STOPPED)

    def run_session(
        self,
        max_turns: int | None = None,
        language: str = "zh",
    ) -> None:
        memory = ConversationMemory()
        limit = max_turns or self.config.session_turn_limit
        empty_results = 0
        for turn in range(limit):
            if self.stop_event.is_set():
                return
            timeout = self.config.first_turn_timeout if turn == 0 else self.config.followup_timeout
            self._finish_input_event.clear()
            self._cancel_input_event.clear()
            self._update_status(
                VoiceState.LISTENING,
                f"请说{_language_label(language)}；说完可点“结束输入”",
            )
            samples = self.recorder.record(
                timeout,
                self.stop_event,
                self._finish_input_event,
            )
            if self._cancel_input_event.is_set():
                self._update_status(VoiceState.IDLE, "已取消本次录音")
                return
            if samples is None:
                detail = "没有检测到语音，请重试" if self._finish_input_event.is_set() else "对话超时"
                self._update_status(VoiceState.IDLE, detail)
                return

            self._update_status(VoiceState.TRANSCRIBING, "录音已结束，正在识别")
            asr_started = time.monotonic()
            text = self.asr.transcribe(
                samples,
                self.config.sample_rate,
                language=language,
            ).strip()
            LOG.info("ASR 完成：%.2f 秒", time.monotonic() - asr_started)
            if not text:
                empty_results += 1
                if limit == 1 or empty_results >= 2:
                    self._update_status(VoiceState.IDLE, "没有听清，请重试")
                    return
                continue
            empty_results = 0
            LOG.info("用户：%s", text)
            if self._respond_to_text(text, language, memory):
                return

        detail = "本轮完成，可再次点击按钮" if limit == 1 else "已达到单次对话轮数上限"
        self._update_status(VoiceState.IDLE, detail)

    def run_text_session(self, text: str, language: str = "zh") -> None:
        """Process a keyboard message without opening the robot microphone."""
        memory = ConversationMemory(max_turns=1)
        LOG.info("键盘用户：%s", text)
        finished = self._respond_to_text(text, language, memory)
        if not finished:
            self._update_status(VoiceState.IDLE, "文字输入处理完成")

    def _respond_to_text(
        self,
        text: str,
        language: str,
        memory: ConversationMemory,
    ) -> bool:
        self._reply_cancel_event.clear()
        if self.observer is not None:
            self.observer.message("user", text)

        if _is_goodbye(text):
            farewell = "好的，需要我时再叫我。"
            self._update_status(VoiceState.SPEAKING, "结束对话")
            if self.observer is not None:
                self.observer.message("assistant", farewell)
            self._speak(iter([farewell]), language)
            self._update_status(VoiceState.IDLE, "本轮对话已结束")
            return True

        motion_reply = self._try_motion(text, language)
        if motion_reply is not None:
            if self.observer is not None:
                self.observer.message("assistant", motion_reply)
            self._speak([motion_reply], language)
            return False

        memory.add("user", text)
        self._update_status(VoiceState.THINKING)
        generated = self._reply_phrases(
            memory,
            language=language,
            stream_sentences=is_chinese_language(language),
        )
        # The English branch intentionally retains the old full-answer/one-request flow.
        phrases = generated if is_chinese_language(language) else list(generated)
        self._speak(phrases, language)
        return False

    def answer_text(self, text: str, speak: bool = True) -> str:
        memory = ConversationMemory(max_turns=1)
        memory.add("user", text)
        language = _text_language(text)
        phrases = self._reply_phrases(
            memory,
            language=language,
            stream_sentences=is_chinese_language(language),
        )
        if speak:
            self.speech.speak(phrases, language=language)
        else:
            list(phrases)
        return memory.messages[-1].content

    def _reply_phrases(
        self,
        memory: ConversationMemory,
        language: str = "auto",
        stream_sentences: bool = False,
    ) -> Iterator[str]:
        complete: list[str] = []
        chunker = SentenceChunker(min_characters=15, max_characters=80)
        try:
            for delta in self.llm.stream_reply(memory.messages, language=language):
                if self._reply_cancel_event.is_set():
                    break
                complete.append(delta)
                if stream_sentences:
                    yield from chunker.push(delta)
        finally:
            answer = "".join(complete).strip()
            if answer and not self._reply_cancel_event.is_set():
                memory.add("assistant", answer)
                LOG.info("%s：%s", self.config.robot_name, answer)
                if self.observer is not None:
                    self.observer.message("assistant", answer)
        if self._reply_cancel_event.is_set():
            return
        if stream_sentences:
            remainder = chunker.flush()
            if remainder:
                yield remainder
        elif complete:
            answer = "".join(complete).strip()
            if answer:
                yield answer

    def _speak(self, phrases: Iterator[str] | list[str], language: str) -> None:
        detail = (
            "首个完整句子已生成，正在本地合成"
            if is_chinese_language(language)
            else "回复已生成，正在合成语音"
        )
        self._update_status(VoiceState.SYNTHESIZING, detail)

        def audio_ready(audio: bytes) -> None:
            if self.observer is not None and audio:
                self.observer.audio(audio)

        def audio_started() -> None:
            self._update_status(VoiceState.SPEAKING, "正在播放")

        def english_audio_ready(audio: bytes) -> None:
            audio_ready(audio)
            audio_started()

        self.speech.speak(
            phrases,
            language=language,
            on_audio_ready=audio_ready if is_chinese_language(language) else english_audio_ready,
            on_audio_started=audio_started,
        )

    def _try_motion(self, text: str, language: str) -> str | None:
        fast = parse_fast_motion(text)
        if fast is None and not looks_motion_related(text):
            return None

        if not self.robot_control.available:
            return (
                "Conversation motion control is off. Take over and initialize the robot, then enable it on the Voice page."
                if language == "en"
                else "语音 / 文字运动控制尚未开启。请先在网页接管并初始化机器人，再开启该开关。"
            )

        command = fast.command if fast is not None else None
        if command is None:
            self._update_status(VoiceState.THINKING, "正在安全解析运动意图")
            try:
                command = self.llm.resolve_motion_intent(text, language=language)
            except LlmError as exc:
                LOG.warning("运动意图解析失败：%s", exc)
                return (
                    "I couldn't safely interpret that motion command. Please use a direct command such as move forward, turn left, wave, shake hands, or stop."
                    if language == "en"
                    else "我无法安全解析这条运动指令。请直接说“前进”、“左转”、“挥手”、“握手”或“停止”。"
                )
            if command is None:
                return (
                    "That request cannot be mapped safely to one supported action. Please give one direct command."
                    if language == "en"
                    else "这句话无法安全映射为一个受支持的动作，请只说一条明确指令。"
                )

        self._update_status(VoiceState.ACTING, f"正在下发动作：{command}")
        try:
            result = self.robot_control.execute(RobotCommandIntent(command, {}))
        except RobotControlError as exc:
            LOG.warning("语音动作被拒绝：%s", exc)
            return (
                f"The motion was not executed: {exc}"
                if language == "en"
                else f"动作未执行：{exc}"
            )
        return _motion_acknowledgement(command, language, result)

    def _consume_requested_language(self) -> str:
        with self._state_lock:
            return self._requested_language

    def _take_pending_text(self) -> tuple[str, str] | None:
        with self._state_lock:
            pending, self._pending_text = self._pending_text, None
            return pending

    def _update_status(self, state: VoiceState, detail: str = "") -> None:
        with self._state_lock:
            self._state = state
        self.status.update(state, detail)


def _is_goodbye(text: str) -> bool:
    normalized = re.sub(r"[\s，。！？,.!?]", "", text)
    phrases = ("再见", "退出对话", "结束对话", "不用了", "休息吧", "先这样")
    return any(phrase in normalized for phrase in phrases)


def _language_label(language: str) -> str:
    return "英文" if language == "en" else "中文"


def _text_language(text: str) -> str:
    """Route mixed text by its dominant script; selected session language still wins elsewhere."""
    chinese = len(re.findall(r"[\u3400-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    return "zh" if chinese and chinese >= max(1, latin // 3) else "en"


def _motion_acknowledgement(
    command: str,
    language: str,
    _result: dict[str, object],
) -> str:
    if language == "en":
        return {
            "stop": "Stopping.",
            "forward": "Moving forward.",
            "backward": "Moving backward.",
            "turn_left": "Turning left.",
            "turn_right": "Turning right.",
            "turn_around": "Turning around.",
            "wave": "Waving.",
            "handshake": "Offering a handshake.",
        }[command]
    return {
        "stop": "正在停止。",
        "forward": "正在前进。",
        "backward": "正在后退。",
        "turn_left": "正在左转。",
        "turn_right": "正在右转。",
        "turn_around": "正在转身。",
        "wave": "正在挥手。",
        "handshake": "正在握手。",
    }[command]
