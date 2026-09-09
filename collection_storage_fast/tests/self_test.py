"""Runs in the copied server binary; imports no real control interfaces."""
import queue
import tempfile
import time
import threading
import zlib
from types import SimpleNamespace

import av
import cv2
import h5py
import numpy as np

source = Path('/data/zerith_data/DailyCGrapeJuice_DailyCOrangeJuice_0.4/episode_000003/episode.hdf5')
CAMERAS = ['rs/cam_high', 'rs/cam_left_wrist', 'rs/cam_right_wrist']

def load_batch():
    result = []
    with h5py.File(source, 'r') as f:
        for index in np.linspace(0, len(f['timestamp/t'])-1, 40).astype(int):
            obs = {'state': {}, 'images': {}}
            act = {}
            for target, prefix in [(obs['state'], 'observation/state'), (act, 'action')]:
                for group in f[prefix]:
                    target[group] = {name: f[prefix+'/'+group+'/'+name][index]
                                     for name in f[prefix+'/'+group]}
            for camera in CAMERAS:
                image_path = 'observation/images/'+camera
                color = cv2.imdecode(f[image_path+'/color'][index], cv2.IMREAD_COLOR)
                depth = np.frombuffer(zlib.decompress(f[image_path+'/depth'][index].tobytes()),
                                      dtype=np.uint16).reshape(480, 640)
                obs['images'][camera] = {
                    'color': SimpleNamespace(last_frame=color, fmt='bgr8', timestamp=float(f['timestamp/t'][index])),
                    'depth': SimpleNamespace(last_frame=depth, fmt='z16', timestamp=float(f['timestamp/t'][index]))}
            result.append((obs, act))
    return result

def write(code, directory, batch, paced=False):
    directory.mkdir()
    durations = []
    with h5py.File(directory/'episode.hdf5', 'w') as f:
        fields = {'JPEG_QUALITY': 60, 'ZLIB_CLEVEL': 3,
                  'action_grp': f.require_group('action'), 'base_dir': str(directory),
                  'data_queue': queue.Queue(30), 'datasets': {},
                  'img_types': ['color', 'depth', 'ir1', 'ir2'],
                  'imgs_grp': f.require_group('observation/images'), 'root': f,
                  'self': SimpleNamespace(fps=30, env=SimpleNamespace(
                      image_recorder=SimpleNamespace(list_all=lambda: CAMERAS))),
                  'state_grp': f.require_group('observation/state'), 'timestamp_source': None,
                  'ts_grp': f.require_group('timestamp'), 'video_writers': {}}
        closures = tuple(patch.cell(fields[name]) for name in code.co_freevars)
        writer = types.FunctionType(code, collect_data.__dict__, closure=closures)
        stream = queue.Queue(30)
        producer_waits = []
        def produce():
            deadline = time.monotonic()
            for sample in batch:
                now = time.monotonic()
                stream.put(sample)
                producer_waits.append(time.monotonic()-now)
                deadline += 1/30
                time.sleep(max(0, deadline-time.monotonic()))
        if paced:
            feeder = threading.Thread(target=produce, daemon=True); feeder.start()
        for start in range(0, len(batch), 10):
            chunk = ([stream.get(timeout=5) for _ in batch[start:start+10]]
                     if paced else batch[start:start+10])
            now = time.monotonic()
            writer(chunk, start)
            durations.append((time.monotonic()-now)*1000)
        if paced:
            feeder.join(5)
            assert not feeder.is_alive()
            assert max(producer_waits) < .05, producer_waits
        assert closures[code.co_freevars.index('timestamp_source')].cell_contents == CAMERAS[0]
        for writer in fields['video_writers'].values():
            writer.release()
    return durations

def compare(a, b):
    with h5py.File(a/'episode.hdf5', 'r') as left, h5py.File(b/'episode.hdf5', 'r') as right:
        names = []
        left.visit(names.append)
        other = []; right.visit(other.append)
        assert names == other
        for name in names:
            x, y = left[name], right[name]
            assert set(x.attrs) == set(y.attrs)
            for key in x.attrs:
                np.testing.assert_array_equal(x.attrs[key], y.attrs[key])
            if isinstance(x, h5py.Dataset):
                assert x.shape == y.shape and x.dtype == y.dtype
                if x.dtype.kind == 'O':
                    for i in range(len(x)): np.testing.assert_array_equal(x[i], y[i])
                else: np.testing.assert_array_equal(x[:], y[:])
    for camera in CAMERAS:
        with av.open(str(a/'videos'/camera)+'.mp4') as first, av.open(str(b/'videos'/camera)+'.mp4') as second:
            x = list(first.decode(video=0)); y = list(second.decode(video=0))
            assert len(x) == len(y) == 40
            for p, q in zip(x, y):
                np.testing.assert_array_equal(p.to_ndarray(format='bgr24'), q.to_ndarray(format='bgr24'))

batch = load_batch()
wrapped_code = patch.find_code(collect_data.EpisodeDataCollector._capture_episode.__code__, 'save_batch')[0]
with tempfile.TemporaryDirectory(prefix='storage-selftest-', dir=ROOT/'runtime') as directory:
    d = Path(directory)
    before = write(original_batch, d/'original', batch)
    after = write(wrapped_code, d/'parallel', batch)
    compare(d/'original', d/'parallel')
    assert context.last_stats['cache_hits'] == 30
    # Duplicate payload cache and non-contiguous IR arrays preserve bytes.
    raw = np.arange(64, dtype=np.uint16).reshape(8, 8)[:, ::2]
    encoded = context.zlib.compress(np.ascontiguousarray(raw).tobytes(), level=3)
    context.proxy.local.cache = {(3, np.ascontiguousarray(raw).tobytes()): encoded}
    context.proxy.local.hits = 0
    assert context.proxy.compress(raw.tobytes(), level=3) == encoded
    assert context.proxy.compress(raw.tobytes(), level=1) == zlib.compress(raw.tobytes(), level=1)
    context.proxy.local.cache = {}
    pool = context.pool
    class FailedPool:
        def map(self, *args): raise RuntimeError('test encoder unavailable')
    try:
        context.pool = FailedPool()
        write(wrapped_code, d/'fallback', batch)
        compare(d/'original', d/'fallback')
    finally:
        context.pool = pool
    sustained = write(wrapped_code, d/'sustained', batch*5, paced=True)
    stats = {'baseline_ms': before, 'parallel_ms': after, 'frames': len(batch),
             'dataset_and_encoded_images_identical': True, 'decoded_videos_identical': True,
             'fallback_equivalence': True, 'paced_30hz_frames': len(batch)*5,
             'paced_batch_p95_ms': float(np.percentile(sustained, 95))}
    (ROOT/'runtime/offline_benchmark.json').write_text(json.dumps(stats, indent=2)+'\n')
    print('FACTORY_WRITER_EQUIVALENCE_PASS', json.dumps(stats), flush=True)
