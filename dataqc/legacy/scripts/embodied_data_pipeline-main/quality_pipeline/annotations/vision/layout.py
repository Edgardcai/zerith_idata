"""Lightweight drink-row layout inference from object detections.

This does not try to detect shelves. It only estimates drink-row y ranges from
the detected beverage boxes, which is less sensitive to robot starting yaw than
fixed horizontal shelf regions.
"""

from __future__ import annotations

from statistics import median
from typing import Any

from .base import Detection


def infer_scene_layout(
    detections: list[Detection],
    base_layout: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Infer drink-row layout from first-frame detections.

    Returns a partial `scene_layout` dict that can be deep-merged with profile,
    manual, or episode metadata.
    """
    base_layout = base_layout or {}
    shelf_base = base_layout.get("shelf") if isinstance(base_layout.get("shelf"), dict) else {}
    min_detections = int(base_layout.get("min_detections") or shelf_base.get("min_detections") or 4)
    dets = [d for d in detections if len(d.bbox) == 4]
    if len(dets) < min_detections:
        return {}

    row_gap = float(base_layout.get("row_split_gap") or shelf_base.get("row_split_gap") or 0.08)
    expected_rows = int(base_layout.get("expected_rows") or shelf_base.get("drink_rows") or 0)
    centers = [(_cx(d), _cy(d), d) for d in dets]
    x_sorted = sorted(centers, key=lambda item: item[0])
    y_sorted = sorted(centers, key=lambda item: item[1])
    x_clusters = _cluster_by_largest_gaps(x_sorted, axis=0, count=2)
    if expected_rows > 0:
        y_clusters = _cluster_by_largest_gaps(y_sorted, axis=1, count=expected_rows)
    else:
        y_clusters = _cluster_by_gap(y_sorted, axis=1, gap=row_gap)

    row_ranges = _build_row_ranges(y_clusters)
    shelf_x_ranges = _build_x_ranges(x_clusters)
    if not row_ranges:
        return {}

    physical_levels = shelf_base.get("physical_levels")
    drink_rows = len(row_ranges) or shelf_base.get("drink_rows")
    shelf: dict[str, Any] = {
        "source": "auto_yolo_rows",
        "confidence": _confidence(len(dets), len(row_ranges)),
    }
    if physical_levels is not None:
        shelf["physical_levels"] = physical_levels
    if drink_rows:
        shelf["drink_rows"] = int(drink_rows)
    if row_ranges:
        shelf["row_y_ranges"] = row_ranges
    if shelf_x_ranges:
        shelf["x_ranges"] = shelf_x_ranges

    return {
        "source": "auto_yolo_rows",
        "shelf": shelf,
    }


def _build_row_ranges(clusters: list[list[tuple[float, float, Detection]]]) -> list[list[float]]:
    if not clusters:
        return []
    clusters = sorted(clusters, key=lambda cluster: median([item[1] for item in cluster]))
    ranges: list[list[float]] = []
    for cluster in clusters:
        y_min = min(item[2].bbox[1] for item in cluster)
        y_max = max(item[2].bbox[3] for item in cluster)
        ranges.append([round(max(0.0, y_min - 0.02), 4), round(min(1.0, y_max + 0.02), 4)])
    return ranges


def _build_x_ranges(clusters: list[list[tuple[float, float, Detection]]]) -> list[list[float]]:
    if len(clusters) < 2:
        return []
    clusters = sorted(clusters, key=lambda cluster: median([item[0] for item in cluster]))
    ranges: list[list[float]] = []
    previous_right = 0.0
    for idx, cluster in enumerate(clusters):
        x_min = min(item[2].bbox[0] for item in cluster)
        x_max = max(item[2].bbox[2] for item in cluster)
        left = max(0.0, x_min - 0.03)
        right = min(1.0, x_max + 0.03)
        if idx > 0 and left < previous_right:
            mid = (left + previous_right) / 2
            ranges[-1][1] = round(mid, 4)
            left = mid
        ranges.append([round(left, 4), round(right, 4)])
        previous_right = right
    return ranges


def _cluster_by_gap(
    items: list[tuple[float, float, Detection]],
    *,
    axis: int,
    gap: float,
) -> list[list[tuple[float, float, Detection]]]:
    if not items:
        return []
    clusters = [[items[0]]]
    for item in items[1:]:
        previous = clusters[-1][-1]
        if item[axis] - previous[axis] > gap:
            clusters.append([item])
        else:
            clusters[-1].append(item)
    return clusters


def _cluster_by_largest_gaps(
    items: list[tuple[float, float, Detection]],
    *,
    axis: int,
    count: int,
) -> list[list[tuple[float, float, Detection]]]:
    if not items or count <= 1:
        return [items] if items else []
    count = min(count, len(items))
    gaps = [
        (items[idx + 1][axis] - items[idx][axis], idx)
        for idx in range(len(items) - 1)
    ]
    split_after = {
        idx for _, idx in sorted(gaps, key=lambda item: item[0], reverse=True)[: count - 1]
    }
    clusters: list[list[tuple[float, float, Detection]]] = [[items[0]]]
    for idx, item in enumerate(items[1:]):
        if idx in split_after:
            clusters.append([item])
        else:
            clusters[-1].append(item)
    return clusters


def _confidence(num_detections: int, num_rows: int) -> float:
    score = 0.45
    score += min(0.25, num_detections * 0.025)
    if num_rows >= 2:
        score += 0.1
    return round(min(0.9, score), 3)


def _cx(detection: Detection) -> float:
    return (float(detection.bbox[0]) + float(detection.bbox[2])) / 2


def _cy(detection: Detection) -> float:
    return (float(detection.bbox[1]) + float(detection.bbox[3])) / 2
