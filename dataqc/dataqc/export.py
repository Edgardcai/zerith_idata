import json

"""LeRobot v2.1 writer and independent round-trip validator; split only validated outputs."""
import math
import os
import shutil

import cv2
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .checks import numeric_checks
from .io import (
    CAMS,
    NAMES,
    Path,
    clean,
    encode_selection,
    open_video,
    load,
    parse_task,
    normalized_task,
    read_json,
    video_path,
    hdf5_path,
    write_json,
)
from .motion import RULE_VERSION, contextual_checks
from .parallel import ordered_map

DATA = "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet"
VIDEO = "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4"


def path_for(root, i, key=None):
    return Path(root) / (VIDEO if key else DATA).format(
        episode_chunk=i // 1000, episode_index=i, video_key=key
    )


def jsonl(p, rows):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "".join(
            json.dumps(clean(r), ensure_ascii=False, allow_nan=False) + "\n"
            for r in rows
        )
    )


def rows(p):
    return [json.loads(x) for x in Path(p).read_text().splitlines() if x.strip()]


def stats(v):
    a = np.asarray(v, dtype=float)
    return clean(
        dict(
            min=a.min(axis=0),
            max=a.max(axis=0),
            mean=a.mean(axis=0),
            std=a.std(axis=0),
            count=[len(a)],
        )
    )


def video_stats(p):
    cap = open_video(p)
    means = []
    squares = []
    mins = []
    maxs = []
    n = 0
    shape = None
    while True:
        ok, img = cap.read()
        if not ok:
            break
        shape = img.shape
        v = (
            cv2.cvtColor(cv2.resize(img, (64, 48)), cv2.COLOR_BGR2RGB).astype(float)
            / 255
        )
        means.append(v.mean(axis=(0, 1)))
        squares.append((v * v).mean(axis=(0, 1)))
        mins.append(v.min(axis=(0, 1)))
        maxs.append(v.max(axis=(0, 1)))
        n += 1
    cap.release()
    if not n:
        raise ValueError("导出视频为空")
    mean = np.mean(means, axis=0)
    std = np.sqrt(np.maximum(0, np.mean(squares, axis=0) - mean**2))
    return clean(
        dict(
            min=np.min(mins, axis=0).reshape(3, 1, 1),
            max=np.max(maxs, axis=0).reshape(3, 1, 1),
            mean=mean.reshape(3, 1, 1),
            std=std.reshape(3, 1, 1),
            count=[n],
        )
    ), shape


def entry_data(entry):
    if "source_parquet" not in entry:
        return load(entry["root"])
    table = pq.read_table(entry["source_parquet"])
    return dict(n=len(table), state=np.asarray(table["observation.state"].to_pylist()),
                action=np.asarray(table["action"].to_pylist()), task=entry.get("task", ""),
                t=np.asarray(table["timestamp"].to_pylist()), transitions=[], attrs={})


def describe_entry(entry):
    d=entry_data(entry)
    b,e=entry.get('range',[0,d['n']])
    if not 0<=b<e<=d['n']:raise ValueError('导出区间越界')
    task=entry.get('task',d['task'])
    if not (entry.get('direct') or entry.get('skip_quality_checks')) and not parse_task(task):raise ValueError('导出指令不符合模板')
    return e-b,task


