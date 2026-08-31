from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class ParsedMotion:
    command: str
    source: str = "rules"


_CHINESE_COMMANDS: dict[str, set[str]] = {
    "stop": {
        "停",
        "停止",
        "停下",
        "停下来",
        "停住",
        "别动",
        "不要再动",
        "别再动",
        "不许动",
        "立即停止",
        "紧急停止",
        "马上停止",
        "马上停下",
        "快停下",
        "快停下来",
    },
    "forward": {"前进", "向前", "往前", "向前移动", "往前移动", "向前走", "往前走"},
    "backward": {"后退", "向后", "往后", "向后移动", "往后移动", "向后走", "往后走"},
    "turn_left": {"左转", "向左转", "往左转", "向左转身"},
    "turn_right": {"右转", "向右转", "往右转", "向右转身"},
    "turn_around": {"转身", "转个身", "掉头", "原地转身", "转过去", "转到后面"},
    "wave": {"挥手", "挥挥手", "挥一下手", "招手", "招招手", "打个招呼"},
    "handshake": {"握手", "握个手", "握一下手", "和我握手", "跟我握手"},
}

_ENGLISH_COMMANDS: dict[str, set[str]] = {
    "stop": {"stop", "stop moving", "halt", "freeze", "emergency stop"},
    "forward": {"forward", "move forward", "go forward"},
    "backward": {"backward", "move backward", "go backward", "reverse"},
    "turn_left": {"turn left", "rotate left"},
    "turn_right": {"turn right", "rotate right"},
    "turn_around": {"turn around", "turn back", "about face"},
    "wave": {"wave", "wave your hand", "say hello"},
    "handshake": {"handshake", "shake hands", "shake my hand"},
}

_MOTION_HINTS = (
    "前进",
    "后退",
    "向前",
    "往前",
    "向后",
    "往后",
    "移动",
    "走",
    "过来",
    "过去",
    "转",
    "掉头",
    "挥手",
    "招手",
    "握手",
    "抬手",
    "手臂",
    "抓",
    "拿",
    "停",
    "move",
    "go ",
    "come",
    "turn",
    "rotate",
    "wave",
    "handshake",
    "shake hand",
    "stop",
    "halt",
)

_NEGATIONS = ("不要", "不许", "不用", "别", "do not", "don't", "dont", "never")


def parse_fast_motion(text: str) -> ParsedMotion | None:
    normalized = _normalize(text)
    if not normalized:
        return None

    # A negated motion request is never converted to a positive command.  If
    # motion control is active, interpreting it as STOP is the safest action.
    if any(token in normalized for token in _NEGATIONS) and looks_motion_related(text):
        return ParsedMotion("stop")

    variants = _polite_variants(normalized)
    tables = (_CHINESE_COMMANDS, _ENGLISH_COMMANDS)
    for command in (
        "stop",
        "forward",
        "backward",
        "turn_left",
        "turn_right",
        "turn_around",
        "wave",
        "handshake",
    ):
        allowed = set().union(*(table[command] for table in tables))
        if variants & allowed:
            return ParsedMotion(command)
    return None


def looks_motion_related(text: str) -> bool:
    normalized = _normalize(text)
    return any(hint in normalized for hint in _MOTION_HINTS)


def _normalize(text: str) -> str:
    value = str(text).strip().lower().replace("小达", "")
    value = re.sub(r"[\s\u3000]+", " ", value)
    value = re.sub(r"[，。！？、,.!?;；:：\"'“”‘’]", "", value)
    return value.strip()


def _polite_variants(text: str) -> set[str]:
    variants = {text, text.replace(" ", "")}
    prefixes = ("请你", "请", "请帮我", "帮我", "给我", "麻烦你", "麻烦", "please ")
    suffixes = ("一下", "一点", "一点点", "吧", "好吗", " please", " now")
    changed = True
    while changed:
        changed = False
        for value in tuple(variants):
            for prefix in prefixes:
                if value.startswith(prefix):
                    candidate = value[len(prefix) :].strip()
                    if candidate and candidate not in variants:
                        variants.add(candidate)
                        changed = True
            for suffix in suffixes:
                if value.endswith(suffix):
                    candidate = value[: -len(suffix)].strip()
                    if candidate and candidate not in variants:
                        variants.add(candidate)
                        changed = True
    return variants


__all__ = ["ParsedMotion", "looks_motion_related", "parse_fast_motion"]
