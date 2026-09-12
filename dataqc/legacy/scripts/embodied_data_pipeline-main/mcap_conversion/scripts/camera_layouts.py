#!/usr/bin/env python3
"""Named camera/video layouts shared by MCAP conversion entry points."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class CameraLayout:
    name: str
    video_sources: dict[str, Path]
    reference_video_names: tuple[str, ...]
    required_video_names: tuple[str, ...]
    required_timestamp_keys: tuple[str, ...]


THREE_CAMERA = CameraLayout(
    name="three_camera",
    video_sources={
        "head_color.mp4": Path("camera/color/front/sync.txt"),
        "hand_left_color.mp4": Path("camera/color/left/sync.txt"),
        "hand_right_color.mp4": Path("camera/color/right/sync.txt"),
    },
    reference_video_names=(
        "hand_left_color.mp4",
        "hand_right_color.mp4",
        "head_color.mp4",
        "head_depth.mp4",
    ),
    required_video_names=(),
    required_timestamp_keys=(),
)

THREE_CAMERA_FRONT = CameraLayout(
    name="three_camera_front",
    video_sources=dict(THREE_CAMERA.video_sources),
    reference_video_names=tuple(THREE_CAMERA.video_sources),
    required_video_names=tuple(THREE_CAMERA.video_sources),
    required_timestamp_keys=("head_color", "head_stereo_left", "head_stereo_right"),
)

THREE_CAMERA_GLOBAL = CameraLayout(
    name="three_camera_global",
    video_sources={
        "head.mp4": Path("camera/color/head/sync.txt"),
        "hand_left_color.mp4": Path("camera/color/left/sync.txt"),
        "hand_right_color.mp4": Path("camera/color/right/sync.txt"),
    },
    reference_video_names=("head.mp4", "hand_left_color.mp4", "hand_right_color.mp4"),
    required_video_names=("head.mp4", "hand_left_color.mp4", "hand_right_color.mp4"),
    required_timestamp_keys=("head", "head_stereo_left", "head_stereo_right"),
)

FOUR_CAMERA = CameraLayout(
    name="four_camera",
    video_sources={
        **THREE_CAMERA.video_sources,
        "head.mp4": Path("camera/color/head/sync.txt"),
    },
    reference_video_names=(*THREE_CAMERA.reference_video_names, "head.mp4"),
    required_video_names=(
        "head_color.mp4",
        "hand_left_color.mp4",
        "hand_right_color.mp4",
        "head.mp4",
    ),
    required_timestamp_keys=(
        "head_color",
        "head_stereo_left",
        "head_stereo_right",
        "head",
    ),
)

CAMERA_LAYOUTS = {
    THREE_CAMERA.name: THREE_CAMERA,
    THREE_CAMERA_FRONT.name: THREE_CAMERA_FRONT,
    THREE_CAMERA_GLOBAL.name: THREE_CAMERA_GLOBAL,
    FOUR_CAMERA.name: FOUR_CAMERA,
}


def get_camera_layout(name: str) -> CameraLayout:
    try:
        return CAMERA_LAYOUTS[str(name).strip().lower()]
    except KeyError as exc:
        choices = ", ".join(CAMERA_LAYOUTS)
        raise ValueError(f"Unknown camera layout {name!r}; expected one of: {choices}") from exc


def _video_has_expected_frames(video_path: Path, expected_frames: int) -> bool:
    try:
        import cv2  # type: ignore

        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            capture.release()
            return False
        reported_frames = int(round(capture.get(cv2.CAP_PROP_FRAME_COUNT)))
        if reported_frames != expected_frames:
            capture.release()
            return False
        positions = [0] if expected_frames == 1 else [0, expected_frames - 1]
        for position in positions:
            capture.set(cv2.CAP_PROP_POS_FRAMES, position)
            ok, frame = capture.read()
            if not ok or frame is None:
                capture.release()
                return False
        capture.release()
        return True
    except Exception:
        return False


def episode_artifacts_complete(episode_dir: Path, layout_name: str = "three_camera") -> bool:
    """Return whether an episode satisfies the selected camera artifact contract."""
    episode_dir = Path(episode_dir)
    layout = get_camera_layout(layout_name)
    h5_path = episode_dir / "states" / "aligned_joints.h5"
    meta_path = episode_dir / "meta" / "episode_meta.json"
    if not h5_path.is_file() or not meta_path.is_file():
        return False

    try:
        import h5py  # type: ignore

        with h5py.File(h5_path, "r") as file_obj:
            frame_keys = sorted(
                (str(key) for key in file_obj.keys() if str(key).isdigit()),
                key=int,
            )
            if not frame_keys:
                return False
            for frame_key in frame_keys:
                for timestamp_key in layout.required_timestamp_keys:
                    dataset_path = f"{frame_key}/timestamp/camera/{timestamp_key}"
                    if dataset_path not in file_obj or file_obj[dataset_path].size <= 0:
                        return False
    except Exception:
        return False

    # Preserve the existing three-camera completion behavior. Four-camera
    # layouts opt into the stronger artifact contract below.
    if not layout.required_video_names:
        return True

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(meta, dict):
        return False
    available_videos = meta.get("available_videos")
    if not isinstance(available_videos, list):
        return False
    video_info_by_name = {
        str(item.get("file")): item
        for item in available_videos
        if isinstance(item, dict) and item.get("file")
    }
    expected_frames = len(frame_keys)
    for video_name in layout.required_video_names:
        video_path = episode_dir / "videos" / video_name
        video_info = video_info_by_name.get(video_name)
        if not video_path.is_file() or video_path.stat().st_size <= 0 or video_info is None:
            return False
        try:
            metadata_frames = int(video_info.get("frame_count", -1))
        except (TypeError, ValueError):
            return False
        if metadata_frames != expected_frames:
            return False
        if not _video_has_expected_frames(video_path, expected_frames):
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate completed episode camera artifacts.")
    parser.add_argument("--episode-dir", type=Path, required=True)
    parser.add_argument("--camera-layout", choices=tuple(CAMERA_LAYOUTS), default="three_camera")
    args = parser.parse_args(argv)
    return 0 if episode_artifacts_complete(args.episode_dir, args.camera_layout) else 1


if __name__ == "__main__":
    raise SystemExit(main())
