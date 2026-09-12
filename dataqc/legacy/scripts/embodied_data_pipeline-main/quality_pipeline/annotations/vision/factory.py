"""Create the right detector from profile config."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Detector
from .mock import MockDetector


def create_detector(profile_raw: dict[str, Any], repo_root: Path | None = None) -> Detector:
    """Instantiate a detector based on processing.annotations.vision_pipeline.

    Args:
        profile_raw: the raw dict from load_profile().raw
        repo_root:   used to resolve relative weight paths (defaults to cwd)
    """
    vp = (
        profile_raw.get("processing", {})
        .get("annotations", {})
        .get("vision_pipeline", {})
    )
    kind = str(vp.get("detector", "mock")).lower()
    class_alias: dict[int, str] = {
        int(k): str(v)
        for k, v in (profile_raw.get("yolo_class_alias") or {}).items()
    }
    class_normalize: dict[str, str] = {
        str(k): str(v)
        for k, v in (profile_raw.get("yolo_class_normalize") or {}).items()
    }

    if kind == "mock":
        return MockDetector()

    if kind == "local_yolo":
        from .local_yolo import LocalYOLODetector

        weights_str = str(vp.get("weights") or "")
        weights = Path(weights_str)
        if not weights.is_absolute():
            base = repo_root or Path.cwd()
            weights = (base / weights).resolve()
        if not weights.exists():
            raise FileNotFoundError(
                f"YOLO weights not found: {weights}\n"
                "Set processing.annotations.vision_pipeline.weights in the profile."
            )
        raw_imgsz = vp.get("imgsz", [640, 400])
        imgsz = (int(raw_imgsz[0]), int(raw_imgsz[1]))
        conf = float(vp.get("conf_threshold", 0.45))
        device = str(vp.get("device", "cpu"))
        return LocalYOLODetector(
            weights=weights,
            class_alias=class_alias,
            class_normalize=class_normalize,
            conf=conf,
            imgsz=imgsz,
            device=device,
        )

    raise ValueError(
        f"Unknown vision detector type: {kind!r}. "
        "Supported: 'local_yolo', 'mock'."
    )
