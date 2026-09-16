import cv2
import json

import h5py
import numpy as np
import pytest

from dataqc.checks import numeric_checks, raw_checks
from dataqc.export import create_dataset, path_for, split_dataset, validate_dataset
from dataqc.io import CAMS, fingerprint, load, parse_task, read_json, video_path
from dataqc.repair import derive
from dataqc.vision import validate_decision, validate_refs


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "Milk_Tea_0"
    root.mkdir()
    n = 150
    state = np.zeros((n, 23))
    action = state.copy()
    state[65:, 0] = np.arange(n - 65) * 0.012
    state[65:, 8] = np.arange(n - 65) * 0.011
    state[70:, 7] = 0.52
    action[:] = state
    action[70:, 7] = 1.5
    state[110:, 15] = 0.28
    action[110:, 15] = 1.5
    with h5py.File(root / "episode.hdf5", "w") as f:
        f.attrs.update(
            dict(
                task_name="Grasp Milk with the left hand and then grasp Tea with the right hand",
                total_frames=n,
                control_frequency=30,
                action_mode="absolute",
                depth_recorded=False,
            )
        )
        for prefix, data in [("observation/state", state), ("action", action)]:
            for key, v in [
                ("arm/position", np.c_[data[:, :7], data[:, 8:15]]),
                ("effector/position", data[:, [7, 15]]),
                ("waist/position", data[:, 16:19]),
                ("head/position", data[:, 19:21]),
                ("base/velocity", data[:, 21:23]),
            ]:
                f.create_dataset(prefix + "/" + key, data=v)
        f.create_dataset("timestamp/t", data=100000 + np.arange(n) * 1000 / 30)
        f.create_dataset("subtask_transitions", data=[90, n])
        for cam in CAMS:
            imgs = f.create_dataset(
                "observation/images/rs/" + cam + "/color",
                (n,),
                dtype=h5py.vlen_dtype(np.dtype("uint8")),
            )
            p = video_path(root, cam)
            p.parent.mkdir(parents=True, exist_ok=True)
            writer = cv2.VideoWriter(
                str(p), cv2.VideoWriter_fourcc(*"mp4v"), 30, (64, 48)
            )
            for i in range(n):
                img = np.full((48, 64, 3), 70 + i % 120, dtype=np.uint8)
                cv2.circle(img, (i % 55 + 4, 24), 5, (10, 150, 240), -1)
                ok, b = cv2.imencode(".jpg", img)
                imgs[i] = b
                writer.write(img)
            writer.release()
    return root


def decision(n=150):
    return dict(
        grade="A",
        reason="完成任务，删除冗余等待",
        findings=[],
        corrected_prompt="Grasp Milk with the left hand and then grasp Tea with the right hand",
        stages=[
            dict(hand="left", item="Milk", start=0, end=90),
            dict(hand="right", item="Tea", start=90, end=n),
        ],
        safe_trim_ids=[0],
    )


def test_stationary_boundary():
    for n, expected in [(40, "pass"), (41, "warn")]:
        s = np.zeros((n, 23))
        c = numeric_checks(s, s, np.arange(n) / 30, 40)
        assert next(x["status"] for x in c if x["key"] == "stationary") == expected


@pytest.mark.parametrize("problem", ["duplicate", "nan", "slow", "jump"])
def test_numeric_failures(problem):
    s = np.zeros((100, 23))
    a = s.copy()
    t = np.arange(100) / 30
    if problem == "duplicate":
        t[30] = t[29]
    if problem == "nan":
        s[20, 5] = np.nan
    if problem == "slow":
        t = np.arange(100) / 28
    if problem == "jump":
        s[20, 5] = 0.81
    checks = numeric_checks(s, a, t)
    key = {"duplicate": "timestamps", "nan": "finite", "slow": "fps", "jump": "motion"}[
        problem
    ]
    assert next(c["status"] for c in checks if c["key"] == key) == ("warn" if problem in ("duplicate", "slow", "jump") else "fail")


def test_strict_prompt():
    assert parse_task("Grasp Milk with the left hand") == {"left": "Milk"}
    for s in [
        "Grasp Milk  with the left hand",
        "grasp Milk with the left hand",
        "Grasp Tea with the left hand and then Grasp Milk with the right hand",
    ]:
        assert parse_task(s) is not None
    assert parse_task("Grasp Milk with right hand") is None


