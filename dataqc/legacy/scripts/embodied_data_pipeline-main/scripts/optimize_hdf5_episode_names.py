#!/usr/bin/env python3
"""Transactionally rename HDF5 episode directories to episode1, episode2, ... ."""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import pickle
import re
import signal
import sys
import threading
import time
from typing import Any, Iterator
import uuid


MANIFEST_NAME = "hdf5_episode_rename_manifest.json"
JOURNAL_NAME = ".hdf5_episode_optimize.transaction.json"
LOCK_NAME = ".hdf5_episode_optimize.lock"
IDENTITY_ATTRS = ("episode_id", "source_episode_id", "source_episode_name")
ZERITH_COLUMN_DATASETS = (
    ("timestamp/t", 1),
    ("observation/state/arm/position", 14),
    ("observation/state/effector/position", 2),
    ("observation/state/waist/position", 3),
    ("observation/state/head/position", 2),
    ("observation/state/base/velocity", 2),
    ("action/arm/position", 14),
    ("action/effector/position", 2),
    ("action/waist/position", 3),
    ("action/head/position", 2),
    ("action/base/velocity", 2),
)


class OptimizationInterrupted(RuntimeError):
    """Raised by the temporary signal handler so the transaction can roll back."""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(f"HDF5 optimization interrupted by signal {signum}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rename direct HDF5 episode subdirectories to episode1..N. "
            "Episodes are ordered by collection timestamp when available."
        )
    )
    parser.add_argument("--input", required=True, help="HDF5 dataset root directory.")
    parser.add_argument("--start-index", type=int, default=1, help="First episode number. Default: 1.")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan without renaming anything.")
    return parser.parse_args()


def natural_key(text: str) -> list[tuple[int, Any]]:
    return [
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.split(r"(\d+)", text)
    ]


def episode_h5_path(episode_dir: Path) -> Path | None:
    candidates = (
        episode_dir / "episode.hdf5",
        episode_dir / "episode.h5",
        episode_dir / "states" / "aligned_joints.h5",
        episode_dir / "states" / "aligned_joints.hdf5",
        episode_dir / "aligned_joints.h5",
        episode_dir / "aligned_joints.hdf5",
    )
    return next((path for path in candidates if path.is_file()), None)


def validate_zerith_columnar_hdf5(path: Path) -> None:
    """Refuse to rename a dataset unless its on-disk schema is really Zerith.

    The Web robot selector is user input, not proof of the dataset type.  This
    guard is deliberately executed even for ``--dry-run`` and before a rename
    transaction is prepared, so an ALOHA ``aligned_joints.h5`` tree cannot be
    mutated after somebody selects the wrong robot in the browser.
    """

    try:
        import h5py  # type: ignore
    except ImportError as exc:
        raise RuntimeError("h5py is required to validate Zerith HDF5 episodes") from exc

    issues: list[str] = []
    frame_count: int | None = None
    try:
        with h5py.File(path, "r") as file_obj:
            for dataset_name, width in ZERITH_COLUMN_DATASETS:
                if dataset_name not in file_obj:
                    issues.append(f"missing {dataset_name}")
                    continue
                dataset = file_obj[dataset_name]
                shape = tuple(int(value) for value in dataset.shape)
                valid_shape = (
                    len(shape) == 1 and width == 1
                ) or (
                    len(shape) == 2 and shape[1] == width
                )
                if not valid_shape:
                    issues.append(
                        f"{dataset_name} has shape {shape}, expected (N, {width})"
                    )
                    continue
                rows = shape[0]
                if frame_count is None:
                    frame_count = rows
                elif rows != frame_count:
                    issues.append(
                        f"{dataset_name} has {rows} frames, expected {frame_count}"
                    )
    except OSError as exc:
        raise ValueError(f"Cannot open HDF5 episode {path}: {exc}") from exc

    if frame_count is None or frame_count <= 0:
        issues.append("no non-empty Zerith frame columns")
    if issues:
        preview = "; ".join(issues[:8])
        if len(issues) > 8:
            preview += f"; ... and {len(issues) - 8} more"
        raise ValueError(
            f"Refusing HDF5 optimization because this is not a valid Zerith "
            f"columnar episode: {path}: {preview}"
        )