def write_episode(payload):
    i,entry,temp,global_index,tid=payload
    features={}
    root = Path(entry["root"])
    d = entry_data(entry)
    b, e = entry.get("range", [0, d["n"]])
    idx = np.arange(b, e)
    n = len(idx)
    if not 0 <= b < e <= d["n"]:
        raise ValueError("导出区间越界")
    task = entry.get("task", d["task"])
    if not (entry.get("direct") or entry.get("skip_quality_checks")) and not parse_task(task):
        raise ValueError("导出指令不符合模板")
    cols = {
        "observation.state": pa.array(
            d["state"][idx].astype(np.float32).tolist(),
            type=pa.list_(pa.float32(), 23),
        ),
        "action": pa.array(
            d["action"][idx].astype(np.float32).tolist(),
            type=pa.list_(pa.float32(), 23),
        ),
        "timestamp": pa.array(np.arange(n, dtype=np.float32) / 30),
        "frame_index": pa.array(np.arange(n, dtype=np.int64)),
        "episode_index": pa.array(np.full(n, i, dtype=np.int64)),
        "index": pa.array(
            np.arange(global_index, global_index + n, dtype=np.int64)
        ),
        "task_index": pa.array(np.full(n, tid, dtype=np.int64)),
    }
    if "source_parquet" in entry:
        source_table = pq.read_table(entry["source_parquet"]).slice(b, n)
        cols["observation.state"] = source_table["observation.state"]
        cols["action"] = source_table["action"]
    table = pa.table(cols)
    p = path_for(temp, i)
    p.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, p, compression="zstd")
    epstats = {}
    for k, v in cols.items():
        arr = np.asarray(v.to_pylist())
        epstats[k] = stats(arr if arr.ndim > 1 else arr[:, None])
        features[k] = dict(
            dtype="float32"
            if k in ("observation.state", "action", "timestamp")
            else "int64",
            shape=[23] if k in ("observation.state", "action") else [1],
            names=NAMES if k in ("observation.state", "action") else None,
        )
    for cam in CAMS:
        k = "observation.images." + cam
        v = path_for(temp, i, k)
        v.parent.mkdir(parents=True, exist_ok=True)
        # For post-conversion split, read videos from the validated full LeRobot output.
        source_video = (
            Path(entry["videos"][cam])
            if "videos" in entry
            else video_path(root, cam)
        )
        if b == 0 and e == d["n"]:
            shutil.copyfile(source_video, v)
        else:
            encode_selection(source_video, v, idx)
        st, shape = video_stats(v)
        if st["count"][0] != n:
            raise ValueError(f"{cam} 视频帧数 {st['count'][0]} 与数据帧数 {n} 不一致")
        epstats[k] = st
        if k in features and features[k]["shape"] != list(shape):
            raise ValueError("同一数据集视频分辨率不一致")
        features[k] = dict(
            dtype="video",
            shape=list(shape),
            names=["height", "width", "channels"],
            info={
                "video.fps": 30,
                "video.codec": "h264",
                "video.pix_fmt": "yuv420p",
                "video.is_depth_map": False,
                "has_audio": False,
            },
        )
    provenance = entry.get("provenance") or read_json(root / "provenance.json")
    if entry.get("direct"):
        targets = parse_task(normalized_task(task)) or {}
        ends = d.get("transitions", [])
        hands = list(targets) if len(ends)==len(targets) else ['left','right'] if len(ends)==2 else []
        stages = [dict(hand=hand, item=targets.get(hand,''), start=b, end=e)
                  for hand,b,e in zip(hands, [0]+ends[:-1], ends)]
        provenance = dict(source=str(root), source_frame_indices=list(range(d["n"])),
                          stages=stages, output_prompt=task)
        from .references import height_reference
        try:provenance["height_reference"] = height_reference(root)
        except (OSError,ValueError,KeyError,TypeError) as exc:provenance["height_reference"] = dict(error=str(exc))
    m = dict(
        episode_index=i,
        lerobot_episode_index=i,
        num_frames=n,
        source_hdf5=entry.get("source_hdf5", "") if "source_parquet" in entry else str(hdf5_path(root)),
        source_format=d.get("source_format", "zerith_columnar"),
        source_episode=str(root),
        source_root=str(root),
        source_original=provenance["source"],
        source_range=[b, e],
        source_frames=provenance["source_frame_indices"][b:e],
        grade=entry["grade"],
        task=task,
        stages=provenance["stages"],
        source_lerobot=entry.get("source_lerobot"),
        source_lerobot_episode_index=entry.get("source_lerobot_episode_index"),
        direct=bool(entry.get("direct")),
        provenance=provenance,
    )
    ep=dict(episode_index=i,tasks=[task],length=n,quality_grade=entry["grade"])
    return ep,dict(episode_index=i,stats=epstats),m,features


