"""Strict RobotWin Zerith frame-group reader; source data stays untouched."""
import json
from pathlib import Path

import h5py
import numpy as np

from .io import CAMS, clean, normalized_task, parse_task, read_json

VERSION = 'zerith_sim_v1'
FIELDS = ([f'left.joint{i}' for i in range(1,8)] + ['left.gripper'] +
          [f'right.joint{i}' for i in range(1,8)] + ['right.gripper','lift.height',
          'waist.pitch','waist.yaw','head.yaw','head.pitch','speed.linear','speed.angular'])
CAMERA_KEYS = dict(zip(CAMS, ('head_color','hand_left_color','hand_right_color')))
POSTURE_NAMES = ['body_pitch_joint','body_yaw_joint','neck_yaw_joint','neck_pitch_joint']


def path_for(root):
    root = Path(root)
    paths = [p for p in (root/'states/aligned_joints.h5',root/'states/aligned_joints.hdf5') if p.is_file()]
    if len(paths)>1:
        raise ValueError('仿真目录包含多个 aligned_joints 文件，无法确定来源')
    return paths[0] if paths else None


def local_path(root, relative):
    root = Path(root).resolve()
    p = (root / str(relative)).resolve()
    if not p.is_relative_to(root):
        raise ValueError('仿真元数据中的文件路径必须位于 episode 目录内')
    return p


def video(root, cam):
    meta=read_json(Path(root)/'meta/episode_meta.json')
    key=CAMERA_KEYS[cam]
    return local_path(root,meta.get('videos',{}).get(key,f'videos/{key}.mp4'))


def task_text(text):
    text=normalized_task(str(text))
    if text.endswith('.') and parse_task(text[:-1]):
        return text[:-1]
    return text


def stage_mapping(meta, steps, n, targets):
    """Interpret annotated intervals, never infer task phases from equal durations."""
    original=meta.get('subtask_segments',[])
    stages=[];cursor=0;issues=[]
    exclusive=meta.get('segment_end_exclusive',False)
    for segment in original:
        b,e=segment.get('start'),segment.get('end')
        if type(b) is not int or type(e) is not int:
            return [],['仿真阶段边界必须为整数帧号']
        e=e if exclusive else e+1
        if b!=cursor or not 0<=b<e<=n:
            return [],['仿真阶段必须连续覆盖所有帧（检查包含/不包含末帧的约定）']
        step=segment.get('step_index')
        if step is not None and not np.all(steps[b:e]==step):
            return [],['仿真阶段描述与逐帧 step_index 不一致']
        text=segment.get('subtask') or (segment.get('description_en') or [''])[0]
        parsed=parse_task(task_text(text))
        if not parsed or len(parsed)!=1:
            return [],['仿真阶段缺少明确的操作手与目标物品标注']
        hand,item=next(iter(parsed.items()))
        if targets.get(hand)!=item:
            return [],['仿真阶段目标与整条任务不一致']
        if stages and stages[-1]['hand']==hand:
            stages[-1]['end']=e
        else:
            stages.append(dict(hand=hand,item=item,start=b,end=e))
        cursor=e
    if cursor!=n or [s['hand'] for s in stages]!=list(targets):
        return [],['仿真阶段缺失、顺序不符或未覆盖完整任务']
    return stages,issues


