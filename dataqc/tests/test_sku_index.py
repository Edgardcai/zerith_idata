import json
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from dataqc.sku_index import PRODUCTS, Searches, dataset_episodes, matches, scan


def dataset(root, prompts, *, metadata=True):
    (root/'meta').mkdir(parents=True)
    (root/'data/chunk-000').mkdir(parents=True)
    (root/'meta/info.json').write_text(json.dumps({'chunks_size':1000}))
    (root/'meta/tasks.jsonl').write_text(''.join(json.dumps(dict(task_index=i,task=p))+'\n' for i,p in enumerate(prompts)))
    (root/'meta/episodes.jsonl').write_text(''.join(json.dumps(dict(episode_index=i,**({'tasks':[p]} if metadata else {})))+'\n' for i,p in enumerate(prompts)))
    for i in range(len(prompts)):
        pq.write_table(pa.table({'task_index':[i,i]}),root/f'data/chunk-000/episode_{i:06d}.parquet')
    return root


@pytest.mark.parametrize('sku',PRODUCTS)
def test_all_products_both_and_single_hands(sku):
    assert matches(f'Grasp {sku} with the left hand.',sku)
    assert matches(f'Grasp {sku} with the right hand',sku)
    assert matches(f'Grasp {sku} with the left hand and then grasp {sku} with the right hand.',sku)
    assert matches(f'grasp  {sku.upper()}  with the left hand.',sku)
    assert matches(sku,sku)


def test_complete_product_not_substring():
    assert not matches('Grasp Pepsi Real Sugar with the left hand.','Pepsi')
    assert not matches('Grasp Bottled Coca-Cola with the right hand.','Coca-Cola')
    assert not matches('Grasp Fanta Grape with the left hand.','Fanta')
    assert not matches('unknown Pepsi instruction','Pepsi')


def test_cross_directory_identity_fallback_no_duplicates_and_no_writes(tmp_path):
    p='Grasp Pepsi with the left hand and then grasp Pepsi with the right hand.'
    a=dataset(tmp_path/'scene2/A',[p,'Grasp Pepsi Real Sugar with the left hand.'])
    b=dataset(tmp_path/'scene10/B',[p,'Grasp Coca-Cola with the right hand.'],metadata=False)
    dataset(tmp_path/'.backup',[p])
    dataset(tmp_path/'scene.previous',[p])
    before={str(f):f.read_bytes() for f in tmp_path.rglob('*') if f.is_file()}
    result=scan(tmp_path,'Pepsi')
    assert [(e['dataset_path'],e['episode_index']) for e in result['episodes']]==[(str(a),0),(str(b),0)]
    assert result['scanned_datasets']==2 and result['scanned_episodes']==4
    assert not result['warnings']
    assert before=={str(f):f.read_bytes() for f in tmp_path.rglob('*') if f.is_file()}
    # A later scan sees new data without changing previous identities.
    c=dataset(tmp_path/'scene11',[p])
    assert len(scan(tmp_path,'Pepsi')['episodes'])==3


def test_bad_dataset_does_not_hide_healthy_and_warns_missing_data(tmp_path):
    p='Grasp Pepsi with the left hand.'
    a=dataset(tmp_path/'bad',[p]);(a/'meta/episodes.jsonl').write_text('not-json')
    b=dataset(tmp_path/'missing',[p]);(b/'data/chunk-000/episode_000000.parquet').unlink()
    dataset(tmp_path/'good',[p])
    result=scan(tmp_path,'Pepsi')
    assert len(result['episodes'])==1 and len(result['warnings'])==2


def test_parquet_unknown_tasks_not_silently_partial_match(tmp_path):
    root=dataset(tmp_path/'ds',['Grasp Pepsi with the left hand.'],metadata=False)
    pq.write_table(pa.table({'task_index':[0,99]}),root/'data/chunk-000/episode_000000.parquet')
    result=scan(tmp_path,'Pepsi')
    assert result['episodes']==[] and '没有对应提示词' in result['warnings'][0]['reason']


def test_unparsed_prompts_and_empty_matches(tmp_path):
    dataset(tmp_path/'ds',['Unrecognized instruction with Pepsi','Grasp Pepsi Real Sugar with the left hand.'])
    result=scan(tmp_path,'Pepsi')
    assert not result['episodes'] and result['unparsed_prompts']==1


def wait_job(jobs,ident):
    for _ in range(100):
        result=jobs.get(ident)
        if result['status']!='running':return result
        time.sleep(.01)
    pytest.fail('search did not finish')


def test_async_validation_and_cancel(tmp_path,monkeypatch):
    import threading
    import dataqc.sku_index as module
    jobs=Searches()
    with pytest.raises(ValueError):jobs.start('','Pepsi')
    with pytest.raises(ValueError):jobs.start(str(tmp_path),'unlisted product')
    entered=threading.Event()
    def slow(root,sku,progress,cancelled):
        entered.set()
        while not cancelled():time.sleep(.001)
        return {}
    monkeypatch.setattr(module,'scan',slow)
    ident=jobs.start(str(tmp_path),'Pepsi')['id'];assert entered.wait(1)
    jobs.get(ident,cancel=True)
    assert wait_job(jobs,ident)['status']=='cancelled'


def test_routes_dropdown_and_original_module(tmp_path):
    import workbench
    dataset(tmp_path/'ds',['Grasp Pepsi with the left hand.','Grasp Pepsi Real Sugar with the left hand.'])
    with TestClient(workbench.app) as client:
        home=client.get('/').text
        assert '④ 按 SKU 可视化' in home and 'src="/sku-visualization/"' in home
        html=client.get('/sku-visualization/').text
        assert 'id="skuSelect"' in html
        for sku in PRODUCTS:assert f'<option value="{sku}">{sku}</option>' in html
        assert 'id="skuSelect"' not in client.get('/lerobot-visualization/').text
        assert client.post('/api/lerobot-visualization/sku/search',json={'root':'','sku':'Pepsi'}).status_code==400
        start=client.post('/api/lerobot-visualization/sku/search',json={'root':str(tmp_path),'sku':'Pepsi'}).json()
        result=wait_job(client.app.state.legacy.sku_searches,start['id'])
        assert result['status']=='completed' and len(result['result']['episodes'])==1
        response=client.get('/api/lerobot-visualization/sku/search',params={'id':start['id']})
        assert response.json()==result
        filtered=client.get('/api/lerobot-visualization/sku/episodes',params={'dataset_path':str(tmp_path/'ds'),'sku':'Pepsi'})
        assert filtered.json()=={'indices':[0]}
        assert client.get('/api/lerobot-visualization/sku/search?id=invalid').status_code==400
