"""Read the user's Word collection plans and compare per-hand product quotas."""
import collections
from functools import lru_cache
from pathlib import Path
import re
import zipfile
import xml.etree.ElementTree as ET

from .collection_dashboard import normalize

PLAN_FILES = (('batch1', '第一批.docx'), ('batch2', '第二批.docx'), ('batch3', '第三批.docx'))
NS = {'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}


def key(value):
    return normalize(value).casefold()


def parse_document(path, batch_id):
    with zipfile.ZipFile(path) as archive:
        body = ET.fromstring(archive.read('word/document.xml')).find('w:body', NS)
    if body is None:raise ValueError('文档缺少正文')
    paragraphs = [];tasks = [];scene = point = 0
    text = lambda node:normalize(''.join(t.text or '' for t in node.findall('.//w:t', NS)))
    for node in body:
        if node.tag.endswith('}p'):
            value = text(node)
            if value:paragraphs.append(value)
            match = re.match(r'^场景\s*(\d+)(?:\s|[：:｜（(]|$)', value)
            if match:scene = int(match[1])
            match = re.search(r'定点\s*(\d+)', value)
            if match:point = int(match[1])
        elif node.tag.endswith('}tbl'):
            rows = [[text(cell) for cell in row.findall('w:tc', NS)] for row in node.findall('w:tr', NS)]
            if not rows or not {'左手商品', '右手商品'} <= set(rows[0]):continue
            quantity_column = next((name for name in ('每手采集条数', '每手条数') if name in rows[0]), None)
            if not quantity_column:continue
            columns = {name:rows[0].index(name) for name in ('左手商品', '右手商品')}
            columns['每手采集条数'] = rows[0].index(quantity_column)
            for row in rows[1:]:
                if not any(row):continue
                quantity = row[columns['每手采集条数']]
                if not quantity.isdigit() or int(quantity)<=0:raise ValueError('任务采集数量无效：'+quantity)
                left = row[columns['左手商品']];right = row[columns['右手商品']]
                if not left or not right:raise ValueError('任务缺少左右手商品')
                tasks.append(dict(scene=scene, point=point, left=left, right=right, quantity=int(quantity)))
    if not tasks:raise ValueError('未找到包含左右手商品和采集条数的任务表')
    products = {}
    scenes = collections.Counter()
    for task in tasks:
        scenes[task['scene']] += task['quantity']
        for hand in ('left', 'right'):
            product = products.setdefault(key(task[hand]), dict(name=task[hand], left_target=0, right_target=0))
            product[hand+'_target'] += task['quantity']
    return dict(id=batch_id, name=path.stem, filename=path.name, status='ready',
                title=paragraphs[0] if paragraphs else path.stem,
                description='；'.join(paragraphs[1:3]),
                episode_target=sum(task['quantity'] for task in tasks), task_count=len(tasks),
                scene_count=len(scenes), scenes=[dict(scene=s, target=q) for s,q in sorted(scenes.items())],
                product_count=len(products), products=list(products.values()),
                document_url='/api/collection-dashboard/plan-document/'+batch_id)


@lru_cache(maxsize=8)
def _load(root, signatures):
    plans = []
    for batch_id, filename in PLAN_FILES:
        path = Path(root)/filename
        try:
            plan = parse_document(path, batch_id)
            products = {key(p['name']) for p in plan['products']}
            duplicate = next((p for p in plans if p['status']=='ready' and
                              products=={key(v['name']) for v in p['products']}), None)
            if duplicate:
                plan.update(status='pending', error='商品与'+duplicate['name']+'重合，计划文档待更新')
        except (OSError, ValueError, IndexError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
            plan = dict(id=batch_id, name=Path(filename).stem, filename=filename,
                        status='pending', error='计划文档待更新：'+str(exc))
        plans.append(plan)
    return plans


def load_plans(root):
    root = Path(root).resolve();signatures = []
    for _, filename in PLAN_FILES:
        try:
            s = (root/filename).stat()
            signatures.append((filename, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns))
        except FileNotFoundError:signatures.append((filename, None))
    return _load(str(root), tuple(signatures))


def compare_plan(plan, rows, basis='all'):
    if basis not in ('all', 'ab'):raise ValueError('计划对照仅支持全部采集或 A/B 等级')
    if plan['status']!='ready':return {k:v for k,v in plan.items() if k!='products'}
    wanted = {key(p['name']) for p in plan['products']}
    matched = [r for r in rows if any(key(r[h]) in wanted for h in ('left', 'right'))]
    selected = [r for r in matched if basis=='all' or r['grade'] in ('A', 'B')]
    counts = {name:dict(left=0, right=0, records=0, grades={g:0 for g in ('A','B','C','F','UNRATED')}) for name in wanted}
    for row in selected:
        for name in {key(row[h]) for h in ('left', 'right')} & wanted:
            value = counts[name];value['records'] += 1;value['grades'][row['grade']] += 1
            for hand in ('left', 'right'):
                if key(row[hand])==name:value[hand] += 1
    products = [];completed = remaining = surplus = achieved = 0
    for product in plan['products']:
        actual = counts[key(product['name'])]
        left_gap = max(0, product['left_target']-actual['left'])
        right_gap = max(0, product['right_target']-actual['right'])
        credited = min(product['left_target'], actual['left'])+min(product['right_target'], actual['right'])
        target = product['left_target']+product['right_target']
        complete = not left_gap and not right_gap
        completed += complete;remaining += left_gap+right_gap;achieved += credited
        surplus += max(0, actual['left']-product['left_target'])+max(0, actual['right']-product['right_target'])
        products.append(dict(product, left_actual=actual['left'], right_actual=actual['right'],
                             actual_records=actual['records'], grades=actual['grades'],
                             left_gap=left_gap, right_gap=right_gap, gap=left_gap+right_gap,
                             completion=credited/target if target else 1, complete=complete))
    return dict(plan, products=products, actual_episodes=len(selected), collected_episodes=len(matched),
                ab_episodes=sum(r['grade'] in ('A','B') for r in matched),
                left_actual=sum(p['left_actual'] for p in products), right_actual=sum(p['right_actual'] for p in products),
                completed_products=completed, remaining=remaining, surplus=surplus,
                completion=achieved/(plan['episode_target']*2), basis=basis)


def comparison(root, rows, basis='all'):
    plans = [compare_plan(plan, rows, basis) for plan in load_plans(root)]
    names = {key(product['name']) for plan in plans if plan['status']=='ready' for product in plan['products']}
    return dict(plans=plans, source=str(Path(root).resolve()), basis=basis,
                ready_count=sum(p['status']=='ready' for p in plans),
                episode_target=sum(p['episode_target'] for p in plans if p['status']=='ready'),
                outside_plan=sum(not any(key(r[h]) in names for h in ('left','right')) for r in rows))
