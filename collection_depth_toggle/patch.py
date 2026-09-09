"""Parallelize lossless image compression; retain the exact factory writer."""
import concurrent.futures
import threading
import time
import types

import numpy as np


def find_code(code, name):
    result = []
    if code.co_name == name:
        result.append(code)
    for child in code.co_consts:
        if isinstance(child, types.CodeType):
            result.extend(find_code(child, name))
    return result


def cell(value):
    return (lambda: value).__closure__[0]


class CompressionCache:
    def __init__(self, original):
        self.original = original
        self.local = threading.local()

    def compress(self, data, level=-1):
        cache = getattr(self.local, 'cache', {})
        encoded = cache.get((level, data))
        if encoded is not None:
            self.local.hits += 1
            return encoded
        return self.original.compress(data, level=level)

    def __getattr__(self, name):
        return getattr(self.original, name)


class ParallelBatch:
    def __init__(self, module, original_code, workers=3):
        self.original_code = original_code
        self.zlib = module.zlib
        self.proxy = CompressionCache(self.zlib)
        self.globals = dict(module.__dict__, zlib=self.proxy)
        self.pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix='storage-compress')
        self.logger = module.logger
        self.workers = workers
        self.last_stats = None

    def close(self):
        self.pool.shutdown(wait=True)

    def __call__(self, batch_data, start_frame, values):
        started = time.monotonic()
        enabled = values['self'].config.get('record_depth', False)
        values = dict(values, img_types=['color', 'depth'] if enabled else ['color'])
        if 'image_streams' not in values['root'].attrs:
            values['root'].attrs['image_streams'] = 'color,depth' if enabled else 'color'
            values['root'].attrs['depth_recorded'] = enabled
        level = values['ZLIB_CLEVEL']
        payloads = {}
        try:
            for obs, _ in batch_data:
                for camera in values['self'].env.image_recorder.list_all():
                    for kind in values['img_types']:
                        if kind == 'color':
                            continue
                        image = obs['images'].get(camera, {}).get(kind)
                        frame = getattr(image, 'last_frame', None)
                        # Unusual colour conversions keep the factory path.
                        if (frame is None or not frame.size or
                                getattr(image, 'fmt', None) in ('rgb8', 'yuyv')):
                            continue
                        payloads[np.ascontiguousarray(frame).tobytes()] = None
            raw = list(payloads)
            encoded = list(self.pool.map(lambda data: self.zlib.compress(data, level=level), raw))
            cache = {(level, data): result for data, result in zip(raw, encoded)}
        except Exception:
            # Preserve recording if an optimization fails before any HDF5 write.
            self.logger.exception('[StorageParallel] 预压缩失败，本批使用原厂写入路径')
            cache = {}
        prepared = time.monotonic()
        closures = tuple(cell(values[name]) for name in self.original_code.co_freevars)
        original = types.FunctionType(self.original_code, self.globals, closure=closures)
        self.proxy.local.cache, self.proxy.local.hits = cache, 0
        try:
            original(batch_data, start_frame)
            hits = self.proxy.local.hits
        finally:
            self.proxy.local.cache = {}
        total = (time.monotonic() - started) * 1000
        self.last_stats = {'frames': len(batch_data), 'prepare_ms': (prepared-started)*1000,
                           'total_ms': total, 'cache_hits': hits, 'workers': self.workers}
        self.logger.info('[StorageParallel] 批次总耗时 %.1f ms | 预压缩 %.1f ms | 帧数 %d | 命中 %d',
                         total, self.last_stats['prepare_ms'], len(batch_data), hits)
        return closures[self.original_code.co_freevars.index('timestamp_source')].cell_contents


def install(module, workers=3):
    method = module.EpisodeDataCollector._capture_episode
    matches = find_code(method.__code__, 'save_batch')
    if len(matches) != 1:
        raise RuntimeError('Factory save_batch layout changed')
    original = matches[0]
    expected = ('JPEG_QUALITY', 'ZLIB_CLEVEL', 'action_grp', 'base_dir', 'data_queue',
                'datasets', 'img_types', 'imgs_grp', 'root', 'self', 'state_grp',
                'timestamp_source', 'ts_grp', 'video_writers')
    if original.co_freevars != expected or original.co_varnames[:2] != ('batch_data', 'start_frame'):
        raise RuntimeError('Factory writer closure changed')
    context = ParallelBatch(module, original, workers)
    names = ', '.join(expected)
    mapping = ', '.join(repr(name)+': '+name for name in expected)
    source = ('def factory('+names+'):\n'
              '    def save_batch(batch_data, start_frame):\n'
              '        nonlocal timestamp_source\n'
              '        timestamp_source = _parallel_save_batch(batch_data, start_frame, {'+mapping+'})\n'
              '    return save_batch\n')
    namespace = {}
    exec(compile(source, 'parallel_storage_wrapper.py', 'exec'), namespace)
    replacement = namespace['factory'](*([None]*len(expected))).__code__
    assert replacement.co_freevars == original.co_freevars
    def replace(code):
        if code is original:
            return replacement
        return code.replace(co_consts=tuple(replace(v) if isinstance(v, types.CodeType) else v
                                             for v in code.co_consts))
    module._parallel_save_batch = context
    method.__code__ = replace(method.__code__)
    return context, original
