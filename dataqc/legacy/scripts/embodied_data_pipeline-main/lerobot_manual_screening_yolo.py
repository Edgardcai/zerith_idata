"""YOLO object verification for cached LeRobot manual-screening frames."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable
import uuid


DEFAULT_MODEL_DIR = Path(
    os.environ.get("PIPELINE_MANUAL_SCREENING_YOLO_MODEL_DIR")
    or "/srv/data/datasets/users/yolo/models/v34_20260807"
).expanduser()
MODEL_PATH = Path(
    os.environ.get("PIPELINE_MANUAL_SCREENING_YOLO_MODEL")
    or DEFAULT_MODEL_DIR / "model_unified_products21_v34.pt"
).expanduser()
THRESHOLDS_PATH = Path(
    os.environ.get("PIPELINE_MANUAL_SCREENING_YOLO_THRESHOLDS")
    or DEFAULT_MODEL_DIR / "thresholds_main_v34.json"
).expanduser()
YOLO_DEVICE = str(os.environ.get("PIPELINE_MANUAL_SCREENING_YOLO_DEVICE") or "0").strip()
YOLO_IMAGE_SIZE = int(os.environ.get("PIPELINE_MANUAL_SCREENING_YOLO_IMGSZ") or 640)
REQUIRED_MATCHES = 2
MOMENTS = ("before", "close", "after")
NON_PRODUCT_CLASSES = {"Robot Arm / Gripper"}
REPORT_FILENAME = "yolo_results.json"
_REPORT_LOCK = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def report_path(dataset_group_id: str, *, storage_root: Path) -> Path:
    if not re.fullmatch(r"[0-9a-f]{20}", str(dataset_group_id)):
        raise ValueError(f"无效的数据集分组 ID: {dataset_group_id}")
    return storage_root.expanduser().resolve() / "extracted_groups" / dataset_group_id / REPORT_FILENAME


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _normalise_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _threshold_payload(path: Path = THRESHOLDS_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取 YOLO 阈值文件 {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"YOLO 阈值文件格式错误: {path}")
    thresholds = payload.get("per_class_conf")
    if not isinstance(thresholds, dict) or not thresholds:
        raise RuntimeError(f"YOLO 阈值文件缺少 per_class_conf: {path}")
    return payload


class YoloRuntime:
    """Lazily loads one Ultralytics model and serializes GPU inference."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._model: Any = None
        self._threshold_payload: dict[str, Any] | None = None

    def _load(self) -> tuple[Any, dict[str, Any]]:
        if self._model is not None and self._threshold_payload is not None:
            return self._model, self._threshold_payload
        if not MODEL_PATH.is_file():
            raise FileNotFoundError(f"YOLO 模型不存在: {MODEL_PATH}")
        try:
            from ultralytics import YOLO  # type: ignore
        except ImportError as exc:
            raise RuntimeError("当前 Python 环境未安装 ultralytics") from exc
        self._threshold_payload = _threshold_payload()
        self._model = YOLO(str(MODEL_PATH))
        return self._model, self._threshold_payload

    def predict(self, image_paths: list[Path]) -> dict[str, Any]:
        if not image_paths:
            raise ValueError("没有可供 YOLO 检测的图片")
        with self._lock:
            model, threshold_payload = self._load()
            thresholds = threshold_payload["per_class_conf"]
            minimum_confidence = min(float(value) for value in thresholds.values())
            started = time.monotonic()
            results = model.predict(
                source=[str(path) for path in image_paths],
                conf=minimum_confidence,
                imgsz=YOLO_IMAGE_SIZE,
                device=YOLO_DEVICE,
                verbose=False,
            )
            elapsed_ms = (time.monotonic() - started) * 1000.0

        if len(results) != len(image_paths):
            raise RuntimeError(
                f"YOLO 返回图片数异常: expected={len(image_paths)} actual={len(results)}"
            )
        predictions: list[list[dict[str, Any]]] = []
        for result in results:
            detections: list[dict[str, Any]] = []
            boxes = result.boxes
            if boxes is not None:
                for class_value, confidence_value, bbox_value in zip(
                    boxes.cls.tolist(),
                    boxes.conf.tolist(),
                    boxes.xyxy.tolist(),
                ):
                    class_id = int(class_value)
                    confidence = float(confidence_value)
                    threshold = float(thresholds.get(str(class_id), minimum_confidence))
                    if confidence < threshold:
                        continue
                    class_name = str(result.names.get(class_id, class_id))
                    detections.append(
                        {
                            "class_id": class_id,
                            "class_name": class_name,
                            "confidence": round(confidence, 4),
                            "threshold": round(threshold, 4),
                            "bbox_xyxy": [round(float(value), 2) for value in bbox_value],
                            "is_product": class_name not in NON_PRODUCT_CLASSES,
                        }
                    )
            detections.sort(key=lambda item: float(item["confidence"]), reverse=True)
            predictions.append(detections)
        return {
            "predictions": predictions,
            "latency_ms": round(elapsed_ms, 1),
            "release": str(threshold_payload.get("release") or MODEL_PATH.stem),
            "model_path": str(MODEL_PATH),
            "device": YOLO_DEVICE,
        }


