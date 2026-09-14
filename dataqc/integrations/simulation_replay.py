"""Adapt simulation measurements to the common HDF5 player's wire format."""
from pathlib import Path

import h5py
from dataqc.io import CAMS, load, video_path
from dataqc.simulation import path_for


def is_simulation(root):
    path = path_for(root)
    if path is None:
        return False
    with h5py.File(path, 'r') as f:
        return f.attrs.get('format') == 'icra_wbc_aligned_joints' and f.attrs.get('robot_type') == 'zerith'


def read_for_replay(h5_path):
    if Path(h5_path).parent.name != 'states':
        return None
    root = Path(h5_path).parent.parent
    return load(root) if is_simulation(root) else None


def h5_payload(d, display_rows):
    times = (d['t'] - d['t'][0]).tolist()
    duration = times[-1]
    n = d['n']
    return dict(frame_count=n, duration=duration,
                inferred_fps=(n - 1) / duration if duration > 0 else 0,
                timestamps=times, state_joint_position=d['state'].tolist(),
                action_joint_position=d['action'].tolist(),
                state_joint_velocity=[[] for _ in range(n)],
                state_joint_effort=[[] for _ in range(n)],
                state_end_position=[[] for _ in range(n)],
                action_robot_velocity=d['action'][:, 21:23].tolist(),
                display_rows=display_rows('zerith', 23, 23))


def videos(root):
    result = []
    for cam, label in zip(CAMS, ('Head', 'Left Hand', 'Right Hand')):
        path = video_path(root, cam)
        if path.is_file():
            result.append(dict(key=cam, label=label, file=cam + '.mp4',
                               episode_relative_path=str(path.resolve().relative_to(Path(root).resolve()))))
    if not result:
        raise FileNotFoundError(f'No supported videos found under {root}')
    return result


def stage_info(d):
    # load() already validated the annotation against step_index and task.
    transitions = d['transitions']
    start = 0
    stages = []
    for i, end in enumerate(transitions):
        stages.append(dict(stage_number=i + 1, label=f'阶段 {i + 1}', start=start, end=end - 1))
        start = end
    boundary = transitions[0] if len(transitions) == 2 else None
    return dict(available=bool(stages), editable=False, consistent=not d['stage_issues'],
                source='simulation', frame_count=d['n'], stage2_start_frame=boundary,
                hdf5_stage2_start_frame=boundary, sidecar_stage2_start_frame=boundary,
                transitions=transitions, stages=stages, errors=d['stage_issues'])
