"""Simulation support at legacy CLI/UI boundaries, sharing dataqc measurements."""
from pathlib import Path
from urllib.parse import urlencode

from dataqc.io import discover, hdf5_path, is_simulation, load, read_json, video_path


def read_legacy_episode(root, profile, actions_json=None):
    from quality_pipeline.episode_io import EpisodeData, StateFrame
    import cv2
    if actions_json:raise ValueError('仿真格式已包含 Action，不支持额外拼接动作文件')
    d=load(root);counts={}
    for cam in profile.cameras:
        cap=cv2.VideoCapture(str(video_path(root,cam.raw_key)))
        counts[cam.raw_key]=int(cap.get(cv2.CAP_PROP_FRAME_COUNT));cap.release()
    return EpisodeData(root=Path(root),episode_id=Path(root).name,
        meta=dict(d['meta'],raw_hdf5_path=str(d['hdf5_path']),source_format=d['source_format']),
        state_frames=[StateFrame(frame_idx=i,timestamp=float(t-d['t'][0]),state=s.tolist()) for i,(t,s) in enumerate(zip(d['t'],d['state']))],
        actions=d['action'].tolist(),camera_counts=counts,
        stationary_state_vectors=d['state'].tolist(),stationary_action_vectors=d['action'].tolist())


def integrate_simulation(app):
    # Legacy inferred every aligned_joints.h5 dataset as ALOHA by filename.
    # Read the explicit simulation identity before selecting the robot profile.
    old_choice=app.dataset_choice_entry
    def choice(dataset_dir,scan_root,group=''):
        result=old_choice(dataset_dir,scan_root,group)
        if result.get('dataset_type')=='hdf5':
            import h5py
            for child in app.safe_iterdir(Path(dataset_dir)):
                if not child.is_dir() or not is_simulation(child):continue
                with h5py.File(hdf5_path(child),'r') as f:
                    if f.attrs.get('format')=='icra_wbc_aligned_joints' and f.attrs.get('robot_type')=='zerith':
                        result.update(robot_type='zerith',source_format='zerith_sim_v1')
                        break
        return result
    app.dataset_choice_entry=choice

    # These old in-place tools assume columnar Zerith or nanosecond G2 data.
    # The shared repair engine supports simulation through derived copies.
    def guard_inplace(old):
        def guarded(cfg,*args,**kwargs):
            if any(is_simulation(p) for p in discover(cfg['hdf5_root'])):
                raise ValueError('仿真格式不使用真机原地删帧/UUID重编号工具；请在模块③复核静止段，生成派生副本后复检导出')
            return old(cfg,*args,**kwargs)
        return guarded
    app.repair_commands=guard_inplace(app.repair_commands)
    app.optimize_hdf5_commands=guard_inplace(app.optimize_hdf5_commands)

    from .legacy_ui import replace
    app.HTML=replace(app.HTML,'      optimize.disabled = !isZerith;','''      const isSimulation = selectedSourceDatasetChoice()?.source_format === "zerith_sim_v1";
      optimize.disabled = !isZerith || isSimulation;
      const repair = document.querySelector('button[data-stage="repair"]');
      repair.disabled = isSimulation;
      repair.title = isSimulation ? "仿真请在模块③复核静止段并生成派生副本" : "选择 episode 剔除静止帧";''')
    app.HTML=replace(app.HTML,'      optimize.title = isZerith ?', '      optimize.title = isSimulation ? "仿真无需真机 UUID 重编号" : isZerith ?')
    old_replay=app.start_replay
    def replay(payload):
        cfg=app.derive_paths(payload)
        episodes=discover(cfg['hdf5_root'])
        if episodes and any(is_simulation(p) for p in episodes):
            return dict(url='/auto/hdf5?'+urlencode(dict(root=str(cfg['hdf5_root']),replay='1')),
                        source_format='zerith_sim_v1',episode_count=len(episodes))
        return old_replay(payload)
    app.start_replay=replay
    old_preflight=app.validate_lerobot_stage_split_grade
    def preflight(cfg,grade,source):
        mapping=read_json(Path(source)/'meta/episode_name_mapping.json')
        records=mapping.get('episodes',[])
        if not any(r.get('source_format')=='zerith_sim_v1' for r in records):
            return old_preflight(cfg,grade,source)
        from dataqc.export import validate_dataset
        try:
            result=validate_dataset(source,app.stationary_threshold_for_cfg(cfg))
            issues=list(result['issues'])
            for r in records:
                d=load(r['source_root'])
                if len(d['transitions'])!=2:issues.append({'episode':r['episode_index'],'reason':'缺少左右手两阶段'})
            return dict(grade=grade,compatible=not issues,checked_episode_count=len(records),error_count=len(issues),
                        errors=[str(i) for i in issues],reason='复检通过' if not issues else str(issues[0]))
        except (ValueError,OSError,KeyError) as exc:
            return dict(grade=grade,compatible=False,checked_episode_count=0,error_count=1,errors=[str(exc)],reason=str(exc))
    app.validate_lerobot_stage_split_grade=preflight
