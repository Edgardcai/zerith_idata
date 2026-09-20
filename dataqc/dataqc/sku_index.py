"""Read-only, per-episode SKU lookup across independent LeRobot datasets."""
import json
import os
import re
import threading
import time
import unicodedata
import uuid
from pathlib import Path

from .io import parse_task

PRODUCTS = [
    'Fanta Grape', 'If coconut', 'Tropicana', 'Daily C Grape Juice',
    'AD Calcium Milk', 'Aojiru', 'Pepsi Real Sugar', 'Wanglaoji',
    'Yili Peach Yogurt', 'Vita Coconut', 'Daily C Orange Juice', 'HK Orange Fanta',
    'Robuk Velvet Latte', 'NEVER Coconut Latte', 'Nongfu Spring', 'Sprite',
    'Dahongpao Milk Tea', 'Coca-Cola', 'Ginger Ale', 'Pepsi',
    'Yili Strawberry Yogurt', 'Fanta Strawberry', 'Bottled Coca-Cola', 'Schweppes C',
]


def normalize(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value)).split()).casefold()


def matches(prompt, sku):
    products = parse_task(prompt)
    if products:
        return normalize(sku) in {normalize(value) for value in products.values()}
    # Some datasets store just the item name as the task. Never substring-match
    # Pepsi against Pepsi Real Sugar or Coca-Cola against Bottled Coca-Cola.
    return normalize(prompt).rstrip('.') == normalize(sku).rstrip('.')


def json_rows(path):
    if not path.exists():return []
    rows = []
    with path.open(encoding='utf-8-sig') as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):raise ValueError('记录必须是对象')
            except ValueError as exc:
                raise ValueError(f'{path.name} 第 {number} 行无法读取：{exc}') from exc
            rows.append(row)
    return rows


def natural(value):
    return [int(v) if v.isdigit() else v.casefold() for v in re.split(r'(\d+)', str(value))]


