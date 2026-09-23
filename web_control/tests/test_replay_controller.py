from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import time
import unittest

import h5py
import numpy as np

from control.web_control.fake_sdk import FakeH1Robot, FakeSdk
from control.web_control.replay_controller import (
    discover_replay_dataset_directories,
    load_replay_episode,
    scan_dataset_directory,
)
from control.web_control.robot_service import (
    POLICY_WIRE_MOTOR_IDS,
    RobotCommandRejected,
    RobotService,
)


def write_episode(
    root: Path,
    *,
    action_mode: str = "absolute",
    state_base: tuple[float, float] = (0.01, 0.0),
    action_base: tuple[float, float] = (0.0, 0.0),
) -> Path:
    episode_dir = root / "episode-001"
    episode_dir.mkdir(parents=True)
    path = episode_dir / "episode.hdf5"
    count = 4
    arm = np.arange(count * 14, dtype=np.float64).reshape(count, 14) / 1000.0
    effector = np.tile(np.asarray((0.0, 1.5)), (count, 1))
    waist = np.tile(np.asarray((0.4, 0.1, -0.1)), (count, 1))
    head = np.tile(np.asarray((0.2, -0.2)), (count, 1))
    with h5py.File(path, "w") as file:
        file.attrs["episode_id"] = "episode-001"
        file.attrs["task_name"] = "test replay"
        file.attrs["control_frequency"] = 30
        file.attrs["action_mode"] = action_mode
        for source, prefix, base in (
            ("state", "observation/state", state_base),
            ("action", "action", action_base),
        ):
            del source
            file.create_dataset(f"{prefix}/arm/position", data=arm)
            file.create_dataset(f"{prefix}/effector/position", data=effector)
            file.create_dataset(f"{prefix}/waist/position", data=waist)
            file.create_dataset(f"{prefix}/head/position", data=head)
            file.create_dataset(
                f"{prefix}/base/velocity",
                data=np.tile(np.asarray(base), (count, 1)),
            )
    return path


def write_aligned_episode(root: Path) -> Path:
    path = root / "demo_0" / "states" / "aligned_joints.h5"
    path.parent.mkdir(parents=True)
    with h5py.File(path, "w") as file:
        file.attrs["format"] = "icra_wbc_aligned_joints"
        file.attrs["fps"] = 20.0
        file.attrs["action_mode"] = "absolute"
        file.attrs["source_demo"] = "demo_0"
        for i in range(12):
            for source in ("state", "action"):
                vector = np.zeros(23, dtype=np.float32)
                vector[:16] = np.arange(16) / 100 + i / 1000
                vector[16] = 0.8
                file.create_dataset(f"{i}/{source}/vector", data=vector)
    return path


class AlignedReplayTests(unittest.TestCase):
    def test_mixed_discovery_numeric_order_mapping_and_rate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write_aligned_episode(root)
            write_episode(root)
            self.assertEqual(scan_dataset_directory(str(root))["count"], 2)
            self.assertEqual(discover_replay_dataset_directories(root)["directories"], [str(root)])
            for source in ("state", "action"):
                frames, metadata = load_replay_episode(
                    str(path.parent.parent), "states/aligned_joints.h5", source, "full", 0.5
                )
                self.assertEqual(frames.shape, (12, 23))
                np.testing.assert_allclose(frames[:, 0], np.arange(12) / 1000, atol=1e-8)
                np.testing.assert_allclose(frames[0, :16], np.arange(16) / 100, atol=1e-8)
                self.assertTrue(np.all(frames[:, 16] == 0.8))
                self.assertEqual(metadata["effective_rate_hz"], 10.0)
                self.assertEqual(metadata["episode_id"], "demo_0")

    def test_invalid_frames_and_motion_constraints(self) -> None:
        cases = [
            ("missing", "缺少"), ("shape", "形状"), ("nan", "NaN"),
            ("gap", "连续"), ("limit", "超出 SDK"),
            ("base", "底盘"), ("delta", "absolute"), ("fields", "字段顺序"),
        ]
        for case, error in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = write_aligned_episode(root)
                with h5py.File(path, "a") as file:
                    if case == "missing": del file["5/action/vector"]
                    elif case == "shape":
                        del file["5/action/vector"]
                        file.create_dataset("5/action/vector", data=np.zeros(22))
                    elif case == "nan": file["5/action/vector"][0] = np.nan
                    elif case == "gap": del file["5"]
                    elif case == "limit": file["5/action/vector"][16] = 0.801
                    elif case == "base": file["5/action/vector"][21] = 0.1
                    elif case == "delta": file.attrs["action_mode"] = "delta"
                    elif case == "fields": file.attrs["state_fields_json"] = '[]'
                with self.assertRaisesRegex(RobotCommandRejected, error):
                    load_replay_episode(str(root), path.relative_to(root).as_posix(), "action", "full", 1.0)