def test_missing_video(source):
    video_path(source, "cam_high").unlink()
    r = raw_checks(source)
    assert (
        next(c["status"] for c in r["checks"] if c["key"] == "video_cam_high") == "fail"
    )


def test_invalid_action(source):
    with h5py.File(source / "episode.hdf5", "a") as f:
        del f["action/effector/position"]
    assert raw_checks(source)["hard_fail"]


def test_invalid_evidence():
    with pytest.raises(ValueError):
        validate_refs({"findings": [{"status": "pass", "evidence_ids": ["fake"]}]}, {})


def test_stage_validation(source):
    d = decision()
    d["stages"][1]["start"] = 89
    with pytest.raises(ValueError):
        validate_decision(d, load(source), [{}])


def test_end_to_end_repair_convert_split(source, tmp_path):
    before = fingerprint(source)
    report = raw_checks(source, 40)
    d = decision()
    assert (
        next(c["status"] for c in report["checks"] if c["key"] == "stationary")
        == "warn"
    )
    repaired = derive(source, tmp_path / "repaired", report, d, 40, before)
    assert fingerprint(source) == before
    post = raw_checks(repaired, 40)
    assert not post["hard_fail"], post
    p = read_json(repaired / "provenance.json")
    assert p["removed_frames"] > 0
    assert len(p["source_frame_indices"]) == load(repaired)["n"]
    assert len(p["source_timestamps_seconds"]) == load(repaired)["n"]
    full = tmp_path / "export/full"
    create_dataset([{"root": str(repaired), "grade": "A"}], full)
    assert validate_dataset(full)["passed"]
    mapping_path=full/'meta/episode_name_mapping.json'
    mapping=read_json(mapping_path)
    assert mapping['episodes'][0]['num_frames']==load(repaired)['n']
    mapping['episodes'][0]['num_frames']+=1
    mapping_path.write_text(json.dumps(mapping))
    mismatched=validate_dataset(full)
    assert not mismatched['passed']
    assert 'mapping num_frames' in str(mismatched['issues'])
    mapping['episodes'][0]['num_frames']-=1
    mapping_path.write_text(json.dumps(mapping))
    split = split_dataset(full, tmp_path / "export/hands")
    assert len(split) == 2
    assert sum(v["frames"] for v in split) == load(repaired)["n"]
    for v in split:
        assert validate_dataset(v["path"])["passed"]
    # Tamper independently with a saved output; source-correspondence validation must catch it.
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pq.read_table(path_for(full, 0))
    data = table.to_pydict()
    data["action"][0][2] = 10
    pq.write_table(pa.Table.from_pydict(data), path_for(full, 0))
    assert not validate_dataset(full)["passed"]
    with pytest.raises(ValueError):
        split_dataset(full, tmp_path / "bad", quality_check=True)


def test_timestamp_length_failure():
    s = np.zeros((5, 23))
    c = numeric_checks(s, s, np.arange(4) / 30)
    assert any(v["key"] == "timestamps" and v["status"] == "fail" for v in c)


def test_whole_worker_automatic_and_resume(source, tmp_path, monkeypatch):
    import dataqc.config as config
    from dataqc import db, worker

    dbpath = tmp_path / "db.sqlite3"
    monkeypatch.setattr(db, "DB", dbpath)
    db.init()
    monkeypatch.setattr(worker, "VAR", tmp_path / "var")
    monkeypatch.setattr(worker, "EXPORTS", tmp_path / "exports")

    def fake_inspect(root, report, cfg, cache, progress, **kwargs):
        return dict(
            decision=decision(),
            verification=dict(
                status="pass",
                prompt_matches=True,
                hand_matches=True,
                items_match=True,
                stage_order_matches=True,
            ),
            windows=[],
            evidence={},
        )

    monkeypatch.setattr(worker.get_adapter(), "inspect", fake_inspect)
    cfg = config.settings()
    rid = db.create(str(source), "auto", cfg, [str(source)])
    worker.process_run(db.get_run(rid))
    run = db.get_run(rid)
    assert run["status"] == "completed", run
    assert db.episodes(rid)[0]["grade"] == "B"
    assert len(run["exports"]) == 1 and len(run["exports"][0]["hands"]) == 2
    # Simulate a crash after one split side was published but before the manifest commit.
    manifest = next((tmp_path / "exports").rglob("split_manifest.json"))
    manifest.unlink()
    worker.process_run(db.get_run(rid))
    assert db.get_run(rid)["status"] == "completed"
    assert fingerprint(source) == read_json(
        next((tmp_path / "var").rglob("fingerprint.json"))
    )


