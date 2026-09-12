"""Vision pipeline for L1 trajectory enrichment.

Runs a detector (YOLO or mock) on key frames of an episode to produce:
  - trajectory.objects[].bbox       physical bounding boxes for target objects
  - trajectory.quality.target_visible_at_start
  - trajectory.quality.target_displaced
  - trajectory.quality.target_count_consistent
"""

from .factory import create_detector
from .base import Detection, Detector

__all__ = ["create_detector", "Detection", "Detector"]
