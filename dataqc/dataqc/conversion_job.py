"""Isolated HDF5 conversion coordinator used by the 8091 job runner."""
import hashlib
import json
from pathlib import Path

from .io import fingerprint, read_json, write_json
from .motion import RULE_VERSION
from .parallel import ordered_map
from .robots import get_adapter


def prepare_episode(payload):
    entry,threshold,cache=payload
    adapter=get_adapter('zerith')
    root=Path(entry['episode_dir']);row=entry['status_row']
    report=row.get('grade_approval') or read_json(Path(row['qc_output'])/'qc_report.json')
    if report.get('rules_version')!=RULE_VERSION or not report.get('accepted') or report.get('review_required'):
        raise ValueError(f'{root.name}: 质检结果已变更，请重新检查')
    before=fingerprint(root)
    if report.get('source_fingerprint')!=before:
        raise ValueError(f'{root.name}: 质检后源数据有变化，请重新质检')
    decision=dict(report['decision']);decision['grade']=entry['quality_grade']
    identity=hashlib.sha256(json.dumps(dict(source=before,root=str(root),decision=decision,threshold=threshold),sort_keys=True).encode()).hexdigest()
    derived=Path(cache)/identity/root.name
    if not derived.exists():adapter.derive(root,derived,report['raw_report'],decision,threshold,before)
    post=adapter.check(derived,threshold)
    if post['hard_fail']:raise ValueError(f'{root.name}: 派生数据未通过复检')
    return dict(root=str(derived),grade=entry['quality_grade'],entry=entry)


def build(request,progress=print):
    entries=request['entries'];threshold=request['threshold']
    identities=[str(Path(e['episode_dir']).resolve())for e in entries]
    if len(set(identities))!=len(identities):raise ValueError('转换列表包含重复 episode')
    prepared=ordered_map(prepare_episode,[(e,threshold,request['cache'])for e in entries],progress,'派生与数值复检')
    groups={}
    for value in prepared:groups.setdefault(value['grade'],[]).append(value)
    results=[]
    for grade,values in groups.items():
        result=get_adapter('zerith').export(values,request['outputs'][grade],threshold,progress)
        results.append(dict(grade=grade,result=result,values=values))
    return results


def main():
    import argparse
    from .parallel import episode_pool
    parser=argparse.ArgumentParser();parser.add_argument('request');parser.add_argument('result')
    args=parser.parse_args();request=read_json(args.request)
    workers=min(request['workers'],max(1,len(request['entries'])))
    print(f"LeRobot 转换进程 {workers}；每进程内部线程 1；NVENC 最多 8 会话",flush=True)
    with episode_pool(workers,request['device']):
        result=build(request,lambda text:print(text,flush=True))
    write_json(args.result,result)


if __name__=='__main__':main()
