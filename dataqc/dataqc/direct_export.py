"""Unchecked, per-episode conversion and portable LeRobot stage extraction."""
from pathlib import Path
import shutil
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from .io import CAMS, load, read_json, write_json, parse_task, normalized_task
from .parallel import ordered_map


def episode_label(entry):
    return str(entry.get('episode_id') or entry.get('entry', {}).get('episode_id') or
               entry.get('source_episode_index', Path(entry.get('root', '')).name))


def export_one(payload):
    index, entry, directory = payload
    from .export import create_dataset
    try:
        result = create_dataset([entry], directory, quality_check=False)
        return dict(index=index, result=result)
    except Exception as exc:
        return dict(index=index, error=str(exc), episode=episode_label(entry))


def create_direct_dataset(entries, out, threshold=40, progress=lambda _:None):
    from .export import path_for, rows, jsonl, stats, publish_dataset
    out=Path(out)
    if out.exists(): raise ValueError(f'输出已存在：{out}')
    temp=out.with_name(out.name+'.partial')
    if temp.exists(): shutil.rmtree(temp)
    temp.mkdir(parents=True)
    jobs=[(i,e,temp/'parts'/str(i)) for i,e in enumerate(entries)]
    results=ordered_map(export_one,jobs,progress,'逐条处理（不质检）')
    episodes=[];statistics=[];mapping=[];tasks=[];features={};count=0;included=[];skipped=[]
    for value in results:
        index=value['index'];entry=entries[index]
        if 'error' in value:
            skipped.append(value);progress(f"未完成 {value['episode']}：{value['error']}");continue
        part=Path(value['result']['path']);info=read_json(part/'meta/info.json')
        if features and features!=info['features']:
            issue=dict(index=index,episode=episode_label(entry),error='字段或视频分辨率与当前输出数据集不一致')
            skipped.append(issue);progress(f"未完成 {issue['episode']}：{issue['error']}");continue
        features=info['features']
        ep=rows(part/'meta/episodes.jsonl')[0];st=rows(part/'meta/episodes_stats.jsonl')[0]
        m=read_json(part/'meta/episode_name_mapping.json')['episodes'][0]
        task=ep['tasks'][0]
        if task not in tasks: tasks.append(task)
        tid=tasks.index(task);i=len(episodes);n=ep['length']
        table=pq.read_table(path_for(part,0))
        for key,values in [('episode_index',np.full(n,i)),('index',np.arange(count,count+n)),('task_index',np.full(n,tid))]:
            table=table.set_column(table.schema.get_field_index(key),key,pa.array(values,type=pa.int64()))
            st['stats'][key]=stats(values[:,None])
        dest=path_for(temp,i);dest.parent.mkdir(parents=True,exist_ok=True);pq.write_table(table,dest,compression='zstd')
        for cam in CAMS:
            key='observation.images.'+cam;dest=path_for(temp,i,key);dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.move(path_for(part,0,key),dest)
        ep['episode_index']=st['episode_index']=m['episode_index']=m['lerobot_episode_index']=i
        episodes.append(ep);statistics.append(st);mapping.append(m);count+=n;included.append(index)
    batch=dict(status='partial' if skipped and included else 'failed' if not included else 'completed',
               attempted=len(entries),completed=len(included),failed=len(skipped),skipped=skipped,quality_check='not_run')
    write_json(temp/'batch_report.json',batch)
    if not included:
        report_path=out.with_name(out.name+'.batch-report.json');write_json(report_path,batch)
        shutil.rmtree(temp)
        return dict(path=None,episodes=0,frames=0,passed=None,quality_check='not_run',included_indices=[],
                    skipped=skipped,batch_report=str(report_path),status='failed')
    shutil.rmtree(temp/'parts',ignore_errors=True)
    result=publish_dataset(temp,out,episodes,statistics,mapping,tasks,features,count,threshold,progress,False)
    return dict(result,included_indices=included,skipped=skipped,status=batch['status'],batch_report=str(out/'batch_report.json'))


