"""Prompt target resolution.

The old implementation used ``meta.task_preset_id`` + ``meta.task_subtask_idx``
to look up an ordered target list. That makes the left/right hand assignment a
dataset-side prior. Current annotation code treats hand-object assignment as a
vision/VLM decision, so this module only extracts task target names from the
episode prompt and infers collaboration from target count.
"""

from __future__ import annotations

import re
from typing import Any


def resolve(episode: Any, profile_raw: dict[str, Any]) -> dict[str, Any]:
    """Return prompt targets without using task preset metadata."""
    del profile_raw
    meta: dict[str, Any] = episode.meta or {}
    prompt = str(meta.get("prompt") or "")

    prompt_targets = extract_targets_from_prompt(prompt)
    n = len(prompt_targets)
    collaboration = "low" if n == 1 else "high" if n >= 2 else "unknown"
    return _build(
        collaboration=collaboration,
        targets_ordered=prompt_targets,
        preset_id=None,
        match_source="prompt_extract" if prompt_targets else "inferred_from_count",
    )


def _build(
    collaboration: str,
    targets_ordered: list[str],
    preset_id: str | None,
    match_source: str,
) -> dict[str, Any]:
    return {
        "collaboration": collaboration,
        "targets_ordered": targets_ordered,
        "preset_id": preset_id,
        "match_source": match_source,
    }


def extract_targets_from_prompt(prompt: str) -> list[str]:
    match = re.search(r"[Tt]arget\s*:\s*(.+?)(?:\.|$)", prompt)
    if not match:
        return []
    text = match.group(1)
    parts = re.split(r"\s+and\s+|,", text)
    return [p.strip() for p in parts if p.strip()]


def _extract_targets_from_prompt(prompt: str) -> list[str]:
    return extract_targets_from_prompt(prompt)
