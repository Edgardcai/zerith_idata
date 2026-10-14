"""Read-only collection inventory. Derived hand datasets never add episodes."""
import collections
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import unicodedata
from zoneinfo import ZoneInfo

from . import config
from .dashboard_grades import GradeLookup
from .grade_policy import capture

TZ = ZoneInfo('Asia/Shanghai')
GRADES = ('A', 'B', 'C', 'F', 'UNRATED')
VERSION = 2
EXCLUDED = {'videos', 'record', 'recordings', 'exports', 'lerobot', 'lefthand', 'righthand',
            'left_hand', 'right_hand', 'left', 'right', 'hands', 'splits', '__pycache__', '.qc_reports'}
META_FILES = ('episode_meta.json', 'meta/episode_meta.json', 'review.json', 'collection_task.json')


def normalize(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).split())


def stamp(path):
    s = path.stat()
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def signature(root):
    result = {}
    for name in (*META_FILES, 'episode.hdf5', 'episode.h5'):
        p = root / name
        if p.is_file():result[name] = stamp(p)
    return result


def discover(root, warnings):
    if not root.is_dir():raise ValueError('数据目录不存在或不可读取：' + str(root))
    found = []
    def onerror(error):
        # A partial traversal must not replace a previously complete snapshot.
        raise OSError('目录扫描失败：' + str(error))
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
        p = Path(directory)
        dirs[:] = sorted(d for d in dirs if d.casefold() not in EXCLUDED and not d.startswith('.')
                         and not d.endswith('.partial') and '.previous-' not in d
                         and not (p/d).is_symlink())
        if (p/'meta/info.json').is_file():dirs[:] = [];continue
        names = [n for n in ('episode.hdf5', 'episode.h5') if n in files]
        if names:
            dirs[:] = []
            if any((p/n).is_symlink() for n in names):
                warnings.append(dict(path=str(p.relative_to(root)), reason='符号链接记录未重复计数'));continue
            found.append(p)
    return sorted(found)


def parse_prompt(text):
    text = normalize(text).rstrip('.')
    double = re.fullmatch(r'Grasp (.+?) with the left hand and then grasp (.+?) with the right hand', text, re.I)
    if double:return dict(left=double[1], right=double[2])
    single = re.fullmatch(r'Grasp (.+?) with the (left|right) hand', text, re.I)
    if single and ' with the ' not in single[1].lower():return {single[2].lower(): single[1]}
    return {}


