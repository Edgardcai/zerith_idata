"""Shared Zerith export engine behind the original manual action buttons."""
import shutil
import time
from pathlib import Path
from dataqc import config
from dataqc.io import read_json, write_json
from dataqc.motion import RULE_VERSION


def direct_entries(app, cfg):
    """Keep recorded grades as labels; never consult QC approval gates."""
    rows = app.dataset_status(app.stringify_config(cfg)).get("episodes", [])
    by_id = {row["episode_id"]: row for row in rows}
    entries = []
    for source in app.lerobot_source_episode_entries(cfg["hdf5_root"]):
        row = by_id.get(source["episode_id"], {})
        grade = (app.normalise_quality_grade(row.get("manual_quality_grade")) or
                 app.normalise_quality_grade(row.get("quality_grade")) or "UNRATED")
        entries.append(dict(source, quality_grade=grade))
    return entries


def post_qc_step(app, cfg):
    def check(job):
        from dataqc.export import validate_dataset
        def progress(text):
            if job.get("stop_requested"): raise RuntimeError("已停止")
            app.append_job(job, text)
        candidates = app.available_lerobot_replay_grades(cfg)
        if not candidates: raise ValueError("请先转换 LeRobot，或选择已有 LeRobot 数据集")
        for item in candidates:
            root = Path(item["dataset_dir"])
            progress("LeRobot 转换后质检：" + str(root))
            try:
                report = validate_dataset(root, app.stationary_threshold_for_cfg(cfg), progress)
            except Exception as exc:
                report = dict(passed=False, issues=[dict(error=str(exc))], warnings=[], rule_version=RULE_VERSION)
            report.update(dataset=str(root), checked_at=time.time(), mode="post_conversion")
            write_json(root / "qc_report.json", report)
            progress(("质检通过" if report["passed"] else "质检未通过") + "；报告：" + str(root / "qc_report.json"))
            for issue in report["issues"]:
                if isinstance(issue, dict) and "check" in issue:
                    from dataqc.motion import failure_reason
                    message = failure_reason([issue["check"]])
                    progress(f"episode {issue.get('episode')}：{message}")
                else: progress(str(issue))
            progress(f"预警 {len(report['warnings'])} 项；转换结果和人工等级已保留")
    check.__name__ = "LeRobot 转换后质检（仅生成报告）"
    return check


