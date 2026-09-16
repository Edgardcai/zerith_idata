import csv
import io
import json
import time
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from . import db
from .config import CONFIG, VAR, REAL_SOURCE_ROOT, SIM_SOURCE_ROOT, api_config, settings
from .io import (
    CAMS,
    clean,
    discover,
    frame,
    load,
    hdf5_path,
    is_simulation,
    read_json,
    sha,
    video_path,
    write_json,
)
from .motion import RULE_VERSION, source_height, warning_messages
from . import library
from .robots import get_adapter, profiles
from .vision import Stage, validate_decision

app = FastAPI(title="数据质检", docs_url="/api/docs")
db.init()
library.init()
app.include_router(library.router)
WEB = Path(__file__).resolve().parents[1] / "web"
SOURCE_ROOT = REAL_SOURCE_ROOT

def source_roots():
    return (SOURCE_ROOT.resolve(), SIM_SOURCE_ROOT.resolve())

def dataset_root(p):
    return p.resolve().parent in source_roots() and not hdf5_path(p).is_file()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewRun(Strict):
    root: str
    mode: Literal["auto", "manual"] = "auto"
    robot: str = "zerith"
    stationary_frames: Literal[20, 40, 60] | None = None
    limit: int = Field(default=0, ge=0, le=10000)
    assist_manual: bool = True
    vlm_enabled: StrictBool | None = None


class Review(Strict):
    revision: int
    grade: Literal["A", "B", "F"]
    reason: str = Field(default="", max_length=2000)
    corrected_prompt: str
    stages: list[Stage]
    safe_trim_ids: list[int]
    actor: str = Field(min_length=1, max_length=80)


class Settings(Strict):
    motion_batch_size: int = Field(default=10, ge=1, le=20)
    motion_batch_concurrency: int = Field(default=2, ge=1, le=4)
    yolo_frame_offset: int = Field(default=40, ge=1, le=120)
    yolo_confidence: float = Field(default=.25, ge=.01, le=1)
    yolo_thresholds_path: str = ''
    api_model: Literal["gpt-5.6-terra"] = "gpt-5.6-terra"
    vlm_enabled: StrictBool = False
    vlm_token_budget: int = Field(default=250000, ge=10000, le=500000)
    max_output_tokens: int = Field(default=2500, ge=500, le=5000)
    stationary_frames: Literal[20, 40, 60] = 40
    yolo_path: str
    vlm_fps: float = Field(default=2, ge=1, le=10)
    window_seconds: float = Field(default=4, ge=2, le=8)
    overlap_seconds: float = Field(default=1, ge=0.5, le=2)
    max_requests_per_episode: int = Field(default=40, ge=5, le=200)
    device: str
    export_grades: list[Literal["A", "B"]]


@app.middleware("http")
async def origin_guard(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "请从本机质检页面提交操作"}, status_code=403)
    return await call_next(request)


def need_run(rid):
    r = db.get_run(rid)
    if not r:
        raise HTTPException(404, "任务不存在")
    return r


def need_ep(eid):
    e = db.episode(eid)
    if not e:
        raise HTTPException(404, "记录不存在")
    return e


@app.get("/api/settings")
def get_settings():
    c = settings()
    c.pop("api_file", None)
    try:
        u, m, k = api_config()
        c.update(api_model=m, api_configured=True)
    except Exception:
        c.update(api_configured=False)
    return c | dict(robots=profiles(), source_roots=dict(real=str(SOURCE_ROOT), simulation=str(SIM_SOURCE_ROOT)))


@app.put("/api/settings")
def set_settings(s: Settings):
    if s.window_seconds <= s.overlap_seconds:
        raise HTTPException(422, "窗口长度必须大于重叠长度")
    p = Path(s.yolo_path).expanduser().resolve()
    if s.vlm_enabled and (not p.is_file() or p.suffix != ".pt"):
        raise HTTPException(422, "请选择本地 .pt 权重文件")
    if not s.export_grades:
        raise HTTPException(422, "至少选择一个导出等级")
    if s.vlm_enabled and s.yolo_thresholds_path:
        from .yolo_gate import legacy_rules
        try:
            payload = legacy_rules()._threshold_payload(Path(s.yolo_thresholds_path))
            if any(not 0 < float(v) <= 1 for v in payload['per_class_conf'].values()):
                raise ValueError('类别阈值应在 (0,1] 内')
        except (RuntimeError, ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc))
    current = settings()
    new = current | s.model_dump() | {"yolo_path": str(p)}
    write_json(CONFIG, new)
    db.audit(None, None, "settings", "operator", current, new)
    return get_settings()