RUNTIME = YoloRuntime()


def _expected_objects_for_arm(episode: dict[str, Any], arm: str) -> list[str]:
    original = episode.get("original") if isinstance(episode.get("original"), dict) else {}
    objects_by_arm = (
        original.get("objects_by_arm") if isinstance(original.get("objects_by_arm"), dict) else {}
    )
    mapped = [str(value).strip() for value in objects_by_arm.get(arm) or [] if str(value).strip()]
    if mapped:
        return mapped
    objects = [str(value).strip() for value in original.get("objects") or [] if str(value).strip()]
    prompt = str(episode.get("prompt") or "")
    if not objects or arm not in {"left", "right"}:
        return objects
    associated: list[str] = []
    for name in objects:
        pattern = (
            re.escape(name)
            + rf"\s+\b(?:with|using)\s+(?:the\s+)?{arm}(?:[- ]?hand|\s+arm)?\b"
        )
        if re.search(pattern, prompt, re.IGNORECASE):
            associated.append(name)
    return associated or objects


def _wrist_image(moment: dict[str, Any], arm: str) -> dict[str, Any] | None:
    images = moment.get("images") if isinstance(moment.get("images"), list) else []
    arm_tokens = (f"hand_{arm}", f"{arm}_hand", f"wrist_{arm}", f"{arm}_wrist")
    for image in images:
        if not isinstance(image, dict):
            continue
        key = str(image.get("camera_key") or "").casefold()
        if any(token in key for token in arm_tokens):
            return image
    for image in images:
        if isinstance(image, dict) and arm in str(image.get("camera_key") or "").casefold():
            return image
    return None


def _resolve_cached_image(
    image: dict[str, Any],
    dataset_id: str,
    storage_root: Path,
) -> Path:
    url = str(image.get("url") or "")
    prefix = f"/manual-screening-image/{dataset_id}/"
    if not url.startswith(prefix):
        raise ValueError(f"截帧 URL 与物理数据集不匹配: {url}")
    root = (storage_root / "extracted" / dataset_id).resolve()
    path = (root / url[len(prefix) :]).resolve()
    path.relative_to(root)
    if not path.is_file():
        raise FileNotFoundError(f"YOLO 截帧不存在: {path}")
    return path


