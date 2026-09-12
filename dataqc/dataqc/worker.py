import fcntl
import hashlib
import json
import os
import time
import traceback

from . import db
from .config import EXPORTS, VAR
from .io import fingerprint, load, read_json, write_json
from .motion import RULE_VERSION, contextual_checks, failure_reason, warning_grade, warning_messages
from .robots import get_adapter
from .vision import RateLimited, APIUnavailable, validate_decision


class Paused(Exception):
    pass


def visual_decision(visual, report):
    if visual.get('pipeline') == 'yolo_first':
        return warning_grade(dict(visual['decision']), report)
    decision = dict(visual["decision"])
    v = visual["verification"]
    findings = decision["findings"]
    failed = [f for f in findings if f["status"] == "fail"]
    matches = all(v[k] for k in ("prompt_matches", "hand_matches", "items_match", "stage_order_matches"))
    conflict = any(
        (f["criterion"] == "拿对" and v["items_match"])
        or (f["criterion"] == "标注一致" and matches)
        or (f["criterion"] == "切换正确" and v["stage_order_matches"] and v["hand_matches"])
        for f in failed
    )
    if v["status"] == "uncertain" or conflict:
        decision["grade"] = "REVIEW"
        decision["reason"] += "；两次视觉核对存在分歧或证据不足，需人工确认"
    elif failed:
        decision["grade"] = "F"
    elif decision["grade"] == "F" or v["status"] != "pass" or not matches or any(f["status"] in ("uncertain", "na") for f in findings):
        decision["grade"] = "REVIEW"
        if v['status'] != 'pass' or not matches:
            decision['reason'] += '；独立视觉核对未通过：' + v.get('reason', '商品、操作手或阶段需人工确认')
    return warning_grade(decision, report)