def read(root):
    root=Path(root)
    path=path_for(root)
    if path is None:raise ValueError('缺少仿真 states/aligned_joints.h5')
    meta=read_json(root/'meta/episode_meta.json')
    if not meta:raise ValueError('缺少仿真 meta/episode_meta.json')
    if local_path(root,meta.get('states_file',''))!=path.resolve():
        raise ValueError('仿真 states_file 与实际 HDF5 不一致')
    for cam in CAMS:
        video(root,cam)  # Validate metadata paths before any video access.
    if meta.get('state_action_fields')!=FIELDS:
        raise ValueError('仿真 State/Action 字段顺序不符合 Zerith 23 维定义')
    with h5py.File(path,'r') as f:
        attrs=clean(dict(f.attrs))
        for k,value in [('format','icra_wbc_aligned_joints'),('robot_type','zerith'),('source','robotwin_zerith_sim'),
                        ('state_dim',23),('action_dim',23),('action_mode','absolute'),('fps',30.0)]:
            if attrs.get(k)!=value:raise ValueError(f'仿真 {k} 应为 {value!r}，实际 {attrs.get(k)!r}')
        if json.loads(attrs.get('state_fields_json','[]'))!=FIELDS:
            raise ValueError('仿真 HDF5 字段顺序与元数据不一致')
        if not all(k.isdigit() and str(int(k))==k for k in f):
            raise ValueError('仿真 HDF5 根节点应为连续的数字帧组')
        keys=sorted(f,key=int);n=len(keys)
        if not n or list(map(int,keys))!=list(range(n)):
            raise ValueError('仿真帧组为空或编号缺失')
        if meta.get('frame_count')!=n or meta.get('fps')!=attrs['fps']:
            raise ValueError('仿真元数据帧数/FPS 与 HDF5 不一致')
        for key in ('state_dim','action_dim'):
            if meta.get(key)!=23:raise ValueError(f'仿真元数据 {key} 不为23')
        s=[];a=[];times=[];steps=[];source_indices=[];posture=[]
        raw_names=json.loads(attrs.get('source_articulation_dof_names_json','[]'))
        locked=bool(attrs.get('locked_posture_target_policy'))
        if locked and not all(k in raw_names for k in POSTURE_NAMES):
            raise ValueError('锁定腰头数据缺少 raw 关节名称映射')
        for key in keys:
            group=f[key]
            for kind,rows in [('state',s),('action',a)]:
                v=np.asarray(group[kind+'/vector'],dtype=float)
                if v.shape!=(23,):raise ValueError(f'第{key}帧 {kind}/vector 维度错误')
                joint=np.asarray(group[kind+'/joint/position'])
                parts=[joint[:7],np.asarray(group[kind+'/left_effector/position']),joint[7:],
                       np.asarray(group[kind+'/right_effector/position']),np.asarray(group[kind+'/waist/position']),
                       np.asarray(group[kind+'/head/position']),np.asarray(group[kind+'/robot/velocity'])]
                if [p.shape for p in parts]!=[(7,),(1,),(7,),(1,),(3,),(2,),(2,)]:
                    raise ValueError(f'第{key}帧 {kind} 部位维度错误')
                if not np.array_equal(v,np.concatenate(parts),equal_nan=True):
                    raise ValueError(f'第{key}帧 {kind} vector 与分部位字段不一致')
                rows.append(v)
            times.append(float(group['main_timestamp'][()]))
            step=group['meta/step_index'][()];index=group['meta/source_frame_index'][()]
            if not isinstance(step,np.integer) or not isinstance(index,np.integer):
                raise ValueError('仿真步骤与源帧编号必须为整数')
            steps.append(int(step));source_indices.append(int(index))
            if locked:
                raw=np.asarray(group['state/raw/joint_position_29'])
                if raw.shape!=(len(raw_names),) or not np.isfinite(raw).all():
                    raise ValueError('仿真 raw 关节值缺失、维度不符或非有限值')
                posture.append(raw[[raw_names.index(k) for k in POSTURE_NAMES]])
    state=np.asarray(s);action=np.asarray(a);t=np.asarray(times);steps=np.asarray(steps)
    if any(i<0 for i in source_indices) or any(a>=b for a,b in zip(source_indices,source_indices[1:])):
        raise ValueError('仿真源帧编号必须非负且严格递增')
    if not attrs.get('qc_derived') and source_indices!=list(range(n)):
        raise ValueError('仿真源帧存在缺失，需检查转换过程')
    copied=attrs.get('gripper_state_policy')=='same_as_converted_action_gripper'
    if copied and not np.array_equal(state[:,[7,15]],action[:,[7,15]],equal_nan=True):
        raise ValueError('仿真声明 State 夹爪复制 Action，但数值不一致')
    original_task=str(meta.get('task',meta.get('prompt','')));task=task_text(original_task)
    targets=parse_task(task) or {}
    stages,stage_issues=stage_mapping(meta,steps,n,targets)
    mismatches=[]
    if meta.get('task') and meta.get('prompt') and task_text(meta['task'])!=task_text(meta['prompt']):
        mismatches.append(f"任务冲突：task={meta['task']}；prompt={meta['prompt']}")
    for hand in ('left','right'):
        if meta.get(hand+'_target') and targets.get(hand)!=meta[hand+'_target']:
            mismatches.append(f"{'左手' if hand=='left' else '右手'}目标冲突：提示词={targets.get(hand)}；元数据={meta[hand+'_target']}")
    measured=state.copy()
    if locked:measured[:,17:21]=np.asarray(posture)
    return dict(root=root,state=state,action=action,t=t,attrs=attrs,n=n,transitions=[s['end'] for s in stages],
                task=task,meta=meta,collection=dict(config=dict(task_name=task),targets={h:meta.get(h+'_target') for h in targets}),
                source_format=VERSION,hdf5_path=path,original_task=original_task,original_stages=meta.get('subtask_segments',[]),
                source_frame_indices=source_indices,stage_issues=stage_issues,metadata_issues=mismatches,
                measured_state=measured,gripper_feedback_available=not copied,
                timestamp_policy=attrs.get('timestamp_policy','unspecified'))


