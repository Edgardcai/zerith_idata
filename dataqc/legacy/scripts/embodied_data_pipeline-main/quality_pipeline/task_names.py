from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


CANONICAL_BEVERAGE_NAMES: tuple[str, ...] = (
    "Yakult",
    "Guangming Probiotic Milk",
    "Coca-Cola",
    "COSTA Peach Oolong",
    "Taro Milk",
    "Robuk Velvet Latte",
    "Yili Peach Yogurt",
    "HK Orange Fanta",
    "Aojiru",
    "NEVER Coconut Latte",
    "Daily C Orange Juice",
    "Daily C Grape Juice",
    "COSTA Grape Jasmine",
    "Wanglaoji",
    "AD Calcium Milk",
    "Yili Strawberry Yogurt",
    "Dahongpao Milk Tea",
    "Sprite",
    "Royal Coconut",
)


_DATASET_MARKERS: tuple[tuple[str, str], ...] = (
    ("guangming-probiotic-milk", "Guangming Probiotic Milk"),
    ("yili-strawberry-yogurt", "Yili Strawberry Yogurt"),
    ("daily-c-orange-juice", "Daily C Orange Juice"),
    ("daily-c-grape-juice", "Daily C Grape Juice"),
    ("costa-grape-jasmine", "COSTA Grape Jasmine"),
    ("costa-peach-oolong", "COSTA Peach Oolong"),
    ("dahongpao-milk-tea", "Dahongpao Milk Tea"),
    ("robuk-velvet-latte", "Robuk Velvet Latte"),
    ("yili-peach-yogurt", "Yili Peach Yogurt"),
    ("never-coconut-latte", "NEVER Coconut Latte"),
    ("ad-calcium-milk", "AD Calcium Milk"),
    ("hk-orange-fanta", "HK Orange Fanta"),
    ("royal-coconut", "Royal Coconut"),
    ("nevercoffee", "NEVER Coconut Latte"),
    ("coca-cola", "Coca-Cola"),
    ("taro-milk", "Taro Milk"),
    ("wanglaoji", "Wanglaoji"),
    ("yakult", "Yakult"),
    ("aojiru", "Aojiru"),
    ("sprite", "Sprite"),
)


def _normalise_dataset_name(name: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(name).lower())).strip("-")


def canonical_product_for_dataset_name(name: str) -> str | None:
    normalised = _normalise_dataset_name(name)
    matches = {
        product
        for marker, product in _DATASET_MARKERS
        if marker in normalised
    }
    if len(matches) != 1:
        return None
    return next(iter(matches))


def canonical_task(product: str) -> str:
    value = str(product).strip()
    if value not in CANONICAL_BEVERAGE_NAMES:
        raise ValueError(f"unknown canonical beverage: {product}")
    return f"Grasp {value} with the left hand."


def canonical_task_for_dataset_name(name: str) -> str | None:
    product = canonical_product_for_dataset_name(name)
    return canonical_task(product) if product else None


def _attribute_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8").strip()
    return str(value).strip() if value is not None else ""


def _tasks_json_task(value: Any) -> str:
    raw = _attribute_text(value)
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(parsed, list):
        return ""
    return next((str(item).strip() for item in parsed if str(item).strip()), "")


def _plain_text_task(value: Any) -> str:
    raw = _attribute_text(value)
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    return parsed.strip() if isinstance(parsed, str) else ""


def read_hdf5_task(path: str | Path) -> str:
    import h5py  # type: ignore

    hdf5_path = Path(path)
    with h5py.File(hdf5_path, "r") as file_obj:
        task = _attribute_text(file_obj.attrs.get("task", ""))
        if task:
            return task
        task = _attribute_text(file_obj.attrs.get("task_name", ""))
        if task:
            return task
        task = _tasks_json_task(file_obj.attrs.get("tasks_json", ""))
        if task:
            return task
        return _plain_text_task(file_obj.attrs.get("text", ""))


def require_hdf5_task(path: str | Path) -> str:
    hdf5_path = Path(path)
    task = read_hdf5_task(hdf5_path)
    if task:
        return task
    raise ValueError(
        f"{hdf5_path}: HDF5 task metadata is missing; expected root attribute "
        "task, task_name, tasks_json, or plain text. Enter task text in the Web pipeline and retry."
    )


def _change(old: Any, new: Any) -> dict[str, Any]:
    return {"old": old, "new": new}


def write_hdf5_task(path: str | Path, task: str) -> dict[str, object]:
    import h5py  # type: ignore

    hdf5_path = Path(path)
    value = str(task).strip()
    if not value:
        raise ValueError("task must not be empty")
    changes: dict[str, object] = {}
    with h5py.File(hdf5_path, "r+") as file_obj:
        old_task = _attribute_text(file_obj.attrs.get("task", ""))
        if old_task != value:
            changes["task"] = _change(old_task if "task" in file_obj.attrs else None, value)
            file_obj.attrs["task"] = value

        if "task_name" in file_obj.attrs:
            old_task_name = _attribute_text(file_obj.attrs["task_name"])
            if old_task_name != value:
                changes["task_name"] = _change(old_task_name, value)
                file_obj.attrs["task_name"] = value

        if "tasks_json" in file_obj.attrs:
            old_tasks_json = _attribute_text(file_obj.attrs["tasks_json"])
            new_tasks_json = json.dumps([value], ensure_ascii=False)
            if old_tasks_json != new_tasks_json:
                changes["tasks_json"] = _change(old_tasks_json, new_tasks_json)
                file_obj.attrs["tasks_json"] = new_tasks_json

        if "text" in file_obj.attrs:
            old_text = _attribute_text(file_obj.attrs["text"])
            if _plain_text_task(old_text) and old_text != value:
                changes["text"] = _change(old_text, value)
                file_obj.attrs["text"] = value
    return changes


def write_episode_sidecar_task(path: str | Path, task: str) -> dict[str, object]:
    sidecar_path = Path(path)
    value = str(task).strip()
    if not value:
        raise ValueError("task must not be empty")
    payload: dict[str, Any] = {}
    if sidecar_path.is_file():
        loaded = json.loads(sidecar_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"{sidecar_path}: sidecar must contain a JSON object")
        payload = loaded

    replacements: dict[str, Any] = {
        "task": value,
        "tasks": [value],
        "full_instructions_en": [value],
    }
    changes: dict[str, object] = {}
    for key, new_value in replacements.items():
        old_value = payload.get(key)
        if old_value != new_value:
            changes[key] = _change(old_value, new_value)
            payload[key] = new_value

    if changes:
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = sidecar_path.with_suffix(sidecar_path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(sidecar_path)
    return changes
