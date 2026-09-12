"""Manual image screening for LeRobot episodes.

The module discovers real-world LeRobot datasets, locates the gripper-closing
frame for every episode, caches three timestamps from up to three camera views,
and stores operator corrections in one JSON document.  Source datasets are
read-only throughout the workflow.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import uuid
from typing import Any, Callable

import lerobot_cross_platform as cross_platform


DEFAULT_DATASET_ROOT = Path(
    os.environ.get("PIPELINE_MANUAL_SCREENING_DATA_ROOT")
    or "/srv/data/datasets/public/stage2_datasets"
).expanduser()
DEFAULT_STORAGE_ROOT = Path(
    os.environ.get("PIPELINE_MANUAL_SCREENING_STORAGE_ROOT")
    or Path(__file__).resolve().parent / "logs" / "lerobot_manual_screening"
).expanduser()
FRAME_OFFSET = 40
MAX_CAMERAS = 3
EXTRACTION_MANIFEST_VERSION = 2
GROUP_MANIFEST_VERSION = 3
RECORDS_FILENAME = "manual_screening_records.json"
GROUPED_GRADES = ("A", "B", "F")
QUALITY_GRADES = ("A", "B", "C", "F")
ERROR_LABELS = {
    "wrong_arm": "左右手错误",
    "wrong_object": "物品错误",
    "bad_trajectory": "轨迹不好",
}
_RECORDS_LOCK = threading.Lock()

PRODUCT_NAMES_ZH = {
    "Yakult": "养乐多",
    "Guangming Probiotic Milk": "光明益生菌风味发酵乳",
    "Coca-Cola": "可口可乐",
    "COSTA Peach Oolong": "COSTA 桃桃乌龙",
    "Taro Milk": "新希望芋泥牛乳",
    "Robuk Velvet Latte": "罗伯克丝绒拿铁",
    "Yili Peach Yogurt": "伊利黄桃果粒酸奶",
    "HK Orange Fanta": "港版芬达橙子味",
    "Aojiru": "伊藤园青汁",
    "NEVER Coconut Latte": "NeverCoffee 生椰拿铁咖啡",
    "Daily C Orange Juice": "味全每日C橙汁",
    "Daily C Grape Juice": "味全每日C葡萄汁",
    "COSTA Grape Jasmine": "COSTA 葡萄茉莉",
    "Wanglaoji": "王老吉",
    "AD Calcium Milk": "娃哈哈AD钙奶",
    "Yili Strawberry Yogurt": "伊利草莓果粒酸奶",
    "Dahongpao Milk Tea": "大红袍奶茶",
    "Sprite": "雪碧",
    "Royal Coconut": "椰树牌椰汁",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def dataset_id(path: Path) -> str:
    resolved = str(path.expanduser().resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:20]


def records_path(storage_root: Path | None = None) -> Path:
    return (storage_root or DEFAULT_STORAGE_ROOT).expanduser().resolve() / RECORDS_FILENAME


def manifest_path(dataset: Path, storage_root: Path | None = None) -> Path:
    root = (storage_root or DEFAULT_STORAGE_ROOT).expanduser().resolve()
    return root / "extracted" / dataset_id(dataset) / "manifest.json"


def group_manifest_path(group_id: str, storage_root: Path | None = None) -> Path:
    if not re.fullmatch(r"[0-9a-f]{20}", str(group_id)):
        raise ValueError(f"无效的数据集分组 ID: {group_id}")
    root = (storage_root or DEFAULT_STORAGE_ROOT).expanduser().resolve()
    return root / "extracted_groups" / str(group_id) / "manifest.json"


def _camera_label(key: str) -> str:
    lower = key.lower()
    if "left" in lower and ("hand" in lower or "wrist" in lower):
        return "左腕视角"
    if "right" in lower and ("hand" in lower or "wrist" in lower):
        return "右腕视角"
    if "head" in lower:
        return "头部视角"
    if "front" in lower:
        return "前视角"
    return key.rsplit(".", 1)[-1]


def _camera_order(key: str) -> tuple[int, str]:
    lower = key.lower()
    if "left" in lower and ("hand" in lower or "wrist" in lower):
        return 0, lower
    if "head" in lower:
        return 1, lower
    if "right" in lower and ("hand" in lower or "wrist" in lower):
        return 2, lower
    if "front" in lower:
        return 3, lower
    return 4, lower


def selected_camera_keys(info: dict[str, Any]) -> list[str]:
    keys = cross_platform.video_feature_keys(info)
    return sorted(keys, key=_camera_order)[:MAX_CAMERAS]


def discover_datasets(root: Path | None = None) -> dict[str, Any]:
    scan_root = (root or DEFAULT_DATASET_ROOT).expanduser().resolve()
    matches = cross_platform.discover_under(scan_root, recursive=True)
    grouped: dict[Path, list[dict[str, Any]]] = {}
    for path in matches:
        try:
            info = cross_platform.load_json(path / "meta" / "info.json")
            episodes = _episode_metadata(path)
            cameras = selected_camera_keys(info)
            grade = path.name.upper() if path.name.upper() in GROUPED_GRADES else ""
            logical_path = path.parent.resolve() if grade else path.resolve()
            grouped.setdefault(logical_path, []).append(
                {
                    "dataset_id": dataset_id(path),
                    "path": str(path),
                    "grade": grade,
                    "total_episodes": len(episodes),
                    "fps": info.get("fps"),
                    "camera_count": len(cross_platform.video_feature_keys(info)),
                    "cameras": [
                        {"key": key, "label": _camera_label(key)} for key in cameras
                    ],
                }
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue

    datasets: list[dict[str, Any]] = []
    grade_order = {grade: index for index, grade in enumerate(GROUPED_GRADES)}
    for logical_path, sources in grouped.items():
        sources.sort(
            key=lambda item: (
                grade_order.get(str(item.get("grade") or ""), len(GROUPED_GRADES)),
                str(item.get("path") or ""),
            )
        )
        grades = [str(item["grade"]) for item in sources if item.get("grade")]
        camera_map: dict[str, dict[str, str]] = {}
        for source in sources:
            for camera in source.get("cameras") or []:
                if isinstance(camera, dict) and camera.get("key"):
                    camera_map.setdefault(str(camera["key"]), camera)
        fps_values = list(dict.fromkeys(source.get("fps") for source in sources if source.get("fps") is not None))
        datasets.append(
            {
                "id": dataset_id(logical_path),
                "name": logical_path.name,
                "path": str(logical_path),
                "logical_path": str(logical_path),
                "grades": grades,
                "grade_label": "/".join(grades),
                "grouped_grades": len(grades) > 1,
                "physical_dataset_count": len(sources),
                "total_episodes": sum(int(item.get("total_episodes") or 0) for item in sources),
                "fps": fps_values[0] if len(fps_values) == 1 else None,
                "camera_count": len(camera_map),
                "cameras": sorted(camera_map.values(), key=lambda item: _camera_order(item["key"]))[:MAX_CAMERAS],
                "sources": sources,
            }
        )
    datasets.sort(key=lambda item: cross_platform.natural_key(str(item["path"])))
    return {
        "root": str(scan_root),
        "exists": scan_root.is_dir(),
        "datasets": datasets,
        "count": len(datasets),
        "record_file": str(records_path()),
    }


def find_dataset_group(group_id: str, root: Path | None = None) -> dict[str, Any]:
    payload = discover_datasets(root)
    group = next((item for item in payload["datasets"] if item.get("id") == group_id), None)
    if group is None:
        raise ValueError(f"没有找到 LeRobot 数据集分组: {group_id}")
    return group


def _episode_indices_from_data(path: Path) -> list[int]:
    indices: list[int] = []
    for parquet in path.glob("data/chunk-*/episode_*.parquet"):
        match = re.search(r"episode_(\d+)\.parquet$", parquet.name)
        if match:
            indices.append(int(match.group(1)))
    return sorted(set(indices))


def _episode_metadata(path: Path) -> list[dict[str, Any]]:
    rows = cross_platform.episode_rows(path)
    if rows:
        return sorted(
            (row for row in rows if cross_platform.as_int(row.get("episode_index")) is not None),
            key=lambda row: int(row.get("episode_index", 0)),
        )
    return [{"episode_index": index} for index in _episode_indices_from_data(path)]


def _task_for_episode(path: Path, row: dict[str, Any]) -> str:
    task = str(row.get("task") or "").strip()
    for key in ("full_instructions_en", "tasks"):
        values = row.get(key)
        if not task and isinstance(values, list) and values:
            task = str(values[0]).strip()
    if task:
        return task
    task_index = cross_platform.as_int(row.get("task_index"))
    for task_row in cross_platform.task_rows(path):
        if cross_platform.as_int(task_row.get("task_index")) == task_index:
            return str(task_row.get("task") or "").strip()
    return ""


def _prompt_arms(prompt: str) -> list[str]:
    found = {
        match.group(1).lower()
        for match in re.finditer(
            r"\b(left|right)(?:[- ]?hand|\s+arm)\b",
            prompt,
            re.IGNORECASE,
        )
    }
    return [arm for arm in ("left", "right") if arm in found]


def _prompt_arm(prompt: str) -> str:
    arms = _prompt_arms(prompt)
    if arms:
        return "both" if len(arms) == 2 else arms[0]
    legacy = re.search(r"\b(left|right)\b", prompt, re.IGNORECASE)
    return legacy.group(1).lower() if legacy else ""


def _prompt_objects(prompt: str, row: dict[str, Any]) -> list[str]:
    output: list[str] = []
    items = row.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            value = str(item.get("product_en") or item.get("name") or item.get("product_cn") or "").strip()
            if value and value not in output:
                output.append(value)
    lower = prompt.lower()
    for name in sorted(cross_platform.KNOWN_ITEMS, key=len, reverse=True):
        if name.lower() in lower and name not in output:
            output.append(name)
    if not output:
        for match in re.finditer(
            r"\b(?:grasp|grab|pick up|pickup)\s+(.+?)\s+(?:with|using)\s+(?:the\s+)?(?:left|right)",
            prompt,
            re.IGNORECASE,
        ):
            value = match.group(1).strip(" .")
            if value and value not in output:
                output.append(value)
    return output


def _prompt_object_details(
    prompt: str,
    row: dict[str, Any],
) -> list[dict[str, str]]:
    metadata_names_zh: dict[str, str] = {}
    items = row.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            name_zh = str(item.get("product_cn") or "").strip()
            if not name_zh:
                continue
            for key in ("product_en", "name", "product_cn"):
                name = str(item.get(key) or "").strip()
                if name:
                    metadata_names_zh[name] = name_zh
    return [
        {
            "name": name,
            # Standard mappings intentionally take precedence because a small
            # number of historic episode metadata rows contain swapped CN names.
            "name_zh": PRODUCT_NAMES_ZH.get(name) or metadata_names_zh.get(name) or "未提供中文名",
        }
        for name in _prompt_objects(prompt, row)
    ]


def _objects_by_arm(prompt: str, object_details: list[dict[str, str]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for arm in _prompt_arms(prompt):
        associated = [
            str(item.get("name") or "")
            for item in object_details
            if item.get("name")
            and re.search(
                re.escape(str(item["name"]))
                + rf"\s+\b(?:with|using)\s+(?:the\s+)?{arm}(?:[- ]?hand|\s+arm)?\b",
                prompt,
                re.IGNORECASE,
            )
        ]
        if associated:
            result[arm] = associated
    return result


def _format_path(pattern: str, episode_index: int, chunks_size: int, video_key: str = "") -> str:
    return pattern.format(
        episode_chunk=episode_index // max(1, chunks_size),
        episode_index=episode_index,
        video_key=video_key,
    )


def _data_file(path: Path, info: dict[str, Any], episode_index: int) -> Path:
    chunks_size = int(info.get("chunks_size") or 1000)
    pattern = str(
        info.get("data_path")
        or "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet"
    )
    candidate = (path / _format_path(pattern, episode_index, chunks_size)).resolve()
    candidate.relative_to(path)
    if candidate.is_file():
        return candidate
    matches = list(path.glob(f"data/chunk-*/episode_{episode_index:06d}.parquet"))
    if not matches:
        raise FileNotFoundError(f"episode_{episode_index:06d} parquet 不存在")
    return matches[0].resolve()


def _video_files_from_mapping(path: Path, episode_index: int) -> dict[str, str]:
    mapping_path = path / "meta" / "episode_name_mapping.json"
    if not mapping_path.is_file():
        return {}
    try:
        payload = cross_platform.load_json(mapping_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    rows = payload.get("episodes") if isinstance(payload.get("episodes"), list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if cross_platform.as_int(row.get("lerobot_episode_index")) != episode_index:
            continue
        files = row.get("lerobot_video_files")
        if isinstance(files, dict):
            return {str(key): str(value) for key, value in files.items()}
    return {}


def _video_file(path: Path, info: dict[str, Any], episode_index: int, video_key: str) -> Path:
    mapped = _video_files_from_mapping(path, episode_index).get(video_key)
    chunks_size = int(info.get("chunks_size") or 1000)
    pattern = str(
        info.get("video_path")
        or "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4"
    )
    rel = mapped or _format_path(pattern, episode_index, chunks_size, video_key)
    candidate = (path / rel).resolve()
    candidate.relative_to(path)
    if not candidate.is_file():
        raise FileNotFoundError(f"{video_key} 视频不存在")
    return candidate


def _action_array(parquet: Path, action_key: str) -> Any:
    import numpy as np  # type: ignore
    import pandas as pd  # type: ignore

    frame = pd.read_parquet(parquet, columns=[action_key])
    values = frame[action_key].tolist()
    array = np.asarray(values, dtype=float)
    if array.ndim != 2 or not len(array):
        raise ValueError(f"action 数据为空或形状异常: {parquet}")
    return array


def locate_closure(action: Any, prompt_arm: str) -> dict[str, Any]:
    import numpy as np  # type: ignore

    if action.ndim == 2 and action.shape[1] == 23:
        from dataqc.motion import closure_events
        events = {hand: closure_events(action[:, index], .1, .8) for hand, index in (("left",7),("right",15))}
        hand = prompt_arm if prompt_arm in events else next((h for h in events if events[h]), "left")
        closes = events[hand]
        warning = "" if len(closes)==1 else f"{hand} 夹爪检测到 {len(closes)} 次闭合，需人工确认；关键帧仅供查看"
        at = closes[0] if closes else max(0,len(action)//2)
        return dict(arm=hand,frame_index=at,drop=0.0,warning=warning,scores={h:len(v) for h,v in events.items()},close_frames=closes)
    candidates: list[tuple[str, int]] = []
    for side, index in (("left", 6), ("right", 13)):
        if action.ndim == 2 and action.shape[1] > index:
            candidates.append((side, index))
    if not candidates:
        raise ValueError("action 中缺少夹爪维度 action[6]/action[13]")

    scores: dict[str, tuple[float, int]] = {}
    for side, index in candidates:
        values = np.asarray(action[:, index], dtype=float)
        deltas = np.diff(values)
        finite = np.where(np.isfinite(deltas), deltas, np.inf)
        if not len(finite):
            scores[side] = (0.0, 0)
            continue
        at = int(np.argmin(finite))
        scores[side] = (float(finite[at]), at + 1)

    strongest_arm = min(scores, key=lambda side: scores[side][0])
    preferred_has_close = prompt_arm in scores and scores[prompt_arm][0] < -1e-4
    detected_arm = prompt_arm if preferred_has_close else strongest_arm
    drop, frame_index = scores[detected_arm]
    warning = ""
    if prompt_arm in scores and detected_arm != prompt_arm and drop < -1e-4:
        warning = (
            f"Prompt 标注为 {prompt_arm}，但动作中检测到 {detected_arm} 夹爪闭合，"
            "请重点核对左右手是否错误"
        )
    elif not drop < -1e-4:
        frame_index = max(0, len(action) // 2)
        warning = f"{detected_arm} 夹爪未检测到明确闭合下降，暂用 episode 中间帧"
    return {
        "arm": detected_arm,
        "frame_index": frame_index,
        "drop": drop,
        "warning": warning,
        "scores": {side: value[0] for side, value in scores.items()},
    }


def locate_prompt_closures(action: Any, prompt_arms: list[str]) -> list[dict[str, Any]]:
    """Locate one exact closure per arm for bimanual prompts, or preserve single-arm fallback."""
    import numpy as np  # type: ignore

    arms = [arm for arm in ("left", "right") if arm in prompt_arms]
    if action.ndim == 2 and action.shape[1] == 23:
        return [locate_closure(action, arm) for arm in arms or ["left", "right"]]
    if len(arms) < 2:
        preferred = arms[0] if arms else ""
        return [locate_closure(action, preferred)]

    closures: list[dict[str, Any]] = []
    scores: dict[str, float] = {}
    for arm, index in (("left", 6), ("right", 13)):
        if action.ndim != 2 or action.shape[1] <= index:
            raise ValueError(f"双手 Prompt 需要 {arm} 夹爪维度 action[{index}]")
        values = np.asarray(action[:, index], dtype=float)
        deltas = np.diff(values)
        finite = np.where(np.isfinite(deltas), deltas, np.inf)
        if len(finite):
            at = int(np.argmin(finite))
            drop = float(finite[at])
            frame_index = at + 1
        else:
            drop = 0.0
            frame_index = max(0, len(action) // 2)
        warning = ""
        if not drop < -1e-4:
            frame_index = max(0, len(action) // 2)
            warning = f"双手任务中 {arm} 夹爪未检测到明确闭合下降，暂用 episode 中间帧"
        scores[arm] = drop
        closures.append(
            {
                "arm": arm,
                "frame_index": frame_index,
                "drop": drop,
                "warning": warning,
            }
        )
    for closure in closures:
        closure["scores"] = dict(scores)
    return closures


def _safe_camera_name(key: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", key).strip("_") or "camera"


def _extract_video_frame(video: Path, frame_index: int, target: Path) -> None:
    import cv2  # type: ignore

    capture = cv2.VideoCapture(str(video))
    try:
        if not capture.isOpened():
            raise ValueError(f"无法打开视频: {video}")
        capture.set(cv2.CAP_PROP_POS_FRAMES, int(frame_index))
        ok, image = capture.read()
        if not ok or image is None:
            raise ValueError(f"无法读取 {video.name} 第 {frame_index} 帧")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(target), image, [int(cv2.IMWRITE_JPEG_QUALITY), 90]):
            raise ValueError(f"无法写入截帧: {target}")
    finally:
        capture.release()


def _image_url(dataset_key: str, relative: Path) -> str:
    return f"/manual-screening-image/{dataset_key}/{relative.as_posix()}"


def _extract_grasp_frames(
    dataset: Path,
    info: dict[str, Any],
    episode_index: int,
    dataset_key: str,
    extract_root: Path,
    camera_keys: list[str],
    closure: dict[str, Any],
    frame_count: int,
    frame_offset: int,
    warnings: list[str],
    *,
    include_arm_directory: bool,
) -> dict[str, Any]:
    arm = str(closure.get("arm") or "")
    close_frame = int(closure["frame_index"])
    targets = (
        ("before", f"前 {frame_offset} 帧", max(0, close_frame - frame_offset)),
        ("close", "夹取时刻", min(frame_count - 1, close_frame)),
        ("after", f"后 {frame_offset} 帧", min(frame_count - 1, close_frame + frame_offset)),
    )
    frames: dict[str, Any] = {}
    for moment, label, frame_index in targets:
        images: list[dict[str, Any]] = []
        for camera_key in camera_keys:
            try:
                video = _video_file(dataset, info, episode_index, camera_key)
                relative_parts = [f"episode_{episode_index:06d}"]
                if include_arm_directory:
                    relative_parts.append(arm)
                relative = Path(*relative_parts) / moment / f"{_safe_camera_name(camera_key)}.jpg"
                target = extract_root / relative
                _extract_video_frame(video, frame_index, target)
                images.append(
                    {
                        "camera_key": camera_key,
                        "camera_label": _camera_label(camera_key),
                        "url": _image_url(dataset_key, relative),
                    }
                )
            except Exception as exc:
                arm_label = f"{arm}手 / " if include_arm_directory else ""
                warnings.append(f"{arm_label}{label} / {_camera_label(camera_key)}: {exc}")
        frames[moment] = {
            "label": label,
            "frame_index": frame_index,
            "images": images,
        }
    return frames


def _episode_has_frames(episode: dict[str, Any]) -> bool:
    grasps = episode.get("grasps") if isinstance(episode.get("grasps"), dict) else {}
    if grasps:
        return all(
            bool(grasp.get("frames", {}).get("close", {}).get("images"))
            for grasp in grasps.values()
            if isinstance(grasp, dict)
        )
    return bool(episode.get("frames", {}).get("close", {}).get("images"))


def extract_dataset(
    dataset: Path,
    *,
    storage_root: Path | None = None,
    frame_offset: int = FRAME_OFFSET,
    progress: Callable[[str], None] | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    dataset = dataset.expanduser().resolve()
    if not cross_platform.is_lerobot_dataset(dataset):
        raise ValueError(f"不是有效的 LeRobot 数据集: {dataset}")
    storage = (storage_root or DEFAULT_STORAGE_ROOT).expanduser().resolve()
    info = cross_platform.load_json(dataset / "meta" / "info.json")
    _, action_key = cross_platform.vector_feature_keys(info)
    if not action_key:
        raise ValueError("LeRobot info.json 中没有 action feature")
    camera_keys = selected_camera_keys(info)
    if not camera_keys:
        raise ValueError("LeRobot 数据集没有可截取的视频 feature")
    rows = _episode_metadata(dataset)
    if not rows:
        raise ValueError("LeRobot 数据集没有 episode")

    key = dataset_id(dataset)
    extract_root = storage / "extracted" / key
    episodes: list[dict[str, Any]] = []
    total = len(rows)
    for ordinal, row in enumerate(rows, start=1):
        if stop_requested and stop_requested():
            raise RuntimeError("人工筛查截帧任务已停止")
        episode_index = int(row["episode_index"])
        prompt = _task_for_episode(dataset, row)
        declared_arms = _prompt_arms(prompt)
        declared_arm = _prompt_arm(prompt)
        warnings: list[str] = []
        episode_result: dict[str, Any] = {
            "episode_index": episode_index,
            "episode_name": f"episode_{episode_index:06d}",
            "prompt": prompt,
            "original": {
                "arm": declared_arm,
                "objects": [],
                "objects_zh": [],
                "object_details": [],
            },
            "closure": {},
            "frames": {},
            "warnings": warnings,
        }
        object_details = _prompt_object_details(prompt, row)
        episode_result["original"]["objects"] = [item["name"] for item in object_details]
        episode_result["original"]["objects_zh"] = [item["name_zh"] for item in object_details]
        episode_result["original"]["object_details"] = object_details
        if len(declared_arms) == 2:
            episode_result["grasp_mode"] = "bimanual"
            episode_result["original"]["arms"] = declared_arms
            episode_result["original"]["objects_by_arm"] = _objects_by_arm(prompt, object_details)
        else:
            episode_result["grasp_mode"] = "single"
        try:
            parquet = _data_file(dataset, info, episode_index)
            action = _action_array(parquet, action_key)
            frame_count = int(len(action))
            closures = locate_prompt_closures(action, declared_arms)
            episode_result["grasps"] = {}
            is_bimanual = len(closures) == 2
            for closure in closures:
                if closure["warning"]:
                    warnings.append(str(closure["warning"]))
                closure_payload = {
                    "arm": closure["arm"],
                    "frame_index": int(closure["frame_index"]),
                    "gripper_drop": closure["drop"],
                    "frame_count": frame_count,
                }
                frames = _extract_grasp_frames(
                    dataset,
                    info,
                    episode_index,
                    key,
                    extract_root,
                    camera_keys,
                    closure,
                    frame_count,
                    frame_offset,
                    warnings,
                    include_arm_directory=is_bimanual,
                )
                episode_result["grasps"][closure["arm"]] = {
                    "arm": closure["arm"],
                    "closure": closure_payload,
                    "frames": frames,
                }
            primary_arm = declared_arms[0] if declared_arms else closures[0]["arm"]
            primary = episode_result["grasps"].get(primary_arm) or next(
                iter(episode_result["grasps"].values())
            )
            # Retain the legacy fields for records and older UI consumers.
            episode_result["closure"] = primary["closure"]
            episode_result["frames"] = primary["frames"]
        except Exception as exc:
            warnings.append(str(exc))
        episodes.append(episode_result)
        if progress:
            progress(
                f"截帧 {ordinal}/{total}: episode_{episode_index:06d}"
                + (f"（{len(warnings)} 条告警）" if warnings else "")
            )

    payload = {
        "version": EXTRACTION_MANIFEST_VERSION,
        "generated_at": _utc_now(),
        "dataset_id": key,
        "dataset_name": dataset.name,
        "dataset_path": str(dataset),
        "frame_offset": frame_offset,
        "camera_keys": camera_keys,
        "camera_labels": {key: _camera_label(key) for key in camera_keys},
        "episode_count": len(episodes),
        "successful_episode_count": sum(
            _episode_has_frames(item) for item in episodes
        ),
        "episodes": episodes,
        "record_file": str(records_path(storage)),
    }
    _atomic_json(manifest_path(dataset, storage), payload)
    return payload


def extract_dataset_group(
    group: dict[str, Any],
    *,
    storage_root: Path | None = None,
    frame_offset: int = FRAME_OFFSET,
    progress: Callable[[str], None] | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    group_id = str(group.get("id") or "")
    logical_path = Path(str(group.get("logical_path") or group.get("path") or "")).expanduser().resolve()
    sources = group.get("sources") if isinstance(group.get("sources"), list) else []
    if not group_id or not sources:
        raise ValueError("LeRobot 数据集分组没有可处理的 A/B/F 数据源")

    episodes: list[dict[str, Any]] = []
    source_results: list[dict[str, Any]] = []
    camera_keys: list[str] = []
    for source_ordinal, source in enumerate(sources, start=1):
        if not isinstance(source, dict):
            continue
        if stop_requested and stop_requested():
            raise RuntimeError("人工筛查截帧任务已停止")
        source_path = Path(str(source.get("path") or "")).expanduser().resolve()
        grade = str(source.get("grade") or "")
        source_label = grade or source_path.name
        if progress:
            progress(
                f"开始处理 {source_label}（{source_ordinal}/{len(sources)}）: {source_path}"
            )
        result = extract_dataset(
            source_path,
            storage_root=storage_root,
            frame_offset=frame_offset,
            progress=(
                (lambda line, label=source_label: progress(f"[{label}] {line}"))
                if progress
                else None
            ),
            stop_requested=stop_requested,
        )
        camera_keys.extend(str(key) for key in result.get("camera_keys") or [])
        source_results.append(
            {
                "dataset_id": result["dataset_id"],
                "dataset_path": result["dataset_path"],
                "grade": grade,
                "episode_count": result["episode_count"],
                "successful_episode_count": result["successful_episode_count"],
            }
        )
        for episode in result.get("episodes") or []:
            if not isinstance(episode, dict):
                continue
            episode["dataset_id"] = result["dataset_id"]
            episode["dataset_name"] = result["dataset_name"]
            episode["dataset_path"] = result["dataset_path"]
            episode["source_grade"] = grade
            episode["episode_key"] = f"{result['dataset_id']}:{int(episode['episode_index'])}"
            episodes.append(episode)

    if not source_results:
        raise ValueError("LeRobot 数据集分组没有可处理的数据源")
    storage = (storage_root or DEFAULT_STORAGE_ROOT).expanduser().resolve()
    payload = {
        "version": GROUP_MANIFEST_VERSION,
        "generated_at": _utc_now(),
        "dataset_id": group_id,
        "dataset_name": str(group.get("name") or logical_path.name),
        "dataset_path": str(logical_path),
        "logical_path": str(logical_path),
        "grades": list(group.get("grades") or []),
        "grade_label": str(group.get("grade_label") or ""),
        "frame_offset": frame_offset,
        "camera_keys": list(dict.fromkeys(camera_keys)),
        "episode_count": len(episodes),
        "successful_episode_count": sum(
            _episode_has_frames(item) for item in episodes
        ),
        "sources": source_results,
        "episodes": episodes,
        "record_file": str(records_path(storage)),
    }
    _atomic_json(group_manifest_path(group_id, storage), payload)
    return payload


def load_manifest(dataset: Path, storage_root: Path | None = None) -> dict[str, Any] | None:
    path = manifest_path(dataset.expanduser().resolve(), storage_root)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"截帧清单格式错误: {path}")
    if int(payload.get("version") or 0) != EXTRACTION_MANIFEST_VERSION:
        return None
    if int(payload.get("frame_offset") or 0) != FRAME_OFFSET:
        return None
    return payload


def load_group_manifest(
    group: dict[str, Any],
    storage_root: Path | None = None,
) -> dict[str, Any] | None:
    group_id = str(group.get("id") or "")
    path = group_manifest_path(group_id, storage_root)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"分组截帧清单格式错误: {path}")
    if int(payload.get("version") or 0) != GROUP_MANIFEST_VERSION:
        return None
    if int(payload.get("frame_offset") or 0) != FRAME_OFFSET:
        return None
    current_sources = {
        str(Path(str(item.get("path") or "")).expanduser().resolve())
        for item in group.get("sources") or []
        if isinstance(item, dict) and item.get("path")
    }
    cached_sources = {
        str(Path(str(item.get("dataset_path") or "")).expanduser().resolve())
        for item in payload.get("sources") or []
        if isinstance(item, dict) and item.get("dataset_path")
    }
    if current_sources != cached_sources:
        return None
    return payload


def _empty_records(path: Path) -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": _utc_now(),
        "record_file": str(path),
        "records": [],
    }


def load_records(storage_root: Path | None = None) -> dict[str, Any]:
    path = records_path(storage_root)
    with _RECORDS_LOCK:
        if not path.is_file():
            return _empty_records(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise ValueError(f"人工筛查记录格式错误: {path}")
    payload["record_file"] = str(path)
    return payload


def records_for_dataset(dataset: Path, storage_root: Path | None = None) -> dict[str, Any]:
    return records_for_datasets([dataset], storage_root)


def records_for_datasets(
    datasets: list[Path],
    storage_root: Path | None = None,
) -> dict[str, Any]:
    dataset_paths = {str(dataset.expanduser().resolve()) for dataset in datasets}
    payload = load_records(storage_root)
    return {
        **payload,
        "records": [
            row
            for row in payload.get("records", [])
            if isinstance(row, dict) and row.get("dataset_path") in dataset_paths
        ],
    }


def records_for_group(
    group: dict[str, Any],
    storage_root: Path | None = None,
) -> dict[str, Any]:
    datasets = [
        Path(str(item.get("path") or ""))
        for item in group.get("sources") or []
        if isinstance(item, dict) and item.get("path")
    ]
    return records_for_datasets(datasets, storage_root)


def save_record(
    dataset: Path,
    episode_index: int,
    error_types: list[Any],
    corrections: dict[str, Any],
    *,
    storage_root: Path | None = None,
) -> dict[str, Any]:
    dataset = dataset.expanduser().resolve()
    manifest = load_manifest(dataset, storage_root)
    if not manifest:
        raise ValueError("请先点击“截取图片”生成该数据集的 episode 截帧")
    episode = next(
        (
            row
            for row in manifest.get("episodes", [])
            if isinstance(row, dict) and int(row.get("episode_index", -1)) == int(episode_index)
        ),
        None,
    )
    if episode is None:
        raise ValueError(f"截帧清单中没有 episode_{episode_index:06d}")
    selected = list(
        dict.fromkeys(
            str(item).strip()
            for item in error_types
            if str(item).strip() in {"wrong_arm", "wrong_object"}
        )
    )
    if not selected:
        raise ValueError("请至少选择“左右手错误”或“物品错误”")
    corrected_arm = str(corrections.get("arm") or "").strip().lower()
    corrected_object = str(corrections.get("object") or "").strip()
    corrected_objects_by_arm = (
        corrections.get("objects_by_arm")
        if isinstance(corrections.get("objects_by_arm"), dict)
        else {}
    )
    corrected_left_object = str(
        corrected_objects_by_arm.get("left") or corrections.get("left_object") or ""
    ).strip()
    corrected_right_object = str(
        corrected_objects_by_arm.get("right") or corrections.get("right_object") or ""
    ).strip()
    is_bimanual = episode.get("grasp_mode") == "bimanual"
    if "wrong_arm" in selected and corrected_arm not in {"left", "right", "both"}:
        raise ValueError("左右手错误时必须选择正确操作臂")
    if (
        "wrong_object" in selected
        and is_bimanual
        and (not corrected_left_object or not corrected_right_object)
    ):
        raise ValueError("双手物品错误时必须分别填写左手和右手的正确物品")
    if "wrong_object" in selected and not is_bimanual and not corrected_object:
        raise ValueError("物品错误时必须选择正确物品")

    original = episode.get("original") if isinstance(episode.get("original"), dict) else {}
    closure = episode.get("closure") if isinstance(episode.get("closure"), dict) else {}
    now = _utc_now()
    record_key = f"{dataset_id(dataset)}:{int(episode_index)}"
    new_record = {
        "record_id": record_key,
        "dataset_id": dataset_id(dataset),
        "dataset_name": manifest.get("dataset_name") or dataset.name,
        "dataset_path": str(dataset),
        "episode_index": int(episode_index),
        "episode_name": f"episode_{int(episode_index):06d}",
        "prompt": str(episode.get("prompt") or ""),
        "closure_frame": closure.get("frame_index"),
        "error_types": selected,
        "error_labels": [
            {"wrong_arm": "左右手错误", "wrong_object": "物品错误"}[item]
            for item in selected
        ],
        "original": {
            "arm": str(original.get("arm") or ""),
            "objects": list(original.get("objects") or []),
            "objects_zh": list(original.get("objects_zh") or []),
            "object_details": list(original.get("object_details") or []),
            **(
                {
                    "arms": list(original.get("arms") or []),
                    "objects_by_arm": dict(original.get("objects_by_arm") or {}),
                }
                if episode.get("grasp_mode") == "bimanual"
                else {}
            ),
        },
        "detected": {
            "arm": str(closure.get("arm") or ""),
            "closure_frame": closure.get("frame_index"),
            **(
                {
                    "grasps": {
                        str(arm): dict(grasp.get("closure") or {})
                        for arm, grasp in (episode.get("grasps") or {}).items()
                        if isinstance(grasp, dict)
                    }
                }
                if episode.get("grasp_mode") == "bimanual"
                else {}
            ),
        },
        "corrections": {
            "arm": corrected_arm if "wrong_arm" in selected else "",
            "object": corrected_object if "wrong_object" in selected else "",
            **(
                {
                    "objects_by_arm": {
                        "left": corrected_left_object if "wrong_object" in selected else "",
                        "right": corrected_right_object if "wrong_object" in selected else "",
                    }
                }
                if is_bimanual
                else {}
            ),
        },
        "updated_at": now,
    }
    path = records_path(storage_root)
    with _RECORDS_LOCK:
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
                raise ValueError(f"人工筛查记录格式错误: {path}")
        else:
            payload = _empty_records(path)
        existing = next(
            (
                row
                for row in payload["records"]
                if isinstance(row, dict) and row.get("record_id") == record_key
            ),
            None,
        )
        if existing:
            # The replay workspace can also annotate trajectory quality and grade.
            # Keep those fields when the image-screening page updates arm/object
            # corrections, because that page intentionally edits only its own two
            # issue types.
            if "bad_trajectory" in existing.get("error_types", []):
                new_record["error_types"].append("bad_trajectory")
                new_record["error_labels"].append(ERROR_LABELS["bad_trajectory"])
            for field in (
                "quality_grade",
                "source_quality_grade",
                "review_note",
                "record_source",
            ):
                if field in existing:
                    new_record[field] = existing[field]
            new_record["created_at"] = existing.get("created_at") or now
            existing.clear()
            existing.update(new_record)
        else:
            new_record["created_at"] = now
            payload["records"].append(new_record)
        payload["updated_at"] = now
        payload["record_file"] = str(path)
        payload["records"].sort(
            key=lambda row: (str(row.get("dataset_path") or ""), int(row.get("episode_index") or 0))
        )
        _atomic_json(path, payload)
    return {"ok": True, "record": new_record, "record_file": str(path)}


def save_visualization_record(
    dataset: Path,
    episode_index: int,
    error_types: list[Any],
    corrections: dict[str, Any],
    *,
    quality_grade: Any = "",
    review_note: Any = "",
    episode_info: dict[str, Any] | None = None,
    storage_root: Path | None = None,
) -> dict[str, Any]:
    """Upsert a replay-screening annotation in the unified records JSON.

    Unlike :func:`save_record`, this path does not require cached screenshots;
    the replay page has already validated the source LeRobot dataset and episode.
    """

    dataset = dataset.expanduser().resolve()
    if not cross_platform.is_lerobot_dataset(dataset):
        raise ValueError(f"不是 LeRobot 数据集: {dataset}")
    info = episode_info if isinstance(episode_info, dict) else {}
    info_index = cross_platform.as_int(info.get("episode_index"))
    if info_index is not None and info_index != int(episode_index):
        raise ValueError("Episode 信息与待记录编号不一致")

    selected = list(
        dict.fromkeys(
            str(item).strip()
            for item in error_types
            if str(item).strip() in ERROR_LABELS
        )
    )
    grade = str(quality_grade or "").strip().upper()
    if grade and grade not in QUALITY_GRADES:
        raise ValueError(f"无效的筛选等级: {quality_grade}")
    note = str(review_note or "").strip()
    if not selected and not grade and not note:
        raise ValueError("请至少选择筛选等级或一个问题类型")

    corrected_arm = str(corrections.get("arm") or "").strip().lower()
    corrected_object = str(corrections.get("object") or "").strip()
    corrected_objects_by_arm = (
        corrections.get("objects_by_arm")
        if isinstance(corrections.get("objects_by_arm"), dict)
        else {}
    )
    corrected_left_object = str(corrected_objects_by_arm.get("left") or "").strip()
    corrected_right_object = str(corrected_objects_by_arm.get("right") or "").strip()
    if "wrong_arm" in selected and corrected_arm not in {"left", "right", "both"}:
        raise ValueError("左右手错误时必须选择正确操作臂")
    if (
        "wrong_object" in selected
        and not corrected_object
        and not corrected_left_object
        and not corrected_right_object
    ):
        raise ValueError("物品错误时必须填写正确物品")

    prompt = str(info.get("task") or "")
    object_details = _prompt_object_details(prompt, info)
    objects = [str(item.get("name") or "") for item in object_details if item.get("name")]
    objects_zh = [str(item.get("name_zh") or "") for item in object_details if item.get("name_zh")]
    original_arm = _prompt_arm(prompt)
    source_grade = str(info.get("quality_grade") or "").strip().upper()
    if source_grade not in QUALITY_GRADES:
        directory_grade = dataset.name.upper()
        source_grade = directory_grade if directory_grade in QUALITY_GRADES else ""
    now = _utc_now()
    record_key = f"{dataset_id(dataset)}:{int(episode_index)}"
    corrections_payload: dict[str, Any] = {
        "arm": corrected_arm if "wrong_arm" in selected else "",
        "object": corrected_object if "wrong_object" in selected else "",
    }
    if corrected_left_object or corrected_right_object:
        corrections_payload["objects_by_arm"] = {
            "left": corrected_left_object if "wrong_object" in selected else "",
            "right": corrected_right_object if "wrong_object" in selected else "",
        }
    original: dict[str, Any] = {
        "arm": original_arm,
        "objects": objects,
        "objects_zh": objects_zh,
        "object_details": object_details,
    }
    if original_arm == "both":
        original["arms"] = _prompt_arms(prompt)
        original["objects_by_arm"] = _objects_by_arm(prompt, object_details)
    new_record = {
        "record_id": record_key,
        "dataset_id": dataset_id(dataset),
        "dataset_name": dataset.name,
        "dataset_path": str(dataset),
        "episode_index": int(episode_index),
        "episode_name": str(info.get("episode_name") or f"episode_{int(episode_index):06d}"),
        "prompt": prompt,
        "closure_frame": None,
        "quality_grade": grade,
        "source_quality_grade": source_grade,
        "error_types": selected,
        "error_labels": [ERROR_LABELS[item] for item in selected],
        "original": original,
        "detected": {},
        "corrections": corrections_payload,
        "review_note": note,
        "record_source": "lerobot_visualization",
        "updated_at": now,
    }

    path = records_path(storage_root)
    with _RECORDS_LOCK:
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
                raise ValueError(f"人工筛查记录格式错误: {path}")
        else:
            payload = _empty_records(path)
        existing = next(
            (
                row
                for row in payload["records"]
                if isinstance(row, dict) and row.get("record_id") == record_key
            ),
            None,
        )
        new_record["created_at"] = existing.get("created_at") or now if existing else now
        if existing:
            new_record["detected"] = existing.get("detected", {})
            new_record["closure_frame"] = existing.get("closure_frame")
            existing.clear()
            existing.update(new_record)
        else:
            payload["records"].append(new_record)
        payload["updated_at"] = now
        payload["record_file"] = str(path)
        payload["records"].sort(
            key=lambda row: (str(row.get("dataset_path") or ""), int(row.get("episode_index") or 0))
        )
        _atomic_json(path, payload)
    return {"ok": True, "record": new_record, "record_file": str(path)}


def delete_records(
    record_ids: list[Any],
    storage_root: Path | None = None,
) -> dict[str, Any]:
    """Delete selected records atomically from the unified JSON document."""

    selected = list(dict.fromkeys(str(item).strip() for item in record_ids if str(item).strip()))
    if not selected:
        raise ValueError("请选择需要删除的记录")
    selected_set = set(selected)
    path = records_path(storage_root)
    with _RECORDS_LOCK:
        if not path.is_file():
            return {
                "ok": True,
                "deleted_count": 0,
                "deleted_record_ids": [],
                "missing_record_ids": selected,
                "record_file": str(path),
            }
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
            raise ValueError(f"人工筛查记录格式错误: {path}")
        existing_ids = {
            str(row.get("record_id") or "")
            for row in payload["records"]
            if isinstance(row, dict)
        }
        deleted_ids = [item for item in selected if item in existing_ids]
        payload["records"] = [
            row
            for row in payload["records"]
            if not isinstance(row, dict) or str(row.get("record_id") or "") not in selected_set
        ]
        if deleted_ids:
            payload["updated_at"] = _utc_now()
            payload["record_file"] = str(path)
            _atomic_json(path, payload)
    return {
        "ok": True,
        "deleted_count": len(deleted_ids),
        "deleted_record_ids": deleted_ids,
        "missing_record_ids": [item for item in selected if item not in existing_ids],
        "record_file": str(path),
    }


def correction_object_options(manifest: dict[str, Any] | None = None) -> list[str]:
    values = list(cross_platform.KNOWN_ITEMS)
    if manifest:
        for episode in manifest.get("episodes", []):
            original = episode.get("original") if isinstance(episode, dict) else None
            if isinstance(original, dict):
                values.extend(str(item) for item in original.get("objects", []) if str(item).strip())
    return list(dict.fromkeys(values))
