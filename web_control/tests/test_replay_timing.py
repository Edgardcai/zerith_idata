from pathlib import Path
import tempfile
import unittest

import h5py
import numpy as np

from control.web_control.replay_controller import load_replay_episode, scan_dataset_directory
from control.web_control.replay_timing import resample_recorded_frames
from control.web_control.robot_service import RobotCommandRejected
from control.web_control.tests.test_replay_controller import write_episode


class ReplayTimingTests(unittest.TestCase):
    def test_five_period_gap_preserves_duration_slope_and_grip_event(self):
        t = np.array([0, 1, 6, 7], dtype=float) / 30
        frames = np.zeros((4, 23))
        frames[:, 0] = t
        frames[:, 7] = [0, 0, 1, 1]
        original = frames.copy()
        result, rate, info = resample_recorded_frames(frames, t, 30, 1000)
        self.assertEqual(result.shape, (8, 23))
        self.assertAlmostEqual((len(result)-1)/rate, t[-1])
        np.testing.assert_allclose(np.diff(result[:, 0]), 1/30)
        np.testing.assert_array_equal(result[:, 7], [0, 0, 0, 0, 0, 0, 1, 1])
        np.testing.assert_array_equal(frames, original)
        self.assertEqual(info['gap_count'], 1)

    def test_epoch_seconds_and_milliseconds_agree(self):
        frames = np.zeros((4, 23)); frames[:, 0] = np.arange(4)
        epoch_ms = np.array([1788921000000, 1788921000033, 1788921000200, 1788921000233], dtype=float)
        a, rate_a, _ = resample_recorded_frames(frames, epoch_ms, 30, 1000)
        b, rate_b, _ = resample_recorded_frames(frames, epoch_ms/1000, 30, 1000)
        np.testing.assert_allclose(a, b, atol=3e-6)
        self.assertAlmostEqual(rate_a, rate_b, places=4)

    def test_duplicate_keeps_last_and_preserves_endpoints(self):
        frames = np.zeros((4, 23)); frames[:, 0] = [1, 2, 3, 4]
        result, _, info = resample_recorded_frames(frames, [0, 0, .1, .2], 30, 1000)
        self.assertEqual(result[0, 0], 2)
        self.assertEqual(result[-1, 0], 4)
        self.assertEqual(info['duplicate_timestamps'], 1)

    def test_rejects_corrupt_or_unbounded_time_arrays(self):
        frames = np.zeros((4, 23))
        for t in ([0, .1, .09, .2], [0, .1, np.nan, .2], [0, .1],
                  [0, 0, 0, 0], [0, .1, .2, 10000]):
            with self.subTest(t=t), self.assertRaises(ValueError):
                resample_recorded_frames(frames, t, 30, 1000)

    def test_single_frame_stays_single(self):
        result, rate, _ = resample_recorded_frames(np.zeros((1, 23)), [10], 30, 100)
        self.assertEqual(result.shape, (1, 23)); self.assertEqual(rate, 30)

    def test_loader_resamples_both_sources_and_applies_speed_afterwards(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = write_episode(root)
            with h5py.File(path, 'a') as f:
                f.create_dataset('timestamp/t', data=[0, 1/30, 6/30, 7/30])
            scanned = scan_dataset_directory(str(root))['episodes'][0]
            self.assertAlmostEqual(scanned['duration_s'], 7/30)
            self.assertEqual(scanned['timing']['gap_count'], 1)
            for source in ('state', 'action'):
                frames, meta = load_replay_episode(str(root), str(path.relative_to(root)), source, 'arms', .5)
                self.assertEqual(len(frames), 8)
                self.assertEqual(meta['frames'], 8)
                self.assertEqual(meta['timing']['source_frames'], 4)
                self.assertAlmostEqual(meta['effective_rate_hz'], 15)
                self.assertAlmostEqual(meta['timing']['recorded_duration_s'], 7/30)

    def test_invalid_raw_target_cannot_disappear_during_resampling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = write_episode(root)
            with h5py.File(path, 'a') as f:
                f.create_dataset('timestamp/t', data=[0, 0, .1, .2])
                f['action/arm/position'][0, 0] = 100
            with self.assertRaisesRegex(RobotCommandRejected, '超出 SDK'):
                load_replay_episode(str(root), str(path.relative_to(root)), 'action', 'arms', 1)

    def test_missing_timestamp_preserves_legacy_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = write_episode(root)
            frames, meta = load_replay_episode(str(root), str(path.relative_to(root)), 'action', 'arms', 1)
            self.assertEqual(len(frames), 4)
            self.assertEqual(meta['timing']['basis'], 'nominal_rate')
