"""Base types for the vision detector abstraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class Detection:
    """One detected object in a single image frame."""
    label: str                          # canonical English name (after alias mapping)
    bbox: list[float]                   # [x1, y1, x2, y2] normalised 0-1
    conf: float
    mask_available: bool = False
    raw_label: str = ""                 # original class name before alias mapping


@dataclass
class FrameDetections:
    """All detections for one image frame."""
    frame_key: str                      # e.g. "first", "last"
    frame_idx: int
    image_path: str
    detections: list[Detection] = field(default_factory=list)
    error: str = ""


@runtime_checkable
class Detector(Protocol):
    def detect(self, image_path: Path) -> list[Detection]:
        """Return all detections for one image."""
        ...

    def close(self) -> None:
        """Release resources (model weights, HTTP sessions, …)."""
        ...
