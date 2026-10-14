"""Shared grade provenance and precedence for review and dashboard displays."""
import re

from .io import read_json

GRADES = ('A', 'B', 'C', 'F')


def capture(root, review=None, meta=None):
    """Collector grades are distinct from mutable QC metadata."""
    if review is None:review = read_json(root/'review.json')
    if meta is None:meta = read_json(root/'meta/episode_meta.json') or read_json(root/'episode_meta.json')
    grade = review.get('grade') or meta.get('collection_grade') or meta.get('collection_quality_grade')
    source = 'review.json' if review.get('grade') else 'episode_meta.json'
    if not grade and not any(k in meta for k in ('manual_quality_grade', 'manual_failure')):
        grade = meta.get('quality_grade')
    grade = str(grade or '').upper()
    identity = str(review.get('episode_uuid') or meta.get('source_episode_id') or meta.get('episode_uuid') or root.name)
    number = review.get('number')
    name = f'episode_{int(number):06d}' if str(number).isdigit() and int(number)>0 else str(meta.get('source_episode_name') or '')
    if not re.fullmatch(r'episode_?\d+', name):name = ''
    return identity, dict(grade=grade if grade in GRADES else '', source=source if grade in GRADES else '', episode_name=name)


def automatic_grade(report, qc_ok=None):
    baseline = report.get('qc_original') or (report if not report.get('manual_review') else {})
    if report.get('execution') == 'incomplete':return ''
    return baseline.get('quality_grade') or ('B' if baseline.get('review_required') else 'F' if qc_ok is False else '')


def effective_grade(collection, automatic, manual):
    return manual or automatic or collection