def create_dataset(entries, out, threshold=40, progress=lambda _: None, *, quality_check=True):
    """entries contain a repaired root, optional [start,end) and task for stage exports."""
    out = Path(out)
    if out.exists():
        raise ValueError("输出已存在")
    temp = out.with_name(out.name + ".partial")
    if temp.exists():
        shutil.rmtree(temp)
    temp.mkdir(parents=True)
    ep_rows = []
    stat_rows = []
    mapping = []
    tasks = []
    features = {}
    global_index = 0
    try:
        descriptions=ordered_map(describe_entry,entries,progress,'读取导出索引')
        jobs=[]
        for i,(entry,(n,task)) in enumerate(zip(entries,descriptions)):
            if task not in tasks:tasks.append(task)
            jobs.append((i,entry,temp,global_index,tasks.index(task)))
            global_index+=n
        for ep,st,m,episode_features in ordered_map(write_episode,jobs,progress,'写入 LeRobot'):
            for key,value in episode_features.items():
                if key in features and features[key]!=value:
                    raise ValueError('同一数据集字段或视频分辨率不一致：'+key)
                features[key]=value
            ep_rows.append(ep);stat_rows.append(st);mapping.append(m)
        return publish_dataset(temp, out, ep_rows, stat_rows, mapping, tasks, features, global_index, threshold, progress, quality_check)
    except BaseException:
        # Keep failed output and report isolated for diagnosis. Never publish it.
        raise



def publish_dataset(temp, out, ep_rows, stat_rows, mapping, tasks, features, global_index,
                    threshold=40, progress=lambda _:None, quality_check=False):
    info = dict(
        codebase_version="v2.1",
        robot_type="zerith",
        fps=30,
        total_episodes=len(ep_rows),
        total_frames=global_index,
        total_tasks=len(tasks),
        total_videos=len(ep_rows) * 3,
        total_chunks=math.ceil(len(ep_rows) / 1000),
        chunks_size=1000,
        splits={"train": f"0:{len(ep_rows)}"},
        data_path=DATA,
        video_path=VIDEO,
        features=features,
    )
    write_json(temp / "meta/info.json", info)
    jsonl(
        temp / "meta/tasks.jsonl",
        [dict(task_index=i, task=t) for i, t in enumerate(tasks)],
    )
    jsonl(temp / "meta/episodes.jsonl", ep_rows)
    jsonl(temp / "meta/episodes_stats.jsonl", stat_rows)
    write_json(temp / "meta/episode_name_mapping.json", dict(episodes=mapping))
    aggregate = {}
    for key in features:
        values = [r["stats"][key] for r in stat_rows]
        weights = np.array([v["count"][0] for v in values], dtype=float)
        if not len(values):
            continue
        means = np.array([v["mean"] for v in values])
        stds = np.array([v["std"] for v in values])
        mean = np.average(means, axis=0, weights=weights)
        variance = np.average(
            stds**2 + (means - mean) ** 2, axis=0, weights=weights
        )
        aggregate[key] = dict(
            min=np.min([v["min"] for v in values], axis=0),
            max=np.max([v["max"] for v in values], axis=0),
            mean=mean,
            std=np.sqrt(variance),
            count=[int(weights.sum())],
        )
    write_json(temp / "meta/stats.json", aggregate)
    if quality_check:
        report = validate_dataset(temp, threshold, progress)
        write_json(temp / "qc_report.json", report)
        if not report["passed"]:
            raise ValueError("LeRobot 复检失败：" + json.dumps(report["issues"], ensure_ascii=False)[:800])
    write_json(temp / "meta/conversion.json", dict(quality_check="completed" if quality_check else "not_run"))
    os.replace(temp, out)
    return dict(path=str(out), episodes=len(ep_rows), frames=global_index,
                passed=True if quality_check else None, quality_check="completed" if quality_check else "not_run")