def dataset_file(root,info,index,key=None):
    from .export import DATA,VIDEO
    template=info.get('video_path',VIDEO) if key else info.get('data_path',DATA)
    path=(Path(root)/template.format(episode_chunk=index//int(info.get('chunks_size',1000)),episode_index=index,video_key=key)).resolve()
    path.relative_to(Path(root).resolve())
    return path


def split_records(full):
    from .export import rows
    full=Path(full);info=read_json(full/'meta/info.json')
    records=read_json(full/'meta/episode_name_mapping.json').get('episodes',[])
    by_index={r.get('episode_index',r.get('lerobot_episode_index')):r for r in records if isinstance(r,dict)}
    episodes=rows(full/'meta/episodes.jsonl')
    return info,[dict(by_index.get(e['episode_index'],{}),episode_index=e['episode_index'],
                      task=by_index.get(e['episode_index'],{}).get('task') or (e.get('tasks') or [''])[0],
                      grade=by_index.get(e['episode_index'],{}).get('grade') or e.get('quality_grade') or 'UNRATED') for e in episodes]


def resolve_stages(full,record,n):
    stages=record.get('stages') or record.get('provenance',{}).get('stages')
    if not stages:
        # Older exports may only retain the HDF5 reference. No QC or completion attributes are consulted.
        roots=[]
        for key in ('source_root','hdf5_episode_dir','source_episode','source_hdf5','hdf5_file','source_h5'):
            if record.get(key):
                p=Path(record[key]);p=p if p.is_absolute() else Path(full)/p
                roots.append(p.parent if p.suffix in ('.h5','.hdf5') else p)
        for root in roots:
            try: d=load(root)
            except (OSError,ValueError,KeyError): continue
            ends=d.get('transitions',[])
            targets=parse_task(normalized_task(record.get('task') or d['task'])) or {}
            hands=list(targets) if len(targets)==len(ends) else ['left','right'] if len(ends)==2 else []
            if hands:
                stages=[dict(hand=h,start=b,end=e,item=targets.get(h,'')) for h,b,e in zip(hands,[0]+ends[:-1],ends)]
                break
    if not stages: raise ValueError('缺少阶段标注；旧 HDF5 中也没有可用边界')
    for st in stages:
        b,e=st.get('start'),st.get('end')
        if st.get('hand') not in ('left','right') or type(b) is not int or type(e) is not int or not 0<=b<e<=n:
            raise ValueError(f'阶段边界或手别无效：{st}')
    return stages


def split_direct(full,out,threshold=40,progress=lambda _:None):
    full=Path(full);out=Path(out);info,records=split_records(full)
    entries={'left':[],'right':[]};skipped=[]
    for m in records:
        i=m['episode_index'];label=m.get('hdf5_episode_name') or m.get('source_episode_name') or str(i)
        try:
            parquet=dataset_file(full,info,i);n=pq.read_metadata(parquet).num_rows
            stages=resolve_stages(full,m,n)
            frames=m.get('source_frames') or list(range(n))
            if len(frames)!=n: raise ValueError('来源帧映射长度与 LeRobot 不一致')
            provenance=dict(source=m.get('source_original') or str(full),source_frame_indices=frames,
                            stages=stages,output_prompt=m['task'],
                            height_reference=m.get('provenance',{}).get('height_reference',{}))
            for st in stages:
                task=f"Grasp {st['item']} with the {st['hand']} hand" if st.get('item') else m['task']
                entries[st['hand']].append(dict(root=m.get('source_root') or str(full),episode_id=label,
                    grade=m['grade'],range=[st['start'],st['end']],task=task,source_lerobot=str(full),
                    source_lerobot_episode_index=i,source_hdf5=m.get('source_hdf5',''),
                    skip_quality_checks=True,provenance=provenance,source_parquet=str(parquet),
                    videos={cam:str(dataset_file(full,info,i,'observation.images.'+cam)) for cam in CAMS}))
        except (OSError,ValueError,KeyError,TypeError) as exc:
            skipped.append(dict(episode=label,error=str(exc)));progress(f'未完成 {label}：{exc}')
    results=[]
    for hand,values in entries.items():
        if not values: continue
        result=create_direct_dataset(values,out/hand,threshold,progress)
        result['hand']=hand;results.append(result)
        skipped.extend(dict(issue,hand=hand) for issue in result['skipped'])
    report=dict(attempted_episodes=len(records),completed_segments=sum(r['episodes'] for r in results),
                skipped=skipped,status='partial' if skipped and any(r['episodes'] for r in results) else 'failed' if not any(r['episodes'] for r in results) else 'completed',quality_check='not_run')
    write_json(out/'batch_report.json',report)
    return results