def episode_meta_path(episode_dir: Path) -> Path | None:
    candidates = (
        episode_dir / "episode_meta.json",
        episode_dir / "meta" / "episode_meta.json",
    )
    return next((path for path in candidates if path.is_file()), None)


def load_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_journal(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Interrupted HDF5 optimization has an unreadable transaction journal: {path}"
        ) from exc
    if not isinstance(value, dict) or int(value.get("version", 0)) != 2:
        raise RuntimeError(f"Unsupported HDF5 optimization transaction journal: {path}")
    return value


def numeric_timestamp(meta: dict[str, Any]) -> float | None:
    for key in (
        "collection_ts",
        "collection_timestamp",
        "created_at_ms",
        "first_timestamp_ns",
        "start_timestamp_ns",
    ):
        value = meta.get(key)
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def attr_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def hdf5_metadata(path: Path | None) -> tuple[float | None, dict[str, str]]:
    if path is None:
        return None, {}
    try:
        import h5py  # type: ignore
        import numpy as np  # type: ignore
    except ImportError:
        return None, {}
    timestamp: float | None = None
    identity: dict[str, str] = {}
    try:
        with h5py.File(path, "r") as file_obj:
            for key in IDENTITY_ATTRS:
                if key in file_obj.attrs:
                    identity[key] = attr_text(file_obj.attrs[key]).strip()
            if "timestamp/t" in file_obj and int(file_obj["timestamp/t"].shape[0]) > 0:
                timestamp = float(file_obj["timestamp/t"][0])
            else:
                frame_keys = sorted(
                    (str(key) for key in file_obj.keys() if str(key).isdigit()),
                    key=int,
                )
                if frame_keys and f"{frame_keys[0]}/main_timestamp" in file_obj:
                    value = file_obj[f"{frame_keys[0]}/main_timestamp"][()]
                    timestamp = float(np.asarray(value).reshape(-1)[0])
    except (OSError, ValueError, TypeError):
        return None, identity
    return timestamp, identity


def discover_episodes(root: Path) -> list[dict[str, Any]]:
    episodes: list[dict[str, Any]] = []
    for child in root.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        h5_path = episode_h5_path(child)
        if h5_path is None:
            continue
        validate_zerith_columnar_hdf5(h5_path)
        meta = load_json(episode_meta_path(child))
        h5_timestamp, h5_identity = hdf5_metadata(h5_path)
        collection_timestamp = numeric_timestamp(meta)
        timestamp_source = "episode_meta"
        if collection_timestamp is None:
            collection_timestamp = h5_timestamp
            timestamp_source = "hdf5"

        current_name = child.name
        explicit_source_name = str(
            meta.get("source_episode_name")
            or h5_identity.get("source_episode_name")
            or ""
        ).strip()
        explicit_source_id = str(
            meta.get("source_episode_id")
            or meta.get("raw_episode_id")
            or h5_identity.get("source_episode_id")
            or ""
        ).strip()
        provenance_name = explicit_source_name or current_name
        source_episode_id = str(
            explicit_source_id
            or meta.get("episode_id")
            or h5_identity.get("episode_id")
            or provenance_name
        ).strip() or provenance_name
        episodes.append(
            {
                "path": child,
                "current_name": current_name,
                "source_name": provenance_name,
                "source_episode_id": source_episode_id,
                "explicit_source_name": explicit_source_name,
                "explicit_source_id": explicit_source_id,
                "hdf5_file": str(h5_path.relative_to(child)),
                "collection_timestamp": collection_timestamp,
                "timestamp_source": timestamp_source if collection_timestamp is not None else "name",
            }
        )
    return sorted(
        episodes,
        key=lambda item: (
            item["collection_timestamp"] is None,
            float(item["collection_timestamp"] or 0.0),
            natural_key(str(item["current_name"])),
        ),
    )