def height_reference(root):
    root=Path(root)
    path=path_for(root)
    meta=read_json(root/'meta/episode_meta.json')
    if path is None or local_path(root,meta.get('states_file',''))!=path.resolve():
        return dict(error='仿真元数据 states_file 与实际 HDF5 不一致')
    if meta.get('state_action_fields')!=FIELDS:
        return dict(error='仿真升降高度字段映射无效')
    with h5py.File(path,'r') as f:
        if json.loads(f.attrs.get('state_fields_json','[]'))!=FIELDS:
            return dict(error='仿真 HDF5 高度字段映射无效')
        value=float(f['0/action/vector'][16])
        component=float(f['0/action/waist/position'][0])
    if not np.isfinite(value) or value!=component:
        return dict(error='仿真首帧 Action 高度无效或分部位数值不一致')
    return dict(expected_m=value,policy='per_episode_first_action',directory=str(root),
                source_hdf5=str(path),source_meta=str(root/'meta/episode_meta.json'),field='lift.height',frame=0,column=16,
                note='检查相对首帧的高度保持，不验证独立任务目标')


def numeric(d, threshold):
    from .checks import numeric_checks
    from .motion import arm_and_posture_checks
    checks=numeric_checks(d['state'],d['action'],d['t'],threshold)
    if d['n']>=2 and np.isfinite(d['state']).all() and np.isfinite(d['action']).all():
        measured=next(c for c in arm_and_posture_checks(d['measured_state'],d['action'],d['t']) if c['key']=='posture_state')
        if d['attrs'].get('locked_posture_target_policy'):
            measured['detail']['measurement_source']='state/raw/joint_position_29'
            checks=[measured if c['key']=='posture_state' else c for c in checks]
    for c in checks:
        if c['key']=='timestamps':c['detail'].update(timebase=d['timestamp_policy'],note='仿真合成时间轴；仅检查格式与间隔，不能证明真实采集时序')
    return checks


