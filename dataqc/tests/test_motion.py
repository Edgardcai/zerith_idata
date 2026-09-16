import numpy as np
import pytest
from test_pipeline import source as source

from dataqc.io import write_json
from dataqc.motion import (
    RULE_VERSION,
    arm_and_posture_checks,
    closure_events,
    gripper_check,
    lift_check,
    source_height,
)

TASK = "Grasp Milk with the left hand and then grasp Tea with the right hand"


def trajectory():
    s = np.zeros((150, 23))
    a = s.copy()
    s[30:, 7], a[30:, 7] = 0.26, 1.5
    s[101:, 15], a[100:, 15] = 0.52, 1.5
    return s, a, np.arange(150) / 30


def test_gripper_feedback_and_stages():
    s, a, _ = trajectory()
    c = gripper_check(s, a, TASK, [80, 150])
    assert c["status"] == "pass"
    assert c["detail"]["channels"]["right"]["state_close_frames"] == [101]


@pytest.mark.parametrize(
    "problem",
    [
        "twice",
        "missing",
        "wrong_stage",
        "no_feedback",
        "delayed_feedback",
        "held_at_start",
    ],
)
def test_gripper_warnings(problem):
    s, a, _ = trajectory()
    stages = [80, 150]
    if problem == "twice":
        s[50:60, 7] = a[50:60, 7] = 0
    elif problem == "missing":
        s[:, 15] = a[:, 15] = 0
    elif problem == "wrong_stage":
        stages = [110, 150]
    elif problem == "no_feedback":
        s[:, 7] = 0
    elif problem == "delayed_feedback":
        s[30:50, 7] = 0
    else:
        s[:, 7], a[:, 7] = 0.26, 1.5
    c = gripper_check(s, a, TASK, stages)
    assert c["status"] == "warn"
    assert c["detail"]["issues"]


def test_hysteresis_and_short_jitter():
    # A single spurious sample cannot create another open/close cycle.
    v = np.zeros(100)
    v[30:] = 0.26
    v[12] = 0.2
    v[50] = 0
    assert closure_events(v, 0.05, 0.1) == [30]
    s, a, _ = trajectory()
    c = gripper_check(s, a, TASK, [])
    assert c["status"] == "warn"  # VLM may supply missing stage labels.


@pytest.mark.parametrize(
    "source,hand,col",
    [
        ("state", "left", 2),
        ("state", "right", 12),
        ("action", "left", 5),
        ("action", "right", 14),
    ],
)
def test_both_arm_sources(source, hand, col):
    s, a, t = trajectory()
    s[:, 0] = np.sin(np.arange(150)) * 0.003
    checks = arm_and_posture_checks(s, a, t)
    assert all(c["status"] == "pass" for c in checks)
    (s if source == "state" else a)[60, col] = 0.81
    check = next(
        c for c in arm_and_posture_checks(s, a, t) if c["key"] == f"arm_{source}_{hand}"
    )
    assert check["status"] == "warn"
    assert check["detail"]["bad_frames"] == [60, 61]


def test_missing_frame_evidence():
    s, a, t = trajectory()
    t[50:] += 0.11
    c = arm_and_posture_checks(s, a, t)
    assert all(
        v["status"] == "pass" and v["detail"]["bad_frames"] == []
        for v in c
        if v["key"].startswith("arm_")
    )


@pytest.mark.parametrize("col", [17, 18, 19, 20])
def test_posture_quantiles_warn_only(col):
    s, a, t = trajectory()
    s[:, col] = np.tile([-0.0202, 0.0202], 75)
    a[:, col] = 0.0202
    c = arm_and_posture_checks(s, a, t)
    for source in ["state", "action"]:
        check = next(v for v in c if v["key"] == f"posture_{source}")
        assert check["status"] == "warn"
        assert check["score_delta"] == 0
    s[:, col] = 0.02
    a[:, col] = np.tile([-0.02, 0.02], 75)
    assert all(v["status"] == "pass" for v in arm_and_posture_checks(s, a, t))