class CategoryQCSettings(Strict):
    vlm_enabled: StrictBool


@app.patch("/api/settings/category-qc")
def set_category_qc(s: CategoryQCSettings):
    current = settings()
    new = current | s.model_dump()
    write_json(CONFIG, new)
    db.audit(None, None, "category_qc_settings", "operator", current, new)
    return dict(vlm_enabled=new['vlm_enabled'], api_model=new['api_model'])


@app.get("/api/sources")
def sources():
    return [str(p) for root in source_roots() if root.exists() for p in sorted(root.iterdir()) if p.is_dir()]


@app.get("/api/datasets")
def datasets():
    out = []
    for name in sources():
        p = Path(name)
        if not dataset_root(p):
            continue
        try:
            episodes = discover(p)
            count = len(episodes)
            sim = bool(episodes) and all(is_simulation(e) for e in episodes)
            height = dict(policy='disabled',note='升降柱不参与质检')
            out.append(dict(root=str(p.resolve()), name=p.name, count=count, height=height,
                            source_format='zerith_sim_v1' if sim else 'zerith_columnar',source_label='仿真' if sim else '真机'))
        except (OSError, ValueError):
            out.append(dict(root=str(p), name=p.name, count=0, error="目录暂时无法读取"))
    return out


@app.post("/api/runs")
def create_run(body: NewRun):
    try:
        get_adapter(body.robot)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    root = Path(body.root).expanduser().resolve()
    if not dataset_root(root):
        raise HTTPException(422, f"请选择 {SOURCE_ROOT} 或 {SIM_SOURCE_ROOT} 下的一级数据集目录，不能选择总目录或单条 episode")
    try:
        paths = discover(root)
    except Exception as ex:
        raise HTTPException(422, str(ex))
    if not paths:
        raise HTTPException(422, "目录中未找到真机 episode.hdf5 或仿真 states/aligned_joints.h5")
    total = len(paths)
    if body.limit:
        paths = paths[: body.limit]
    for r in db.runs():
        if r["root"] == str(root) and r["status"] in (
            "queued",
            "running",
            "paused",
            "retry_wait",
        ):
            raise HTTPException(409, "此目录已有未结束任务")
    cfg = settings() | dict(
        stationary_frames=body.stationary_frames or settings()["stationary_frames"],
        assist_manual=body.assist_manual,
        source_count=total,
        selected_count=len(paths),
        robot=body.robot,
    )
    if body.vlm_enabled is not None:
        cfg['vlm_enabled'] = body.vlm_enabled
    if cfg.get("vlm_enabled", True):
        cfg["yolo_sha256"] = sha(cfg["yolo_path"])
        try:
            cfg["api_model"] = api_config(cfg)[1]
        except Exception:
            pass
    rid = db.create(str(root), body.mode, cfg, paths)
    db.audit(rid, None, "create", "operator", None, body.model_dump())
    return {"id": rid}


@app.get("/api/runs")
def list_runs():
    out = []
    for r in db.runs():
        ep = db.episodes(r["id"])
        counts = {g: sum(e["grade"] == g for e in ep) for g in ["A", "B", "F"]}
        counts["pending"] = sum(e["status"] in ("review", "incomplete") for e in ep)
        r.update(counts=counts, total=len(ep))
        r["config"].pop("api_file", None)
        out.append(r)
    return out


