#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quality_pipeline.task_names import (  # noqa: E402
    canonical_task_for_dataset_name,
    read_hdf5_task,
)


FALLBACK_TASK = "grasp the bottles on the table"
QUALITY_GRADES = {"A", "B", "C", "F"}


def _json_text(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _jsonl_text(rows: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return payload


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{line_number}: expected a JSON object")
        rows.append(row)
    return rows


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


def _plain_text(value: Any) -> str:
    raw = str(value).strip() if value is not None else ""
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    return parsed.strip() if isinstance(parsed, str) else ""


def _normalise_instruction_payload(payload: dict[str, Any], task: str) -> dict[str, Any]:
    out = json.loads(json.dumps(payload, ensure_ascii=False))
    out["task"] = task
    out["tasks"] = [task]
    out["full_instructions_en"] = [task]
    for key in ("subtask_segments", "segment_instructions"):
        segments = out.get(key)
        if not isinstance(segments, list):
            continue
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            if "subtask" in segment:
                segment["subtask"] = task
            if "description_en" in segment:
                segment["description_en"] = [task]
    return out


def _text_file_operation(path: Path, new_text: str) -> dict[str, Any] | None:
    old_exists = path.is_file()
    old_text = path.read_text(encoding="utf-8") if old_exists else ""
    if old_exists and old_text == new_text:
        return None
    return {
        "kind": "text_file",
        "path": str(path),
        "old_exists": old_exists,
        "old_text": old_text,
        "new_text": new_text,
    }


def _hdf5_operation(path: Path, task: str) -> dict[str, Any] | None:
    import h5py  # type: ignore

    changes: list[dict[str, Any]] = []
    with h5py.File(path, "r") as file_obj:
        old_task = str(file_obj.attrs.get("task", "")).strip()
        if old_task != task:
            changes.append(
                {
                    "name": "task",
                    "old_exists": "task" in file_obj.attrs,
                    "old": old_task,
                    "new": task,
                }
            )
        if "tasks_json" in file_obj.attrs:
            old_value = str(file_obj.attrs["tasks_json"]).strip()
            new_value = json.dumps([task], ensure_ascii=False)
            if old_value != new_value:
                changes.append(
                    {
                        "name": "tasks_json",
                        "old_exists": True,
                        "old": old_value,
                        "new": new_value,
                    }
                )
        if "text" in file_obj.attrs:
            old_value = str(file_obj.attrs["text"]).strip()
            if _plain_text(old_value) and old_value != task:
                changes.append(
                    {
                        "name": "text",
                        "old_exists": True,
                        "old": old_value,
                        "new": task,
                    }
                )
    if not changes:
        return None
    return {"kind": "hdf5_attrs", "path": str(path), "changes": changes}


def _parquet_operation(path: Path) -> tuple[dict[str, Any] | None, list[int], list[int]]:
    import pyarrow.parquet as pq  # type: ignore

    table = pq.read_table(path, columns=["episode_index", "task_index"])
    episode_indices = [int(value) for value in table["episode_index"].to_pylist()]
    task_indices = [int(value) for value in table["task_index"].to_pylist()]
    operation = None
    if any(value != 0 for value in task_indices):
        operation = {
            "kind": "parquet_task_index",
            "path": str(path),
            "old_values": task_indices,
            "new_value": 0,
        }
    return operation, episode_indices, task_indices


def _dataset_task(path: Path) -> str:
    task = canonical_task_for_dataset_name(path.name)
    if not task:
        raise ValueError(f"{path}: unknown or ambiguous dataset identity")
    return task


def build_migration_plan(hdf5_root: str | Path, lerobot_root: str | Path) -> dict[str, Any]:
    hroot = Path(hdf5_root).expanduser().resolve()
    lroot = Path(lerobot_root).expanduser().resolve()
    if not hroot.is_dir():
        raise ValueError(f"HDF5 root does not exist: {hroot}")
    if lroot.exists() and not lroot.is_dir():
        raise ValueError(f"LeRobot root is not a directory: {lroot}")

    operations: list[dict[str, Any]] = []
    hdf5_total = 0
    noncanonical_hdf5 = 0
    sidecar_mismatch = 0
    fallback_occurrences = 0
    for dataset_dir in sorted(path for path in hroot.iterdir() if path.is_dir()):
        h5_paths = sorted(dataset_dir.rglob("states/aligned_joints.h5"))
        if not h5_paths:
            continue
        canonical = _dataset_task(dataset_dir)
        for h5_path in h5_paths:
            hdf5_total += 1
            current = read_hdf5_task(h5_path)
            if current != canonical:
                noncanonical_hdf5 += 1
            if current == FALLBACK_TASK:
                fallback_occurrences += 1
            hdf5_operation = _hdf5_operation(h5_path, canonical)
            if hdf5_operation:
                operations.append(hdf5_operation)

            sidecar_path = h5_path.parents[1] / "meta" / "episode_meta.json"
            payload = _read_json(sidecar_path)
            current_sidecar_task = str(payload.get("task") or "").strip()
            if current_sidecar_task != canonical:
                sidecar_mismatch += 1
            new_payload = _normalise_instruction_payload(payload, canonical)
            sidecar_operation = _text_file_operation(sidecar_path, _json_text(new_payload))
            if sidecar_operation:
                operations.append(sidecar_operation)

    lerobot_episode_total = 0
    noncanonical_lerobot_episodes = 0
    orphan_lerobot_episodes = 0
    grade_total = 0
    lerobot_dataset_dirs = (
        sorted(path for path in lroot.iterdir() if path.is_dir())
        if lroot.is_dir()
        else []
    )
    for dataset_dir in lerobot_dataset_dirs:
        grade_dirs = sorted(
            path for path in dataset_dir.iterdir() if path.is_dir() and path.name in QUALITY_GRADES
        )
        if not grade_dirs:
            continue
        canonical = _dataset_task(dataset_dir)
        for grade_dir in grade_dirs:
            grade_total += 1
            meta_dir = grade_dir / "meta"
            tasks_path = meta_dir / "tasks.jsonl"
            old_task_rows = _read_jsonl(tasks_path)
            task_by_index: dict[int, str] = {}
            for row in old_task_rows:
                try:
                    task_by_index[int(row.get("task_index"))] = str(row.get("task") or "").strip()
                except (TypeError, ValueError):
                    raise ValueError(f"{tasks_path}: invalid task row {row!r}")
            tasks_operation = _text_file_operation(
                tasks_path,
                _jsonl_text([{"task_index": 0, "task": canonical}]),
            )
            if tasks_operation:
                operations.append(tasks_operation)

            for parquet_path in sorted((grade_dir / "data").rglob("*.parquet")):
                parquet_operation, episode_indices, task_indices = _parquet_operation(parquet_path)
                if parquet_operation:
                    operations.append(parquet_operation)
                for episode_index in sorted(set(episode_indices)):
                    lerobot_episode_total += 1
                    row_positions = [
                        idx for idx, value in enumerate(episode_indices) if value == episode_index
                    ]
                    episode_task_indices = {task_indices[idx] for idx in row_positions}
                    if len(episode_task_indices) != 1:
                        raise ValueError(
                            f"{parquet_path}: episode {episode_index} has multiple task_index values"
                        )
                    current_task = task_by_index.get(next(iter(episode_task_indices)), "")
                    if current_task != canonical:
                        noncanonical_lerobot_episodes += 1
                    if current_task == FALLBACK_TASK:
                        fallback_occurrences += 1

            episodes_path = meta_dir / "episodes.jsonl"
            if episodes_path.is_file():
                episode_rows = [
                    _normalise_instruction_payload(row, canonical)
                    for row in _read_jsonl(episodes_path)
                ]
                operation = _text_file_operation(episodes_path, _jsonl_text(episode_rows))
                if operation:
                    operations.append(operation)

            mapping_path = meta_dir / "episode_name_mapping.json"
            if mapping_path.is_file():
                mapping = _read_json(mapping_path)
                records = mapping.get("episodes")
                if not isinstance(records, list):
                    raise ValueError(f"{mapping_path}: episodes must be a list")
                for record in records:
                    if not isinstance(record, dict):
                        continue
                    record["task"] = canonical
                    source_text = str(record.get("source_h5") or record.get("hdf5_file") or "").strip()
                    if source_text:
                        source_path = Path(source_text)
                        if not source_path.is_absolute():
                            source_path = hroot / dataset_dir.name / source_path
                        if not source_path.is_file():
                            orphan_lerobot_episodes += 1
                operation = _text_file_operation(mapping_path, _json_text(mapping))
                if operation:
                    operations.append(operation)

            info_path = meta_dir / "info.json"
            if info_path.is_file():
                info = _read_json(info_path)
                info["total_tasks"] = 1
                operation = _text_file_operation(info_path, _json_text(info))
                if operation:
                    operations.append(operation)

    report = {
        "hdf5_total": hdf5_total,
        "noncanonical_hdf5": noncanonical_hdf5,
        "sidecar_mismatches": sidecar_mismatch,
        "lerobot_grade_total": grade_total,
        "lerobot_episode_total": lerobot_episode_total,
        "noncanonical_lerobot_episodes": noncanonical_lerobot_episodes,
        "orphan_lerobot_episodes": orphan_lerobot_episodes,
        "fallback_occurrences": fallback_occurrences,
        "pending_changes": len(operations),
    }
    return {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(),
        "hdf5_root": str(hroot),
        "lerobot_root": str(lroot),
        "report": report,
        "operations": operations,
    }


def audit_roots(hdf5_root: str | Path, lerobot_root: str | Path) -> dict[str, Any]:
    return build_migration_plan(hdf5_root, lerobot_root)["report"]


def _rewrite_parquet_task_index(path: Path, values: list[int]) -> None:
    import pyarrow as pa  # type: ignore
    import pyarrow.parquet as pq  # type: ignore

    parquet_file = pq.ParquetFile(path)
    table = parquet_file.read()
    if len(values) != table.num_rows:
        raise ValueError(f"{path}: task_index value count does not match row count")
    column_index = table.column_names.index("task_index")
    field = table.schema.field(column_index)
    replacement = pa.array(values, type=field.type)
    updated = table.set_column(column_index, field, replacement)
    compression = "snappy"
    if parquet_file.metadata.num_row_groups and parquet_file.metadata.num_columns:
        compression = parquet_file.metadata.row_group(0).column(0).compression.lower()
    temp_path = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(updated, temp_path, compression=compression)
    temp_path.replace(path)


def _apply_operation(operation: dict[str, Any], *, forward: bool) -> None:
    kind = operation["kind"]
    path = Path(operation["path"])
    if kind == "text_file":
        if forward:
            _atomic_write_text(path, operation["new_text"])
        elif operation["old_exists"]:
            _atomic_write_text(path, operation["old_text"])
        elif path.exists():
            path.unlink()
        return
    if kind == "hdf5_attrs":
        import h5py  # type: ignore

        with h5py.File(path, "r+") as file_obj:
            for change in operation["changes"]:
                name = change["name"]
                if forward:
                    file_obj.attrs[name] = change["new"]
                elif change["old_exists"]:
                    file_obj.attrs[name] = change["old"]
                elif name in file_obj.attrs:
                    del file_obj.attrs[name]
        return
    if kind == "parquet_task_index":
        if forward:
            values = [int(operation["new_value"])] * len(operation["old_values"])
        else:
            values = [int(value) for value in operation["old_values"]]
        _rewrite_parquet_task_index(path, values)
        return
    raise ValueError(f"unknown migration operation kind: {kind}")


def apply_migration(plan: dict[str, Any], manifest_path: str | Path) -> None:
    path = Path(manifest_path).expanduser().resolve()
    if path.exists():
        raise ValueError(f"migration manifest already exists: {path}")
    manifest = json.loads(json.dumps(plan, ensure_ascii=False))
    manifest["status"] = "applying"
    _atomic_write_text(path, _json_text(manifest))
    for operation in manifest.get("operations", []):
        _apply_operation(operation, forward=True)
    manifest["status"] = "applied"
    manifest["applied_at"] = datetime.now().astimezone().isoformat()
    _atomic_write_text(path, _json_text(manifest))


def rollback_migration(manifest_path: str | Path) -> None:
    path = Path(manifest_path).expanduser().resolve()
    manifest = _read_json(path)
    if manifest.get("status") not in {"applying", "applied"}:
        raise ValueError(f"{path}: manifest is not applying/applied")
    for operation in reversed(manifest.get("operations", [])):
        _apply_operation(operation, forward=False)
    manifest["status"] = "rolled_back"
    manifest["rolled_back_at"] = datetime.now().astimezone().isoformat()
    _atomic_write_text(path, _json_text(manifest))


def verify_roots(hdf5_root: str | Path, lerobot_root: str | Path) -> dict[str, Any]:
    report = audit_roots(hdf5_root, lerobot_root)
    if report["pending_changes"] or report["noncanonical_hdf5"]:
        raise ValueError(f"task metadata verification failed: {report}")
    if report["noncanonical_lerobot_episodes"] or report["fallback_occurrences"]:
        raise ValueError(f"task metadata verification failed: {report}")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize ALOHA HDF5/LeRobot task metadata.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("audit", "apply", "verify"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--hdf5-root", type=Path, required=True)
        subparser.add_argument("--lerobot-root", type=Path, required=True)
        if command == "apply":
            subparser.add_argument("--manifest", type=Path, required=True)
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--manifest", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "rollback":
        rollback_migration(args.manifest)
        print(_json_text({"status": "rolled_back", "manifest": str(args.manifest)}), end="")
        return 0
    if args.command == "audit":
        result = audit_roots(args.hdf5_root, args.lerobot_root)
    elif args.command == "verify":
        result = verify_roots(args.hdf5_root, args.lerobot_root)
    else:
        plan = build_migration_plan(args.hdf5_root, args.lerobot_root)
        apply_migration(plan, args.manifest)
        result = verify_roots(args.hdf5_root, args.lerobot_root)
        result["manifest"] = str(args.manifest)
    print(_json_text(result), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