def _detect_grasp(
    episode: dict[str, Any],
    episode_key: str,
    arm: str,
    frames: dict[str, Any],
    *,
    storage_root: Path,
    predictor: Callable[[list[Path]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    original = episode.get("original") if isinstance(episode.get("original"), dict) else {}
    expected_objects = _expected_objects_for_arm(episode, arm)
    if not expected_objects:
        raise ValueError(f"Prompt 中没有可供 YOLO 核对的{arm}手物品")

    dataset_id = str(episode.get("dataset_id") or "")
    selected: list[dict[str, Any]] = []
    image_paths: list[Path] = []
    for moment_name in MOMENTS:
        moment = frames.get(moment_name)
        if not isinstance(moment, dict):
            raise ValueError(f"缺少 {moment_name} 时刻的截帧")
        image = _wrist_image(moment, arm)
        if image is None:
            raise ValueError(f"{moment.get('label') or moment_name} 缺少{arm}腕部相机截帧")
        image_paths.append(_resolve_cached_image(image, dataset_id, storage_root))
        selected.append(
            {
                "moment": moment_name,
                "label": str(moment.get("label") or moment_name),
                "frame_index": moment.get("frame_index"),
                "camera_key": str(image.get("camera_key") or ""),
                "camera_label": str(image.get("camera_label") or ""),
                "image_url": str(image.get("url") or ""),
            }
        )

    prediction = (predictor or RUNTIME.predict)(image_paths)
    predictions = prediction.get("predictions")
    if not isinstance(predictions, list) or len(predictions) != len(selected):
        raise RuntimeError("YOLO 推理结果与三个时刻不对应")
    expected_normalised = {_normalise_name(name) for name in expected_objects}
    matched_count = 0
    moments: list[dict[str, Any]] = []
    for selected_item, raw_detections in zip(selected, predictions):
        detections = [item for item in raw_detections if isinstance(item, dict)]
        product_detections_raw = [item for item in detections if item.get("is_product", True)]
        best_by_class: dict[str, dict[str, Any]] = {}
        for detection in product_detections_raw:
            class_key = _normalise_name(detection.get("class_name"))
            current = best_by_class.get(class_key)
            if current is None or float(detection.get("confidence") or 0.0) > float(
                current.get("confidence") or 0.0
            ):
                best_by_class[class_key] = dict(detection)
        product_detections = sorted(
            best_by_class.values(),
            key=lambda item: float(item.get("confidence") or 0.0),
            reverse=True,
        )
        for detection in product_detections:
            detection["matched"] = _normalise_name(detection.get("class_name")) in expected_normalised
        matched = any(bool(item.get("matched")) for item in product_detections)
        matched_count += int(matched)
        moments.append(
            {
                **selected_item,
                "matched": matched,
                "detections": product_detections,
                "detected_products": list(
                    dict.fromkeys(str(item.get("class_name") or "") for item in product_detections)
                ),
            }
        )
    correct = matched_count >= REQUIRED_MATCHES
    object_details = [
        item
        for item in original.get("object_details") or []
        if isinstance(item, dict) and str(item.get("name") or "") in expected_objects
    ]
    return {
        "ok": True,
        "status": "correct" if correct else "incorrect",
        "correct": correct,
        "message": (
            f"物品正确：3 个时刻中 {matched_count} 个识别到目标物品"
            if correct
            else f"物品不正确或未稳定识别：3 个时刻中仅 {matched_count} 个识别到目标物品"
        ),
        "matched_count": matched_count,
        "required_matches": REQUIRED_MATCHES,
        "total_moments": len(MOMENTS),
        "expected_objects": expected_objects,
        "expected_object_details": object_details,
        "arm": arm,
        "camera_label": "左腕视角" if arm == "left" else "右腕视角",
        "episode_key": episode_key,
        "episode_index": episode.get("episode_index"),
        "source_grade": episode.get("source_grade") or "",
        "dataset_path": episode.get("dataset_path") or "",
        "moments": moments,
        "latency_ms": prediction.get("latency_ms"),
        "model": {
            "release": prediction.get("release") or MODEL_PATH.stem,
            "path": prediction.get("model_path") or str(MODEL_PATH),
            "device": prediction.get("device") or YOLO_DEVICE,
        },
    }


def _grasp_inputs(episode: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    grasps = episode.get("grasps") if isinstance(episode.get("grasps"), dict) else {}
    output: list[tuple[str, dict[str, Any]]] = []
    for arm in ("left", "right"):
        grasp = grasps.get(arm)
        if not isinstance(grasp, dict):
            continue
        frames = grasp.get("frames") if isinstance(grasp.get("frames"), dict) else {}
        output.append((arm, frames))
    if output:
        return output
    closure = episode.get("closure") if isinstance(episode.get("closure"), dict) else {}
    original = episode.get("original") if isinstance(episode.get("original"), dict) else {}
    arm = str(closure.get("arm") or original.get("arm") or "").strip().lower()
    if arm not in {"left", "right"}:
        raise ValueError("无法确定抓取手，不能选择对应腕部相机")
    frames = episode.get("frames") if isinstance(episode.get("frames"), dict) else {}
    return [(arm, frames)]


def detect_episode(
    manifest: dict[str, Any],
    episode_key: str,
    *,
    storage_root: Path,
    predictor: Callable[[list[Path]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    episode = next(
        (
            item
            for item in manifest.get("episodes") or []
            if isinstance(item, dict) and item.get("episode_key") == episode_key
        ),
        None,
    )
    if episode is None:
        raise ValueError(f"组合截帧清单中没有 episode: {episode_key}")
    grasp_inputs = _grasp_inputs(episode)
    grasp_results = [
        _detect_grasp(
            episode,
            episode_key,
            arm,
            frames,
            storage_root=storage_root,
            predictor=predictor,
        )
        for arm, frames in grasp_inputs
    ]
    if len(grasp_results) == 1:
        return grasp_results[0]

    correct = all(result.get("status") == "correct" for result in grasp_results)
    arm_labels = {"left": "左手", "right": "右手"}
    detail = "，".join(
        f"{arm_labels.get(str(result.get('arm')), str(result.get('arm')))} "
        f"{result.get('matched_count', 0)}/3"
        for result in grasp_results
    )
    return {
        "ok": True,
        "status": "correct" if correct else "incorrect",
        "correct": correct,
        "message": f"双手物品{'正确' if correct else '存在预警'}：{detail}",
        "grasp_mode": "bimanual",
        "grasp_results": grasp_results,
        "matched_count": sum(int(result.get("matched_count") or 0) for result in grasp_results),
        "required_matches": REQUIRED_MATCHES,
        "total_moments": len(MOMENTS) * len(grasp_results),
        "expected_objects": list(
            dict.fromkeys(
                str(name)
                for result in grasp_results
                for name in result.get("expected_objects") or []
            )
        ),
        "expected_object_details": [
            item
            for result in grasp_results
            for item in result.get("expected_object_details") or []
        ],
        "arm": "both",
        "camera_label": "左腕 / 右腕视角",
        "episode_key": episode_key,
        "episode_index": episode.get("episode_index"),
        "source_grade": episode.get("source_grade") or "",
        "dataset_path": episode.get("dataset_path") or "",
        "moments": [],
        "latency_ms": round(
            sum(float(result.get("latency_ms") or 0.0) for result in grasp_results),
            1,
        ),
        "model": grasp_results[0].get("model") or {},
    }


def _error_result(episode: dict[str, Any], exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "error",
        "correct": False,
        "message": f"YOLO 识别失败：{exc}",
        "matched_count": 0,
        "required_matches": REQUIRED_MATCHES,
        "total_moments": len(MOMENTS),
        "expected_objects": [],
        "expected_object_details": [],
        "arm": "",
        "camera_label": "",
        "episode_key": str(episode.get("episode_key") or ""),
        "episode_index": episode.get("episode_index"),
        "source_grade": episode.get("source_grade") or "",
        "dataset_path": episode.get("dataset_path") or "",
        "moments": [],
        "latency_ms": None,
        "error": str(exc),
        "model": {
            "release": MODEL_PATH.stem,
            "path": str(MODEL_PATH),
            "device": YOLO_DEVICE,
        },
    }


def _report_payload(
    manifest: dict[str, Any],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    correct_count = sum(item.get("status") == "correct" for item in results)
    error_count = sum(item.get("status") == "error" for item in results)
    warning_count = len(results) - correct_count
    return {
        "version": 1,
        "generated_at": _utc_now(),
        "dataset_id": str(manifest.get("dataset_id") or ""),
        "dataset_name": str(manifest.get("dataset_name") or ""),
        "dataset_path": str(manifest.get("dataset_path") or ""),
        "manifest_generated_at": str(manifest.get("generated_at") or ""),
        "episode_count": len(results),
        "correct_count": correct_count,
        "warning_count": warning_count,
        "error_count": error_count,
        "required_matches": REQUIRED_MATCHES,
        "model": {
            "release": next(
                (
                    str(item.get("model", {}).get("release") or "")
                    for item in results
                    if isinstance(item.get("model"), dict)
                    and item.get("model", {}).get("release")
                ),
                MODEL_PATH.stem,
            ),
            "path": str(MODEL_PATH),
            "device": YOLO_DEVICE,
        },
        "results": results,
    }


def save_report(
    manifest: dict[str, Any],
    results: list[dict[str, Any]],
    *,
    storage_root: Path,
) -> dict[str, Any]:
    report = _report_payload(manifest, results)
    path = report_path(str(manifest.get("dataset_id") or ""), storage_root=storage_root)
    with _REPORT_LOCK:
        _atomic_json(path, report)
    return report


def load_report(
    manifest: dict[str, Any],
    *,
    storage_root: Path,
) -> dict[str, Any] | None:
    path = report_path(str(manifest.get("dataset_id") or ""), storage_root=storage_root)
    if not path.is_file():
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(report, dict) or not isinstance(report.get("results"), list):
        return None
    if str(report.get("manifest_generated_at") or "") != str(manifest.get("generated_at") or ""):
        return None
    return report


def detect_manifest(
    manifest: dict[str, Any],
    *,
    storage_root: Path,
    predictor: Callable[[list[Path]], dict[str, Any]] | None = None,
    progress: Callable[[str], None] | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    episodes = [item for item in manifest.get("episodes") or [] if isinstance(item, dict)]
    if not episodes:
        raise ValueError("截帧清单中没有可供 YOLO 检测的 episode")
    results: list[dict[str, Any]] = []
    total = len(episodes)
    for ordinal, episode in enumerate(episodes, start=1):
        if stop_requested and stop_requested():
            raise RuntimeError("YOLO 批量识别已停止")
        episode_key = str(episode.get("episode_key") or "")
        try:
            result = detect_episode(
                manifest,
                episode_key,
                storage_root=storage_root,
                predictor=predictor,
            )
        except Exception as exc:
            result = _error_result(episode, exc)
        results.append(result)
        if progress:
            status = "正确" if result.get("status") == "correct" else "预警"
            progress(
                f"YOLO 批量识别 {ordinal}/{total}: "
                f"{episode.get('source_grade') or '-'} · "
                f"{episode.get('episode_name') or episode_key} · {status}"
            )
    return save_report(manifest, results, storage_root=storage_root)


def save_episode_result(
    manifest: dict[str, Any],
    result: dict[str, Any],
    *,
    storage_root: Path,
) -> dict[str, Any]:
    with _REPORT_LOCK:
        existing = load_report(manifest, storage_root=storage_root)
        by_key = {
            str(item.get("episode_key") or ""): item
            for item in (existing or {}).get("results") or []
            if isinstance(item, dict) and item.get("episode_key")
        }
        by_key[str(result.get("episode_key") or "")] = result
        ordered = [
            by_key[key]
            for episode in manifest.get("episodes") or []
            if isinstance(episode, dict)
            and (key := str(episode.get("episode_key") or "")) in by_key
        ]
        report = _report_payload(manifest, ordered)
        path = report_path(str(manifest.get("dataset_id") or ""), storage_root=storage_root)
        _atomic_json(path, report)
    return report
