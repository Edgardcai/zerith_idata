"""Single isolated market QC/export job, with incremental reports and cancellation."""
import os
import re
import shutil
from pathlib import Path
import sys
import time
from collections import Counter

from . import market
from .io import write_json, load, CAMS
from .market_api import HOME, BASE, locked, resolve, snapshot, report_path, revision, directory_identity


class Cancelled(Exception): pass


def run(ident):
    path=HOME/'jobs'/(ident+'.json')
    # Parent publishes the child PID before releasing the admission lock.
    with locked('jobs'): job=market.read(path)
    scope_root=None;scope_identity=None
    def update(**fields):
        job.update(fields,updated=time.time());write_json(path,job)
    def check_stop():
        if (HOME/'jobs'/(ident+'.cancel')).exists(): raise Cancelled()
        if scope_root is not None:
            try:
                unchanged=not scope_root.is_symlink() and directory_identity(scope_root)==scope_identity
            except OSError:unchanged=False
            if not unchanged:raise ValueError('所选目录已被移动、删除或替换；任务已停止，未发布。请重新选择该批目录')
    def progress(text):
        check_stop();update(message=str(text))
    try:
        root=resolve(job['root']);scope_root=root
        scope_identity=job.get('source_identity') or directory_identity(root)
        check_stop();not_scanned=[];sources=market.discover(root, excluded=not_scanned)
        update(not_scanned=not_scanned)
        if not sources:
            raise ValueError('没有可处理的商超录制；录制中的 .partial 目录不参与质检或转换')
        update(status='running',total=len(sources),pid=os.getpid(),source_identity=scope_identity)
        if job['kind'] in ('qc','qc_export'):
            cached=0;errors=[]
            for index,source in enumerate(sources):
                check_stop();progress(f'{index+1}/{len(sources)} · {source.relative_to(root)}')
                old=market.read(report_path(source))
                try:
                    if not job['force'] and old.get('version')==market.VERSION and old.get('signature')==market.signature(source): cached+=1
                    else:
                        kwargs={'previous':old} if not job['force'] and old.get('version')=='market_four_phase_v1' else {}
                        result=market.inspect(source,lambda text:progress(f'{index+1}/{len(sources)} · {source.name} · {text}'),**kwargs)
                        write_json(report_path(source),result)
                except Cancelled: raise
                except Exception as exc: errors.append(dict(id=str(source.relative_to(root)),error=str(exc)))
                update(done=index+1,reused=cached,errors=errors)
            update(status=('running' if job['kind']=='qc_export' else 'completed' if not errors else 'partial'),message=f'质检结束：{len(sources)-len(errors)} 条完成，{cached} 条复用，{len(errors)} 条执行失败')
        if job['kind'] in ('export','qc_export'):
            update(status='running',done=0,message='质检完成，开始按等级转换')
            check_stop()
            result=export_dataset(root,sources,job,progress,lambda done:update(done=done),check_stop)
            counts='、'.join(f"{d['grade']} {d['episodes']} 条" for d in result['datasets'])
            update(status='completed' if not result['skipped'] and not result['not_scanned'] and not job.get('errors') else 'partial',result=result,
                   message=f"分级转换完成：{counts} → {result['path']}；排除 {len(result['skipped'])} 条，未扫描 {len(result['not_scanned'])} 个目录")
    except Cancelled:
        update(status='cancelled',message='已停止；已完成的质检报告保留')
    except BaseException as exc:
        update(status='failed',message=str(exc))
        raise


