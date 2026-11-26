"""Parallelize lossless image compression; retain the exact factory writer."""
import concurrent.futures
import dis
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


class ImageEncodingCache:
    def __init__(self, original):
        self.original = original
        self.local = threading.local()

    def imencode(self, extension, frame, params):
        cached = getattr(self.local, 'cache', {}).get((extension, id(frame), tuple(params)))
        if cached is not None:
            return cached
        return self.original.imencode(extension, frame, params)

    def __getattr__(self, name):
        return getattr(self.original, name)


class ParallelBatch:
    def __init__(self, module, original_code, workers=3):
        self.original_code = original_code
        self.zlib = module.zlib
        self.proxy = CompressionCache(self.zlib)
        self.cv2 = module.cv
        self.image_proxy = ImageEncodingCache(self.cv2)
        self.globals = dict(module.__dict__, zlib=self.proxy, cv=self.image_proxy)
        self.pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix='storage-compress')
        self.logger = module.logger
        self.workers = workers
        self.last_stats = None

    def close(self):
        self.pool.shutdown(wait=True)

    def __call__(self, batch_data, start_frame, values):
        started = time.monotonic()
        timing_required = getattr(values['self'], '_timing_required', False)
        if timing_required:
            if not all('_timing' in obs for obs, _ in batch_data):
                raise RuntimeError('采集样本缺少时序信息，拒绝写入')
            values = dict(values, timestamp_source='rs/cam_high')
            values['ts_grp'].attrs['source'] = 'rs/cam_high'
            values['ts_grp'].attrs['unit'] = 'ms'
        enabled = values['self'].config.get('record_depth', False)
        values = dict(values, img_types=['color', 'depth'] if enabled else ['color'])
        if 'image_streams' not in values['root'].attrs:
            values['root'].attrs['image_streams'] = 'color,depth' if enabled else 'color'
            values['root'].attrs['depth_recorded'] = enabled
        level = values['ZLIB_CLEVEL']
        payloads = {}
        images = []
        try:
            for obs, _ in batch_data:
                for camera in values['self'].env.image_recorder.list_all():
                    for kind in values['img_types']:
                        if kind == 'color':
                            image = obs['images'].get(camera, {}).get(kind)
                            frame = getattr(image, 'last_frame', None)
                            if frame is not None and frame.size and getattr(image, 'fmt', None) == 'bgr8':
                                images.append(frame)
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
            params = [self.cv2.IMWRITE_JPEG_QUALITY, values['JPEG_QUALITY']]
            encoded_images = list(self.pool.map(lambda image: self.cv2.imencode('.jpg', image, params), images))
            image_cache = {('.jpg', id(image), tuple(params)): encoded for image, encoded in zip(images, encoded_images)}
        except Exception:
            # Preserve recording if an optimization fails before any HDF5 write.
            self.logger.exception('[StorageParallel] 预压缩失败，本批使用原厂写入路径')
            cache = {}
            image_cache = {}
        prepared = time.monotonic()
        closures = tuple(cell(values[name]) for name in self.original_code.co_freevars)
        original = types.FunctionType(self.original_code, self.globals, closure=closures)
        self.proxy.local.cache, self.proxy.local.hits = cache, 0
        self.image_proxy.local.cache = image_cache
        try:
            original(batch_data, start_frame)
            if timing_required:
                self.globals['_timing_write_metadata'](values['root'], batch_data, start_frame)
            hits = self.proxy.local.hits
        finally:
            self.proxy.local.cache = {}
            self.image_proxy.local.cache = {}
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
        updated = code.replace(co_consts=tuple(replace(v) if isinstance(v, types.CodeType) else v
                                              for v in code.co_consts))
        if code.co_name == 'save_worker':
            instructions = {i.offset: i for i in dis.get_instructions(code)}
            if (instructions[704].argval != 'save_batch' or instructions[726].opname != 'BUILD_LIST'
                    or instructions[732].argval != 'len'):
                raise RuntimeError('Factory stage flush layout changed')
            # Stage markers retain their original frame indices; encoding uses
            # the same regular batch threshold across stage boundaries.
            updated = updated.replace(co_code=updated.co_code[:704] + bytes((dis.opmap['NOP'], 0))*14 + updated.co_code[732:])
        return updated
    module._parallel_save_batch = context
    method.__code__ = replace(method.__code__)
    return context, original
