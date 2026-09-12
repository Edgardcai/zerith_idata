from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - optional in minimal environments
    yaml = None  # type: ignore


@dataclass(frozen=True)
class CameraSpec:
    raw_key: str
    lerobot_key: str
    role: str
    required: bool = True


@dataclass(frozen=True)
class RobotProfile:
    path: Path
    raw: dict[str, Any]

    @property
    def profile_id(self) -> str:
        return str(self.raw["profile_id"])

    @property
    def display_name(self) -> str:
        return str(self.raw.get("display_name") or self.profile_id)

    @property
    def state_dim(self) -> int:
        return int(self.raw["state"]["dim"])

    @property
    def action_dim(self) -> int:
        return int(self.raw["action"]["dim"])

    @property
    def cameras(self) -> list[CameraSpec]:
        return [
            CameraSpec(
                raw_key=str(item["raw_key"]),
                lerobot_key=str(item["lerobot_key"]),
                role=str(item.get("role") or item["raw_key"]),
                required=bool(item.get("required", True)),
            )
            for item in self.raw.get("cameras", [])
        ]

    @property
    def required_camera_keys(self) -> list[str]:
        return [camera.raw_key for camera in self.cameras if camera.required]

    @property
    def processing(self) -> dict[str, Any]:
        return dict(self.raw.get("processing") or {})

    @property
    def default_task(self) -> str:
        tasks = self.raw.get("tasks")
        if isinstance(tasks, dict):
            for key in ("default", "default_task", "instruction"):
                value = tasks.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        value = self.raw.get("default_task")
        return value.strip() if isinstance(value, str) else ""

    def state_layout(self) -> list[dict[str, Any]]:
        return list(self.raw["state"].get("layout") or [])

    def action_layout(self) -> list[dict[str, Any]]:
        return list(self.raw["action"].get("layout") or [])

    def state_gripper_indices(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in self.state_layout():
            name = str(item.get("name") or "")
            if "gripper" in name and "index" in item:
                side = "left" if "left" in name else "right" if "right" in name else name
                out[side] = int(item["index"])
        return out

    def action_gripper_indices(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in self.action_layout():
            name = str(item.get("name") or "")
            if "gripper" in name and "index" in item:
                side = "left" if "left" in name else "right" if "right" in name else name
                out[side] = int(item["index"])
        return out

    def base_position_slice(self) -> tuple[int, int] | None:
        for item in self.state_layout():
            if str(item.get("name")) == "base_position_xyz" and "slice" in item:
                start, end = item["slice"]
                return int(start), int(end)
        return None

    def joint_state_indices(self) -> list[int]:
        indices: list[int] = []
        for item in self.state_layout():
            unit = str(item.get("unit") or "")
            name = str(item.get("name") or "")
            if "gripper" in name or name.startswith("base_"):
                continue
            if unit in ("rad", "native_aloha_position") and "slice" in item:
                start, end = item["slice"]
                indices.extend(range(int(start), int(end)))
            elif unit == "rad" and "index" in item:
                indices.append(int(item["index"]))
        return indices


def load_profile(path: str | Path) -> RobotProfile:
    profile_path = Path(path).expanduser().resolve()
    with profile_path.open("r", encoding="utf-8") as f:
        if profile_path.suffix.lower() in {".yaml", ".yml"}:
            if yaml is None:
                raise RuntimeError("YAML profile requires PyYAML. Install quality_pipeline/requirements.txt.")
            raw = yaml.safe_load(f)
        else:
            raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"{profile_path}: profile must be a mapping")
    _validate_profile(raw, profile_path)
    return RobotProfile(path=profile_path, raw=raw)


def _validate_profile(raw: dict[str, Any], path: Path) -> None:
    required_top = ("profile_id", "state", "action", "cameras")
    for key in required_top:
        if key not in raw:
            raise ValueError(f"{path}: missing required profile key {key!r}")
    for key in ("dim", "layout"):
        if key not in raw["state"]:
            raise ValueError(f"{path}: missing state.{key}")
        if key not in raw["action"]:
            raise ValueError(f"{path}: missing action.{key}")
    if int(raw["state"]["dim"]) <= 0:
        raise ValueError(f"{path}: state.dim must be positive")
    if int(raw["action"]["dim"]) <= 0:
        raise ValueError(f"{path}: action.dim must be positive")
    for camera in raw.get("cameras", []):
        if "raw_key" not in camera or "lerobot_key" not in camera:
            raise ValueError(f"{path}: every camera must define raw_key and lerobot_key")