def convert_step(app,cfg):
    entries=direct_entries(app,cfg)
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
        request=dict(entries=entries,direct=True,threshold=threshold,cache=str(config.VAR/'manual-derived'),
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
        outputs=[];skipped=[]
        for group in built:
            grade=group['grade'];values=group['values'];result=group['result']
            skipped.extend(dict(issue,grade=grade) for issue in result.get('skipped',[]))
            if not result.get('path'):continue
            output=outputs_by_grade[grade];temporary=Path(temporary_by_grade[grade])
            progress('发布 '+grade+' · 直接转换，未执行质检')
            # Keep the old source-name / quality / task mapping visible to old tools.
            mapping=read_json(temporary/'meta/episode_name_mapping.json')
            mapping.update(quality_grade=grade,repo_id=app.lerobot_grade_repo_id(cfg,grade),quality_check="not_run",data_dir=str(cfg["hdf5_root"]))
            for item,value in zip(mapping['episodes'],values):
                e=value['entry'];item.update(quality_grade=grade,hdf5_episode_name=e['episode_id'],source_episode_name=e['episode_id'],hdf5_episode_dir=str(e['episode_dir']),hdf5_file=str(e['hdf5_file']))
            write_json(temporary/'meta/episode_name_mapping.json',mapping)
            backup=output.with_name(output.name+'.previous-'+str(time.time_ns()))
            if output.exists():output.rename(backup)
            try:temporary.rename(output)
            except Exception:
                if backup.exists():backup.rename(output)
                raise
            result['path']=str(output);result['batch_report']=str(output/'batch_report.json');outputs.append(result)
            progress(f'{grade}：{len(values)} 条，转换完成（未质检） · {output}')

        if not built:progress('没有可转换的 HDF5 数据。')
        report=dict(outputs=outputs,attempted=len(entries),completed=sum(r['episodes'] for r in outputs),
                    failed=len(skipped),skipped=skipped,quality_check="not_run")
        write_json(config.VAR/'manual-last-export.json',report)
        progress(f"转换结果：成功 {report['completed']} 条，未完成 {report['failed']} 条；清单：{config.VAR/'manual-last-export.json'}")
        for issue in skipped:progress(f"未完成 {issue.get('episode')}：{issue['error']}")
        if skipped:job['label']=f"LeRobot 转换部分完成：成功 {report['completed']}，未完成 {report['failed']}"
        if entries and not outputs:raise ValueError('没有转换成功的数据，请查看未完成清单')
    convert_impl=convert
    def convert(job):
        from dataqc.video_encoding import video_encoding, selected_encoder
        device=cfg.get('gpu_device','0') if cfg.get('lerobot_cuda',True) else 'cpu'
        with video_encoding(device):
            app.append_job(job, '视频编码：'+selected_encoder()[1]+'；无需裁剪的视频直接复用')
            return convert_impl(job)
    convert.__name__='直接转换 LeRobot（不质检，保留已有等级）'
    return convert


def split_step(app,cfg):
    def split(job):
        from dataqc.export import split_dataset
        def progress(text):
            if job.get("stop_requested"):raise RuntimeError("已停止")
            app.append_job(job,text)
        count=0;skipped=[];outputs=[]
        from .direct_controls import split_sources, output_config
        sources=split_sources(app,cfg)
        for grade,source in sources:
            staging=config.VAR/'manual-splits'/str(time.time_ns())/grade
            try:
                results=split_dataset(source,staging,app.stationary_threshold_for_cfg(cfg),progress,quality_check=False)
            except (OSError,ValueError,KeyError,TypeError) as exc:
                skipped.append(dict(grade=grade,episode=str(source),error=str(exc)))
                progress(f'未完成 {grade}：{exc}');continue
            skipped.extend(dict(issue,grade=grade) for issue in read_json(staging/'batch_report.json').get('skipped',[]))
            for result in results:
                if not result.get('path'):continue
                hand=result.get('hand') or Path(result['path']).name
                output=app.lerobot_stage_split_output_dir(output_config(app,cfg,source,grade),'left_hand' if hand=='left' else 'righthand',grade)
                output.parent.mkdir(parents=True,exist_ok=True)
                backup=output.with_name(output.name+'.previous-'+str(time.time_ns()))
                if output.exists():output.rename(backup)
                try:shutil.move(result['path'],output)
                except Exception:
                    if backup.exists():backup.rename(output)
                    raise
                outputs.append(dict(path=str(output),hand=hand,grade=grade,episodes=result['episodes']))
                progress(f'{grade} {hand}：成功 {result["episodes"]} 段（未质检） · {output}');count+=result['episodes']
        report=dict(completed_segments=count,skipped=skipped,outputs=outputs,quality_check='not_run')
        write_json(config.VAR/'manual-last-split.json',report)
        progress(f"切分结果：成功 {count} 段，未完成 {len(skipped)} 项；清单：{config.VAR/'manual-last-split.json'}")
        for issue in skipped:progress(f"未完成 {issue.get('grade','')} {issue.get('episode','')}：{issue['error']}")
        if skipped:job['label']=f'LeRobot 切分部分完成：成功 {count} 段，未完成 {len(skipped)} 项'
        if not count:raise ValueError('没有切分成功的数据，请查看未完成清单或选择已有 LeRobot 数据集')
    split_impl=split
    def split(job):
        from dataqc.video_encoding import video_encoding, selected_encoder
        device=cfg.get('gpu_device','0') if cfg.get('lerobot_cuda',True) else 'cpu'
        with video_encoding(device):
            app.append_job(job, '阶段切分视频编码：'+selected_encoder()[1])
            return split_impl(job)
    split.__name__='按左右手阶段直接切分（不质检）'
    return split
