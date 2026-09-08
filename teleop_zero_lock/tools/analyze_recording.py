#!/usr/bin/env python3
"""Read-only audit of a finalized vendor episode; never edits training data."""
import argparse
import datetime
import json
from pathlib import Path

import h5py
import numpy as np


def analyze(directory):
    directory = Path(directory)
    if '采集流结束' not in (directory / 'collection.log').read_text():
        raise ValueError('Episode is not finalized; refusing to read live HDF5')
    report = {'directory': str(directory), 'fields': {}, 'images': {}}
    with h5py.File(directory / 'episode.hdf5', 'r') as f:
        t = f['timestamp/t'][:]
        seconds = t / (1000 if np.median(t) > 1e11 else 1)
        dt = np.diff(seconds)
        report.update(frames=len(t), start=datetime.datetime.fromtimestamp(seconds[0]).isoformat(),
                      end=datetime.datetime.fromtimestamp(seconds[-1]).isoformat(),
                      duration_s=float(seconds[-1] - seconds[0]),
                      actual_hz=float((len(t) - 1) / (seconds[-1] - seconds[0])),
                      monotonic_timestamps=bool(np.all(dt > 0)),
                      max_interval_ms=float(dt.max() * 1000),
                      intervals_gt50ms=int(np.sum(dt > .05)),
                      configured_hz=float(f.attrs['control_frequency']),
                      completed_subtasks=int(f.attrs['completed_subtasks']),
                      total_subtasks=int(f.attrs['total_subtasks']))

        def visit(name, obj):
            if not isinstance(obj, h5py.Dataset):
                return
            if name.startswith(('action/', 'observation/state/')):
                a = obj[:]
                report['fields'][name] = {'frames_match': len(a) == len(t),
                                         'finite': bool(np.isfinite(a).all()),
                                         'min': np.min(a, axis=0).tolist(),
                                         'max': np.max(a, axis=0).tolist()}
            elif name.startswith('observation/images/'):
                # Verify length and three encoded samples without loading every image.
                indexes = sorted({0, len(t) // 2, len(t) - 1})
                report['images'][name] = {'frames_match': len(obj) == len(t),
                                         'sample_nonempty': all(np.asarray(obj[i]).size > 0 for i in indexes)}
        f.visititems(visit)
        actual = np.c_[f['observation/state/waist/position'][:, 1:3],
                       f['observation/state/head/position'][:]]
        commands = np.c_[f['action/waist/position'][:, 1:3], f['action/head/position'][:]]
        limits = np.array([.005, .005, .005, .021])
        report['axis_order'] = ['waist_pitch', 'waist_yaw', 'head_yaw', 'head_pitch']
        report['limits_rad'] = limits.tolist()
        report['actual_max_abs_rad'] = np.max(np.abs(actual), axis=0).tolist()
        report['actual_outside_frames'] = np.sum(np.abs(actual) > limits, axis=0).tolist()
        report['command_max_abs_rad'] = np.max(np.abs(commands), axis=0).tolist()
        report['commands_zero_within_1e_minus_6'] = bool(np.all(np.abs(commands) <= 1e-6))
        report['actual_within_limits'] = bool(np.isfinite(actual).all() and np.all(np.abs(actual) <= limits))
        report['numeric_integrity'] = all(d['frames_match'] and d['finite'] for d in report['fields'].values())
        report['image_sample_integrity'] = all(d['frames_match'] and d['sample_nonempty'] for d in report['images'].values())
        report['subtask_transitions'] = f['subtask_transitions'][:].tolist()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('directory')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = analyze(args.directory)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k not in ('fields', 'images')}, ensure_ascii=False, indent=2))
