"""Read review choices and the latest QC run without starting any QC work."""
import json
from pathlib import Path

from .grade_policy import GRADES, automatic_grade, effective_grade
from .quality_policy import display_report


def file_stamp(path):
    s = path.stat()
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def compact_summary(value):
    return dict(input=value.get('input'), episodes=[
        {key:row.get(key) for key in ('episode_id', 'input', 'output', 'ok')}
        for row in value.get('episodes', [])])


def compact_report(value):
    report = display_report(value)
    fields = ('quality_grade', 'review_required', 'execution', 'manual_review')
    result = {key:report.get(key) for key in fields}
    result['qc_original'] = {key:report['qc_original'].get(key) for key in fields} if report.get('qc_original') else {}
    return result


class GradeLookup:
    def __init__(self, root, selections):
        self.root = Path(root).resolve()
        self.selections = Path(selections)
        self.cache = {}

    def read(self, path, compact=lambda value:value):
        try:before = file_stamp(path)
        except FileNotFoundError:return {}
        previous = self.cache.get(str(path))
        if previous and previous[0] == before:return previous[1]
        value = json.loads(path.read_text())
        if not isinstance(value, dict):raise ValueError('等级记录不是 JSON 对象：'+str(path))
        value = compact(value)
        if file_stamp(path) != before:raise ValueError('等级记录正在更新，请重试：'+str(path))
        self.cache[str(path)] = (before, value)
        return value

    def relevant(self, path):
        return path.is_relative_to(self.root) or self.root.is_relative_to(path)

    def load(self):
        stores = {}; configs = {}; latest = {}
        report_roots = {self.root/'qc_reports', self.root.parent/'qc_reports'}
        for path in sorted(self.selections.glob('*.json')):
            store = self.read(path); cfg = store.get('config') or {}
            if not cfg.get('hdf5_root'):continue
            scope = Path(cfg['hdf5_root']).expanduser().resolve()
            if not self.relevant(scope):continue
            stores[scope] = store;configs[scope] = cfg
            if cfg.get('qc_root'):report_roots.add(Path(cfg['qc_root']).expanduser().resolve())
        for report_root in sorted(report_roots):
            for path in sorted(report_root.glob('*/batch_summary.json')):
                report = self.read(path, compact_summary)
                if not isinstance(report.get('input'), str) or not report['input']:continue
                scope = Path(report['input']).expanduser().resolve()
                if not self.relevant(scope):continue
                cfg = configs.get(scope, {})
                if cfg.get('qc_root') and report_root != Path(cfg['qc_root']).expanduser().resolve():continue
                modified = file_stamp(path)[3]
                if scope not in latest or modified > latest[scope][0]:latest[scope] = (modified, path, report)
        self.stores = stores
        self.reports = {scope:{row['episode_id']:row for row in value[2]['episodes']}
                        for scope,value in latest.items()}

    def enrich(self, row):
        root = self.root/row['path']
        scope = next((p for p in root.parents if p in self.stores or p in self.reports), None)
        store = self.stores.get(scope, {})
        original = store.get('capture', {}).get(row['grade_key'], {})
        collection = original.get('grade', row['collection_grade'])
        choice = store.get('choices', {}).get(row['grade_key'], {})
        manual = choice.get('grade') if choice.get('source') in ('manual', 'collection', 'qc') else ''
        if manual not in GRADES:manual = ''
        item = self.reports.get(scope, {}).get(root.name, {})
        report = self.read(Path(item['output'])/'qc_report.json', compact_report) if item.get('output') else {}
        if not manual and report.get('manual_review'):manual = report.get('quality_grade') or ''
        qc = automatic_grade(report, item.get('ok'))
        grade = effective_grade(collection, qc, manual)
        return dict(row, grade=grade or 'UNRATED', collection_grade=collection,
                    qc_grade=qc, manual_quality_grade=manual,
                    grade_source='manual' if manual else 'qc' if qc else 'collection' if collection else 'unrated',
                    qc_execution=report.get('execution') or ('completed' if report else ''),
                    manual_note=choice.get('note', ''), manual_problem=choice.get('problem', ''))