def derive(root, out, report, decision, threshold, source_fingerprint):
    """Preserve frame-group format, raw channels and original frame provenance."""
    import os
    import shutil
    from .repair import keep_indices
    from .io import video_path, encode_selection, write_json
    from .video_encoding import can_copy_video
    root,out=Path(root),Path(out);d=read(root)
    kept=keep_indices(d,report,decision,threshold)
    if out.exists():raise ValueError('派生目录已存在，不能覆盖历史结果')
    tmp=out.with_name(out.name+'.partial')
    if tmp.exists():shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    stages=[]
    for st in decision['stages']:
        b,e=np.searchsorted(kept,[st['start'],st['end']]).tolist()
        if b==e:raise ValueError('删帧后阶段为空')
        stages.append(st|dict(start=b,end=e))
    trimmed=len(kept)!=d['n']
    times=d['t'][kept] if not trimmed else d['t'][0]+np.arange(len(kept))/30
    try:
        target=tmp/'states/aligned_joints.h5';target.parent.mkdir(parents=True)
        with h5py.File(d['hdf5_path'],'r') as src,h5py.File(target,'w') as dst:
            for k,v in src.attrs.items():dst.attrs[k]=v
            dst.attrs['qc_derived']=True
            dst.attrs['quality_grade']=decision['grade']
            if trimmed:dst.attrs['timestamp_policy']='uniform_after_stationary_trim; original timestamps in provenance.json'
            for new,old in enumerate(kept):
                src.copy(src[str(old)],dst,name=str(new))
                g=dst[str(new)]
                stage=next(i for i,s in enumerate(stages) if s['start']<=new<s['end'])
                if 'meta/source_step_index' not in g:
                    g.create_dataset('meta/source_step_index',data=g['meta/step_index'][()])
                g['meta/step_index'][()]=stage+1
                g['main_timestamp'][()]=times[new]
                if 'timestamp/camera' in g:
                    for key in g['timestamp/camera']:g['timestamp/camera/'+key][()]=times[new]
        for cam in CAMS:
            dest=tmp/'videos'/f'{CAMERA_KEYS[cam]}.mp4';dest.parent.mkdir(parents=True,exist_ok=True)
            if not trimmed and can_copy_video(video_path(root,cam)):
                shutil.copyfile(video_path(root,cam),dest)
            else:encode_selection(video_path(root,cam),dest,kept)
        meta=dict(d['meta'])
        meta.update(frame_count=len(kept),states_file='states/aligned_joints.h5',fps=30.0,state_fps=30.0,video_fps=30.0,
                    duration_sec=float(times[-1]-times[0]),task=decision['corrected_prompt'],prompt=decision['corrected_prompt'],
                    segment_end_exclusive=True,quality_grade=decision['grade'],video_output_frames=len(kept),
                    videos={key:f'videos/{key}.mp4' for key in CAMERA_KEYS.values()},
                    subtask_segments=[dict(start=s['start'],end=s['end'],step_index=i+1,
                        subtask=f"Grasp {s['item']} with the {s['hand']} hand") for i,s in enumerate(stages)])
        meta['segment_instructions']=meta['subtask_segments']
        targets=parse_task(decision['corrected_prompt']) or {}
        for hand in ('left','right'):meta[hand+'_target']=targets.get(hand,'')
        write_json(tmp/'meta/episode_meta.json',meta)
        write_json(tmp/'provenance.json',dict(source=str(root),source_format=VERSION,source_fingerprint=source_fingerprint,
            source_frame_indices=kept.tolist(),original_source_frame_indices=[d['source_frame_indices'][i] for i in kept],
            source_timestamps_seconds=d['t'][kept].tolist(),original_prompt=d['original_task'],output_prompt=decision['corrected_prompt'],
            removed_frames=d['n']-len(kept),stages=stages,original_stages=decision['stages'],original_sim_stages=d['original_stages'],
            source_semantics=report.get('source_semantics',{}),source_report=report,decision=decision))
        write_json(tmp/'review.json',dict(grade=decision['grade'],reviewed=True,source='dataqc',reason=decision['reason']))
        os.replace(tmp,out)
    except BaseException:
        if tmp.exists():shutil.rmtree(tmp)
        raise
    return out
