"""Non-destructive repairs. Source clock and frame identity are retained separately."""

import os
import shutil

import h5py
import numpy as np

from .io import (
    CAMS,
    Path,
    encode_selection,
    load,
    parse_task,
    stationary_spans,
    video_path,
    write_json,
)


def keep_indices(d, report, decision, threshold):
    n = d["n"]
    mask = np.ones(n, dtype=bool)
    candidates = next(
        c["detail"]["intervals"] for c in report["checks"] if c["key"] == "stationary"
    )
    for ident in decision["safe_trim_ids"]:
        if not 0 <= ident < len(candidates):
            raise ValueError("静止候选不存在")
        st = candidates[ident]
        b, e = st["start"], st["end"]
        if (b, e) not in stationary_spans(d["state"], d["action"]):
            raise ValueError("仅允许剔除经规则确认的静止段")
        if e - b <= threshold:
            continue
        # Preserve context on both ends. Never delete a labelled stage transition.
        left = threshold // 2
        right = threshold - left
        remove_start, remove_end = b + left, e - right
        if any(
            remove_start <= s["start"] <= remove_end for s in decision["stages"][1:]
        ):
            raise ValueError("静止删除范围包含阶段切换点，需调整范围")
        before, after = remove_start - 1, remove_end
        for data in (d["state"], d["action"]):
            if np.max(abs(data[after] - data[before])) > 0.01:
                raise ValueError("静止段累计漂移过大，删除会制造跳变")
        mask[remove_start:remove_end] = False
    kept = np.flatnonzero(mask)
    if len(kept) < 2:
        raise ValueError("剔除后有效帧不足")
    return kept


def derive(root, out, report, decision, threshold, source_fingerprint):
    root, out = Path(root), Path(out)
    d = load(root)
    if d.get('source_format'):
        from .simulation import derive as derive_simulation
        return derive_simulation(root, out, report, decision, threshold, source_fingerprint)
    kept = keep_indices(d, report, decision, threshold)
    if out.exists():
        raise ValueError("派生目录已存在，不能覆盖历史结果")
    tmp = out.with_name(out.name + ".partial")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    stages = []
    for st in decision["stages"]:
        start, end = np.searchsorted(kept, [st["start"], st["end"]]).tolist()
        if end <= start:
            raise ValueError("删帧后阶段为空")
        stages.append(st | dict(start=start, end=end))
    try:
        with (
            h5py.File(root / "episode.hdf5", "r") as src,
            h5py.File(tmp / "episode.hdf5", "w") as dst,
        ):
            for k, v in src.attrs.items():
                dst.attrs[k] = v

            def copy(k, v):
                if isinstance(v, h5py.Group):
                    g = dst.require_group(k)
                    for a, b in v.attrs.items():
                        g.attrs[a] = b
                elif k == "subtask_transitions":
                    return
                else:
                    value = (
                        v[kept]
                        if v.shape
                        and v.shape[0] == d["n"]
                        and k.startswith(("action/", "observation/", "timestamp/"))
                        else v[()]
                    )
                    z = dst.create_dataset(k, data=value, dtype=v.dtype)
                    for a, b in v.attrs.items():
                        z.attrs[a] = b

            src.visititems(copy)
            dst.attrs["total_frames"] = len(kept)
            dst.attrs["task_name"] = decision["corrected_prompt"]
            dst.attrs["quality_grade"] = decision["grade"]
            dst.attrs["total_subtasks"] = len(stages)
            dst.attrs["completed_subtasks"] = len(stages)
            dst.attrs["qc_source_episode_id"] = d["attrs"].get("episode_id", "")
            dst.attrs["qc_derived"] = True
            dst.create_dataset(
                "subtask_transitions", data=[s["end"] for s in stages], dtype="int32"
            )
            # Explicit derived clock; never used to clear a failing source-clock check.
            if len(kept) != d["n"]:
                dst["timestamp/t"][:] = (
                    d["t"][0] * 1000 + np.arange(len(kept)) * 1000 / 30
                )
                dst.attrs["qc_timebase"] = (
                    "uniform_after_stationary_trim; see provenance.json for original clock"
                )
        from .video_encoding import can_copy_video
        for cam in CAMS:
            if len(kept) == d['n'] and can_copy_video(video_path(root, cam)):
                video_path(tmp, cam).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(video_path(root, cam), video_path(tmp, cam))
            else:
                encode_selection(video_path(root, cam), video_path(tmp, cam), kept)
        meta = dict(d["meta"])
        meta["step_index"] = [
            dict(
                step_number=i + 1,
                start_frame_id=s["start"],
                end_frame_id=s["end"] - 1,
                hand=s["hand"],
                item=s["item"],
            )
            for i, s in enumerate(stages)
        ]
        meta["quality_grade"] = decision["grade"]
        write_json(tmp / "episode_meta.json", meta)
        collection = d["collection"]
        collection.setdefault("config", {})["task_name"] = decision["corrected_prompt"]
        collection["targets"] = parse_task(decision["corrected_prompt"])
        write_json(tmp / "collection_task.json", collection)
        write_json(
            tmp / "review.json",
            dict(
                grade=decision["grade"],
                reviewed=True,
                source="dataqc",
                reason=decision["reason"],
            ),
        )
        write_json(
            tmp / "provenance.json",
            dict(
                source=str(root),
                source_fingerprint=source_fingerprint,
                source_frame_indices=kept.tolist(),
                source_timestamps_seconds=d["t"][kept].tolist(),
                original_prompt=d["task"],
                output_prompt=decision["corrected_prompt"],
                removed_frames=d["n"] - len(kept),
                stages=stages,
                original_stages=decision["stages"],
                source_report=report,
                decision=decision,
            ),
        )
        os.replace(tmp, out)
    except BaseException:
        if tmp.exists():
            shutil.rmtree(tmp)
        raise
    return out
