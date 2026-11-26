"""Check frame boundaries, catalog scope, and conflicting stage annotations."""
import json

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from integrations.replay_stages import attach_stage_annotations


def fixture_dataset(root, metadata=True, columns=True, prefix='stage'):
    root.mkdir()
    (root/'meta').mkdir()
    relative='data/chunk-000/episode_000000.parquet'
    path=root/relative
    path.parent.mkdir(parents=True)
    prompts=[f'{prefix} {i+1} exact prompt.' for i in range(4)]
    ids=[7,2,19,4]  # Catalog indices are not stage indices and need not be contiguous.
    stages=np.repeat(np.arange(4,dtype=np.int64),3)
    data=dict(frame_index=np.arange(12))
    if columns:
        data.update(stage_index=stages,stage_task_index=np.array(ids)[stages],
                    stage_start=np.arange(12)%3==0,stage_end=np.arange(12)%3==2,
                    source_frame_index=np.arange(100,112))
        (root/'meta/stage_tasks.jsonl').write_text(''.join(json.dumps(dict(stage_task_index=i,task=p))+'\n'
                                                       for i,p in zip(ids,prompts)))
    pq.write_table(pa.table(data),path)
    segments=[dict(start=i*3,end=i*3+2,stage_index=i,subtask=p) for i,p in enumerate(prompts)]
    payload=dict(episode=dict(episode_index=0,data_file=relative,subtask_segments=segments if metadata else []),
                 frames=[dict(frame_index=i,task='Full task must stay intact.',
                              subtask=prompts[i//3] if metadata else '') for i in range(12)])
    return payload,prompts


@pytest.mark.parametrize('metadata',[True,False])
def test_catalog_prompts_and_exact_boundary_frames(tmp_path,metadata):
    root=tmp_path/'dataset'
    payload,prompts=fixture_dataset(root,metadata=metadata)
    result=attach_stage_annotations(payload,root)
    assert result['episode']['stage_count']==4
    assert [s['end'] for s in result['episode']['subtask_segments']]==[2,5,8,11]
    for i,frame in enumerate(result['frames']):
        assert frame['stage_index']==i//3
        assert frame['stage_number']==i//3+1
        assert frame['stage_prompt']==prompts[i//3]
        assert frame['stage_start']==(i%3==0)
        assert frame['stage_end']==(i%3==2)
        assert frame['source_frame_index']==i+100
        assert frame['task']=='Full task must stay intact.'


def test_stage_catalog_is_scoped_to_physical_dataset(tmp_path):
    first,_=fixture_dataset(tmp_path/'A',metadata=False,prefix='first dataset')
    second,prompts=fixture_dataset(tmp_path/'B',metadata=False,prefix='second dataset')
    attach_stage_annotations(first,tmp_path/'A')
    result=attach_stage_annotations(second,tmp_path/'B')
    assert result['frames'][6]['stage_prompt']==prompts[2]


def test_legacy_metadata_without_parquet_stage_columns(tmp_path):
    payload,prompts=fixture_dataset(tmp_path/'legacy',columns=False)
    result=attach_stage_annotations(payload,tmp_path/'legacy')
    assert result['stage_annotation']['source']=='episode_metadata'
    assert [f['subtask'] for f in result['frames']]==[p for p in prompts for _ in range(3)]
    assert result['frames'][3]['stage_number']==2


@pytest.mark.parametrize('problem',['prompt','boundary','flag','missing_catalog_entry'])
def test_conflicting_annotation_is_rejected(tmp_path,problem):
    root=tmp_path/'dataset'
    payload,_=fixture_dataset(root)
    if problem=='prompt':
        payload['frames'][3]['subtask']='An incorrect prompt.'
    elif problem=='boundary':
        payload['episode']['subtask_segments'][0]['end']=3
    elif problem=='flag':
        path=root/payload['episode']['data_file']
        table=pq.read_table(path).to_pydict();table['stage_end'][2]=False
        pq.write_table(pa.table(table),path)
    else:
        (root/'meta/stage_tasks.jsonl').write_text(json.dumps(dict(stage_task_index=7,task='stage 1 exact prompt.'))+'\n')
    with pytest.raises(ValueError):
        attach_stage_annotations(payload,root)


def test_unannotated_dataset_has_explicit_empty_stage_state(tmp_path):
    root=tmp_path/'unannotated'
    payload,_=fixture_dataset(root,metadata=False,columns=False)
    result=attach_stage_annotations(payload,root)
    assert result['stage_annotation']['available'] is False
    assert result['episode']['stage_count']==0
    assert all(f['stage_number'] is None and not f['stage_prompt'] for f in result['frames'])
