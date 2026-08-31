from __future__ import annotations

import argparse
import logging
import shutil
import signal
import sys
from pathlib import Path

from .config import VoiceConfig
from .robot_control import WebRobotControl
from .service import LogStatusSink, VoiceAssistant
from .web_api import VoiceWebBridge


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="小达语音助手")
    parser.add_argument("--api-file", default="/home/robot/api.txt", help="API 配置文件")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="持续运行：唤醒、倾听和多轮对话")
    run.add_argument("--without-wake", action="store_true", help="跳过一次唤醒，直接开始对话")
    run.add_argument("--one-turn", action="store_true", help="只对话一轮，便于联调")
    run.add_argument(
        "--web-only",
        action="store_true",
        help="关闭唤醒词监听，仅接受网页按钮启动单轮对话",
    )
    run.add_argument("--web-api-host", default="127.0.0.1", help="网页语音桥接监听地址（仅允许回环地址）")
    run.add_argument("--web-api-port", type=int, default=8765, help="网页语音桥接端口")
    run.add_argument("--no-web-api", action="store_true", help="不启动本机网页语音桥接")

    text = subparsers.add_parser("text", help="用文字测试 LLM/TTS 链路")
    text.add_argument("message")
    text.add_argument("--no-speak", action="store_true")

    subparsers.add_parser("wake-test", help="只测试“小达”唤醒词")
    subparsers.add_parser("doctor", help="检查配置、设备、依赖和模型")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = VoiceConfig.load(args.api_file)

    if args.command == "doctor":
        return doctor(config)

    bridge = None
    if args.command == "run" and not args.no_web_api:
        bridge = VoiceWebBridge(
            LogStatusSink(),
            host=args.web_api_host,
            port=args.web_api_port,
        )
    robot_control = (
        WebRobotControl(config.robot_control_host, config.robot_control_port)
        if args.command == "run"
        else None
    )
    assistant = VoiceAssistant(
        config,
        status=bridge or LogStatusSink(),
        robot_control=robot_control,
        observer=bridge,
    )
    if bridge is not None:
        bridge.attach(assistant)
        bridge.start()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: assistant.stop())

    try:
        if args.command == "text":
            answer = assistant.answer_text(args.message, speak=not args.no_speak)
            print(answer)
            return 0
        if args.command == "wake-test":
            print(f'正在监听，请说“{config.robot_name}”……', flush=True)
            detected = assistant.wakeword.wait(assistant.stop_event)
            if detected:
                assistant.speaker.chime()
                print(f"唤醒成功：{detected}")
                return 0
            return 130
        if args.without_wake:
            assistant.begin_session("cli", "zh")
            assistant.speaker.chime()
            assistant.run_session(max_turns=1 if args.one_turn else None, language="zh")
        elif args.web_only:
            assistant.run_web_only_forever()
        elif args.one_turn:
            detected = assistant.wakeword.wait(assistant.stop_event)
            if detected:
                assistant.begin_session("wakeword", "zh")
                assistant.speaker.chime()
                assistant.run_session(max_turns=1, language="zh")
        else:
            assistant.run_forever()
        return 0
    finally:
        if bridge is not None:
            bridge.close()
        assistant.close()


def doctor(config: VoiceConfig) -> int:
    print(f"机器人名称：{config.robot_name}")
    print(f"LLM 模型：{config.llm_model}（reasoning={config.reasoning_effort}）")
    for profile in config.llm_profiles:
        print(f"LLM 接口：{profile.redacted}")
    print(f"ASR/TTS 接口：{config.audio_api_base}（key={'set' if config.audio_api_key else 'missing'}）")
    print(f"TTS 语速：{config.tts_speed:.2f}x")
    print(f"句尾静音：{config.end_silence_seconds:.2f}s")
    print(f"麦克风：{config.microphone_device}")
    print(f"扬声器：{config.speaker_device}")
    print(f"唤醒模型线程：{config.kws_threads}")
    print(f"语音运动桥接：{config.robot_control_host}:{config.robot_control_port}")

    errors = config.validate(require_models=True)
    for command in ("arecord", "aplay", "ffmpeg"):
        location = shutil.which(command)
        print(f"命令 {command}：{location or 'missing'}")
        if location is None:
            errors.append(f"缺少命令：{command}")
    try:
        import httpx  # noqa: F401
        import numpy  # noqa: F401
        import sherpa_onnx  # noqa: F401
    except ImportError as exc:
        errors.append(f"缺少 Python 依赖：{exc.name}")

    for path in _model_files(config):
        print(f"模型：{path}（{'ok' if path.is_file() else 'missing'}）")
    if errors:
        print("检查失败：")
        for error in errors:
            print(f"- {error}")
        return 1
    print("检查通过。")
    return 0


def _model_files(config: VoiceConfig) -> tuple[Path, ...]:
    return (
        config.kws_model_dir / "encoder-epoch-13-avg-2-chunk-8-left-64.int8.onnx",
        config.kws_model_dir / "decoder-epoch-13-avg-2-chunk-8-left-64.onnx",
        config.kws_model_dir / "joiner-epoch-13-avg-2-chunk-8-left-64.int8.onnx",
        config.kws_model_dir / "tokens.txt",
        config.kws_model_dir / "keywords_xiaoda.txt",
        config.vad_model,
    )


if __name__ == "__main__":
    sys.exit(main())
