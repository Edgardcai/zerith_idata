"""Display exact per-frame stage prompts without changing the whole-task label."""
from pathlib import Path

import pyarrow.parquet as pq


def attach_stage_annotations(payload, dataset):
    """Decode the selected physical dataset's catalog, checking metadata agreement."""
    import json

    dataset = Path(dataset)
    episode = payload['episode']
    path = (dataset / episode['data_file']).resolve()
    path.relative_to(dataset.resolve())
    available = set(pq.read_schema(path).names)
    keys = ['stage_index', 'stage_task_index', 'stage_start', 'stage_end', 'source_frame_index']
    columns = pq.read_table(path, columns=[k for k in keys if k in available]).to_pydict() if available.intersection(keys) else {}
    catalog = {}
    catalog_path = dataset / 'meta/stage_tasks.jsonl'
    if catalog_path.is_file():
        for line in catalog_path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            index = int(row['stage_task_index'])
            if index in catalog:
                raise ValueError(f'{catalog_path}: stage_task_index={index} 重复')
            catalog[index] = str(row['task'])
    if 'stage_task_index' in columns and not catalog:
        raise ValueError(f'{dataset}: 缺少阶段提示词表 meta/stage_tasks.jsonl')
    segments = episode.get('subtask_segments') or []
    decoded = []
    for position, frame in enumerate(payload['frames']):
        number = int(frame['frame_index'])
        match = next(((i, s) for i, s in enumerate(segments)
                      if int(s['start']) <= number <= int(s['end'])), None)
        stage = int(columns['stage_index'][position]) if 'stage_index' in columns else (
            int(match[1].get('stage_index', match[0])) if match else None)
        if stage is not None and stage < 0:
            raise ValueError(f'Episode {episode["episode_index"]} frame {number}: stage_index 无效')
        if match and stage != int(match[1].get('stage_index', match[0])):
            raise ValueError(f'Episode {episode["episode_index"]} frame {number}: Parquet 与元数据的阶段编号不一致')
        prompt = frame.get('subtask') or ''
        if 'stage_task_index' in columns:
            task_index = int(columns['stage_task_index'][position])
            if task_index not in catalog:
                raise ValueError(f'Episode {episode["episode_index"]}: 未找到阶段提示词 {task_index}')
            if prompt and prompt != catalog[task_index]:
                raise ValueError(f'Episode {episode["episode_index"]} frame {number}: 阶段提示词表与元数据不一致')
            prompt = catalog[task_index]
            frame['stage_task_index'] = task_index
        frame.update(stage_index=stage, stage_number=stage + 1 if stage is not None else None,
                     subtask=prompt, stage_prompt=prompt)
        if 'source_frame_index' in columns:
            frame['source_frame_index'] = int(columns['source_frame_index'][position])
        decoded.append((stage, prompt))
    # Reconstruct display intervals from actual frames; all ends here are inclusive.
    actual_segments = []
    for position, (stage, prompt) in enumerate(decoded):
        if stage is None:
            continue
        frame_number = int(payload['frames'][position]['frame_index'])
        if not actual_segments or position == 0 or decoded[position - 1] != (stage, prompt):
            actual_segments.append(dict(start=frame_number, end=frame_number, stage_index=stage,
                                        stage_number=stage + 1, subtask=prompt))
        else:
            actual_segments[-1]['end'] = frame_number
    if segments and 'stage_index' in columns:
        if [(int(s['start']), int(s['end'])) for s in segments] != [(s['start'], s['end']) for s in actual_segments]:
            raise ValueError(f'Episode {episode["episode_index"]}: Parquet 与元数据的阶段边界不一致')
    starts = {s['start'] for s in actual_segments}
    ends = {s['end'] for s in actual_segments}
    for position, frame in enumerate(payload['frames']):
        number = int(frame['frame_index'])
        for name, expected in [('stage_start', number in starts), ('stage_end', number in ends)]:
            if name in columns and bool(columns[name][position]) != expected:
                raise ValueError(f'Episode {episode["episode_index"]} frame {number}: {name} 与阶段边界不一致')
            frame[name] = expected
        frame['stage_count'] = len(actual_segments)
    episode.update(subtask_segments=actual_segments, stage_count=len(actual_segments))
    payload['stage_annotation'] = dict(available=bool(actual_segments), stage_count=len(actual_segments),
                                       source='parquet' if columns else 'episode_metadata')
    return payload


