"""Small generated LeRobot fixture; no upstream robot runtime required."""
import json
from pathlib import Path
import numpy as np
import pandas as pd

CAMERAS = (
    "observation.images.hand_left_color",
    "observation.images.hand_head_color",
    "observation.images.hand_right_color",
)

def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

def make_dataset(path: Path) -> Path:
    info = {
        "codebase_version": "v2.1",
        "robot_type": "aloha",
        "total_episodes": 2,
        "total_frames": 300,
        "chunks_size": 1000,
        "fps": 30,
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": {
            "observation.state": {"dtype": "float32", "shape": [21]},
            "action": {"dtype": "float32", "shape": [18]},
            **{camera: {"dtype": "video", "shape": [64, 64, 3]} for camera in CAMERAS},
        },
    }
    prompts = (
        "Grasp Sprite with the left hand.",
        "Grasp Wanglaoji with the right hand.",
    )
    write_json(path / "meta" / "info.json", info)
    write_jsonl(
        path / "meta" / "tasks.jsonl",
        [{"task_index": index, "task": prompt} for index, prompt in enumerate(prompts)],
    )
    write_jsonl(
        path / "meta" / "episodes.jsonl",
        [
            {
                "episode_index": index,
                "task_index": index,
                "task": prompt,
                "tasks": [prompt],
                "length": 150,
                "items": [{"product_en": "Sprite" if index == 0 else "Wanglaoji"}],
            }
            for index, prompt in enumerate(prompts)
        ],
    )
    for episode_index in range(2):
        actions = np.full((150, 18), 0.1, dtype=np.float32)
        actions[:, 14:] = 0.0
        close_frame = 60 if episode_index == 0 else 90
        gripper_index = 6 if episode_index == 0 else 13
        actions[close_frame:, gripper_index] = 0.0
        frame = pd.DataFrame(
            {
                "action": list(actions),
                "episode_index": [episode_index] * 150,
                "frame_index": list(range(150)),
                "task_index": [episode_index] * 150,
            }
        )
        parquet = path / "data" / "chunk-000" / f"episode_{episode_index:06d}.parquet"
        parquet.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(parquet, index=False)
        for camera in CAMERAS:
            video = path / "videos" / "chunk-000" / camera / f"episode_{episode_index:06d}.mp4"
            video.parent.mkdir(parents=True, exist_ok=True)
            video.write_bytes(b"test-video")
    return path
