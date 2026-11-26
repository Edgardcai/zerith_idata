"""Split an existing mixed market export using its saved grades, without raw data."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import market
from .export import path_for, publish_dataset, rows, stats
from .io import CAMS, read_json, write_json
from .market_source_fields import relocate_source_fields
from .market_validation import validate_dataset


def fingerprint(root):
    values = []
    for folder in ('data', 'meta', 'videos'):
        for path in sorted((root / folder).rglob('*')):
            if path.is_symlink():
                raise ValueError('已有导出不能包含符号链接')
            if path.is_file():
                st = path.stat()
                values.append((str(path.relative_to(root)), st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns))
    return hashlib.sha256(repr(values).encode()).hexdigest()


def regroup_dataset(source, output, progress=lambda _: None):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output == source or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError('分级输出必须与原数据集分开存放')
    temp = output.with_name(output.name + '.partial')
    if output.exists() or temp.exists():
        raise ValueError('分级输出已存在，拒绝覆盖')
    signature = fingerprint(source)
    validate_dataset(source, None)
    episodes = rows(source / 'meta/episodes.jsonl')
    old_stats = rows(source / 'meta/episodes_stats.jsonl')
    mappings = read_json(source / 'meta/episode_name_mapping.json')['episodes']
    old_tasks = rows(source / 'meta/tasks.jsonl')
    info = read_json(source / 'meta/info.json')
    temp.mkdir(parents=True)
    groups = {}
    for old_index, (episode, mapping, statistics) in enumerate(zip(episodes, mappings, old_stats)):
        grade = mapping['grade']
        progress(f'整理 {old_index + 1}/{len(episodes)} · {grade}')
        group = groups.setdefault(grade, dict(episodes=[], stats=[], mapping=[], tasks=[], count=0))
        root = temp / (grade + '.partial')
        index = len(group['episodes'])
        table = pq.read_table(path_for(source, old_index))
        n = len(table)
        tids = []
        for tid in table['task_index'].to_pylist():
            task = old_tasks[tid]['task']
            if task not in group['tasks']:
                group['tasks'].append(task)
            tids.append(group['tasks'].index(task))
        columns = dict(episode_index=np.full(n, index, dtype=np.int64),
                       index=np.arange(group['count'], group['count'] + n, dtype=np.int64),
                       task_index=np.array(tids, dtype=np.int64))
        new_stats = dict(statistics['stats'])
        for key, values in columns.items():
            table = table.set_column(table.schema.get_field_index(key), key, pa.array(values))
            new_stats[key] = stats(values[:, None])
        destination = path_for(root, index)
        destination.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, destination, compression='zstd')
        for cam in CAMS:
            key = 'observation.images.' + cam
            destination = path_for(root, index, key)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path_for(source, old_index, key), destination)
        group['episodes'].append(dict(episode, episode_index=index))
        group['stats'].append(dict(episode_index=index, stats=new_stats))
        regrouped_mapping = dict(mapping, episode_index=index, lerobot_episode_index=index,
                                 source_lerobot=str(source), source_lerobot_episode_index=old_index)
        if mapping.get('source_fields_archive'):
            regrouped_mapping['source_fields_archive'] = relocate_source_fields(source, old_index, root, index)
        group['mapping'].append(regrouped_mapping)
        group['count'] += n
    datasets = []
    for grade in market.GRADES:
        if grade not in groups:
            continue
        group = groups[grade]
        publish_dataset(temp / (grade + '.partial'), temp / grade, group['episodes'], group['stats'],
                        group['mapping'], group['tasks'], info['features'], group['count'], quality_check=False)
        validation = validate_dataset(temp / grade, grade)
        write_json(temp / grade / 'meta/conversion.json', dict(quality_check='not_run', structure_check='completed',
            migration='split_existing_export_by_saved_grade', source_lerobot=str(source),
            note='沿用原导出评级；未重新质检源录制。原始录制不存在时仍可按已保存评级整理。'))
        write_json(temp / grade / 'market_qc_report.json', dict(validation=validation, source_lerobot=str(source), source_qc_rerun=False))
        datasets.append(dict(grade=grade, path=str(output / grade), episodes=len(group['episodes']),
                             frames=group['count'], structure_check='completed'))
    if fingerprint(source) != signature:
        raise ValueError('整理期间原导出发生变化，未发布')
    result = dict(path=str(output), layout=market.EXPORT_LAYOUT, datasets=datasets,
        episodes=len(episodes), frames=info['total_frames'], skipped=[], quality_check='not_run',
        structure_check='completed', source_lerobot=str(source), source_qc_rerun=False)
    write_json(temp / 'market_qc_report.json', result)
    os.rename(temp, output)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    print(__import__('json').dumps(regroup_dataset(args.source, args.output, lambda text: print(text, flush=True)), ensure_ascii=False))
