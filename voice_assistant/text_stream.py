from __future__ import annotations


class SentenceChunker:
    """Turn token deltas into speakable phrases without waiting for the full reply."""

    SENTENCE_END = "。！？!?；;\n"
    SOFT_END = "，,：:"

    def __init__(self, min_characters: int = 10, max_characters: int = 52) -> None:
        self._buffer = ""
        self._minimum = min_characters
        self._maximum = max_characters

    def push(self, delta: str) -> list[str]:
        self._buffer += delta
        ready: list[str] = []
        while True:
            split = self._sentence_boundary()
            if split is None and len(self._buffer) >= self._maximum:
                split = self._soft_boundary()
            if split is None:
                break
            text = self._take(split)
            if text:
                ready.append(text)
        return ready

    def flush(self) -> str:
        text = self._buffer.strip()
        self._buffer = ""
        return text

    def _sentence_boundary(self) -> int | None:
        for index, character in enumerate(self._buffer):
            if character in self.SENTENCE_END and index + 1 >= self._minimum:
                return index + 1
        return None

    def _soft_boundary(self) -> int:
        start = max(self._minimum, self._maximum // 2)
        candidates = [
            index + 1
            for index, character in enumerate(self._buffer[: self._maximum])
            if index + 1 >= start and character in self.SOFT_END
        ]
        return candidates[-1] if candidates else self._maximum

    def _take(self, count: int) -> str:
        text = self._buffer[:count].strip()
        self._buffer = self._buffer[count:].lstrip()
        return text
