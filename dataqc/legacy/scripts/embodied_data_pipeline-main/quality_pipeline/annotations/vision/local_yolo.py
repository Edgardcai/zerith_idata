"""Local YOLO detector using ultralytics.

Requires `pip install ultralytics` and a compatible model weights file.
ultralytics is intentionally NOT in quality_pipeline/requirements.txt — it is a
heavy GPU dependency that should only be installed when vision is enabled.

Usage:
    from quality_pipeline.annotations.vision.local_yolo import LocalYOLODetector
    det = LocalYOLODetector(weights_path, class_alias, class_normalize, conf=0.45)
    detections = det.detect(Path("frame.jpg"))
    det.close()
"""

from __future__ import annotations

from pathlib import Path

from .base import Detection


class LocalYOLODetector:
    """Wraps an ultralytics segmentation/detection model."""

    def __init__(
        self,
        weights: str | Path,
        class_alias: dict[int, str],
        class_normalize: dict[str, str],
        conf: float = 0.45,
        imgsz: tuple[int, int] = (640, 400),
        device: str = "cpu",
    ) -> None:
        try:
            from ultralytics import YOLO  # type: ignore
        except ImportError as e:
            raise ImportError(
                "ultralytics is required for local_yolo detector. "
                "Install it inside a venv/conda env, for example: "
                "python3 -m venv .venv-data && source .venv-data/bin/activate && "
                "python -m pip install -r quality_pipeline/requirements-vision.txt"
            ) from e

        self._model = YOLO(str(weights))
        self._class_alias = class_alias
        self._class_normalize = class_normalize
        self._conf = conf
        self._imgsz = imgsz
        self._device = device

    def detect(self, image_path: Path) -> list[Detection]:
        results = self._model(
            str(image_path),
            conf=self._conf,
            imgsz=list(self._imgsz),
            device=self._device,
            verbose=False,
        )
        detections: list[Detection] = []
        for result in results:
            boxes = result.boxes
            if boxes is None:
                continue
            has_masks = result.masks is not None
            for i in range(len(boxes)):
                cls_idx = int(boxes.cls[i].item())
                conf_val = float(boxes.conf[i].item())
                # normalized bbox [x1, y1, x2, y2]
                xyxyn = boxes.xyxyn[i].tolist()
                raw_label = result.names.get(cls_idx, str(cls_idx))
                canonical = self._class_alias.get(cls_idx, raw_label)
                normalized = self._class_normalize.get(canonical, canonical)
                detections.append(
                    Detection(
                        label=normalized,
                        bbox=[round(v, 4) for v in xyxyn],
                        conf=round(conf_val, 4),
                        mask_available=has_masks,
                        raw_label=raw_label,
                    )
                )
        return detections

    def close(self) -> None:
        del self._model