def validate_episode(payload):
    i,ep,m,root,info,tasks,count,threshold=payload
    issues=[];warnings=[];checked=0
    try:
        if ep["episode_index"] != i or m["episode_index"] != i:
            raise ValueError("episode 索引不连续")
        table = pq.read_table(path_for(root, i))
        n = table.num_rows
        data = table.to_pydict()
        if n != ep["length"] or not n:
            raise ValueError("轨迹长度不一致")
        # Older shared-engine exports omitted this legacy preflight field.
        # Accept their absence, but never accept a conflicting frame count.
        if "num_frames" in m and (
            type(m["num_frames"]) is not int or m["num_frames"] != n
        ):
            raise ValueError("mapping num_frames 与 Parquet 帧数不一致")
        for key, want in [
            ("frame_index", np.arange(n)),
            ("index", np.arange(count, count + n)),
            ("episode_index", np.full(n, i)),
        ]:
            if not np.array_equal(data[key], want):
                raise ValueError(key + " 不一致")
        if not np.allclose(
            data["timestamp"], np.arange(n) / info["fps"], atol=1e-5, rtol=1e-5
        ):
            raise ValueError("时间索引不一致")
        tid = set(data["task_index"])
        if len(tid) != 1 or not 0 <= next(iter(tid)) < len(tasks):
            raise ValueError("任务索引无效")
        task = tasks[next(iter(tid))]["task"]
        if ep["tasks"] != [task] or m["task"] != task:
            raise ValueError("任务映射不一致")
        source = load(m["source_root"])
        b, e = m["source_range"]
        prov = m.get("provenance") or read_json(Path(m["source_root"]) / "provenance.json")
        if m["source_frames"] != prov["source_frame_indices"][b:e]:
            raise ValueError("来源帧映射不一致")
        for key, src in [
            ("observation.state", source["state"]),
            ("action", source["action"]),
        ]:
            arr = np.asarray(data[key])
            expected = src[b:e].astype(np.float32)
            if (
                arr.shape != (n, 23)
                or not np.isfinite(arr).all()
                or not np.array_equal(arr, expected)
            ):
                raise ValueError(key + " 与 HDF5 不一致")
        checks = numeric_checks(
            np.array(data["observation.state"]),
            np.array(data["action"]),
            np.array(data["timestamp"]),
            threshold,
        )
        if source.get('source_format'):
            from .simulation import numeric as simulation_numeric
            sim_context=dict(source, state=np.array(data['observation.state']), action=np.array(data['action']),
                t=np.array(data['timestamp']), n=len(data['timestamp']), measured_state=source['measured_state'][b:e])
            checks=simulation_numeric(sim_context,threshold)
        # Both closures are assessed on the complete episode. A right-hand
        # clip may start with the left gripper already holding its object.
        full_context = dict(source, task=prov["output_prompt"])
        if m.get("direct"):
            full_context.update(state=np.array(data["observation.state"]), action=np.array(data["action"]),
                                t=np.array(data["timestamp"]), n=n, task=normalized_task(task))
            from .vision import validate_decision
            try:
                validate_decision(dict(grade="A", corrected_prompt=normalized_task(task), stages=m["stages"], safe_trim_ids=[]),
                                  full_context, [], allow_relabel=True)
            except (ValueError, KeyError) as ex:
                warnings.append(dict(episode=i, check=dict(key="stages_prompt", label="任务文本与阶段", status="warn", detail=dict(issues=[str(ex)]))))
            # Source-only metadata and embedded-image checks are deferred too.
            from .checks import raw_checks
            raw = raw_checks(m["source_root"], threshold)
            checks.extend(c for c in raw["checks"] if c["key"] in ("schema", "timestamps", "fps") or c["key"].startswith(("hdf5_", "depth_")))
        checks.extend(
            contextual_checks(full_context, m["source_root"], prov["stages"])
        )
        for c in checks:
            if c["status"] == "fail":
                issues.append(dict(episode=i, check=c))
            elif c["status"] == "warn":
                warnings.append(dict(episode=i, check=c))
        for cam in CAMS:
            k = "observation.images." + cam
            p = path_for(root, i, k)
            cap = open_video(p)
            frames = 0
            shape = None
            source_video = video_path(m["source_root"], cam)
            src = open_video(source_video)
            src.set(cv2.CAP_PROP_POS_FRAMES, b)
            mismatch = []
            while True:
                ok, img = cap.read()
                if not ok:
                    break
                good, original = src.read()
                shape = list(img.shape)
                if (
                    not good
                    or original.shape != img.shape
                    or float(
                        np.mean(abs(img.astype(float) - original.astype(float)))
                    )
                    > 12
                ):
                    mismatch.append(frames)
                frames += 1
            fps = cap.get(cv2.CAP_PROP_FPS)
            cap.release()
            src.release()
            if (
                frames != n
                or shape != info["features"][k]["shape"]
                or abs(fps - 30) > 0.1
                or mismatch
            ):
                raise ValueError(cam + " 视频帧数 / 形状 / 来源一致性失败")
        checked = n
    except Exception as ex:
        issues.append(dict(episode=i, error=str(ex)))
    return checked,issues,warnings