def build_plan(root: Path, start_index: int) -> list[dict[str, Any]]:
    if start_index < 0:
        raise ValueError("start-index must be non-negative")
    plan = discover_episodes(root)
    for offset, item in enumerate(plan):
        target_name = f"episode{start_index + offset}"
        item["target_name"] = target_name
        item["target_path"] = root / target_name
        item["changed"] = item["current_name"] != target_name
    return plan


def merge_manifest_provenance(plan: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
    """Fill partial provenance only when current HDF5 explicitly proves identity.

    A target directory name alone is not identity: users may replace ``episode1``
    with unrelated data.  In that case an old manifest must be refreshed instead
    of silently assigning the old collector UUID to the replacement.
    """

    records = manifest.get("episodes")
    if not isinstance(records, list):
        return
    by_old_target = {
        str(record.get("target_name")): record
        for record in records
        if isinstance(record, dict) and str(record.get("target_name") or "")
    }
    for item in plan:
        record = by_old_target.get(str(item["current_name"]))
        if not isinstance(record, dict):
            continue
        old_name = str(record.get("source_name") or "").strip()
        old_id = str(record.get("source_episode_id") or "").strip()
        explicit_name = str(item.get("explicit_source_name") or "").strip()
        explicit_id = str(item.get("explicit_source_id") or "").strip()
        identity_proven = bool(
            (explicit_name and old_name and explicit_name == old_name)
            or (explicit_id and old_id and explicit_id == old_id)
        )
        if not identity_proven:
            continue
        if not explicit_name and old_name:
            item["source_name"] = old_name
        if not explicit_id and old_id:
            item["source_episode_id"] = old_id


def validate_plan(root: Path, plan: list[dict[str, Any]]) -> None:
    if not plan:
        raise FileNotFoundError(f"No HDF5 episode directories found directly under {root}")
    source_paths = {Path(item["path"]).resolve() for item in plan}
    target_names: set[str] = set()
    for item in plan:
        target_name = str(item["target_name"])
        if target_name in target_names:
            raise ValueError(f"Duplicate rename target: {target_name}")
        target_names.add(target_name)
        target_path = Path(item["target_path"])
        if target_path.exists() and target_path.resolve() not in source_paths:
            raise FileExistsError(
                f"Rename target already exists and is not an episode in this plan: {target_path}"
            )


@contextmanager
def dataset_lock(root: Path) -> Iterator[None]:
    lock_path = root / LOCK_NAME
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, content: bytes, mode: int | None = None) -> None:
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with tmp_path.open("xb") as file_obj:
            file_obj.write(content)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        if mode is not None:
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
        fsync_directory(path.parent)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    atomic_write_bytes(path, content)


def durable_rename(source: Path, target: Path) -> None:
    source.rename(target)
    fsync_directory(source.parent)
    if target.parent != source.parent:
        fsync_directory(target.parent)


def durable_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    fsync_directory(path.parent)


def encode_pickle(value: Any) -> str:
    return base64.b64encode(pickle.dumps(value, protocol=4)).decode("ascii")


def decode_pickle(value: str) -> Any:
    return pickle.loads(base64.b64decode(value.encode("ascii")))


def file_backup(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False}
    stat = path.stat()
    return {
        "exists": True,
        "mode": stat.st_mode & 0o7777,
        "content_b64": base64.b64encode(path.read_bytes()).decode("ascii"),
    }


def restore_file(path: Path, backup: dict[str, Any]) -> None:
    if bool(backup.get("exists")):
        content = base64.b64decode(str(backup["content_b64"]).encode("ascii"))
        mode_value = backup.get("mode")
        atomic_write_bytes(path, content, int(mode_value) if mode_value is not None else None)
    else:
        durable_unlink(path)


