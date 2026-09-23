"""Reconstruct a uniform playback timeline from recorded sample times."""
from __future__ import annotations

import numpy as np


def recorded_timeline(timestamps, frame_count, nominal_rate):
    """Validate recorded times and return elapsed seconds plus diagnostics."""
    t = np.asarray(timestamps, dtype=np.float64)
    if frame_count < 1 or t.shape != (frame_count,) or not np.isfinite(t).all():
        raise ValueError('timestamp/t 必须是与关节帧数一致的有限一维时间戳')
    if not np.isfinite(nominal_rate) or nominal_rate <= 0:
        raise ValueError('采样率必须为正数')
    unit = 'ms' if np.median(t) > 1e11 else 's'
    # Subtract before converting to seconds to preserve epoch precision.
    t = (t - t[0]) / (1000.0 if unit == 'ms' else 1.0)
    dt = np.diff(t)
    if np.any(dt < 0):
        raise ValueError('timestamp/t 存在倒退，无法按采集时间回放')
    duplicates = int(np.count_nonzero(dt == 0))
    duration = float(t[-1])
    info = {'basis': 'timestamp/t', 'timestamp_unit': unit,
            'source_frames': frame_count, 'duplicate_timestamps': duplicates,
            'recorded_duration_s': duration,
            'max_interval_ms': float(dt.max()*1000) if len(dt) else 0.0,
            'gap_count': int(np.count_nonzero(dt > 1.5/nominal_rate))}
    if frame_count > 1 and duration == 0:
        raise ValueError('timestamp/t 所有时间戳相同，无法还原动作时长')
    return t, info


def resample_recorded_frames(frames, timestamps, nominal_rate, max_frames):
    """Return samples, their rate and timing diagnostics; inputs stay untouched.

    Vendor timestamp/t uses epoch milliseconds (older exports may use seconds).
    Duplicate timestamps keep the last sample. Grippers use sample-and-hold;
    position joints interpolate linearly. Validate raw joint limits first so
    resampling cannot conceal an invalid source row.
    """
    values = np.asarray(frames, dtype=np.float64)
    t, info = recorded_timeline(timestamps, len(values), nominal_rate)
    keep = np.r_[np.diff(t) > 0, True]
    unique_t, unique_values = t[keep], values[keep]
    duration = info['recorded_duration_s']
    if len(unique_t) == 1:
        result, rate = unique_values.copy(), nominal_rate
    else:
        intervals = max(1, int(round(duration * nominal_rate)))
        if intervals + 1 > max_frames:
            raise ValueError('按时间戳重建后的帧数超过回放上限')
        grid = np.linspace(0.0, duration, intervals + 1)
        result = np.column_stack([
            np.interp(grid, unique_t, unique_values[:, column])
            for column in range(values.shape[1])
        ])
        held = np.searchsorted(unique_t, grid, side='right') - 1
        result[:, [7, 15]] = unique_values[held][:, [7, 15]]
        result[0], result[-1] = unique_values[0], unique_values[-1]
        rate = intervals / duration
    info['playback_frames'] = len(result)
    info['resampled'] = bool(len(result) != len(values) or not np.array_equal(result, values))
    return result, float(rate), info
