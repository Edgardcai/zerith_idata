"""Mock detector for CI and unit tests.

Returns hard-coded detections based on a configurable fixture. No GPU, no
network, no model weights required.
"""

from __future__ import annotations

from pathlib import Path

from .base import Detection, Detector


class MockDetector:
    """Always returns the same list of detections regardless of the image.

    Configure via `fixture_detections` when constructing, or use the
    class method `for_targets` to auto-build fixtures from a target list.
    """

    def __init__(self, fixture_detections: list[Detection] | None = None) -> None:
        self._detections = fixture_detections or []

    @classmethod
    def for_targets(cls, targets: list[str]) -> "MockDetector":
        """Build a mock that 'detects' each target with a plausible bbox."""
        detections = []
        for i, label in enumerate(targets):
            x1 = 0.1 + i * 0.35
            y1 = 0.2
            x2 = x1 + 0.25
            y2 = 0.7
            detections.append(
                Detection(
                    label=label,
                    bbox=[round(x1, 3), y1, round(min(x2, 0.99), 3), y2],
                    conf=0.92,
                    mask_available=False,
                    raw_label=label,
                )
            )
        return cls(detections)

    def detect(self, image_path: Path) -> list[Detection]:
        return list(self._detections)

    def close(self) -> None:
        pass