class ReplayDatasetTests(unittest.TestCase):
    def test_scan_and_exact_23d_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_episode(root)
            report = scan_dataset_directory(str(root))

            self.assertEqual(report["count"], 1)
            episode = report["episodes"][0]
            self.assertEqual(episode["path"], "episode-001/episode.hdf5")
            self.assertEqual(episode["sources"], {"action": True, "state": True})
            self.assertEqual(episode["base_abs_max"]["action"], 0.0)

            frames, metadata = load_replay_episode(
                str(root), episode["path"], "action", "full", 0.5
            )
            self.assertEqual(frames.shape, (4, 23))
            np.testing.assert_allclose(frames[0, :7], np.arange(7) / 1000.0)
            self.assertEqual(frames[0, 7], 0.0)
            np.testing.assert_allclose(frames[0, 8:15], np.arange(7, 14) / 1000.0)
            self.assertEqual(frames[0, 15], 1.5)
            np.testing.assert_allclose(frames[0, 16:21], (0.4, 0.1, -0.1, 0.2, -0.2))
            np.testing.assert_allclose(frames[0, 21:23], (0.0, 0.0))
            self.assertEqual(metadata["effective_rate_hz"], 15.0)

            fast_frames, fast_metadata = load_replay_episode(
                str(root), episode["path"], "action", "full", 2.0
            )
            self.assertEqual(fast_frames.shape, (4, 23))
            self.assertEqual(fast_metadata["effective_rate_hz"], 60.0)

            with self.assertRaisesRegex(RobotCommandRejected, "0.5x..2.0x"):
                load_replay_episode(
                    str(root), episode["path"], "action", "full", 2.1
                )

    def test_discovers_only_valid_dataset_directories_above_episode_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_dataset = root / "group-a" / "dataset-one"
            second_dataset = root / "group-b" / "dataset-two"
            write_episode(first_dataset)
            write_episode(second_dataset)
            invalid = root / "invalid" / "episode-bad"
            invalid.mkdir(parents=True)
            with h5py.File(invalid / "episode.hdf5", "w") as file:
                file.create_dataset("unrelated", data=np.zeros((1,)))

            report = discover_replay_dataset_directories(root)

            self.assertEqual(report["count"], 2)
            self.assertEqual(
                report["directories"],
                sorted((str(first_dataset.resolve()), str(second_dataset.resolve()))),
            )
            self.assertEqual(report["inspected_episode_files"], 3)
            self.assertEqual(report["invalid_count"], 1)
            self.assertFalse(report["truncated"])

    def test_full_rejects_unmapped_nonzero_base_but_arms_accepts_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write_episode(root)
            relative = path.relative_to(root).as_posix()
            with self.assertRaisesRegex(RobotCommandRejected, "底盘"):
                load_replay_episode(str(root), relative, "state", "full", 1.0)
            frames, _ = load_replay_episode(
                str(root), relative, "state", "arms", 1.0
            )
            self.assertEqual(frames.shape, (4, 23))

    def test_action_requires_absolute_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write_episode(root, action_mode="delta")
            with self.assertRaisesRegex(RobotCommandRejected, "absolute"):
                load_replay_episode(
                    str(root), path.relative_to(root).as_posix(), "action", "arms", 1.0
                )


class RobotReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sdk = FakeSdk()
        self.robot = FakeH1Robot()
        self.service = RobotService(
            sdk_loader=lambda: self.sdk,
            robot_factory=lambda _sdk: self.robot,
            lease_seconds=5.0,
            trajectory_rate_hz=250.0,
            state_rate_hz=25.0,
        )
        self.lease = self.service.acquire("replay-test")["lease_id"]
        self.service.initialize(self.lease)

    def tearDown(self) -> None:
        self.service.close()

    @staticmethod
    def frame(value: float = 0.05) -> list[float]:
        frame = [0.0] * 23
        for index in (*range(0, 7), *range(8, 15)):
            frame[index] = value
        frame[7] = 0.0
        frame[15] = 1.5
        frame[16] = 0.4
        return frame

    def test_arms_mode_only_sends_arms_and_grippers(self) -> None:
        self.robot.calls.clear()
        result = self.service.replay_trajectory(
            self.lease,
            [self.frame(0.05), self.frame(0.06)],
            mode="arms",
            rate_hz=200,
            alignment_duration_s=0,
        )
        self.assertEqual(result["frames_sent"], 2)
        position_ids = {
            call[1]
            for call in self.robot.calls
            if call[0] in {"setArm_low", "setGripper_low", "setWaist_low", "setHead_low"}
        }
        self.assertEqual(position_ids, set(POLICY_WIRE_MOTOR_IDS[:16]))
        self.assertEqual(self.robot.states[0].Speed_Actual, 0.0)
        self.assertEqual(self.robot.states[1].Speed_Actual, 0.0)

    def test_full_mode_sends_all_position_motors_and_zero_base(self) -> None:
        self.robot.calls.clear()
        result = self.service.replay_trajectory(
            self.lease,
            [self.frame()],
            mode="full",
            rate_hz=100,
            alignment_duration_s=0,
        )
        self.assertTrue(result["ok"])
        position_ids = {
            call[1]
            for call in self.robot.calls
            if call[0] in {"setArm_low", "setGripper_low", "setWaist_low", "setHead_low"}
        }
        self.assertEqual(position_ids, set(POLICY_WIRE_MOTOR_IDS))

    def test_emergency_stop_cancels_active_replay_without_deinit(self) -> None:
        outcome: list[BaseException | dict] = []

        def run() -> None:
            try:
                outcome.append(
                    self.service.replay_trajectory(
                        self.lease,
                        [self.frame(index / 1000.0) for index in range(100)],
                        mode="arms",
                        rate_hz=20,
                        alignment_duration_s=0,
                    )
                )
            except BaseException as exc:
                outcome.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        time.sleep(0.08)
        stopped = self.service.emergency_stop_motion(reason="test_stop")
        thread.join(2.0)

        self.assertTrue(stopped["ok"])
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], RobotCommandRejected)
        self.assertNotIn(("robot_deinit",), self.robot.calls)
        self.assertEqual(self.robot.states[0].Speed_Actual, 0.0)
        self.assertEqual(self.robot.states[1].Speed_Actual, 0.0)


if __name__ == "__main__":
    unittest.main()