def capture_identity(episode_dir: Path, hdf5_file: str) -> dict[str, Any]:
    meta_path = episode_meta_path(episode_dir)
    meta_relative = str(meta_path.relative_to(episode_dir)) if meta_path is not None else None
    h5_path = episode_dir / hdf5_file
    try:
        import h5py  # type: ignore
    except ImportError as exc:
        raise RuntimeError("h5py is required to synchronize HDF5 episode IDs") from exc
    attrs: dict[str, Any] = {}
    with h5py.File(h5_path, "r") as file_obj:
        for key in IDENTITY_ATTRS:
            exists = key in file_obj.attrs
            attrs[key] = {
                "exists": exists,
                "value_pickle_b64": encode_pickle(file_obj.attrs[key]) if exists else None,
            }
    return {
        "meta_relative": meta_relative,
        "meta_backup": file_backup(meta_path) if meta_path is not None else {"exists": False},
        "hdf5_relative": hdf5_file,
        "hdf5_attrs": attrs,
    }


def prepare_transaction(root: Path, plan: list[dict[str, Any]]) -> dict[str, Any]:
    token = uuid.uuid4().hex
    entries: list[dict[str, Any]] = []
    for index, item in enumerate(plan):
        if not bool(item["changed"]):
            continue
        current_name = str(item["current_name"])
        entries.append(
            {
                "index": index,
                "source_name": current_name,
                "temp_name": f".hdf5_episode_optimize_{token}_{index}",
                "target_name": str(item["target_name"]),
                "marker_name": f".hdf5_episode_optimize_marker_{token}.json",
                "provenance_source_name": str(item["source_name"]),
                "source_episode_id": str(item["source_episode_id"]),
                "identity_backup": capture_identity(root / current_name, str(item["hdf5_file"])),
            }
        )
    return {
        "version": 2,
        "phase": "prepared",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset_root": str(root),
        "token": token,
        "entries": entries,
        "external_backups": {
            "manual_failure_annotations": file_backup(root / "manual_failure_annotations.json"),
            "renumber_plan": file_backup(root / "renumber_plan.json"),
            "manifest": file_backup(root / MANIFEST_NAME),
        },
    }


def write_journal(root: Path, journal: dict[str, Any]) -> None:
    atomic_write_json(root / JOURNAL_NAME, journal)


def set_phase(root: Path, journal: dict[str, Any], phase: str) -> None:
    journal["phase"] = phase
    journal["phase_updated_at"] = datetime.now(timezone.utc).isoformat()
    write_journal(root, journal)


def marker_payload(journal: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "transaction_token": journal["token"],
        "entry_index": entry["index"],
        "source_name": entry["source_name"],
    }


def write_marker(episode_dir: Path, journal: dict[str, Any], entry: dict[str, Any]) -> None:
    atomic_write_json(episode_dir / str(entry["marker_name"]), marker_payload(journal, entry))


def marker_matches(episode_dir: Path, journal: dict[str, Any], entry: dict[str, Any]) -> bool:
    marker = load_json(episode_dir / str(entry["marker_name"]))
    return (
        marker.get("transaction_token") == journal.get("token")
        and marker.get("entry_index") == entry.get("index")
    )


def entry_location(root: Path, journal: dict[str, Any], entry: dict[str, Any]) -> tuple[str, Path] | None:
    seen: set[Path] = set()
    for location in ("source_name", "temp_name", "target_name"):
        path = root / str(entry[location])
        if path in seen:
            continue
        seen.add(path)
        if path.is_dir() and marker_matches(path, journal, entry):
            return location, path
    return None


