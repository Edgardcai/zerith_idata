"""Head-camera product observations for DataQC category review."""
from functools import lru_cache
from threading import Lock

from .io import frame, sha

_lock = Lock()


def model_hash(cfg):
    digest = sha(cfg['head_yolo_path'])
    if cfg.get('head_yolo_sha256') and cfg['head_yolo_sha256'] != digest:
        raise ValueError('头部 YOLO 文件与配置的 SHA-256 不一致')
    return digest


@lru_cache(maxsize=1)
def _load_model(path, digest):
    from ultralytics import YOLO
    return YOLO(path)


def observe(root, indices, cfg, digest):
    indices = list(dict.fromkeys(indices))
    confidence = cfg.get('yolo_confidence', .25)
    with _lock:
        model = _load_model(cfg['head_yolo_path'], digest)
        images = [frame(root, 'cam_high', index, max_width=0) for index in indices]
        results = model.predict(images, device=cfg['device'], imgsz=960, conf=confidence,
                                iou=.45, end2end=False, verbose=False)
        observations = []
        for index, result in zip(indices, results, strict=True):
            boxes = result.boxes
            detections = [dict(class_id=int(cls), name=result.names[int(cls)], confidence=round(score, 4),
                               box=[round(v, 2) for v in coords])
                          for cls, score, coords in zip(boxes.cls.tolist(), boxes.conf.tolist(),
                                                       boxes.xyxy.tolist(), strict=True)]
            observations.append(dict(frame=index, detections=detections))
    return dict(camera='cam_high', model_path=cfg['head_yolo_path'], model_hash=digest,
                class_count=len(model.names), confidence=confidence, frames=observations)