@pytest.mark.parametrize("height", [0.0, 0.4, 0.8])
def test_lift_height_all_values_and_float32_boundary(tmp_path, height):
    s, a, _ = trajectory()
    root = tmp_path / f"Milk_Tea_{height:g}" / "episode_009999"
    write_json(root/'collection_task.json',dict(targets=dict(lift_height=height)))
    s[:, 16] = np.float32(height - 0.02)
    a[:, 16] = np.float32(height + 0.02)
    assert lift_check(s, a, root)["status"] == "pass"
    a[42, 16] = height + 0.0202
    c = lift_check(s, a, root)
    assert c["status"] == "fail" and c["detail"]["channels"]["action"][
        "bad_frames"
    ] == [42]
    assert "42" in c["detail"]["issues"][0]


def test_lift_provenance_and_directory_is_not_a_reference(tmp_path):
    original = tmp_path / "Milk_Tea_0.4" / "episode_000000"
    repaired = tmp_path / "repaired" / "episode_000001_v0"
    write_json(original/'collection_task.json',dict(targets=dict(lift_height='0.4')))
    write_json(repaired / "provenance.json", dict(source=str(original)))
    assert source_height(repaired)["expected_m"] == 0.4
    assert "error" in source_height(tmp_path / "Milk_Tea_0.4_0.8" / "episode_000001")
    assert "error" in source_height(tmp_path / "Milk_Tea" / "episode_000001")


def test_stage_feedback_disagreement_warns_instead_of_f(
    source, tmp_path, monkeypatch
):
    from test_pipeline import decision

    from dataqc import config, db, worker

    dbpath = tmp_path / "db.sqlite3"
    monkeypatch.setattr(db, "DB", dbpath)
    db.init()
    monkeypatch.setattr(worker, "VAR", tmp_path / "var")
    monkeypatch.setattr(worker, "EXPORTS", tmp_path / "exports")

    def fake_inspect(*args, **kwargs):
        d = decision()
        d["stages"][0]["end"] = 120
        d["stages"][1]["start"] = 120
        return dict(
            decision=d,
            verification=dict(
                status="pass",
                prompt_matches=True,
                hand_matches=True,
                items_match=True,
                stage_order_matches=True,
            ),
        )

    monkeypatch.setattr(worker.get_adapter(), "inspect", fake_inspect)
    rid = db.create(str(source), "auto", config.settings(), [str(source)])
    worker.process_run(db.get_run(rid))
    ep = db.episodes(rid)[0]
    assert ep["grade"] == "B"
    assert ep["status"] == "ready"
    assert any(c["key"]=="gripper_sequence" and c["status"]=="warn" for c in ep["data"]["repaired_report"]["checks"])


def test_hard_failure_rejects_without_vlm_and_upgrade_cache(
    source, tmp_path, monkeypatch
):
    import h5py

    from dataqc import config, db, worker
    from dataqc.checks import raw_checks

    monkeypatch.setattr(db, "DB", tmp_path / "db.sqlite3")
    db.init()
    monkeypatch.setattr(worker, "VAR", tmp_path / "var")
    monkeypatch.setattr(worker, "EXPORTS", tmp_path / "exports")
    with h5py.File(source / "episode.hdf5", "a") as f:
        f["action/arm/position"][33, 0] = float("nan")

    def unexpected_call(*args):
        raise AssertionError("Numeric failure must not depend on VLM availability")

    monkeypatch.setattr(worker.get_adapter(), "inspect", unexpected_call)
    rid = db.create(str(source), "auto", config.settings(), [str(source)])
    ep = db.episodes(rid)[0]
    old = raw_checks(source)
    old["version"] = "zerith_qc_1"
    old["checks"] = [c for c in old["checks"] if c["key"] != "lift_height"]
    db.update(
        "episodes", ep["id"], status="ready", grade="A", data=dict(raw_report=old)
    )
    write_json(
        tmp_path / "var" / "runs" / rid / "episode_000000" / "raw_report.json", old
    )
    worker.process_run(db.get_run(rid))
    ep = db.episodes(rid)[0]
    assert ep["grade"] == "F" and "NaN" in ep["reason"]
    assert ep["data"]["raw_report"]["version"] == RULE_VERSION
    assert ep["revision"] == 1