def read_record(root, base):
    issues = []
    def read(name):
        p = root/name
        if not p.exists():return {}
        try:
            value = json.loads(p.read_text())
            if not isinstance(value, dict):raise ValueError('不是 JSON 对象')
            return value
        except (ValueError, OSError) as exc:
            issues.append(name+' 无法读取：'+str(exc));return {}
    meta = read('meta/episode_meta.json') or read('episode_meta.json')
    review = read('review.json'); task = read('collection_task.json')
    conf = task.get('config') if isinstance(task.get('config'), dict) else {}
    targets = task.get('targets') if isinstance(task.get('targets'), dict) else {}
    prompt = normalize(conf.get('task_name') or meta.get('task'))
    h5_name = 'episode.hdf5' if (root/'episode.hdf5').exists() else 'episode.h5'
    attrs = {}
    # Older records lack sidecars; only inspect HDF5 attributes for these rows.
    if not prompt or not meta:
        try:
            import h5py
            with h5py.File(root/h5_name, 'r') as f:
                attrs = {k: v.decode() if isinstance(v, bytes) else str(v) for k,v in f.attrs.items()}
            prompt = prompt or normalize(attrs.get('task_name'))
        except (OSError, ValueError) as exc:
            if getattr(exc,'errno',None) in (11,35) or 'unable to lock file' in str(exc) or 'already open for write' in str(exc):
                raise ValueError('记录仍在采集或保存中，待保存完成后统计') from exc
            issues.append('HDF5 元数据无法读取：'+str(exc))
    parsed = parse_prompt(prompt)
    left = normalize(targets.get('left') or parsed.get('left'))
    right = normalize(targets.get('right') or parsed.get('right'))
    if not left and not right:issues.append('缺少可识别的左右手商品')
    grade_key, collection = capture(root, review, meta)
    grade = collection['grade']
    if grade not in GRADES:grade = 'UNRATED';issues.append('缺少采集等级')
    timestamp = meta.get('collection_ts'); date = ''; date_source = 'unknown'
    try:
        if isinstance(timestamp, bool):raise ValueError()
        value = float(timestamp)
        if value > 1e11:value /= 1000
        if value <= 0:raise ValueError()
        date = dt.datetime.fromtimestamp(value,TZ).date().isoformat()
        date_source = 'collection_ts'
    except (TypeError, ValueError, OSError, OverflowError):pass
    rel = root.relative_to(base).as_posix()
    directory_date = ''
    for part in Path(rel).parts[:-1]:
        if re.match(r'^\d{8}(?:_|$)',part):
            try:directory_date = dt.datetime.strptime(part[:8],'%Y%m%d').date().isoformat();break
            except ValueError:pass
    if not date and directory_date:
        date = directory_date; date_source = 'directory';issues.append('采集时间缺失，使用目录日期')
    if not date:issues.append('采集日期未知')
    mismatch = bool(date and directory_date and date != directory_date)
    if mismatch:issues.append('目录日期与采集日期不同，按采集日期统计')
    identity = normalize(review.get('episode_uuid') or meta.get('source_episode_id') or meta.get('episode_id') or attrs.get('episode_id'))
    # A renamed episode number is not a globally unique source identity.
    if not re.fullmatch(r'[0-9a-fA-F-]{32,36}', identity):identity = ''
    return dict(id=hashlib.sha256(rel.encode()).hexdigest()[:24], path=rel, date=date,
                date_source=date_source, directory_date=directory_date, date_mismatch=mismatch,
                left=left, right=right, grade=grade, collection_grade=collection['grade'], grade_key=grade_key,
                scene=normalize(conf.get('scene_name') or (f"scene{conf['scene_id']}" if 'scene_id' in conf else '')) or Path(rel).parts[0],
                prompt=prompt, identity=identity, collection_ts=timestamp,
                reviewed=review.get('reviewed') is True, review_updated=review.get('updated_at') or 0,
                issues=issues, aliases=[], duplicate_count=0, identity_conflict=False,
                h5_name=h5_name)