def usage_summary(rid):
    from .vision import usage_records
    usage = {
        "completed_requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    for p in (VAR / "runs" / rid).glob("episode_*"):
        for data in usage_records(p):
            u = data.get("usage") or {}
            usage["completed_requests"] += int(data.get('status', 'completed') == 'completed') * data.get('request_count', 1)
            for k in ("input_tokens", "output_tokens", "total_tokens"):
                usage[k] += int(u.get(k, 0))
    return usage


@app.get("/api/runs/{rid}")
def run_detail(rid: str):
    r = need_run(rid)
    r["config"].pop("api_file", None)
    r["usage"] = usage_summary(rid)
    ep = db.episodes(rid)
    return r | {
        "episodes": [
            {k: v for k, v in e.items() if k != "data"}
            | {
                "score": e["data"].get("raw_report", {}).get("score"),
                "rules_outdated": bool(e["data"].get("raw_report"))
                and e["data"]["raw_report"].get("version") != RULE_VERSION,
                "warnings": warning_messages(e["data"].get("raw_report", {})) + e['data'].get('yolo_report', {}).get('warnings', []),
                "matching": dict(status=e['data'].get('yolo_report', {}).get('status'),
                                 vlm_called=e['data'].get('visual', {}).get('vlm_called')),
            }
            for e in ep
        ]
    }


@app.post("/api/runs/{rid}/{action}")
def control(rid: str, action: Literal["pause", "resume", "cancel", "retry"]):
    r = need_run(rid)
    if action == "pause":
        if r["status"] not in ("running", "queued", "retry_wait"):
            raise HTTPException(409, "当前任务不在运行")
        db.update("runs", rid, status="paused")
    elif action == "cancel":
        db.update("runs", rid, status="cancelled")
    else:
        if r["status"] == "running":
            raise HTTPException(409, "任务仍在运行")
        for e in db.episodes(rid):
            if e["status"] == "retry_wait" or (
                action == "retry" and e["status"] == "incomplete"
            ):
                data = e["data"]
                data["auto_retry_count"] = 0
                db.update("episodes", e["id"], status="queued", data=data)
        config = dict(r["config"])
        current = settings()
        for key in ("api_model", "vision_version", "reasoning_effort", "max_output_tokens", "vlm_token_budget", "yolo_frame_offset", "yolo_confidence", "yolo_thresholds_path"):
            config[key] = current[key]
        db.update("runs", rid, status="queued", error="", config=config)
    return need_run(rid)


@app.get("/api/episodes/{eid}")
def episode_detail(eid: int):
    e = need_ep(eid)
    from .reporting import presentation
    e['data']['presentation']=presentation(e['data'])
    try:
        d = load(e["root"])
        e["trajectory"] = library.trajectory(d["state"], d["action"], d["t"]-d["t"][0], d["task"], d["transitions"])
        e["trajectory"]["attrs"] = d["attrs"]
        e["trajectory"]["source_format"] = d.get("source_format","zerith_columnar")
        e["trajectory"]["source_timestamps"] = d["t"].tolist()
    except Exception as ex:
        e["trajectory_error"] = str(ex)
    return clean(e)


@app.post("/api/episodes/{eid}/review")
def review(eid: int, body: Review):
    e = need_ep(eid)
    r = need_run(e["run_id"])
    if r["status"] in ("running", "queued"):
        raise HTTPException(409, "请在本轮处理结束后保存标注，或暂停任务")
    data = e["data"]
    report = data.get("raw_report")
    if not report:
        raise HTTPException(409, "尚无基础质检报告")
    from .quality_policy import normalize_raw
    report=normalize_raw(report)
    fatal = [
        c
        for c in report["checks"]
        if c["status"] == "fail" and c["key"] not in ("stationary", "prompt")
    ]
    if fatal and body.grade != "F":
        raise HTTPException(422, "结构、数值或视频硬失败不可手动改成通过")
    decision = body.model_dump(exclude={"revision", "actor"})
    decision["findings"] = []
    if body.grade != "F":
        try:
            candidates = next(
                c["detail"]["intervals"]
                for c in report["checks"]
                if c["key"] == "stationary"
            )
            validate_decision(decision, load(e["root"]), candidates, allow_relabel=True)
        except Exception as ex:
            raise HTTPException(422, str(ex))
    previous = data.get("manual_decision")
    data["manual_decision"] = decision
    # Optimistic concurrency keeps two operators from overwriting each other.
    with db.connect() as conn:
        result = conn.execute(
            "UPDATE episodes SET data=?,revision=revision+1,status=?,grade=?,reason=? WHERE id=? AND revision=?",
            (
                json.dumps(clean(data), ensure_ascii=False),
                "rejected" if body.grade == "F" else "queued",
                body.grade,
                body.reason,
                eid,
                body.revision,
            ),
        )
        if result.rowcount != 1:
            raise HTTPException(409, "标注已被修改，请刷新后重试")
    db.audit(e["run_id"], eid, "manual_review", body.actor, previous, decision)
    return need_ep(eid)


@app.get("/api/episodes/{eid}/video/{cam}")
def media(eid: int, cam: str):
    e = need_ep(eid)
    if cam not in CAMS:
        raise HTTPException(404)
    p = video_path(e["root"], cam)
    if not p.is_file():
        raise HTTPException(404, "视频不存在")
    return FileResponse(p, media_type="video/mp4")


@app.get("/api/episodes/{eid}/frame/{cam}/{index}")
def still(eid: int, cam: str, index: int):
    e = need_ep(eid)
    if cam not in CAMS or index < 0:
        raise HTTPException(404)
    try:
        img = frame(e["root"], cam, index)
        ok, b = cv2.imencode(".jpg", img)
        return Response(b.tobytes(), media_type="image/jpeg")
    except Exception:
        raise HTTPException(404, "帧不可用")


@app.get("/api/runs/{rid}/report.json")
def report(rid: str):
    r = need_run(rid)
    r["config"].pop("api_file", None)
    return Response(
        json.dumps(
            clean({"run": r, "episodes": db.episodes(rid)}),
            ensure_ascii=False,
            indent=2,
        ),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="qc-{rid}.json"'},
    )


@app.get("/api/runs/{rid}/report.csv")
def report_csv(rid: str):
    need_run(rid)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        ["episode", "source", "grade", "status", "reason", "check", "result", "detail"]
    )
    for e in db.episodes(rid):
        checks = list(e["data"].get("raw_report", {}).get("checks", []))
        checks += [dict(label='YOLO ' + ('左手' if h['hand']=='left' else '右手') + '物品匹配',
                        status=h['status'],detail=h) for h in e['data'].get('yolo_report', {}).get('hands', [])]
        for c in checks:
            writer.writerow(
                [
                    e["number"],
                    e["root"],
                    e["grade"],
                    e["status"],
                    e["reason"],
                    c["label"],
                    c["status"],
                    json.dumps(c["detail"], ensure_ascii=False),
                ]
            )
    return Response(
        "\ufeff" + out.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="qc-{rid}.csv"'},
    )