def test_worker_api_error_never_passes(source, tmp_path, monkeypatch):
    from dataqc import db, worker
    from dataqc.config import settings

    monkeypatch.setattr(db, "DB", tmp_path / "db.sqlite3")
    db.init()
    monkeypatch.setattr(worker, "VAR", tmp_path / "var")
    monkeypatch.setattr(worker, "EXPORTS", tmp_path / "exports")

    def unavailable(*args):
        raise RuntimeError("VLM HTTP 503")

    monkeypatch.setattr(worker.get_adapter(), "inspect", unavailable)
    rid = db.create(str(source), "auto", settings(), [str(source)])
    worker.process_run(db.get_run(rid))
    assert db.episodes(rid)[0]["status"] == "incomplete"
    assert db.episodes(rid)[0]["grade"] == "B"  # provisional warning grade; not exportable
    assert not db.get_run(rid)["exports"]


def test_manual_review_conflict(source, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from dataqc import api, db
    from dataqc.config import settings

    monkeypatch.setattr(db, "DB", tmp_path / "db.sqlite3")
    db.init()
    rid = db.create(str(source), "manual", settings(), [str(source)])
    ep = db.episodes(rid)[0]
    db.update("runs", rid, status="needs_review")
    db.update(
        "episodes", ep["id"], status="review", data={"raw_report": raw_checks(source)}
    )
    d = decision()
    d.pop("findings")
    d.update(revision=0, actor="测试操作员")
    client = TestClient(api.app)
    url = f"/api/episodes/{ep['id']}/review"
    assert client.post(url, json=d).status_code == 200
    assert client.post(url, json=d).status_code == 409
    assert (
        client.post(
            url, json=d, headers={"Origin": "http://untrusted.example"}
        ).status_code
        == 403
    )


def test_no_repair_record_does_not_deduct(source):
    c = next(c for c in raw_checks(source)["checks"] if c["key"] == "repair_records")
    assert c["status"] == "na" and c["score_delta"] == 0


def test_one_frame_cannot_pass_temporal_checks():
    s = np.zeros((1, 23))
    checks = numeric_checks(s, s, np.array([0.0]))
    assert any(c["status"] == "fail" for c in checks)


def test_rate_limit_is_durably_retried(source, tmp_path, monkeypatch):
    from dataqc import db, worker
    from dataqc.config import settings
    from dataqc.vision import RateLimited

    monkeypatch.setattr(db, "DB", tmp_path / "db.sqlite3")
    db.init()
    monkeypatch.setattr(worker, "VAR", tmp_path / "var")
    monkeypatch.setattr(worker, "EXPORTS", tmp_path / "exports")

    def limited(*args, **kwargs):
        raise RateLimited(30)

    monkeypatch.setattr(worker.get_adapter(), "inspect", limited)
    rid = db.create(str(source), "auto", settings(), [str(source)])
    worker.process_run(db.get_run(rid))
    ep = db.episodes(rid)[0]
    assert db.get_run(rid)["status"] == "retry_wait"
    assert ep["status"] == "retry_wait" and ep["data"]["auto_retry_count"] == 1
    assert ep["data"]["retry_at"] > 0 and ep["grade"] == "B"
    assert not db.get_run(rid)["exports"]


def test_rejected_episode_needs_no_fabricated_stage(source):
    rejected = decision()
    rejected.update(grade="F", stages=[], safe_trim_ids=[])
    validate_decision(rejected, load(source), [])
    assert rejected["grade"] == "F"


def test_unsupported_robot_does_not_use_zerith_rules():
    from dataqc.robots import get_adapter, profiles

    assert get_adapter().id == "zerith"
    assert any(p["id"] == "agilex" and not p["enabled"] for p in profiles())
    with pytest.raises(ValueError):
        get_adapter("agilex")
