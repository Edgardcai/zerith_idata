"""Round-trip checks for recorded-phase LeRobot outputs, without grasp-only rules."""
from pathlib import Path

import cv2
import numpy as np
import pyarrow.parquet as pq

from . import market
from .export import path_for, rows
from .io import CAMS, read_json
from .market_source_fields import archive_path, validate_source_fields


def validate_dataset(root, grade, check_stop=lambda: None):
    root = Path(root)
    info = read_json(root / 'meta/info.json')
    episodes = rows(root / 'meta/episodes.jsonl')
    tasks = rows(root / 'meta/tasks.jsonl')
    mappings = read_json(root / 'meta/episode_name_mapping.json')['episodes']
    statistics = rows(root / 'meta/episodes_stats.jsonl')

    def require(condition, message):
        if not condition:
            raise ValueError(f'{grade} 等级 LeRobot 校验失败：{message}')

    require(info.get('codebase_version') == 'v2.1' and info.get('fps') == 30, '格式版本或帧率无效')
    require(bool(episodes) and len(episodes) == len(mappings) == len(statistics) == info['total_episodes'], 'episode 元数据数量不一致')
    require(info['total_tasks'] == len(tasks) and [t['task_index'] for t in tasks] == list(range(len(tasks))), '任务编号不连续')
    require(info['total_videos'] == len(episodes) * len(CAMS), '视频数量不一致')
    count = 0
    for i, (episode, mapping, stat) in enumerate(zip(episodes, mappings, statistics)):
        check_stop()
        require(episode['episode_index'] == mapping['episode_index'] == mapping['lerobot_episode_index'] == stat['episode_index'] == i, 'episode 编号不连续')
        expected_grade = mapping.get('grade') if grade is None else grade
        require(expected_grade in market.GRADES and episode.get('quality_grade') == mapping.get('grade') == expected_grade, '不同等级混入同一数据集')
        table = pq.read_table(path_for(root, i))
        n = table.num_rows
        if relative := mapping.get('source_fields_archive'):
            archive = archive_path(root, i)
            require(relative == str(archive.relative_to(root)), '原始字段存档路径错误')
            source_fields = validate_source_fields(archive, root, i)
            for field in source_fields['fields'].values():
                if field['storage'] == 'video':
                    require(field['shape'] == [n], '原始相机字段与逐帧视频不匹配')
        require(n > 0 and n == episode['length'] == mapping['num_frames'], '帧数不一致')
        require(set(table.column_names) == {k for k, v in info['features'].items() if v['dtype'] != 'video'}, 'Parquet 字段与 info.json 不一致')
        for key, expected in [('frame_index', np.arange(n)), ('episode_index', np.full(n, i)), ('index', np.arange(count, count + n))]:
            require(np.array_equal(table[key].to_numpy(), expected), key + ' 不连续')
        require(np.allclose(table['timestamp'].to_numpy(), np.arange(n) / info['fps'], atol=1e-5, rtol=1e-5), '时间索引不一致')
        state = np.asarray(table['observation.state'].to_pylist())
        action = np.asarray(table['action'].to_pylist())
        valid = np.asarray(table['action_valid'].to_pylist())
        require(state.shape == action.shape == valid.shape == (n, 23), 'State / Action / 有效位应为 23 维')
        require(np.isfinite(state).all() and np.isfinite(action[valid]).all(), '有效观测或动作中包含非有限值')
        tids = table['task_index'].to_numpy()
        pids = table['phase_index'].to_numpy()
        require(np.all((tids >= 0) & (tids < len(tasks))), '任务索引越界')
        require(episode['tasks'] == [tasks[int(t)]['task'] for t in dict.fromkeys(tids)], '任务文本映射不一致')
        stages = mapping['stages']
        require(bool(stages), '没有实际阶段标注')
        require(all(isinstance(p.get('phase'),str) and p['phase'].strip() and not p.get('missing') for p in stages), '阶段名称无效或包含虚构的缺失阶段')
        require([p['index'] for p in stages] == list(range(len(stages))), '阶段编号不连续')
        if 'phase_names' in mapping:
            require(mapping['phase_names'] == [p['phase'] for p in stages], '阶段名称映射不一致')
        cursor = 0
        for stage in stages:
            start, end = stage['start'], stage['end']
            require(start == cursor and start < end <= n, '阶段帧覆盖不连续')
            require(np.all(pids[start:end] == stage['index']), '逐帧阶段索引不一致')
            expected_task = stage['prompt'] or stage['label']
            require(all(tasks[int(t)]['task'] == expected_task for t in np.unique(tids[start:end])), '阶段任务文本不一致')
            cursor = end
        require(cursor == n, '阶段没有覆盖全部数据帧')
        require(all(s['count'] == [n] for s in stat['stats'].values()), '逐 episode 统计帧数不一致')
        for cam in CAMS:
            check_stop()
            key = 'observation.images.' + cam
            capture = cv2.VideoCapture(str(path_for(root, i, key)))
            try:
                require(capture.isOpened(), cam + ' 无法打开')
                require(round(capture.get(cv2.CAP_PROP_FRAME_COUNT)) == n, cam + ' 帧数不一致')
                require(abs(capture.get(cv2.CAP_PROP_FPS) - info['fps']) < 0.1, cam + ' 帧率不一致')
                shape = [round(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)), round(capture.get(cv2.CAP_PROP_FRAME_WIDTH)), 3]
                require(info['features'][key]['shape'] == shape, cam + ' 分辨率不一致')
            finally:
                capture.release()
        count += n
    require(count == info['total_frames'], '总帧数不一致')
    return dict(passed=True, episodes=len(episodes), frames=count,
                scope='分级、元数据、连续索引、实际阶段任务/帧覆盖、有效动作、三路视频结构')