def validate_dataset(root, threshold=40, progress=lambda _:None):
    root = Path(root)
    info = read_json(root / "meta/info.json")
    episodes = rows(root / "meta/episodes.jsonl")
    tasks = rows(root / "meta/tasks.jsonl")
    maps = read_json(root / "meta/episode_name_mapping.json")["episodes"]
    from .portable_quality import sources_available, validate_portable
    if not sources_available(maps):
        return validate_portable(root, threshold, progress)
    issues = []
    warnings = []
    count = 0
    if info.get("codebase_version") != "v2.1":
        issues.append("不支持的 LeRobot 版本")
    if (
        len(episodes) != info.get("total_episodes")
        or len(maps) != len(episodes)
        or len(list(root.glob("data/**/*.parquet"))) != len(episodes)
    ):
        issues.append("episode 总量不一致")
    if (
        len(tasks) != info.get("total_tasks")
        or [t["task_index"] for t in tasks] != list(range(len(tasks)))
        or len({t["task"] for t in tasks}) != len(tasks)
    ):
        issues.append("task 总量或索引不一致")
    for t in tasks:
        if not parse_task(normalized_task(t["task"])):
            warnings.append(dict(check="prompt",reason="提示词句式需要复核，物品名称不限"))
    for k in ["observation.state", "action"]:
        if info.get("features", {}).get(k, {}).get("names") != NAMES:
            issues.append(k + " 维度名称或顺序不符")
    jobs=[];offset=0
    for i,ep in enumerate(episodes):
        jobs.append((i,ep,maps[i] if i<len(maps) else {},root,info,tasks,offset,threshold))
        offset+=ep.get('length',0) if type(ep.get('length')) is int else 0
    for checked,episode_issues,episode_warnings in ordered_map(validate_episode,jobs,progress,'完整复检'):
        count+=checked;issues.extend(episode_issues);warnings.extend(episode_warnings)
    if count != info.get("total_frames"):
        issues.append("total_frames 不一致")
    return dict(
        rule_version=RULE_VERSION,
        passed=not issues,
        issues=issues,
        warnings=warnings,
        episodes=len(episodes),
        frames=count,
        source_correspondence_checked=True,
    )


def split_dataset(full, out, threshold=40, progress=lambda _: None, *, quality_check=False):
    if not quality_check:
        from .direct_export import split_direct
        return split_direct(full, out, threshold, progress)
    full = Path(full)
    if quality_check:
        check = validate_dataset(full, threshold)
        if not check["passed"]:
            raise ValueError("完整 LeRobot 尚未通过复检，不能切分")
    entries = {"left": [], "right": []}
    for m in read_json(full / "meta/episode_name_mapping.json")["episodes"]:
        if not m.get("stages"):
            raise ValueError(f"episode {m.get('episode_index')}: 缺少阶段边界，无法确定切分位置")
        for st in m["stages"]:
            hand = st["hand"]
            if hand not in entries:
                raise ValueError(f"无法识别阶段手别：{hand}")
            task = f"Grasp {st['item']} with the {hand} hand"
            entries[hand].append(
                dict(
                    root=m["source_root"],
                    grade=m["grade"],
                    range=[st["start"], st["end"]],
                    task=task,
                    source_lerobot=str(full),
                    skip_quality_checks=not quality_check,
                    provenance=m.get("provenance"),
                    source_parquet=str(path_for(full, m["episode_index"])),
                    videos={
                        cam: str(
                            path_for(
                                full, m["episode_index"], "observation.images." + cam
                            )
                        )
                        for cam in CAMS
                    },
                )
            )
    results = []
    for hand, values in entries.items():
        if not values:
            continue
        target = Path(out) / hand
        if target.exists():
            if not quality_check:
                raise ValueError(f"切分输出已存在：{target}")
            report = validate_dataset(target, threshold)
            if not report["passed"]:
                raise ValueError("已有切分目录未通过复检")
            results.append(
                dict(
                    path=str(target),
                    episodes=report["episodes"],
                    frames=report["frames"],
                    passed=True,
                )
            )
        else:
            results.append(create_dataset(values, target, threshold, progress, quality_check=quality_check))
    return results