class Inventory:
    def __init__(self, root, cache_path, grade_selections=None):
        self.root = Path(root).resolve(); self.cache_path = Path(cache_path)
        self.lock = threading.RLock(); self.running = False; self.phase = '';self.error = '';self.last_attempt = 0
        self.snapshot = dict(rows=[], scanned_count=0, duplicate_count=0, warnings=[], updated_at=None)
        self.entries = {}; self.hashes = {}
        self.grades = GradeLookup(self.root, grade_selections if grade_selections is not None else config.VAR/'grade-selections')
        try:
            data = json.loads(self.cache_path.read_text())
            if data.get('version') == VERSION and data.get('root') == str(self.root):
                self.entries = data['entries'];self.hashes = data.get('hashes',{});self.snapshot = data['snapshot']
        except (OSError, ValueError, KeyError, TypeError):pass

    def state(self):
        with self.lock:
            return dict(source=str(self.root), timezone=str(TZ), scanning=self.running, phase=self.phase,
                        error=self.error, updated_at=self.snapshot['updated_at'],
                        scanned_count=self.snapshot['scanned_count'], duplicate_count=self.snapshot['duplicate_count'],
                        warning_count=len(self.snapshot['warnings']), warnings=self.snapshot['warnings'][:100],
                        ready=self.snapshot['updated_at'] is not None)

    def refresh(self, force=False, background=True):
        with self.lock:
            if self.running or (not force and time.time()-self.last_attempt < 60):return False
            self.running = True;self.last_attempt = time.time();self.phase = '正在扫描原始采集目录';self.error = ''
        if background:threading.Thread(target=self._scan,daemon=True,name='collection-inventory').start()
        else:self._scan()
        return True

    def _digest(self, path, hashes):
        key = str(path.relative_to(self.root));before = stamp(path);cached = self.hashes.get(key,{})
        if cached.get('stamp') == before:digest = cached['digest']
        else:
            h = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda:stream.read(2*1024*1024),b''):h.update(chunk)
            if stamp(path) != before:raise ValueError('文件仍在写入：'+key)
            digest = h.hexdigest()
        hashes[key] = dict(stamp=before,digest=digest)
        return digest

    def _scan(self):
        try:
            warnings=[];found=discover(self.root,warnings);entries={};rows=[];hashes={}
            for i, path in enumerate(found):
                if i % 100 == 0:
                    with self.lock:self.phase = f'读取采集元数据 {i:,} / {len(found):,}'
                key = path.relative_to(self.root).as_posix()
                try:
                    before = signature(path);old = self.entries.get(key,{})
                    row = old['row'] if old.get('signature') == before else read_record(path,self.root)
                    if signature(path) != before:raise ValueError('元数据仍在写入，等待下次刷新')
                    entries[key] = dict(signature=before,row=row)
                    # Fresh containers: duplicate annotations must never contaminate the parse cache.
                    rows.append(dict(row,issues=list(row['issues']),aliases=[]))
                except (OSError, ValueError, TypeError, AttributeError) as exc:
                    warnings.append(dict(path=key,reason=str(exc)))
            grouped=collections.defaultdict(list)
            for row in rows:
                if row['identity']:grouped[row['identity']].append(row)
            excluded=set()
            duplicates=[group for group in grouped.values() if len(group)>1]
            for i,group in enumerate(duplicates):
                with self.lock:self.phase=f'核对重复标识 {i+1} / {len(duplicates)}'
                contents=collections.defaultdict(list)
                for row in group:
                    path=self.root/row['path']
                    try:
                        media=[path/row['h5_name'],*sorted((path/'videos').rglob('*.mp4'))]
                        key=tuple((p.relative_to(path).as_posix(),self._digest(p,hashes)) for p in media)
                        contents[key].append(row)
                    except (OSError,ValueError) as exc:
                        row['issues'].append('重复内容核对未完成：'+str(exc));contents[('unverified',row['id'])].append(row)
                for matches in contents.values():
                    # If metadata disagree, keep records separate rather than choosing a grade silently.
                    versions=collections.defaultdict(list)
                    for row in matches:
                        versions[(row['date'],row['left'].casefold(),row['right'].casefold(),row['grade'])].append(row)
                    for copies in versions.values():
                        canonical=sorted(copies,key=lambda r:r['path'])[0]
                        for duplicate in copies:
                            if duplicate is canonical:continue
                            canonical['aliases'].append(duplicate['path']);excluded.add(duplicate['id'])
                        canonical['duplicate_count']=len(canonical['aliases'])
                kept=[r for r in group if r['id'] not in excluded]
                if len(kept)>1:
                    for row in kept:
                        row['identity_conflict']=True;row['issues'].append('相同原始标识的内容或元数据不同，保留并待核对')
            with self.lock:self.phase='读取人工等级与自动质检结果'
            self.grades.load()
            # Deduplicate original collection records before applying later review choices.
            graded = [self.grades.enrich(r) for r in rows if r['id'] not in excluded]
            snapshot=dict(rows=graded,scanned_count=len(found),
                          duplicate_count=len(excluded),warnings=warnings,updated_at=time.time())
            payload=dict(version=VERSION,root=str(self.root),entries=entries,hashes=hashes,snapshot=snapshot)
            self.cache_path.parent.mkdir(parents=True,exist_ok=True)
            temporary=self.cache_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(payload,ensure_ascii=False));os.replace(temporary,self.cache_path)
            with self.lock:self.entries=entries;self.hashes=hashes;self.snapshot=snapshot;self.phase='已更新'
        except Exception as exc:
            with self.lock:self.error=str(exc);self.phase='更新失败，保留上次完整统计'
        finally:
            with self.lock:self.running=False

    def rows(self):
        with self.lock:return self.snapshot['rows']


def date_filter(start='', end=''):
    for value in (start,end):
        if value:
            try:
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):raise ValueError()
                dt.date.fromisoformat(value)
            except ValueError:raise ValueError('日期格式应为 YYYY-MM-DD')
    if start and end and start>end:raise ValueError('开始日期不能晚于结束日期')