def process_run(run):
    rid = run["id"]
    cfg = run["config"]
    adapter = get_adapter(cfg.get("robot", "zerith"))
    work = VAR / "runs" / rid
    work.mkdir(parents=True, exist_ok=True)

    def progress(text):
        write_json(
            VAR / "worker-heartbeat.json", dict(pid=os.getpid(), time=time.time(), batch_policy="motion_batch_v1")
        )
        current = db.get_run(rid)
        if current["status"] in ("paused", "cancelled"):
            raise Paused()
        db.update("runs", rid, phase=text, updated=time.time())

    db.update("runs", rid, status="running", error="")
    write_json(
        work / "renumber_mapping.json",
        [
            dict(
                number=e["number"],
                episode=f"episode_{e['number']:06d}",
                source=e["root"],
            )
            for e in db.episodes(rid)
        ],
    )
    for ep in db.episodes(rid):
        old_report = ep["data"].get("raw_report")
        if old_report and old_report.get("version") != RULE_VERSION:
            db.audit(
                rid,
                ep["id"],
                "qc_rules_upgrade",
                "system",
                dict(
                    version=old_report.get("version"),
                    grade=ep["grade"],
                    reason=ep["reason"],
                    report=old_report,
                ),
                dict(version=RULE_VERSION),
            )
            ep["status"] = "queued"
            ep["revision"] += 1
            db.update(
                "episodes",
                ep["id"],
                status="queued",
                grade=None,
                revision=ep["revision"],
            )
    from .batch_prepare import prepare_run
    from .batch_motion import episode_key
    motion_outcomes = prepare_run(run, work, progress)
    for ep in db.episodes(rid):
        if ep["status"] in ("ready", "rejected", "review", "incomplete", "retry_wait"):
            continue
        progress(f"记录 {ep['number']:06d} · HDF5 质检")
        eid = ep["id"]
        cache = work / f"episode_{ep['number']:06d}"
        cache.mkdir(exist_ok=True)
        data = ep["data"]
        db.update("episodes", eid, status="checking")
        try:
            before = fingerprint(ep["root"])
            previous = read_json(cache / "fingerprint.json")
            if previous and previous != before:
                raise ValueError("原始数据自本次任务开始后发生变化，请新建任务")
            write_json(cache / "fingerprint.json", before)
            report = read_json(cache / "raw_report.json")
            if not report or report.get("version") != RULE_VERSION:
                report = adapter.check(ep["root"], cfg["stationary_frames"], progress)
                write_json(cache / "raw_report.json", report)
            data["raw_report"] = report
            data["report_path"] = str(cache / "raw_report.json")
            db.update("episodes", eid, data=data)
            fatal = [
                c
                for c in report["checks"]
                if c["status"] == "fail" and c["key"] not in ("stationary", "prompt")
            ]
            if fatal:
                db.update(
                    "episodes",
                    eid,
                    status="rejected",
                    grade="F",
                    reason="判 F：" + failure_reason(fatal),
                    data=data,
                )
                continue
            if warning_messages(report):
                db.update("episodes", eid, grade="B", reason="预警默认 B；视觉核对未完成：" + "；".join(warning_messages(report)))
            decision = data.get("manual_decision")
            if decision is None:
                if run["mode"] == "manual" and not cfg.get("assist_manual", True):
                    db.update(
                        "episodes",
                        eid,
                        status="review",
                        reason="基础报告完成，等待手动分级与标注",
                        data=data,
                    )
                    continue
                visual = read_json(cache / "visual_report.json")
                policy_changed = False
                if visual.get('pipeline') == 'yolo_first':
                    from .yolo_gate import cache_policy
                    policy_changed = visual.get('matching_policy') != cache_policy(cfg)
                if not visual or visual.get("errors") or policy_changed or visual.get("rule_version") != RULE_VERSION or visual.get("model") != cfg.get("api_model") or visual.get("vision_version") != cfg.get("vision_version"):
                    try:
                        visual = adapter.inspect(ep["root"], report, cfg, cache, progress,
                                                 motion_outcome=motion_outcomes.get(episode_key(ep['root'])))
                    finally:
                        gate = read_json(cache / 'category/yolo_match.json') if cfg.get('vlm_enabled', False) else {}
                        if gate:
                            data['yolo_report'] = gate
                            if gate['status'] == 'warn':
                                db.update('episodes', eid, grade='B' if warning_messages(report) else None, reason='YOLO 类别匹配待核对：' + '；'.join(gate['warnings']), data=data)
                data["visual"] = visual
                if visual.get('pipeline') == 'yolo_first':
                    data['yolo_report'] = visual['yolo']
                decision = visual_decision(visual, report)
                if visual.get('errors') and decision['grade'] != 'F':
                    if visual.get('api_unavailable'):raise APIUnavailable(decision['reason'])
                    if visual.get('retry_after'):raise RateLimited(visual['retry_after'])
                    db.update('episodes',eid,status='incomplete',reason=decision['reason'],data=data)
                    continue
                if run["mode"] == "manual":
                    db.update(
                        "episodes",
                        eid,
                        status="review",
                        grade=decision["grade"]
                        if decision["grade"] != "REVIEW"
                        else ("B" if warning_messages(report) else None),
                        reason=decision["reason"],
                        data=data,
                    )
                    continue
            if not data.get("manual_decision"):
                decision = warning_grade(decision, report)
            if decision["grade"] == "F":
                db.update(
                    "episodes",
                    eid,
                    status="rejected",
                    grade="F",
                    reason=decision["reason"],
                    data=data,
                )
                continue
            if decision["grade"] == "REVIEW":
                db.update(
                    "episodes",
                    eid,
                    status="review",
                    grade="B" if warning_messages(report) else None,
                    reason=decision["reason"],
                    data=data,
                )
                continue
            stationary = next(
                c["detail"]["intervals"]
                for c in report["checks"]
                if c["key"] == "stationary"
            )
            validate_decision(
                decision,
                load(ep["root"]),
                stationary,
                allow_relabel=bool(data.get("manual_decision")),
            )
            annotation_source = dict(
                load(ep["root"]), task=decision["corrected_prompt"]
            )
            annotation_checks = contextual_checks(
                annotation_source, ep["root"], decision["stages"]
            )
            data["annotation_checks"] = annotation_checks
            if any(c["status"] == "fail" for c in annotation_checks):
                db.update(
                    "episodes",
                    eid,
                    status="rejected",
                    grade="F",
                    reason="标注复核判 F：" + failure_reason(annotation_checks),
                    data=data,
                )
                continue
            if decision["grade"] == "REVIEW" and not data.get("manual_decision"):
                db.update(
                    "episodes",
                    eid,
                    status="review",
                    reason=decision["reason"],
                    data=data,
                )
                continue
            progress(f"记录 {ep['number']:06d} · 同步修复 HDF5 和视频")
            repaired = (
                work / "repaired" / f"episode_{ep['number']:06d}_v{ep['revision']}"
            )
            if not repaired.exists():
                adapter.derive(
                    ep["root"],
                    repaired,
                    report,
                    decision,
                    cfg["stationary_frames"],
                    before,
                )
            post = adapter.check(repaired, cfg["stationary_frames"], progress)
            data["repaired_report"] = post
            data["repaired_root"] = str(repaired)
            data["decision"] = decision
            write_json(cache / "repaired_report.json", post)
            if post["hard_fail"]:
                db.update(
                    "episodes",
                    eid,
                    status="rejected",
                    grade="F",
                    reason="修复后仍未通过：" + failure_reason(post["checks"]),
                    data=data,
                )
                continue
            if fingerprint(ep["root"]) != before:
                raise ValueError("质检期间原始文件发生变化，结果不可用于导出")
            db.update(
                "episodes",
                eid,
                status="ready",
                grade=decision["grade"],
                reason=decision["reason"],
                data=data,
            )
            db.audit(
                rid,
                eid,
                "qc_completed",
                "manual" if data.get("manual_decision") else "system",
                None,
                decision,
            )
        except Paused:
            db.update("episodes", eid, status="queued", data=data)
            raise
        except APIUnavailable as exc:
            db.update("episodes", eid, status="incomplete", reason=str(exc), data=data)
            db.update("runs", rid, status="paused", phase="接口不可用，已暂停，避免重复调用", error=str(exc))
            raise Paused()
        except RateLimited as exc:
            attempts = data.get("auto_retry_count", 0) + 1
            data.update(
                auto_retry_count=attempts,
                retry_at=time.time() + exc.retry_after * attempts,
            )
            db.update(
                "episodes",
                eid,
                status="retry_wait" if attempts <= 3 else "incomplete",
                reason=str(exc)
                if attempts <= 3
                else "接口持续限流，自动重试已达 3 次，可稍后手动重试",
                data=data,
            )
        except Exception as exc:
            db.update(
                "episodes", eid, status="incomplete", reason=str(exc)[:500], data=data
            )
            with open(work / "errors.log", "a") as log:
                log.write(traceback.format_exc() + "\n")
    progress("汇总等级，转换已通过记录")
    entries = db.episodes(rid)
    exports = []
    # A task's first successful publication is immutable. A later review publishes a fresh revision.
    revision = max([e["revision"] for e in entries], default=0)
    for grade in cfg["export_grades"]:
        selected = [
            e for e in entries if e["status"] == "ready" and e["grade"] == grade
        ]
        if not selected:
            continue
        sig = hashlib.sha256(
            json.dumps(
                [RULE_VERSION, [(e["id"], e["revision"]) for e in selected]]
            ).encode()
        ).hexdigest()[:8]
        destination = EXPORTS / rid / f"{grade}-{sig}"
        full = destination / "full"
        for e in selected:
            if fingerprint(e["root"]) != read_json(
                work / f"episode_{e['number']:06d}" / "fingerprint.json"
            ):
                raise ValueError("导出前来源文件已变更")
        if not full.exists():
            result = adapter.export(
                [dict(root=e["data"]["repaired_root"], grade=grade) for e in selected],
                full,
                cfg["stationary_frames"],
                progress,
            )
        else:
            result = read_json(full / "qc_report.json") | dict(path=str(full))
        progress(f"{grade} 等级 · 转换后质检与左右手切分")
        split_manifest = destination / "split_manifest.json"
        if not split_manifest.exists():
            # If interrupted between left and right, retain existing validated side and finish missing side.
            split = adapter.split(
                full, destination / "hands", cfg["stationary_frames"], progress
            )
            write_json(split_manifest, split)
        else:
            split = read_json(split_manifest, [])
        exports.append(dict(grade=grade, full=result, hands=split))
        db.update("runs", rid, exports=exports)
    pending = sum(e["status"] in ("review", "incomplete") for e in db.episodes(rid))
    waiting = sum(e["status"] == "retry_wait" for e in db.episodes(rid))
    state = "retry_wait" if waiting else "needs_review" if pending else "completed"
    db.update(
        "runs",
        rid,
        status=state,
        phase=f"等待接口恢复 · {waiting} 条稍后自动继续"
        if waiting
        else f"完成 · {pending} 条需处理"
        if pending
        else "完成",
        exports=exports,
        updated=time.time(),
    )
    write_json(
        work / "report.json", dict(run=db.get_run(rid), episodes=db.episodes(rid))
    )


def main():
    db.init()
    VAR.mkdir(parents=True, exist_ok=True)
    lock = open(VAR / "worker.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    for r in db.runs():
        if r["status"] == "running":
            db.update("runs", r["id"], status="queued", phase="重启后继续")
    while True:
        write_json(
            VAR / "worker-heartbeat.json", dict(pid=os.getpid(), time=time.time(), batch_policy="motion_batch_v1")
        )
        for r in reversed(db.runs()):
            if r["status"] == "retry_wait":
                due = [
                    e
                    for e in db.episodes(r["id"])
                    if e["status"] == "retry_wait"
                    and e["data"].get("retry_at", 0) <= time.time()
                ]
                if due:
                    for e in due:
                        db.update("episodes", e["id"], status="queued")
                    db.update("runs", r["id"], status="queued")
                    r = db.get_run(r["id"])
            if r["status"] != "queued":
                continue
            try:
                process_run(r)
            except Paused:
                pass
            except Exception as ex:
                db.update(
                    "runs",
                    r["id"],
                    status="error",
                    error=str(ex)[:700],
                    phase="任务未完成，可重试",
                )
                traceback.print_exc()
        time.sleep(2)


if __name__ == "__main__":
    main()
