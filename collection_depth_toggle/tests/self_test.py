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

def write(code, directory, batch, paced=False, record_depth=False):
    directory.mkdir()
    durations = []
    with h5py.File(directory/'episode.hdf5', 'w') as f:
        fields = {'JPEG_QUALITY': 60, 'ZLIB_CLEVEL': 3,
                  'action_grp': f.require_group('action'), 'base_dir': str(directory),
                  'data_queue': queue.Queue(30), 'datasets': {},
                  'img_types': ['color', 'depth', 'ir1', 'ir2'],
                  'imgs_grp': f.require_group('observation/images'), 'root': f,
                  'self': SimpleNamespace(fps=30, config={'record_depth':record_depth}, env=SimpleNamespace(
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

def compare(a, b, record_depth=False):
    with h5py.File(a/'episode.hdf5', 'r') as left, h5py.File(b/'episode.hdf5', 'r') as right:
        names = []
        left.visit(names.append)
        other = []; right.visit(other.append)
        if not record_depth:
            names = [n for n in names if not n.endswith('/depth')]
        assert names == other
        assert right.attrs['image_streams'] == ('color,depth' if record_depth else 'color')
        assert bool(right.attrs['depth_recorded']) == record_depth
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
assert rgb.depth_option({}) is False
for invalid in ('false', 0, 1, None):
    try: rgb.depth_option({'record_depth':invalid})
    except ValueError: pass
    else: raise AssertionError('non-boolean mode accepted')

requested = []
def image_getter(camera, kind):
    requested.append((camera, kind))
    return batch[0][0]['images'][camera][kind]
fake_env = SimpleNamespace(image_recorder=SimpleNamespace(list_all=lambda: CAMERAS, get_image=image_getter))
for enabled in (False, True, False):
    fake_env._record_depth = enabled
    requested.clear()
    images = real_env.Real_Env.get_images(fake_env)
    expected = {'color','depth'} if enabled else {'color'}
    assert len(requested) == len(CAMERAS)*len(expected)
    assert all(set(x) == expected for x in images.values())

specs = [RsRecorder.CameraSpec(name=name, serial='test', align_to=None, desired_streams=[
    RsRecorder.StreamSpec(type='color', width=640, height=480, fps=30, fmt='bgr8'),
    RsRecorder.StreamSpec(type='depth', width=640, height=480, fps=30, fmt='z16')]) for name in CAMERAS]
class FakeRecorder:
    def __init__(self, camera_specs=None):
        self._lock = threading.RLock()
        self.valid_cameras = {}
        self.transitions = []
        for spec in camera_specs:self.reinit_camera(spec.name, streams=spec.desired_streams)
    def reinit_camera(self, camera_name, *, streams=None):
        actual = [SimpleNamespace(type=s.type, last_frame=batch[0][0]['images'][camera_name][s.type].last_frame) for s in streams]
        self.valid_cameras[camera_name] = SimpleNamespace(actual_streams=actual, is_matched=lambda: True)
        self.transitions.append([s.type for s in streams])
        return True
rgb.install_cameras(SimpleNamespace(ImageRecorder=FakeRecorder, logger=collect_data.logger))
recorder = FakeRecorder(specs)
fake_env.image_recorder.rs = recorder
assert recorder._depth_mode is False and all(x==['color'] for x in recorder.transitions)
for enabled in (False,True,False):
    result = rgb.configure_depth(fake_env, enabled, timeout=.1)
    assert result['record_depth'] is enabled and result['frames_ready']
assert all(len(s.desired_streams)==2 for s in specs)

# Exercise session exclusivity, default reset, and diagnostic mode without
# collector construction or any hardware/network side effects.
import asyncio
from types import SimpleNamespace as NS
class FakeService:
    collector_poll = None
    def __init__(self):self.hub=fake_env;self.starts=0;self.stops=0
    async def MetaTransfer(self, request, context):
        self.starts+=1
        try:yield NS(json_data='{}')
        finally:self.stops+=1
class RpcContext:
    def set_code(self, code):self.code=code
    def set_details(self, detail):self.detail=detail
fake_module=NS(RobotService=FakeService,grpc=NS(StatusCode=NS(ALREADY_EXISTS='busy',FAILED_PRECONDITION='failed')),
               pb=NS(MetaData=NS),logger=collect_data.logger)
rgb.install_service(fake_module)
async def session_tests():
    svc=FakeService()
    stream=svc.MetaTransfer(NS(json_config='{"record_depth":true}'),RpcContext())
    await stream.__anext__()
    assert fake_env._record_depth and svc.starts==1
    second=RpcContext()
    assert [v async for v in svc.MetaTransfer(NS(json_config='{}'),second)]==[]
    assert second.code=='busy'
    await stream.aclose()
    assert svc.stops==1 and not fake_env._record_depth
    for enabled in (True,False):
        replies=[v async for v in svc.MetaTransfer(NS(json_config=json.dumps({'record_depth':enabled,'_probe_depth_mode':True})),RpcContext())]
        assert json.loads(replies[0].json_data)['record_depth'] is enabled
        assert svc.starts==1 and not fake_env._record_depth
asyncio.run(session_tests())

with tempfile.TemporaryDirectory(prefix='storage-selftest-', dir=ROOT/'runtime') as directory:
    d = Path(directory)
    before = write(original_batch, d/'original', batch)
    rgb_ms = write(wrapped_code, d/'rgb', batch)
    compare(d/'original', d/'rgb')
    assert context.last_stats['cache_hits']==0
    depth_ms = write(wrapped_code, d/'depth', batch, record_depth=True)
    compare(d/'original', d/'depth', record_depth=True)
    assert context.last_stats['cache_hits']==30
    sustained_rgb = write(wrapped_code, d/'sustained_rgb', batch*5, paced=True)
    sustained_depth = write(wrapped_code, d/'sustained_depth', batch*5, paced=True, record_depth=True)
    stats = {'baseline_rgbd_ms':before, 'rgb_only_ms':rgb_ms, 'parallel_rgbd_ms':depth_ms,
             'frames':len(batch), 'both_modes_dataset_and_video_equivalence':True,
             'camera_mode_transitions_and_session_lifecycle':True,
             'paced_30hz_frames_per_mode':len(batch)*5,
             'paced_rgb_p95_ms':float(np.percentile(sustained_rgb,95)),
             'paced_depth_p95_ms':float(np.percentile(sustained_depth,95))}
    (ROOT/'runtime/offline_benchmark.json').write_text(json.dumps(stats,indent=2)+'\n')
    print('SELECTABLE_DEPTH_OFFLINE_PASS',json.dumps(stats),flush=True)
