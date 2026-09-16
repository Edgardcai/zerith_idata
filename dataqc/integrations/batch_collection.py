"""Two-phase collection CLI: all numeric reports, then bounded model batches."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
import os

from dataqc.batch_prepare import measure
from dataqc.batch_motion import review_many, episode_key, error_outcome
from dataqc.io import hdf5_path, write_json
from .zerith_rules import settings_for_profile, manual_cache, fatal_checks


@contextmanager
def prepare_collection(inputs, profile, args, out_root):
    if str(profile.raw.get('adapter') or '').lower() != 'zerith_columnar':
        yield
        return
    cfg = settings_for_profile(profile)
    roots = []
    for source in inputs:
        root = Path(source)
        if root.is_file(): root = root.parent.parent if root.parent.name == 'states' else root.parent
        if hdf5_path(root).is_file(): roots.append(root)
    review_summary = []
    def progress(text):
        if text.startswith('动作 VLM 复核 · 缓存复用'):
            review_summary[:] = [text]
        print(text, flush=True)
    items = []; outcomes = {}; reused_count=0
    def check(root):
        progress(f'传统质检 · {root.name}')
        cache = manual_cache(root, cfg)
        from dataqc.incremental import completed_report
        if completed_report(cache,cfg):
            progress('复用质检 · '+root.name)
            return None
        raw, before = measure(root, cfg, cache, progress)
        return dict(root=root, report=raw, cache=cache / 'motion', fingerprint_expected=before)
    with ThreadPoolExecutor(max_workers=max(1, min(2, args.num_workers))) as pool:
        futures = {pool.submit(check, root): root for root in roots}
        for future in as_completed(futures):
            try:
                item = future.result()
                if item is None:reused_count+=1
                elif not fatal_checks(item['report']): items.append(item)
            except Exception as exc: outcomes[episode_key(futures[future])] = error_outcome(exc)
    progress(f'增量质检 · 发现 {len(roots)} 条 · 复用 {reused_count} 条 · 新增/变化/未完成 {len(roots)-reused_count} 条')
    items.sort(key=lambda item: str(item['root']))
    progress(f'传统质检完成 · 动作 VLM 可审 {len(items)} 条 · 硬失败跳过 {len(roots)-reused_count-len(items)-len(outcomes)} 条 · 准备失败 {len(outcomes)} 条')
    branch_cfg = cfg | dict(vlm_token_budget=cfg.get('vlm_token_budget', 250000) // (2 if cfg.get('vlm_enabled', False) else 1))
    outcomes.update(review_many(items, branch_cfg, Path(out_root) / 'motion_batches', progress))
    folder = Path(out_root) / '.motion_outcomes'
    folder.mkdir(parents=True, exist_ok=True)
    for key, value in outcomes.items(): write_json(folder / (key + '.json'), value)
    old = os.environ.get('DATAQC_MOTION_OUTCOMES')
    os.environ['DATAQC_MOTION_OUTCOMES'] = str(folder)
    try:
        yield
        # Repeat after per-episode reports so the cache accounting remains visible.
        if review_summary:progress(review_summary[0])
    finally:
        if old is None: os.environ.pop('DATAQC_MOTION_OUTCOMES', None)
        else: os.environ['DATAQC_MOTION_OUTCOMES'] = old
