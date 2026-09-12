"""Shared Zerith export engine behind the original manual action buttons."""
import shutil
import time
from pathlib import Path
from dataqc import config
from dataqc.io import read_json, write_json
from dataqc.motion import RULE_VERSION


def convert_step(app,cfg):
    entries=app.authoritative_quality_grade_entries(cfg)
    def convert(job):
        threshold=app.stationary_threshold_for_cfg(cfg)
        def progress(text):
            if job.get("stop_requested"):raise RuntimeError("已停止")
            app.append_job(job,text)
        from dataqc.conversion_job import build
        import os,sys,uuid
        grades=list(dict.fromkeys(e['quality_grade'] for e in entries))
        outputs_by_grade={grade:app.lerobot_grade_dataset_dir(cfg,grade) for grade in grades}
        temporary_by_grade={grade:str(output.with_name(output.name+'.unified-build-'+uuid.uuid4().hex)) for grade,output in outputs_by_grade.items()}
        request=dict(entries=entries,threshold=threshold,cache=str(config.VAR/'manual-derived'),
                     outputs=temporary_by_grade,workers=cfg.get('lerobot_workers',1),
                     device=cfg.get('gpu_device','0') if cfg.get('lerobot_cuda',True) else 'cpu')
        if 'lerobot_workers' in cfg:
            from dataqc.parallel import THREAD_ENV
            run_dir=config.VAR/'conversion-jobs'/uuid.uuid4().hex
            request_path=run_dir/'request.json';result_path=run_dir/'result.json'
            write_json(request_path,request)
            env=dict(os.environ);env.update({key:'1'for key in THREAD_ENV})
            project=Path(__file__).resolve().parents[1]
            env['PYTHONPATH']=os.pathsep.join([str(project),str(project/'vendor'),env.get('PYTHONPATH','')])
            code=app.run_process_for_job(job,[sys.executable,'-m','dataqc.conversion_job',str(request_path),str(result_path)],project,env)
            progress('转换进程已结束' if code==0 else '转换进程失败')
            if code!=0:raise ValueError('LeRobot 转换失败，请查看任务日志；未发布本次输出')
            built=read_json(result_path)
        else:
            built=build(request,progress)
        outputs=[]
        for group in built:
            grade=group['grade'];values=group['values'];result=group['result']
            output=outputs_by_grade[grade];temporary=Path(temporary_by_grade[grade])
            progress('发布 '+grade+' · 已通过完整复检')
            # Keep the old source-name / quality / task mapping visible to old tools.
            mapping=read_json(temporary/'meta/episode_name_mapping.json')
            mapping.update(quality_grade=grade,repo_id=app.lerobot_grade_repo_id(cfg,grade),rules_version=RULE_VERSION,data_dir=str(cfg["hdf5_root"]))
            for item,value in zip(mapping['episodes'],values):
                e=value['entry'];item.update(quality_grade=grade,hdf5_episode_name=e['episode_id'],source_episode_name=e['episode_id'],hdf5_episode_dir=str(e['episode_dir']),hdf5_file=str(e['hdf5_file']))
            write_json(temporary/'meta/episode_name_mapping.json',mapping)
            backup=output.with_name(output.name+'.previous-'+str(time.time_ns()))
            if output.exists():output.rename(backup)
            try:temporary.rename(output)
            except Exception:
                if backup.exists():backup.rename(output)
                raise
            result['path']=str(output);outputs.append(result)
            progress(f'{grade}：{len(values)} 条，转换并复检通过 · {output}')
        # Let the existing status view read its normal renumber manifest.
        app.write_renumber_plan_step(cfg,entries)(job)
        if not built:progress('没有可导出的 A/B 数据；F 和待确认记录已排除。')
        write_json(config.VAR/'manual-last-export.json',dict(outputs=outputs,episodes=len(entries),rules_version=RULE_VERSION))
    convert_impl=convert
    def convert(job):
        from dataqc.video_encoding import video_encoding, selected_encoder
        device=cfg.get('gpu_device','0') if cfg.get('lerobot_cuda',True) else 'cpu'
        with video_encoding(device):
            app.append_job(job, '视频编码：'+selected_encoder()[1]+'；无需裁剪的视频直接复用')
            return convert_impl(job)
    convert.__name__='共享质检规则复核 → LeRobot A/B 转换'
    return convert


def split_step(app,cfg):
    def split(job):
        from dataqc.export import split_dataset
        def progress(text):
            if job.get("stop_requested"):raise RuntimeError("已停止")
            app.append_job(job,text)
        count=0
        for grade in ('A','B'):
            source=app.lerobot_grade_dataset_dir(cfg,grade)
            if not (source/'meta/info.json').is_file():continue
            staging=config.VAR/'manual-splits'/str(time.time_ns())/grade
            results=split_dataset(source,staging,app.stationary_threshold_for_cfg(cfg),progress)
            for result in results:
                hand=Path(result['path']).name
                output=app.lerobot_stage_split_output_dir(cfg,'left_hand' if hand=='left' else 'righthand',grade)
                output.parent.mkdir(parents=True,exist_ok=True)
                backup=output.with_name(output.name+'.previous-'+str(time.time_ns()))
                if output.exists():output.rename(backup)
                try:shutil.move(result['path'],output)
                except Exception:
                    if backup.exists():backup.rename(output)
                    raise
                progress(f'{grade} {hand}：切分并复检通过 · {output}');count+=1
        if not count:raise ValueError('请先完成 A/B LeRobot 转换')
    split_impl=split
    def split(job):
        from dataqc.video_encoding import video_encoding, selected_encoder
        device=cfg.get('gpu_device','0') if cfg.get('lerobot_cuda',True) else 'cpu'
        with video_encoding(device):
            app.append_job(job, '阶段切分视频编码：'+selected_encoder()[1])
            return split_impl(job)
    split.__name__='共享引擎按左右手阶段切分'
    return split
