"""Run detector on key frames of an episode and produce FrameDetections.

Key-frame strategy (from profile.processing.annotations.vision_pipeline.key_frames):
  first  → frame index 0
  last   → frame index n-1
  middle → frame index n//2

Returns a list of FrameDetections, one per key frame.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Any

from .base import Detector, FrameDetections


def run_on_episode(
    detector: Detector,
    episode: Any,
    profile_raw: dict[str, Any],
) -> list[FrameDetections]:
    vp = (
        profile_raw.get("processing", {})
        .get("annotations", {})
        .get("vision_pipeline", {})
    )
    camera = str(vp.get("camera", "head_color"))
    key_frame_names: list[str] = list(vp.get("key_frames", ["first", "last"]))

    n = episode.n_frames
    if n == 0:
        return []

    frame_map = {
        "first": 0,
        "middle": n // 2,
        "last": n - 1,
    }
    results: list[FrameDetections] = []
    seen: set[int] = set()
    for name in key_frame_names:
        idx = frame_map.get(name, 0)
        if idx in seen:
            continue
        seen.add(idx)
        image_path = _image_path(episode, camera, idx)
        if image_path is None or not image_path.exists():
            continue
        error = ""
        try:
            dets = detector.detect(image_path)
        except Exception as exc:
            dets = []
            error = f"{type(exc).__name__}: {exc}"
        results.append(
            FrameDetections(
                frame_key=name,
                frame_idx=idx,
                image_path=str(image_path),
                detections=dets,
                error=error,
            )
        )
    return results


def _image_path(episode: Any, camera: str, frame_idx: int) -> Path | None:
    """Resolve the jpg path for a specific frame of a camera stream."""
    root = Path(episode.root)
    images_dir = root / "images" / camera
    if images_dir.exists():
        name = f"{frame_idx:06d}.jpg"
        p = images_dir / name
        if p.exists():
            return p
        # Try png
        p2 = images_dir / f"{frame_idx:06d}.png"
        if p2.exists():
            return p2
        # Fall back: sort and pick by index
        all_images = sorted(
            f for f in images_dir.iterdir()
            if f.suffix.lower() in {".jpg", ".jpeg", ".png"}
        )
        if frame_idx < len(all_images):
            return all_images[frame_idx]

    meta = getattr(episode, "meta", {}) or {}
    video_path = _video_image_path(
        root,
        camera,
        frame_idx,
        int(getattr(episode, "n_frames", 0) or 0),
        meta if isinstance(meta, dict) else {},
    )
    if video_path is not None:
        return video_path

    parquet_path = None
    if isinstance(meta, dict):
        raw_path = meta.get("raw_parquet_path")
        if raw_path:
            parquet_path = Path(str(raw_path)).expanduser()
    if parquet_path is not None:
        return _parquet_image_path(parquet_path, camera, frame_idx)
    return None


def _video_image_path(
    root: Path,
    camera: str,
    frame_idx: int,
    episode_n_frames: int,
    meta: dict[str, Any],
) -> Path | None:
    """Extract a key frame from videos/<camera>.mp4 into a reusable jpg cache."""
    video_path = root / "videos" / f"{camera}.mp4"
    if not video_path.exists():
        return None

    cache_dir = _video_cache_dir(root, camera, meta)
    cached = cache_dir / f"{frame_idx:06d}.jpg"
    if cached.exists():
        return cached

    try:
        import cv2  # type: ignore
    except ImportError:
        return None

    cap = cv2.VideoCapture(str(video_path))
    try:
        if not cap.isOpened():
            return None
        video_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        target_idx = _map_episode_frame_to_video_frame(frame_idx, episode_n_frames, video_frames)
        cap.set(cv2.CAP_PROP_POS_FRAMES, target_idx)
        ok, frame = cap.read()
        if not ok or frame is None:
            return None
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            if not cv2.imwrite(str(cached), frame):
                return None
        except OSError:
            return None
    finally:
        cap.release()
    return cached if cached.exists() else None


def _video_cache_dir(root: Path, camera: str, meta: dict[str, Any]) -> Path:
    processed_dir = meta.get("processed_dir")
    if processed_dir:
        output_cache = Path(str(processed_dir)).expanduser().resolve() / "video_keyframes" / camera
        try:
            output_cache.mkdir(parents=True, exist_ok=True)
            return output_cache
        except OSError:
            pass

    local_cache = root / ".video_keyframes" / camera
    try:
        local_cache.mkdir(parents=True, exist_ok=True)
        return local_cache
    except OSError:
        digest = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:16]
        return Path(tempfile.gettempdir()) / "agibot_video_keyframes" / digest / camera


def _map_episode_frame_to_video_frame(
    frame_idx: int,
    episode_n_frames: int,
    video_n_frames: int,
) -> int:
    if video_n_frames <= 0:
        return max(0, frame_idx)
    if episode_n_frames > 1:
        ratio = max(0.0, min(1.0, frame_idx / float(episode_n_frames - 1)))
        return max(0, min(video_n_frames - 1, round(ratio * (video_n_frames - 1))))
    return max(0, min(video_n_frames - 1, frame_idx))


def _parquet_image_path(parquet_path: Path, camera: str, frame_idx: int) -> Path | None:
    """Extract one embedded parquet image to a local cache and return its path."""
    parquet_path = parquet_path.resolve()
    if not parquet_path.exists():
        return None
    cache_dir = parquet_path.parent / ".parquet_keyframes" / parquet_path.stem / camera
    cached = cache_dir / f"{frame_idx:06d}.jpg"
    if cached.exists():
        return cached
    try:
        import pyarrow.parquet as pq  # type: ignore
    except ImportError:
        return None
    try:
        pf = pq.ParquetFile(parquet_path)
        names = set(pf.schema_arrow.names)
        if camera not in names:
            return None
        table = pf.read(columns=[camera])
        rows = table.column(camera).to_pylist()
        if frame_idx >= len(rows):
            return None
        item = rows[frame_idx]
    except Exception:
        return None

    if not isinstance(item, dict):
        return None
    embedded_path = item.get("path")
    if embedded_path:
        p = Path(str(embedded_path))
        if p.exists():
            return p
    data = item.get("bytes")
    if not data:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        cached.write_bytes(bytes(data))
    except OSError:
        return None
    return cached