def export_dataset(root,sources,job,progress,done=lambda _:None,check_stop=lambda:None):
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq
    from .export import write_episode, path_for, stats, publish_dataset
    from .market_validation import validate_dataset
    from .market_source_fields import archive_path, write_source_fields
    from .video_encoding import can_copy_video, encode_full_video
    from .io import video_path
    camera_mode = job.get('source_camera_mode', os.environ.get('DATAQC_MARKET_SOURCE_CAMERAS', 'video'))
    out=BASE/'lerobot'/('market_all_'+job['id'])
    temp=out.with_name(out.name+'.partial')
    if out.exists() or temp.exists(): raise ValueError('输出目录已存在，拒绝覆盖')
    temp.mkdir(parents=True)
    duplicate_ids=Counter(ident for p in sources
                          if isinstance(ident:=market.read(p/'episode_meta.json').get('episode_id'),str))
    groups={};skipped=[]
    failed_qc={e['id'] for e in job.get('errors',[]) if e.get('id')}
    source_snapshots=[]

    def selection():
        write_json(temp/'selection.json',dict(layout=market.EXPORT_LAYOUT,
            included=sum(len(g['episodes']) for g in groups.values()),skipped=skipped,
            requested_grades=job['grades'],scanned=len(sources),not_scanned=job.get('not_scanned',[]),
            duplicate_uuid_policy='preserve_all_source_directories',
            duplicate_uuid_groups=sum(count>1 for count in duplicate_ids.values()),
            grades={grade:len(g['episodes']) for grade,g in groups.items()}))

    def assert_sources_unchanged():
        for source,sig,rev in source_snapshots:
            check_stop()
            if sig!=market.signature(source) or rev!=revision(source):
                raise ValueError('发布前源数据或人工评级发生变化，未发布；请重新转换')

    selection()
    for index,source in enumerate(sources):
        check_stop();progress(f'按等级转换 {index+1}/{len(sources)} · {source.name}')
        ident=str(source.relative_to(root))
        try:
            row=snapshot(source,root,True);report=row['report']
            reason=None
            if ident in failed_qc:reason='本次质检执行失败，不采用以前的报告导出'
            elif row.get('error'):reason=row['error']
            elif not re.fullmatch(r'[0-9a-fA-F]{32}',str(row['uuid'] or '')):reason='没有有效 episode UUID'
            elif row.get('complete') is not True:reason='录制未完整结束，不能导出'
            elif row['qc_status']!='completed':reason='尚未完成当前版本质检，或源文件已变化'
            elif row['hard_fail']:reason='结构或媒体硬失败，不能发布为训练数据'
            elif row['effective_grade'] not in job['grades']:reason='等级不在本次选择范围'
            elif job.get('manual_only') and not row['manual_grade']:reason='尚未人工确认整体等级'
            elif not row['phase_sequence'] or any(not p.get('valid') for p in report['phases'] if not p.get('missing')):reason='实际阶段标注或帧范围无效'
            elif any(c['key']=='phase_layout' and c['status']!='pass' for c in report['checks']):reason='阶段边界不一致'
        except (OSError,ValueError,TypeError,KeyError) as exc:
            reason=str(exc)
        if reason:
            skipped.append(dict(id=ident,reason=reason));done(index+1);selection();continue
        grade=row['effective_grade']
        if grade not in groups:
            groups[grade]=dict(episodes=[],stats=[],mapping=[],tasks=[],features={},count=0)
        group=groups[grade]
        group_temp=temp/(grade+'.partial')
        group_temp.mkdir(exist_ok=True)
        i=len(group['episodes'])
        tasks=list(group['tasks'])
        before=market.signature(source)
        review_revision=row['revision']
        if before != report.get('signature') or review_revision != revision(source):
            raise ValueError('转换前源数据或人工评级发生变化，请重新质检')
        source_changed=False
        try:
            d=load(source);phases=[p for p in report['phases'] if not p.get('missing')]
            actual,issues=market.stages(d['meta'],d['n'],d['transitions'])
            if issues:raise ValueError('阶段边界不一致：'+'；'.join(issues))
            fields=('index','phase','start','end','prompt')
            if [[p[k] for k in fields] for p in phases]!=[[p[k] for k in fields] for p in actual]:
                raise ValueError('质检阶段与源标注不一致')
            prompt=' '.join(p['prompt'] for p in phases)
            provenance=dict(source=str(source),source_frame_indices=list(range(d['n'])),
                            stages=phases,output_prompt=prompt,source_uuid=row['uuid'],source_id=row['id'],
                            source_remote=row['remote'],qc_version=market.VERSION,qc_grade=row['auto_grade'],
                            manual_grade=row['manual_grade'],phase_reviews=row['phases'],source_timestamps_seconds=d['t'].tolist())
            entry=dict(root=str(source),grade=row['effective_grade'],task=prompt,skip_quality_checks=True,provenance=provenance)
            converted={}
            for cam in CAMS:
                check_stop()
                src=video_path(source,cam)
                if can_copy_video(src):converted[cam]=str(src)
                else:
                    dst=group_temp/'encoding'/str(i)/(cam+'.mp4')
                    encode_full_video(src, dst, d['n']);converted[cam]=str(dst)
            entry['videos']=converted
            ep,st,m,feats=write_episode((i,entry,group_temp,group['count'],0))
            table=pq.read_table(path_for(group_temp,i));n=len(table)
            tids=np.full(n,-1,dtype=np.int64);pids=np.full(n,-1,dtype=np.int64)
            for p in phases:
                task=p['prompt'] or p['label']
                if task not in tasks:tasks.append(task)
                tids[p['start']:p['end']]=tasks.index(task);pids[p['start']:p['end']]=p['index']
            if np.any(tids<0) or np.any(pids<0):raise ValueError('逐帧任务/阶段未完整覆盖')
            table=table.set_column(table.schema.get_field_index('task_index'),'task_index',pa.array(tids))
            table=table.append_column('phase_index',pa.array(pids))
            valid=d.get('action_valid')
            if valid is None:valid=np.isfinite(d['action'])
            table=table.append_column('action_valid',pa.array(valid.tolist(),type=pa.list_(pa.bool_(),23)))
            feats['phase_index']=dict(dtype='int64',shape=[1],names=None)
            feats['action_valid']=dict(dtype='bool',shape=[23],names=feats['action']['names'])
            st['stats']['task_index']=stats(tids[:,None]);st['stats']['phase_index']=stats(pids[:,None]);st['stats']['action_valid']=stats(valid)
            if group['features'] and group['features']!=feats:raise ValueError('同一等级的相机分辨率或字段不一致，不能合并到同一个数据集')
            # Re-read source identity after media decoding/copying.
            if before != market.signature(source) or review_revision != revision(source):
                source_changed=True
                raise ValueError('转换期间源数据或人工评级发生变化')
            pq.write_table(table,path_for(group_temp,i),compression='zstd')
            ep['tasks']=list(dict.fromkeys(p['prompt'] or p['label'] for p in phases))
            m['source_fields_archive'] = write_source_fields(
                source / 'episode.hdf5', group_temp, i, camera_mode=camera_mode)
            m.update(source_uuid=row['uuid'],source_relative_path=row['id'],source_remote=row['remote'],phase_names=[p['phase'] for p in phases])
            group['features']=feats;group['episodes'].append(ep);group['stats'].append(st)
            group['mapping'].append(m);group['count']+=n;group['tasks']=tasks
            source_snapshots.append((source,before,review_revision))
        except Cancelled:raise
        except Exception as exc:
            path_for(group_temp,i).unlink(missing_ok=True)
            archive_path(group_temp, i).unlink(missing_ok=True)
            for cam in CAMS:path_for(group_temp,i,'observation.images.'+cam).unlink(missing_ok=True)
            if source_changed or before != market.signature(source) or review_revision != revision(source):
                raise ValueError('转换期间源数据或人工评级发生变化，未发布') from exc
            skipped.append(dict(id=row['id'],reason=str(exc)))
        done(index+1)
        selection()
    if not any(g['episodes'] for g in groups.values()):raise ValueError('没有可导出的记录；请先完成质检并选择可用等级。排除原因见 '+str(temp/'selection.json'))
    check_stop()
    assert_sources_unchanged()
    datasets=[]
    for grade in market.GRADES:
        if grade not in groups:continue
        group=groups[grade];group_temp=temp/(grade+'.partial')
        if not group['episodes']:
            shutil.rmtree(group_temp)
            continue
        check_stop();progress(f"校验 {grade} 等级 · {len(group['episodes'])} 条")
        shutil.rmtree(group_temp/'encoding',ignore_errors=True)
        publish_dataset(group_temp,temp/grade,group['episodes'],group['stats'],group['mapping'],
                        group['tasks'],group['features'],group['count'],quality_check=False)
        validation=validate_dataset(temp/grade,grade,check_stop)
        write_json(temp/grade/'meta/conversion.json',dict(quality_check='completed',
            quality_check_scope='market_recorded_phases',source_quality_check='completed',
            structure_check='completed',qc_version=market.VERSION,grade=grade,
            legacy_grasp_quality_check='not_applicable'))
        write_json(temp/grade/'market_qc_report.json',dict(version=market.VERSION,grade=grade,
            source_root=str(root),
            phase_names=list(dict.fromkeys(p['phase'] for m in group['mapping'] for p in m['stages'])),
            phase_sequences=[list(seq) for seq in dict.fromkeys(tuple(p['phase'] for p in m['stages']) for m in group['mapping'])],
            validation=validation))
        datasets.append(dict(grade=grade,path=str(out/grade),episodes=len(group['episodes']),
                             frames=group['count'],structure_check='completed'))
    write_json(temp/'market_qc_report.json',dict(version=market.VERSION,source_root=str(root),
        layout=market.EXPORT_LAYOUT,datasets=datasets,skipped=skipped,not_scanned=job.get('not_scanned',[])))
    assert_sources_unchanged();check_stop()
    os.rename(temp,out)
    return dict(path=str(out),layout=market.EXPORT_LAYOUT,datasets=datasets,
                episodes=sum(d['episodes'] for d in datasets),frames=sum(d['frames'] for d in datasets),
                passed=None,quality_check='completed',quality_check_scope='market_recorded_phases',
                structure_check='completed',skipped=skipped,not_scanned=job.get('not_scanned',[]))


if __name__=='__main__':
    if len(sys.argv)!=2 or not __import__('re').fullmatch(r'\d{8}_\d{6}_[0-9a-f]{8}',sys.argv[1]):raise SystemExit('invalid job id')
    run(sys.argv[1])