def sync_episode_identity(episode_dir: Path, entry: dict[str, Any]) -> None:
    """Update current IDs while retaining collector UUID/name provenance."""

    identity = dict(entry["identity_backup"])
    meta_relative = identity.get("meta_relative")
    target_name = str(entry["target_name"])
    if isinstance(meta_relative, str) and meta_relative:
        meta_path = episode_dir / meta_relative
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot update episode metadata: {meta_path}") from exc
        if not isinstance(meta, dict):
            raise ValueError(f"Episode metadata must be a JSON object: {meta_path}")
        original_id = str(
            meta.get("source_episode_id")
            or meta.get("episode_id")
            or entry["source_episode_id"]
            or entry["provenance_source_name"]
        )
        meta.setdefault("source_episode_id", original_id)
        meta.setdefault("source_episode_name", str(entry["provenance_source_name"]))
        meta["episode_id"] = target_name
        if "episode_name" in meta:
            meta["episode_name"] = target_name
        atomic_write_json(meta_path, meta)

    try:
        import h5py  # type: ignore
    except ImportError as exc:
        raise RuntimeError("h5py is required to synchronize HDF5 episode IDs") from exc
    h5_path = episode_dir / str(identity["hdf5_relative"])
    with h5py.File(h5_path, "r+") as file_obj:
        original_id = attr_text(
            file_obj.attrs.get("source_episode_id")
            or file_obj.attrs.get("episode_id")
            or entry["source_episode_id"]
            or entry["provenance_source_name"]
        )
        if "source_episode_id" not in file_obj.attrs:
            file_obj.attrs["source_episode_id"] = original_id
        if "source_episode_name" not in file_obj.attrs:
            file_obj.attrs["source_episode_name"] = str(entry["provenance_source_name"])
        file_obj.attrs["episode_id"] = target_name
        file_obj.flush()


def restore_episode_identity(episode_dir: Path, entry: dict[str, Any]) -> None:
    identity = dict(entry["identity_backup"])
    meta_relative = identity.get("meta_relative")
    if isinstance(meta_relative, str) and meta_relative:
        restore_file(episode_dir / meta_relative, dict(identity["meta_backup"]))

    try:
        import h5py  # type: ignore
    except ImportError as exc:
        raise RuntimeError("h5py is required to restore HDF5 episode IDs") from exc
    h5_path = episode_dir / str(identity["hdf5_relative"])
    with h5py.File(h5_path, "r+") as file_obj:
        for key in IDENTITY_ATTRS:
            state = dict(identity["hdf5_attrs"][key])
            if bool(state.get("exists")):
                file_obj.attrs[key] = decode_pickle(str(state["value_pickle_b64"]))
            elif key in file_obj.attrs:
                del file_obj.attrs[key]
        file_obj.flush()


def restored_identity_matches(episode_dir: Path, entry: dict[str, Any]) -> bool:
    """Return whether a marker-less source is the already-restored episode."""

    identity = dict(entry.get("identity_backup") or {})
    meta_relative = identity.get("meta_relative")
    if isinstance(meta_relative, str) and meta_relative:
        meta_backup = dict(identity.get("meta_backup") or {})
        meta_path = episode_dir / meta_relative
        if not bool(meta_backup.get("exists")) or not meta_path.is_file():
            return False
        try:
            expected_meta = base64.b64decode(str(meta_backup["content_b64"]).encode("ascii"))
            if meta_path.read_bytes() != expected_meta:
                return False
        except (KeyError, OSError, ValueError):
            return False

    h5_relative = str(identity.get("hdf5_relative") or "")
    h5_path = episode_dir / h5_relative
    if not h5_relative or not h5_path.is_file():
        return False
    try:
        import h5py  # type: ignore

        with h5py.File(h5_path, "r") as file_obj:
            attr_backup = dict(identity.get("hdf5_attrs") or {})
            for key in IDENTITY_ATTRS:
                state = dict(attr_backup.get(key) or {})
                existed = bool(state.get("exists"))
                if existed != (key in file_obj.attrs):
                    return False
                if existed:
                    expected = decode_pickle(str(state.get("value_pickle_b64") or ""))
                    if attr_text(file_obj.attrs[key]) != attr_text(expected):
                        return False
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return True