def integrate_stages(app):
    from .legacy_ui import replace

    original_summary = app.build_lerobot_replay_summary
    original_payload = app.load_lerobot_episode_payload

    def summary(cfg):
        from dataqc.io import read_json
        result = original_summary(cfg)
        root = Path(result['dataset_dir'])
        mapping = read_json(root / 'meta/episode_name_mapping.json')
        maps = {int(m.get('lerobot_episode_index', m.get('episode_index'))): m
                for m in mapping.get('episodes', [])
                if m.get('lerobot_episode_index', m.get('episode_index')) is not None}
        metadata = {int(m['episode_index']): m for m in app.load_jsonl(root / 'meta/episodes.jsonl')}
        for episode in result['episodes']:
            index = episode['episode_index']
            m = maps.get(index, {})
            # Existing local segments take priority; mapping stages use half-open ends.
            if not metadata.get(index, {}).get('subtask_segments') and m.get('stages'):
                exact = []
                for i, stage in enumerate(m['stages']):
                    prompt = stage.get('prompt') or stage.get('subtask')
                    if not prompt:
                        break
                    start, end = int(stage['start']), int(stage['end'])
                    if not m.get('stage_annotation_source'):
                        frames = m.get('source_frames')
                        if frames is not None:
                            selected = [j for j, source in enumerate(frames) if start <= source < end]
                            if not selected:
                                continue
                            start, end = selected[0], selected[-1] + 1
                        else:
                            offset = int((m.get('source_range') or [0])[0])
                            start, end = max(0, start-offset), min(episode['length'], end-offset)
                    exact.append(dict(start=start, end=end-1, stage_index=i, stage_number=i+1, subtask=prompt))
                else:
                    episode['subtask_segments'] = exact
            episode['stage_count'] = len(episode.get('subtask_segments') or [])
            episode['source_episode_uuid'] = m.get('source_episode_uuid', '')
        return result

    def payload(cfg, episode_index):
        result = original_payload(cfg, episode_index)
        return attach_stage_annotations(result, result['dataset']['dataset_dir'])

    app.build_lerobot_replay_summary = summary
    app.load_lerobot_episode_payload = payload
    html = app.LEROBOT_REPLAY_HTML
    html = replace(html, '<span class="meta-label">task</span>', '<span class="meta-label">任务提示词</span>')
    html = replace(html, '<div class="meta-line"><span class="meta-label">subtask</span><span class="meta-value" id="subtaskText"></span></div>',
                   '<div class="meta-line"><span class="meta-label">子阶段提示词</span><div class="meta-value current-subtask"><span id="stageText" role="status" aria-live="polite"></span><span id="subtaskText"></span></div></div>')
    html = replace(html, '</style>', '''
    .meta-strip .meta-line{grid-template-columns:76px minmax(0,1fr)}
    .current-subtask{display:flex;flex-direction:column;gap:4px;white-space:normal}
    #stageText{color:#91bfff;font-size:11px;white-space:normal}
    #subtaskText{font-family:inherit}
  </style>''')
    html = replace(html, '    function updateInfo() {', '''    function renderSubtaskStage(frameIndex) {
      const frame = data?.frames?.[frameIndex];
      const segments = data?.episode?.subtask_segments || [];
      const index = segments.findIndex(s => Number(s.start) <= frameIndex && frameIndex <= Number(s.end));
      const segment = index >= 0 ? segments[index] : null;
      const prompt = frame?.stage_prompt || frame?.subtask || segment?.subtask || "";
      const label = segment ? `第 ${index + 1} / ${segments.length} 阶段 · 帧 ${segment.start}–${segment.end}` : "未标注子阶段";
      if ($("stageText").textContent !== label) $("stageText").textContent = label;
      $("subtaskText").textContent = prompt || "未提供子阶段提示词";
      $("subtaskText").title = prompt;
    }

    function updateInfo() {''')
    html = replace(html, '      renderJointTable();\n    }\n\n    const ALOHA_JOINT_LABELS',
                   '      renderSubtaskStage(currentFrame);\n      renderJointTable();\n    }\n\n    const ALOHA_JOINT_LABELS')
    app.LEROBOT_REPLAY_HTML = html