def dataset_episodes(root, cancelled=lambda:False):
    root = Path(root).expanduser().resolve()
    info = json.loads((root/'meta/info.json').read_text())
    task_map = {int(r['task_index']): str(r['task']) for r in json_rows(root/'meta/tasks.jsonl')}
    rows = json_rows(root/'meta/episodes.jsonl')
    if not rows:
        rows = [dict(episode_index=int(p.stem.removeprefix('episode_')))
                for p in root.glob('data/chunk-*/episode_*.parquet') if p.stem.removeprefix('episode_').isdigit()]
    if not rows:raise ValueError('没有可供当前播放器使用的 episode 元数据或分集 Parquet')
    seen = set()
    for row in sorted(rows, key=lambda r:int(r['episode_index'])):
        if cancelled():return
        index = int(row['episode_index'])
        if index < 0 or index in seen:raise ValueError(f'episode_index 重复或无效：{index}')
        seen.add(index)
        pattern = info.get('data_path') or 'data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet'
        data = (root/pattern.format(episode_chunk=index//max(1,int(info.get('chunks_size') or 1000)),episode_index=index)).resolve()
        data.relative_to(root)
        if not data.is_file():
            yield dict(episode_index=index,error=f'Episode {index} 的 Parquet 不存在：{data.name}')
            continue
        prompts = row.get('full_instructions_en') or row.get('tasks') or []
        if not isinstance(prompts, list):prompts = [prompts]
        prompts = [v.strip() for v in prompts if isinstance(v,str) and v.strip()]
        if not prompts and row.get('task'):prompts = [str(row['task'])]
        if not prompts and row.get('task_index') is not None:
            prompts = [task_map.get(int(row['task_index']), '')]
        prompts = [v for v in prompts if v]
        if not prompts:
            # Resolve this episode's actual task indices, never every task in the dataset.
            import pyarrow.parquet as pq
            try:
                indices = pq.read_table(data, columns=['task_index']).column('task_index').unique().to_pylist()
                if any(i is None or int(i) not in task_map for i in indices):
                    raise ValueError('task_index 在 tasks.jsonl 中没有对应提示词')
                prompts = [task_map[int(i)] for i in indices if i is not None and int(i) in task_map]
            except Exception as exc:
                yield dict(episode_index=index,error=f'Episode {index} 的任务索引读取失败：{exc}')
                continue
        if not prompts:
            yield dict(episode_index=index,error=f'Episode {index} 缺少对应提示词')
            continue
        yield dict(episode_index=index,episode_name=f'episode_{index:06d}',prompts=list(dict.fromkeys(prompts)))


def scan(root, sku, progress=lambda _:None, cancelled=lambda:False):
    root = Path(root).expanduser().resolve()
    if not root.is_dir():raise ValueError('数据目录不存在或不可读取')
    if not isinstance(sku,str) or not sku.strip() or len(sku)>200:raise ValueError('请选择商品名称')
    result = dict(root=str(root),sku=sku.strip(),datasets=[],episodes=[],warnings=[],scanned_datasets=0,scanned_episodes=0,unparsed_prompts=0)
    def warning(path, reason):
        result['warnings'].append(dict(path=str(path),reason=str(reason)))
    def onerror(exc):warning(exc.filename,exc)
    for directory, dirs, _ in os.walk(root, followlinks=False, onerror=onerror):
        if cancelled():return result
        dirs[:] = sorted([d for d in dirs if not d.startswith('.') and '.previous' not in d and d not in ('__pycache__','.venv')],key=natural)
        dataset = Path(directory)
        if not (dataset/'meta/info.json').is_file():continue
        dirs[:] = []  # Never recurse through millions of video frames or Parquet files.
        result['scanned_datasets'] += 1
        relative = str(dataset.relative_to(root))
        found = []
        try:
            for episode in dataset_episodes(dataset,cancelled):
                result['scanned_episodes'] += 1
                if episode.get('error'):
                    warning(dataset,episode['error']);continue
                if any(matches(prompt,sku) for prompt in episode['prompts']):
                    found.append(dict(dataset_path=str(dataset),relative_path=relative,
                                      episode_index=episode['episode_index'],episode_name=episode['episode_name'],
                                      prompt=' / '.join(episode['prompts'])))
                elif any(not parse_task(p) and normalize(p).rstrip('.') not in {normalize(x) for x in PRODUCTS} for p in episode['prompts']):
                    result['unparsed_prompts'] += 1
        except Exception as exc:warning(dataset,exc)
        if found:
            result['datasets'].append(dict(path=str(dataset),name=dataset.name,relative_path=relative,episode_count=len(found),error=''))
            result['episodes'].extend(found)
        progress({k:result[k] for k in ('scanned_datasets','scanned_episodes','unparsed_prompts')} | dict(matched_episodes=len(result['episodes']),warning_count=len(result['warnings'])))
    return result


class Searches:
    def __init__(self):
        self.lock = threading.RLock()
        self.jobs = {}

    def start(self, root, sku):
        if not isinstance(root,str) or not root.strip():raise ValueError('请输入数据目录')
        path=Path(root).expanduser().resolve()
        if not path.is_dir():raise ValueError('数据目录不存在或不可读取')
        if sku not in PRODUCTS:raise ValueError('请从商品下拉菜单选择商品')
        with self.lock:
            self.jobs = {k:v for k,v in self.jobs.items() if v['status']=='running' or time.time()-v['created']<1800}
            if sum(j['status']=='running' for j in self.jobs.values())>=2:raise ValueError('已有两个目录扫描正在运行，请稍候或取消旧扫描')
            # Bound completed in-memory result sets without deleting active scans.
            for key in list(self.jobs):
                if len(self.jobs)<12:break
                if self.jobs[key]['status']!='running':self.jobs.pop(key)
            ident=uuid.uuid4().hex
            job=dict(id=ident,status='running',created=time.time(),progress={},stop=threading.Event())
            self.jobs[ident]=job
        def update(value):
            with self.lock:job['progress']=value
        def run():
            try:
                result=scan(path,sku,update,job['stop'].is_set)
                with self.lock:
                    job['status']='cancelled' if job['stop'].is_set() else 'completed'
                    if job['status']=='completed':job['result']=result
            except Exception as exc:
                with self.lock:job.update(status='failed',error=str(exc))
        threading.Thread(target=run,daemon=True).start()
        return dict(id=ident)

    def get(self, ident, cancel=False):
        with self.lock:
            if ident not in self.jobs:raise ValueError('扫描结果已过期，请重新筛选')
            job=self.jobs[ident]
            if cancel:job['stop'].set()
            return {k:v for k,v in job.items() if k not in ('stop','created')}
