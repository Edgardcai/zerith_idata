"""Per-episode references, independent of dataset names and other episodes."""
from pathlib import Path
import h5py
import numpy as np
from .io import read_json, is_simulation


def height_reference(root):
    root=Path(root)
    references=[]
    for filename in ('collection_task.json','episode_meta.json','meta/episode_meta.json'):
        meta=read_json(root/filename)
        for keys in (('targets','lift_height'),('target_lift_height',),('lift_target_height',)):
            value=meta
            for key in keys:
                value=value.get(key) if isinstance(value,dict) else None
            if value is None:continue
            field=filename+' → '+'.'.join(keys)
            try:
                if isinstance(value,bool):raise ValueError()
                number=float(value)
                if not np.isfinite(number):raise ValueError()
            except (ValueError,TypeError):
                return dict(error=f'目标高度无效：{field}={value!r}，需要有限数值（m）')
            references.append(dict(source=field,value_m=number))
    if references:
        height=references[0]['value_m']
        if any(abs(r['value_m']-height)>1e-7 for r in references):
            return dict(error='目标高度冲突：'+'；'.join(f"{r['source']}={r['value_m']:g} m" for r in references),references=references)
        return dict(expected_m=height,policy='episode_target',source=references[0]['source'],references=references)
    try:
        if is_simulation(root):
            from .simulation import height_reference as simulation_reference
            return simulation_reference(root)
        with h5py.File(root/'episode.hdf5','r') as f:
            value=float(f['action/waist/position'][0,0])
        if not np.isfinite(value):raise ValueError('首帧 Action 高度不是有限数值')
        return dict(expected_m=value,policy='per_episode_first_action',source='episode.hdf5 → action/waist/position[0,0]',
                    note='检查相对首帧指令的高度保持；未提供独立任务目标')
    except (OSError,ValueError,KeyError,IndexError) as exc:
        return dict(error='无法读取本条数据的升降柱参考：'+str(exc))