def filtered(rows,start='',end='',product='',scene='',grade='',issues=False):
    date_filter(start,end)
    if grade and grade not in GRADES:raise ValueError('未知等级')
    wanted=normalize(product).casefold()
    return [r for r in rows if (not start or (r['date'] and r['date']>=start))
            and (not end or (r['date'] and r['date']<=end))
            and (not wanted or wanted in (r['left'].casefold(),r['right'].casefold()))
            and (not scene or r['scene']==scene) and (not grade or r['grade']==grade)
            and (not issues or r['issues'] or r['duplicate_count'])]


def metrics(rows):
    grades={g:0 for g in GRADES};products=set();dates=set();left=right=0
    for row in rows:
        grades[row['grade']]+=1
        if row['date']:dates.add(row['date'])
        if row['left']:left+=1;products.add(row['left'].casefold())
        if row['right']:right+=1;products.add(row['right'].casefold())
    return dict(total=len(rows),left=left,right=right,products=len(products),days=len(dates),grades=grades,
                issues=sum(bool(r['issues'] or r['duplicate_count']) for r in rows),
                unknown_dates=sum(not r['date'] for r in rows),
                unknown_products=sum(not r['left'] and not r['right'] for r in rows),
                identity_conflicts=sum(r['identity_conflict'] for r in rows),
                date_mismatches=sum(r['date_mismatch'] for r in rows))


def aggregate(rows):
    products={};daily={}
    for row in rows:
        date=row['date'] or '未知日期'
        day=daily.setdefault(date,dict(date=date,total=0,left=0,right=0,grades={g:0 for g in GRADES},products=set()))
        day['total']+=1;day['grades'][row['grade']]+=1
        for hand in ('left','right'):
            if row[hand]:day[hand]+=1;day['products'].add(row[hand].casefold())
        # Same item in both hands counts once in the product's episode/grade total.
        for name in dict.fromkeys(v.casefold() for v in (row['left'],row['right']) if v):
            display=next(v for v in (row['left'],row['right']) if v.casefold()==name)
            item=products.setdefault(name,dict(name=display,total=0,left=0,right=0,grades={g:0 for g in GRADES},
                                               left_grades={g:0 for g in GRADES},right_grades={g:0 for g in GRADES},first='',last='',aliases=set()))
            item['total']+=1;item['grades'][row['grade']]+=1
            for hand in ('left','right'):
                if row[hand].casefold()==name:
                    item[hand]+=1;item[hand+'_grades'][row['grade']]+=1;item['aliases'].add(row[hand])
            if row['date']:
                item['first']=min(item['first'] or row['date'],row['date']);item['last']=max(item['last'],row['date'])
    for item in products.values():item['aliases']=sorted(item['aliases'])
    for day in daily.values():day['products']=len(day['products'])
    return sorted(products.values(),key=lambda r:(-r['total'],r['name'])),sorted(daily.values(),key=lambda r:r['date'])


def summary(inventory, **filters):
    with inventory.lock:
        rows=inventory.snapshot['rows'];state=inventory.state()
    selected=filtered(rows,**filters);products,daily=aggregate(selected)
    dates=sorted({r['date'] for r in rows if r['date']})
    context=filtered(rows,**dict(filters,start='',end=''))
    dated=[r['date'] for r in context if r['date']]
    finish=filters.get('end') or (max(dated) if dated else dt.datetime.now(TZ).date().isoformat())
    single=bool(filters.get('start') and filters.get('start')==filters.get('end'))
    count=14 if single else 30
    begin=(dt.date.fromisoformat(finish)-dt.timedelta(days=count-1)).isoformat()
    if not single and filters.get('start'):begin=max(begin,filters['start'])
    _,trend_rows=aggregate(filtered(context,start=begin,end=finish))
    lookup={r['date']:r for r in trend_rows};trend=[];day=dt.date.fromisoformat(begin)
    while day.isoformat()<=finish:
        key=day.isoformat();trend.append(lookup.get(key,dict(date=key,total=0,grades={g:0 for g in GRADES})))
        day+=dt.timedelta(days=1)
    return dict(trend=trend,state=state,global_metrics=metrics(rows),metrics=metrics(selected),products=products,daily=daily,
                filters=filters,options=dict(dates=dates,scenes=sorted({r['scene'] for r in rows}),
                products=sorted({normalize(v).casefold():normalize(v) for r in rows for v in (r['left'],r['right']) if v}.values(),key=str.casefold)),
                today=dt.datetime.now(TZ).date().isoformat())
