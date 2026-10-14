import csv
import io
import json
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dataqc.collection_plans import compare_plan, comparison, load_plans, parse_document
from dataqc import collection_dashboard_api as api

W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'


def document(path, tasks, quantity_label='每手采集条数'):
    path.parent.mkdir(parents=True,exist_ok=True)
    root=ET.Element('{'+W+'}document');body=ET.SubElement(root,'{'+W+'}body')
    def paragraph(parent,value):
        node=ET.SubElement(parent,'{'+W+'}p');run=ET.SubElement(node,'{'+W+'}r');ET.SubElement(run,'{'+W+'}t').text=value
    paragraph(body,'采集计划');paragraph(body,'场景 1 定点 2')
    table=ET.SubElement(body,'{'+W+'}tbl')
    for values in [('左手商品','右手商品',quantity_label),*tasks]:
        row=ET.SubElement(table,'{'+W+'}tr')
        for value in values:
            cell=ET.SubElement(row,'{'+W+'}tc');paragraph(cell,str(value))
    paragraph(body,'场景 8 测评保留');paragraph(body,'本场景不安排采集')
    with zipfile.ZipFile(path,'w') as archive:archive.writestr('word/document.xml',ET.tostring(root))
    return path


def record(left,right,grade='A'):
    return dict(left=left,right=right,grade=grade)


def test_parse_both_quantity_headers_combined_scene_point_and_repeated_products(tmp_path):
    for quantity_label in ('每手采集条数','每手条数'):
        plan=parse_document(document(tmp_path/(quantity_label+'.docx'),[('Apple','Pear',20),('apple','Pear',40)],quantity_label),'batch1')
        assert plan['episode_target']==60 and plan['task_count']==2 and plan['scene_count']==1
        assert plan['scenes']==[dict(scene=1,target=60)]
        assert plan['products']==[dict(name='Apple',left_target=60,right_target=0),dict(name='Pear',left_target=0,right_target=60)]


def test_surplus_cannot_hide_other_hand_shortage_and_same_item_counts_once(tmp_path):
    plan=parse_document(document(tmp_path/'第一批.docx',[('Apple','Pear',2)]),'batch1')
    rows=[record('Apple','Apple') for _ in range(5)]
    result=compare_plan(plan,rows)
    assert result['actual_episodes']==5 and result['episode_target']==2
    assert result['completion']==.5 and result['remaining']==2 and result['completed_products']==1
    apple=next(p for p in result['products'] if p['name']=='Apple')
    assert apple['actual_records']==5 and apple['left_actual']==apple['right_actual']==5
    assert apple['completion']==1 and result['surplus']==8


def test_ab_basis_uses_adopted_grade_and_preserves_plan_denominator(tmp_path):
    plan=parse_document(document(tmp_path/'第一批.docx',[('Apple','Pear',2)]),'batch1')
    rows=[record('Apple','Pear','F'),record('apple','pear','B'),record('Unknown','Else','A')]
    all_rows=compare_plan(plan,rows,'all');ab=compare_plan(plan,rows,'ab')
    assert all_rows['actual_episodes']==2 and all_rows['completion']==1
    assert ab['episode_target']==2 and ab['actual_episodes']==1 and ab['completion']==.5
    assert ab['collected_episodes']==2 and ab['remaining']==2


def test_document_updates_missing_sources_and_duplicate_plan_are_visible(tmp_path):
    first=document(tmp_path/'第一批.docx',[('Apple','Pear',20)])
    shutil.copy2(first,tmp_path/'第二批.docx')
    before=load_plans(tmp_path)
    assert [p['status'] for p in before]==['ready','pending','pending']
    document(tmp_path/'第二批.docx',[('Soda','Candy',40)],'每手条数')
    document(tmp_path/'第三批.docx',[('Towel','Cup',20)])
    after=load_plans(tmp_path)
    assert [p['status'] for p in after]==['ready']*3
    assert [p['episode_target'] for p in after]==[20,40,20]
    assert comparison(tmp_path,[record('Soda','Candy'),record('Other','Else')])['outside_plan']==1


def test_plan_api_csv_and_source_document(tmp_path,monkeypatch):
    document(tmp_path/'第一批.docx',[('Apple','Pear',2)])
    rows=[record('Apple','Pear','F'),record('Apple','Pear','A')]
    index=SimpleNamespace(rows=lambda:rows,refresh=lambda:False,state=lambda:dict(scanning=False,updated_at=0))
    monkeypatch.setattr(api,'inventory',lambda:index);monkeypatch.setattr(api,'plan_root',lambda:tmp_path)
    app=FastAPI();app.include_router(api.router);client=TestClient(app)
    all_result=client.get('/api/collection-dashboard/plans').json()
    assert all_result['plans'][0]['completion']==1
    ab=client.get('/api/collection-dashboard/plans?basis=ab').json()
    assert ab['plans'][0]['completion']==.5
    assert client.get('/api/collection-dashboard/plans?basis=bad').status_code==400
    response=client.get('/api/collection-dashboard/export.csv?kind=plans&batch=batch1&basis=ab')
    exported=list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
    assert len(exported)==2
    assert (exported[0]['左手计划'],exported[0]['左手实际'],exported[0]['左手缺口'])==('2','1','1')
    assert exported[0]['数量口径']=='仅 A/B 等级'
    assert client.get('/api/collection-dashboard/export.csv?kind=plans&batch=unknown').status_code==400
    assert client.get('/api/collection-dashboard/plan-document/batch1').content.startswith(b'PK')
    assert client.get('/api/collection-dashboard/plan-document/unknown').status_code==404