def identity_mapping(plan: list[dict[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in plan:
        target = str(item["target_name"])
        for value in (item["current_name"], item["source_name"], item["source_episode_id"]):
            mapping[str(value)] = target
    return mapping


def manual_annotations_need_sync(root: Path, plan: list[dict[str, Any]]) -> bool:
    payload = load_json(root / "manual_failure_annotations.json")
    episodes = payload.get("episodes")
    if not isinstance(episodes, list):
        return False
    mapping = identity_mapping(plan)
    return any(
        isinstance(record, dict)
        and str(record.get("episode_name") or "") in mapping
        and mapping[str(record.get("episode_name") or "")] != str(record.get("episode_name") or "")
        for record in episodes
    )


def sync_manual_annotations(root: Path, plan: list[dict[str, Any]]) -> None:
    path = root / "manual_failure_annotations.json"
    if not path.is_file():
        return
    payload = load_json(path)
    episodes = payload.get("episodes")
    if not isinstance(episodes, list):
        return
    mapping = identity_mapping(plan)
    changed = False
    for record in episodes:
        if not isinstance(record, dict):
            continue
        old_name = str(record.get("episode_name") or "")
        target_name = mapping.get(old_name)
        if not target_name or target_name == old_name:
            continue
        record.setdefault("source_episode_name", old_name)
        record["episode_name"] = target_name
        changed = True
    if changed:
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(path, payload)


def manifest_payload(root: Path, plan: list[dict[str, Any]], status: str) -> dict[str, Any]:
    return {
        "version": 2,
        "status": status,
        "dataset_root": str(root),
        "ordering": "collection_timestamp_then_natural_name",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "episode_count": len(plan),
        "renamed_count": sum(bool(item["changed"]) for item in plan),
        "episodes": [
            {
                "source_name": item["source_name"],
                "current_name": item["current_name"],
                "target_name": item["target_name"],
                "source_episode_id": item["source_episode_id"],
                "hdf5_file": item["hdf5_file"],
                "collection_timestamp": item["collection_timestamp"],
                "timestamp_source": item["timestamp_source"],
                "changed": item["changed"],
            }
            for item in plan
        ],
    }


def manifest_matches_plan(root: Path, manifest: dict[str, Any], plan: list[dict[str, Any]]) -> bool:
    if manifest.get("status") != "completed" or int(manifest.get("episode_count", -1)) != len(plan):
        return False
    try:
        if Path(str(manifest.get("dataset_root", ""))).resolve() != root:
            return False
    except (OSError, RuntimeError):
        return False
    records = manifest.get("episodes")
    if not isinstance(records, list) or len(records) != len(plan):
        return False
    for item, record in zip(plan, records, strict=True):
        if not isinstance(record, dict):
            return False
        expected = {
            "source_name": str(item["source_name"]),
            "target_name": str(item["target_name"]),
            "source_episode_id": str(item["source_episode_id"]),
            "hdf5_file": str(item["hdf5_file"]),
        }
        if any(str(record.get(key) or "") != value for key, value in expected.items()):
            return False
        item_timestamp = item.get("collection_timestamp")
        record_timestamp = record.get("collection_timestamp")
        if (item_timestamp is None) != (record_timestamp is None):
            return False
        if item_timestamp is not None:
            try:
                if float(item_timestamp) != float(record_timestamp):
                    return False
            except (TypeError, ValueError):
                return False
        if str(record.get("timestamp_source") or "") != str(item.get("timestamp_source") or ""):
            return False
        if str(item["current_name"]) != str(item["target_name"]):
            return False
    return True


@contextmanager
def interruption_as_exception() -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    interrupted = False
    previous: dict[int, Any] = {}

    def handler(signum: int, _frame: Any) -> None:
        nonlocal interrupted
        if interrupted:
            return
        interrupted = True
        raise OptimizationInterrupted(signum)

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, handler)
    try:
        yield
    finally:
        for signum, old_handler in previous.items():
            signal.signal(signum, old_handler)


def ensure_recovery_markers(root: Path, journal: dict[str, Any]) -> None:
    phase = str(journal.get("phase") or "")
    entries = list(journal.get("entries") or [])
    if phase == "prepared":
        return
    for entry in entries:
        if entry_location(root, journal, entry) is not None:
            continue
        if phase == "marking":
            assumed = root / str(entry["source_name"])
        elif phase == "cleanup_markers":
            assumed = root / str(entry["target_name"])
        elif phase == "recovering":
            assumed = root / str(entry["source_name"])
            if not assumed.is_dir() or not restored_identity_matches(assumed, entry):
                raise RuntimeError(
                    "Cannot safely identify a marker-less episode while resuming recovery: "
                    f"{assumed}"
                )
        else:
            raise RuntimeError(
                "Cannot locate an episode belonging to interrupted transaction "
                f"{journal.get('token')}: {entry.get('source_name')}"
            )
        if not assumed.is_dir():
            raise RuntimeError(f"Cannot recover missing episode directory: {assumed}")
        write_marker(assumed, journal, entry)


def restore_external_files(root: Path, journal: dict[str, Any]) -> None:
    backups = dict(journal.get("external_backups") or {})
    restore_file(root / MANIFEST_NAME, dict(backups.get("manifest") or {"exists": False}))
    # Compatibility with an interrupted transaction created by versions that
    # archived renumber_plan.json during commit.
    legacy_archive = journal.get("renumber_archive_name")
    if isinstance(legacy_archive, str) and legacy_archive:
        durable_unlink(root / legacy_archive)
    restore_file(root / "renumber_plan.json", dict(backups.get("renumber_plan") or {"exists": False}))

    restore_file(
        root / "manual_failure_annotations.json",
        dict(backups.get("manual_failure_annotations") or {"exists": False}),
    )


def recover_transaction(root: Path, journal: dict[str, Any]) -> None:
    """Idempotently restore the exact pre-transaction files and episode identities."""

    journal_root = Path(str(journal.get("dataset_root") or "")).resolve()
    if journal_root != root:
        raise RuntimeError(f"Transaction journal belongs to {journal_root}, not {root}")
    phase = str(journal.get("phase") or "")
    interrupted_phase = (
        str(journal.get("interrupted_phase") or "") if phase == "recovering" else phase
    )
    entries = list(journal.get("entries") or [])
    if phase == "prepared":
        restore_external_files(root, journal)
        durable_unlink(root / JOURNAL_NAME)
        return

    ensure_recovery_markers(root, journal)
    if phase != "recovering":
        journal["interrupted_phase"] = phase
        set_phase(root, journal, "recovering")

    # First place every moved episode in its unique temporary directory. Markers
    # disambiguate target/source name cycles, including recovery interrupted twice.
    for entry in reversed(entries):
        located = entry_location(root, journal, entry)
        if located is None:
            raise RuntimeError(f"Cannot locate transaction episode: {entry.get('source_name')}")
        location, episode_dir = located
        if location == "target_name":
            temp = root / str(entry["temp_name"])
            if temp.exists():
                raise FileExistsError(f"Recovery temp path is occupied: {temp}")
            durable_rename(episode_dir, temp)

    # The identity phase starts only after every source is safely in its temp
    # directory. Avoid even opening HDF5 in r+ when a crash happened earlier;
    # doing so can alter HDF5 bookkeeping bytes despite identical attributes.
    if interrupted_phase in {
        "identity",
        "temp_to_target",
        "post_ops",
        "cleanup_markers",
    }:
        for entry in entries:
            located = entry_location(root, journal, entry)
            if located is None:
                raise RuntimeError(f"Cannot locate transaction episode: {entry.get('source_name')}")
            _, episode_dir = located
            restore_episode_identity(episode_dir, entry)

    for entry in reversed(entries):
        located = entry_location(root, journal, entry)
        if located is None:
            raise RuntimeError(f"Cannot locate transaction episode: {entry.get('source_name')}")
        location, episode_dir = located
        source = root / str(entry["source_name"])
        if location == "temp_name":
            if source.exists():
                raise FileExistsError(f"Recovery source path is occupied: {source}")
            durable_rename(episode_dir, source)
        elif location != "source_name":
            raise RuntimeError(f"Unexpected recovery location for {source}: {location}")

    restore_external_files(root, journal)
    for entry in entries:
        source = root / str(entry["source_name"])
        durable_unlink(source / str(entry["marker_name"]))
    durable_unlink(root / JOURNAL_NAME)


def execute_transaction(
    root: Path,
    journal: dict[str, Any],
    plan: list[dict[str, Any]],
    completed_manifest: dict[str, Any],
) -> None:
    entries = list(journal["entries"])
    try:
        set_phase(root, journal, "marking")
        for entry in entries:
            write_marker(root / str(entry["source_name"]), journal, entry)

        set_phase(root, journal, "source_to_temp")
        for entry in entries:
            source = root / str(entry["source_name"])
            temp = root / str(entry["temp_name"])
            if temp.exists():
                raise FileExistsError(temp)
            durable_rename(source, temp)

        set_phase(root, journal, "identity")
        for entry in entries:
            sync_episode_identity(root / str(entry["temp_name"]), entry)

        set_phase(root, journal, "temp_to_target")
        for entry in entries:
            temp = root / str(entry["temp_name"])
            target = root / str(entry["target_name"])
            if target.exists():
                raise FileExistsError(target)
            durable_rename(temp, target)

        set_phase(root, journal, "post_ops")
        sync_manual_annotations(root, plan)
        # The optimizer is an in-place rename.  A stale numbering plan is no
        # longer useful after the physical names change, so remove it instead
        # of retaining a persistent pre-optimization backup.  Its bytes remain
        # in the transaction journal only until the operation commits, allowing
        # an interrupted operation to restore the exact pre-rename state.
        durable_unlink(root / "renumber_plan.json")
        atomic_write_json(root / MANIFEST_NAME, completed_manifest)

        set_phase(root, journal, "cleanup_markers")
        for entry in entries:
            durable_unlink(root / str(entry["target_name"]) / str(entry["marker_name"]))
        durable_unlink(root / JOURNAL_NAME)
    except BaseException:
        recover_transaction(root, journal)
        raise


def optimise(root: Path, start_index: int = 1, dry_run: bool = False) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)
    with dataset_lock(root):
        journal_path = root / JOURNAL_NAME
        if journal_path.is_file():
            with interruption_as_exception():
                recover_transaction(root, load_journal(journal_path))

        existing_manifest = load_json(root / MANIFEST_NAME)
        plan = build_plan(root, start_index)
        merge_manifest_provenance(plan, existing_manifest)
        validate_plan(root, plan)
        planned_payload = manifest_payload(root, plan, "dry_run" if dry_run else "completed")
        if dry_run:
            return planned_payload

        can_return_existing = (
            not any(bool(item["changed"]) for item in plan)
            and manifest_matches_plan(root, existing_manifest, plan)
            and not (root / "renumber_plan.json").is_file()
            and not manual_annotations_need_sync(root, plan)
        )
        if can_return_existing:
            return existing_manifest

        journal = prepare_transaction(root, plan)
        write_journal(root, journal)
        with interruption_as_exception():
            execute_transaction(root, journal, plan, planned_payload)
        return planned_payload


def main() -> int:
    args = parse_args()
    try:
        result = optimise(Path(args.input), args.start_index, args.dry_run)
    except OptimizationInterrupted as exc:
        print(str(exc), file=sys.stderr)
        return 128 + int(exc.signum)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