@app.get("/api/health")
def health():
    heartbeat = read_json(VAR / "worker-heartbeat.json")
    alive = False
    try:
        pid = int(heartbeat["pid"])
        alive = "dataqc.worker" in Path(f"/proc/{pid}/cmdline").read_text()
    except (KeyError, ValueError, OSError):
        pass
    return {
        "ok": True,
        "time": time.time(),
        "worker_alive": alive,
        "last_worker_update": heartbeat.get("time"),
    }


app.mount("/assets", StaticFiles(directory=WEB), name="assets")


@app.get("/")
@app.get("/datasets")
@app.get("/review")
@app.get("/jobs")
@app.get("/exports")
@app.get("/settings")
@app.get("/hdf5")
@app.get("/lerobot")
@app.get("/compare")
def index():
    return FileResponse(WEB / "index.html")


def raw_root(value):
    root=Path(value).expanduser().resolve()
    if not any(root.is_relative_to(p) for p in source_roots()) or not hdf5_path(root).is_file():
        raise HTTPException(422,"请选择采集目录内的 episode")
    return root


@app.get("/api/hdf5/episodes")
def raw_episodes(root:str):
    path=Path(root).expanduser().resolve()
    if not dataset_root(path):raise HTTPException(422,"请选择一级数据集")
    return [dict(root=p,name=Path(p).name)for p in discover(path)]


@app.get("/api/hdf5/replay")
def raw_replay(root:str):
    path=raw_root(root)
    try:
        d=load(path)
        t=library.trajectory(d["state"],d["action"],d["t"]-d["t"][0],d["task"],d["transitions"])
        t['attrs']=d['attrs'];t['source_timestamps']=d['t'].tolist();t['source_format']=d.get('source_format','zerith_columnar')
        return dict(root=str(path),trajectory=t,data={},status='unprocessed',grade=None,reason='尚未创建处理任务，可先回放查看')
    except (ValueError,KeyError,OSError) as e:raise HTTPException(422,str(e))


@app.get("/api/hdf5/video/{cam}")
def raw_video(cam:str,root:str):
    path=raw_root(root)
    if cam not in CAMS:raise HTTPException(404)
    file=video_path(path,cam)
    if not file.is_file():raise HTTPException(404,'视频不存在')
    return FileResponse(file,media_type='video/mp4')
