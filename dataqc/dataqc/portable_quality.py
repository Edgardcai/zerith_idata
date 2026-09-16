"""Optional numeric/video QC when the original HDF5 is no longer available."""
from pathlib import Path
import numpy as np
import pyarrow.parquet as pq
import cv2
from .io import prompt_issues, CAMS, read_json, hdf5_path, parse_task, normalized_task, open_video
from .checks import numeric_checks
from .motion import RULE_VERSION, gripper_check, result


def sources_available(maps):
    for m in maps:
        try:
            if not hdf5_path(m['source_root']).is_file():return False
        except (KeyError,ValueError,OSError):return False
    return True


def validate_portable(root,threshold=40,progress=lambda _:None):
    from .direct_export import dataset_file,split_records
    from .export import rows
    root=Path(root);info,maps=split_records(root);episodes=rows(root/'meta/episodes.jsonl');tasks=rows(root/'meta/tasks.jsonl')
    issues=[];warnings=[dict(check='source_correspondence',reason='原 HDF5 不可用；检查当前 LeRobot 的数值和视频，未核对原始 HDF5/内嵌图像。')]
    count=0
    if info.get('total_episodes')!=len(episodes):issues.append('episode 总量不一致')
    if info.get('total_tasks')!=len(tasks):issues.append('task 总量不一致')
    for position,(ep,m) in enumerate(zip(episodes,maps)):
        i=m['episode_index'];progress(f'LeRobot 独立质检 {position+1}/{len(maps)}')
        try:
            table=pq.read_table(dataset_file(root,info,i));data=table.to_pydict();n=len(table)
            if not n or n!=ep['length'] or i!=position:raise ValueError('episode 编号或帧数不一致')
            s=np.asarray(data['observation.state']);a=np.asarray(data['action']);t=np.asarray(data['timestamp'])
            for key,want in [('frame_index',np.arange(n)),('episode_index',np.full(n,i)),('index',np.arange(count,count+n))]:
                if not np.array_equal(data[key],want):raise ValueError(key+' 不一致')
            tids=set(data['task_index'])
            if len(tids)!=1 or not 0<=next(iter(tids))<len(tasks):raise ValueError('任务索引无效')
            task=tasks[next(iter(tids))]['task']
            if ep['tasks']!=[task]:raise ValueError('任务映射不一致')
            checks=numeric_checks(s,a,t,threshold)
            parsed=parse_task(normalized_task(task))
            if not parsed:checks.append(result('prompt','任务文本','warn',{},prompt_issues(task)))
            stages=m.get('stages') or []
            # A hand clip has its own local range; do not reuse the full episode's offsets.
            if m.get('source_lerobot') and parsed and len(parsed)==1:
                hand=next(iter(parsed));stages=[dict(hand=hand,start=0,end=n,item=parsed[hand])]
            if s.shape==(n,23) and a.shape==(n,23) and n>=2 and np.isfinite(s).all() and np.isfinite(a).all():
                checks.append(gripper_check(s,a,task,[],stages))
            for c in checks:
                if c['status']=='fail':issues.append(dict(episode=i,check=c))
                elif c['status']=='warn':warnings.append(dict(episode=i,check=c))
            for cam in CAMS:
                key='observation.images.'+cam;cap=open_video(dataset_file(root,info,i,key));frames=0;shape=None
                try:
                    while True:
                        ok,img=cap.read()
                        if not ok:break
                        frames+=1;shape=list(img.shape)
                    if frames!=n or shape!=info['features'][key]['shape']:raise ValueError(cam+' 视频帧数或尺寸不一致')
                finally:cap.release()
            count+=n
        except (OSError,ValueError,KeyError,TypeError) as exc:issues.append(dict(episode=i,error=str(exc)))
    if count!=info.get('total_frames'):issues.append('total_frames 不一致')
    return dict(rule_version=RULE_VERSION,passed=not issues,issues=issues,warnings=warnings,
                episodes=len(episodes),frames=count,source_correspondence_checked=False)
