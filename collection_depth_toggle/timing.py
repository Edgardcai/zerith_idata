"""Fresh camera driven recording, aligned on HOST RECEIVE time (not exposure time).

No robot commands are sent here. Preserve factory control, stage handling and
storage. Device timestamps remain unmodified in the existing timestamp/t field.
"""
from collections import Counter, deque
import copy
import dis
import functools
import hashlib
import json
import math
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np

VERSION = 1
CAMERAS = ('rs/cam_high', 'rs/cam_left_wrist', 'rs/cam_right_wrist')
MASTER = CAMERAS[0]
SKEW_NS = 20_000_000
STATE_AGE_NS = 50_000_000
FRAME_AGE_NS = 100_000_000
WAIT_NS = 70_000_000
LOOKAHEAD_NS = 60_000_000
_arrival = threading.local()
STATE_FIELDS = ('upper_joint_state', 'gripper_state', 'chassis_state',
                'waist_state', 'head_state')
ACTION_FIELDS = ('upper_joint_control', 'gripper_control', 'chassis_control',
                 'waist_control', 'head_control')
POSE_FIELDS = ('left_arm_actual', 'right_arm_actual', 'left_camera_actual',
               'right_camera_actual', 'head_camera_actual')
ACTION_POSES = ('left_arm_action', 'right_arm_action')
ALL_FIELDS = STATE_FIELDS + ACTION_FIELDS + POSE_FIELDS + ACTION_POSES


def replace_span(code, start, end, operations):
    """Same-length CPython 3.10 patch: all factory branch targets stay intact."""
    if sys.version_info[:2] != (3, 10):
        raise RuntimeError('Timing patch requires the validated Python 3.10 binary')
    names = list(code.co_names)
    payload = bytearray()
    for op, arg in operations:
        if op == 'LOAD_GLOBAL':
            if arg not in names:
                names.append(arg)
            arg = names.index(arg)
        elif op == 'LOAD_DEREF':
            arg = (code.co_cellvars + code.co_freevars).index(arg)
        elif op in ('LOAD_FAST', 'STORE_FAST'):
            arg = code.co_varnames.index(arg)
        if arg > 255:
            payload.extend((dis.opmap['EXTENDED_ARG'], arg >> 8))
        payload.extend((dis.opmap[op], arg & 255))
    if len(payload) > end-start or (end-start) % 2:
        raise RuntimeError('Timing bytecode span does not fit')
    payload.extend(bytes((dis.opmap['NOP'], 0)) * ((end-start-len(payload))//2))
    return code.replace(co_code=code.co_code[:start]+bytes(payload)+code.co_code[end:],
                        co_names=tuple(names))


def wait_frames(pipeline):
    frames = pipeline.wait_for_frames(timeout_ms=1000)
    _arrival.receive_ns = time.monotonic_ns()
    return frames


def publish_frame(recorder, camera, actual, frame):
    """Copy outside the shared lock; publish an immutable snapshot atomically."""
    received = getattr(_arrival, 'receive_ns', None)
    if received is None:
        received = time.monotonic_ns()
    timestamp = float(frame.get_timestamp())
    number = int(frame.get_frame_number())
    domain = str(frame.get_frame_timestamp_domain())
    key = (camera, actual.type)
    if not math.isfinite(timestamp):
        with recorder._lock:
            recorder._timing_events['invalid_source_time'] += 1
        return
    pixels = np.asanyarray(frame.get_data()).copy()
    pixels.flags.writeable = False
    with recorder._lock:
        _publish_snapshot(recorder, key, actual, pixels, timestamp, number, domain, received)


def _publish_snapshot(recorder, key, actual, pixels, timestamp, number, domain, received):
    previous = recorder._timing_latest.get(key)
    epoch = previous._timing_epoch if previous is not None else 0
    if previous is not None:
        if (previous._timing_stream is not actual or
                domain != previous._timing_domain or number < previous._timing_number or
                timestamp < previous.timestamp):
            epoch += 1
            recorder._timing_events['clock_or_stream_reset'] += 1
        elif number == previous._timing_number or timestamp == previous.timestamp:
            recorder._timing_events['duplicate_source_frame'] += 1
            return
    actual.last_frame, actual.timestamp = pixels, timestamp
    snapshot = copy.copy(actual)
    snapshot._timing_stream = actual
    snapshot._timing_epoch = epoch
    snapshot._timing_number = number
    snapshot._timing_domain = domain
    snapshot._timing_receive_ns = received
    recorder._timing_latest[key] = snapshot
    recorder._timing_frames.setdefault(key, deque(maxlen=8)).append(snapshot)


def install_cameras(module):
    cls = module.ImageRecorder
    original_init = cls.__init__
    @functools.wraps(original_init)
    def initialize(self, *args, **kwargs):
        self._timing_latest = {}
        self._timing_frames = {}
        self._timing_events = Counter()
        original_init(self, *args, **kwargs)
    code = cls._camera_worker.__code__
    if hashlib.sha256(code.co_code).hexdigest() != '03082c248e40452d48fc0602ac4cf2cb6c5e2cc77c5f05d0ae8668a2b800b047':
        raise RuntimeError('Factory camera worker changed; refusing partial timing patch')
    code = replace_span(code, 2870, 2882, [
        ('LOAD_GLOBAL', '_timing_wait_frames'), ('LOAD_FAST', 'pipeline'),
        ('CALL_FUNCTION', 1), ('STORE_FAST', 'frames')])
    # Remove the vendor's with-lock block around the whole image copy. The
    # replacement publishes under its own short lock; exception paths stay local.
    code = replace_span(code, 3244, 3314, [
        ('LOAD_GLOBAL', '_timing_publish_frame'), ('LOAD_FAST', 'self'),
        ('LOAD_FAST', 'camera_name'), ('LOAD_FAST', 'actual'), ('LOAD_FAST', 'f'),
        ('CALL_FUNCTION', 4), ('POP_TOP', 0)])
    cls._camera_worker.__globals__['_timing_publish_frame'] = publish_frame
    cls._camera_worker.__globals__['_timing_wait_frames'] = wait_frames
    cls._camera_worker.__code__ = code
    cls.__init__ = initialize


def install_env(module):
    cls = module.Real_Env
    original_init = cls.__init__
    @functools.wraps(original_init)
    def initialize(self, *args, **kwargs):
        self._timing_state_lock = threading.Lock()
        self._timing_messages = {k: deque(maxlen=256) for k in ALL_FIELDS}
        self._timing_message_seq = Counter()
        original_init(self, *args, **kwargs)
    cls.__init__ = initialize
    handlers = {f'_{name}_handler': name for name in STATE_FIELDS + ACTION_FIELDS}
    handlers.update({f'_{side}_arm_pose_handler': f'{side}_arm_actual' for side in ('left', 'right')})
    handlers.update({f'_{side}_camera_pose_handler': f'{side}_camera_actual' for side in ('left', 'right', 'head')})
    for name, field in handlers.items():
        original = getattr(cls, name)
        def wrap(callback, field):
            @functools.wraps(callback)
            def receive(self, channel, msg):
                received = time.monotonic_ns()
                # Retain vendor assignments and control behavior exactly.
                callback(self, channel, msg)
                snapshot = copy.deepcopy(msg)
                with self._timing_state_lock:
                    self._timing_message_seq[field] += 1
                    self._timing_messages[field].append(
                        (received, self._timing_message_seq[field], snapshot))
            return receive
        setattr(cls, name, wrap(original, field))
    original_vr = cls._vr_frame_handler
    @functools.wraps(original_vr)
    def receive_vr(self, channel, msg):
        received = time.monotonic_ns()
        original_vr(self, channel, msg)
        with self._timing_state_lock:
            for field in ACTION_POSES:
                self._timing_message_seq[field] += 1
                self._timing_messages[field].append(
                    (received, self._timing_message_seq[field], copy.deepcopy(getattr(self, field))))
    cls._vr_frame_handler = receive_vr


class Sampler:
    def __init__(self, env, record_depth=False, now_ns=None, fps=30):
        self.env = env
        self.recorder = env.image_recorder.rs
        if self.recorder is None or set(CAMERAS) - set(env.image_recorder.list_all()):
            raise RuntimeError('采集时序检查：缺少配置的三路 RealSense 相机，未回退时间源')
        self.kinds = ('color', 'depth') if record_depth else ('color',)
        self.started_ns = time.monotonic_ns() if now_ns is None else now_ns
        self.last_master_ns = self.started_ns
        self.last_used = {}
        self.epochs = {}
        self.stats = Counter()
        self.rejections = []
        self.last_reason = None
        self.last_accepted_ns = self.started_ns
        self.fps = fps
        self.next_due_ns = None
        with self.recorder._lock:
            self.source_events_start = dict(self.recorder._timing_events)

    def _histories(self):
        with self.recorder._lock:
            return {(cam, kind): tuple(self.recorder._timing_frames.get((cam[3:], kind), ()))
                    for cam in CAMERAS for kind in self.kinds}

    @staticmethod
    def identity(frame):
        return frame._timing_epoch, frame._timing_number

    def pick(self, now_ns):
        """Non-blocking attempt. Invalid candidates advance only after deadline."""
        histories = self._histories()
        candidates = [f for f in histories[(MASTER, 'color')]
                      if f._timing_receive_ns > self.last_master_ns]
        if not candidates:
            return None, 'waiting_master'
        main = candidates[0]
        ref = main._timing_receive_ns
        # Keep lower requested recording rates; never manufacture extra samples
        # when the requested rate is higher than the source camera rate.
        source_fps = getattr(main, 'fps', 30)
        if self.fps < source_fps and self.next_due_ns is not None and ref < self.next_due_ns-2_000_000:
            self.last_master_ns = ref
            self.stats['intentional_rate_skip'] += 1
            return None, 'waiting_rate'
        if now_ns-ref > FRAME_AGE_NS:
            self.reject(main, 'master_stale')
            return None, 'master_stale'
        # Recording latency only: allow the following main frame and its wrist
        # candidates to arrive before committing a one-to-one match.
        if now_ns-ref < LOOKAHEAD_NS:
            return None, 'waiting_lookahead'
        following = candidates[1] if len(candidates) > 1 else None
        selected = {}
        for key, frames in histories.items():
            if key == (MASTER, 'color'):
                choice = main
            else:
                fresh = [f for f in frames if self.identity(f) > self.last_used.get(key, (-1, -1))
                         and abs(f._timing_receive_ns-ref) <= SKEW_NS]
                # Wait for a frame at/after the reference, allowing a fair nearest
                # comparison; use preceding frame on bounded-wait expiration.
                if now_ns-ref < WAIT_NS and not any(f._timing_receive_ns >= ref for f in frames):
                    return None, 'waiting_camera:'+key[0]+'/'+key[1]
                if not fresh:
                    if now_ns-ref >= WAIT_NS:
                        self.reject(main, 'camera_missing_or_skew:'+key[0]+'/'+key[1])
                    return None, 'camera_missing_or_skew:'+key[0]+'/'+key[1]
                def cost(frame):
                    # Prefer a valid successor pair over a marginally nearer
                    # current pair. Do not reuse frames or relax the skew limit.
                    successor = following is not None and any(
                        self.identity(other) > self.identity(frame)
                        and other._timing_epoch == frame._timing_epoch
                        and abs(other._timing_receive_ns-following._timing_receive_ns) <= SKEW_NS
                        for other in frames)
                    return (not successor, abs(frame._timing_receive_ns-ref), self.identity(frame))
                choice = min(fresh, key=cost)
            if key in self.epochs and choice._timing_epoch != self.epochs[key]:
                self.reject(main, 'camera_clock_reset')
                return None, 'camera_clock_reset'
            if self.identity(choice) <= self.last_used.get(key, (-1, -1)):
                self.reject(main, 'camera_reused')
                return None, 'camera_reused'
            selected[key] = choice

        messages, message_info = {}, {}
        with self.env._timing_state_lock:
            for field in ALL_FIELDS:
                history = self.env._timing_messages[field]
                # Causal hold on the receive-time axis. Commands may be latched:
                # their age is recorded, not treated as new commands or interpolated.
                prior = next((entry for entry in reversed(history) if entry[0] <= ref), None)
                if prior is None or (field in STATE_FIELDS and ref-prior[0] > STATE_AGE_NS):
                    if field in STATE_FIELDS + ACTION_FIELDS:
                        self.reject(main, 'state_or_action_unavailable:'+field)
                        return None, 'state_or_action_unavailable:'+field
                    messages[field] = None
                    message_info[field] = {'receive_ns': -1, 'seq': -1}
                else:
                    messages[field] = prior[2]
                    message_info[field] = {'receive_ns': prior[0], 'seq': prior[1]}
        images = {cam: {kind: selected[(cam, kind)] for kind in self.kinds} for cam in CAMERAS}
        view = SimpleNamespace(**messages, get_images=lambda: images)
        observation = self.env.__class__.get_observation(view)
        action = self.env.__class__.get_action(view)
        for part, width, name in [('arm',14,'position'), ('effector',2,'position'),
                                  ('waist',3,'position'), ('head',2,'position'), ('base',2,'velocity')]:
            for values in (observation['state'], action):
                value = np.asarray(values[part][name])
                if value.shape != (width,) or not np.isfinite(value).all():
                    self.reject(main, 'invalid_robot_values:'+part)
                    return None, 'invalid_robot_values:'+part
        self.last_master_ns = ref
        self.last_accepted_ns = now_ns
        if self.fps < source_fps:
            period = int(1e9/self.fps)
            self.next_due_ns = ref+period if self.next_due_ns is None else max(self.next_due_ns+period,ref)
        for key, frame in selected.items():
            self.last_used[key] = self.identity(frame)
            self.epochs[key] = frame._timing_epoch
        self.stats['accepted'] += 1
        self.stats['candidates'] += 1
        with self.recorder._lock:
            events = {k: v-self.source_events_start.get(k, 0)
                      for k, v in self.recorder._timing_events.items()}
        observation['_timing'] = {
            'version': VERSION, 'reference_receive_ns': ref,
            'cameras': {cam+'/'+kind: {
                'device_timestamp_ms': f.timestamp, 'receive_ns': f._timing_receive_ns,
                'frame_number': f._timing_number, 'epoch': f._timing_epoch,
                'clock_domain': f._timing_domain}
                for (cam, kind), f in selected.items()},
            'messages': message_info, 'stats': dict(self.stats), 'source_events': events,
            'rejections': self.rejections[:]}
        return (action, observation), None

    def reject(self, main, reason):
        self.last_master_ns = main._timing_receive_ns
        self.stats['candidates'] += 1
        self.stats['rejected'] += 1
        self.stats[reason] += 1
        self.rejections.append(dict(master_frame=int(main._timing_number),
                                    reference_receive_ns=int(main._timing_receive_ns),
                                    device_timestamp_ms=float(main.timestamp), reason=reason))


def prepare(collector, stop_event):
    sampler = getattr(collector, '_timing_sampler', None)
    if sampler is None:
        try:
            sampler = collector._timing_sampler = Sampler(
                collector.env, collector.config.get('record_depth', False), fps=collector.fps)
        except Exception:
            collector._timing_logger.exception('[Timing] 时序采集初始化失败，结束本条采集')
            collector.discard_evt.set()
            return False
    deadline = time.monotonic_ns()+100_000_000
    while not stop_event.is_set() and not collector.discard_evt.is_set():
        try:
            bundle, reason = sampler.pick(time.monotonic_ns())
        except Exception:
            collector._timing_logger.exception('[Timing] 时序采集异常，结束本条采集')
            collector.discard_evt.set()
            return False
        if bundle is not None:
            collector._timing_bundle = bundle
            return True
        if time.monotonic_ns() >= deadline:
            sampler.stats['wait_timeouts'] += 1
            collector._timing_logger.warning('[Timing] 未写入旧帧：%s | %s', reason, dict(sampler.stats))
            if time.monotonic_ns()-sampler.last_accepted_ns > 2_000_000_000:
                collector._timing_logger.error('[Timing] 连续两秒无可对齐新帧，结束本条采集：%s', reason)
                collector.discard_evt.set()
            return False
        stop_event.wait(.002)
    return False


def bundle(collector):
    return collector._timing_bundle


def install_collector(module):
    cls = module.EpisodeDataCollector
    original = cls._capture_episode
    code = original.__code__
    if hashlib.sha256(code.co_code).hexdigest() != '600e3629421f19ebeb38e0bad23a7886f954ea53c70d34554f5751714de0d0e4':
        raise RuntimeError('Factory producer changed; refusing partial timing patch')
    # Replace only the old timer wait. On timeout, return to existing hardware,
    # stop and task-duration checks BEFORE reading/consuming stage buttons.
    code = replace_span(code, 1224, 1262, [
        ('LOAD_GLOBAL', '_timing_prepare'), ('LOAD_DEREF', 'self'),
        ('LOAD_FAST', 'stop_event'), ('CALL_FUNCTION', 2),
        ('POP_JUMP_IF_FALSE', 748//2)])
    code = replace_span(code, 1404, 1424, [
        ('LOAD_GLOBAL', '_timing_bundle'), ('LOAD_DEREF', 'self'),
        ('CALL_FUNCTION', 1), ('UNPACK_SEQUENCE', 2),
        ('STORE_FAST', 'action'), ('STORE_FAST', 'observation')])
    original.__globals__.update(_timing_prepare=prepare, _timing_bundle=bundle)
    original.__code__ = code
    # Four seconds of bounded RGB buffering (~317 MiB at 640x480x3x3),
    # absorbing measured sub-second writer stalls without holding up sampling.
    # The default remains explicitly bounded; sustained overload is still logged.
    module.DEFAULT_DATA_QUEUE_MAXSIZE = 120
    @functools.wraps(original)
    def capture(self, *args, **kwargs):
        self._timing_sampler = None
        self._timing_logger = module.logger
        # Writer checks each saved sample has timing metadata; no silent fallback.
        self._timing_required = True
        return original(self, *args, **kwargs)
    cls._capture_episode = capture


def write_metadata(root, batch, start):
    """Called in the factory's single HDF5 writer thread after the normal write."""
    group = root.require_group('timing')
    group.attrs.update(version=VERSION, master=MASTER, alignment='host_receive_causal_hold',
                       camera_skew_limit_ns=SKEW_NS, state_age_limit_ns=STATE_AGE_NS,
                       sampler_revision=2, lookahead_ns=LOOKAHEAD_NS,
                       action_semantics='latest_received_control_not_confirmed_execution')
    end = start+len(batch)
    def column(name, values):
        arr = np.asarray(values)
        if name not in group:
            if start:
                raise RuntimeError('Timing metadata missing from earlier batches')
            group.create_dataset(name, shape=(0,), maxshape=(None,), dtype=arr.dtype, chunks=True)
        ds = group[name]
        if len(ds) != start:
            raise RuntimeError('Timing metadata row count mismatch')
        ds.resize((end,)); ds[start:end] = arr
    data = [obs['_timing'] for obs, _ in batch]
    column('reference_receive_ns', [d['reference_receive_ns'] for d in data])
    for stream in data[0]['cameras']:
        for field in ('device_timestamp_ms', 'receive_ns', 'frame_number', 'epoch'):
            column('cameras/'+stream+'/'+field, [d['cameras'][stream][field] for d in data])
        group['cameras/'+stream].attrs['clock_domain'] = data[0]['cameras'][stream]['clock_domain']
    for field in ALL_FIELDS:
        for key in ('receive_ns', 'seq'):
            column('messages/'+field+'/'+key, [d['messages'][field][key] for d in data])
    group.attrs['sample_stats'] = json.dumps(data[-1]['stats'], ensure_ascii=False)
    group.attrs['source_events'] = json.dumps(data[-1]['source_events'], ensure_ascii=False)
    group.attrs['rejection_events'] = json.dumps(data[-1].get('rejections', []), ensure_ascii=False)


def probe(env, count=90, timeout=8):
    """Read-only live camera/state sampling. Does not create a collector or files."""
    sampler = Sampler(env, getattr(env, '_record_depth', False))
    deadline = time.monotonic()+timeout
    samples = []
    reasons = Counter()
    while len(samples) < count and time.monotonic() < deadline:
        result, reason = sampler.pick(time.monotonic_ns())
        if result:
            samples.append(result[1]['_timing'])
        elif not reason.startswith('waiting_'):
            reasons[reason] += 1
        time.sleep(.002)
    ts = [s['cameras'][MASTER+'/color']['device_timestamp_ms'] for s in samples]
    return {'timing_version': VERSION, 'alignment': 'host_receive_causal_hold',
            'master': MASTER, 'samples': len(samples), 'stats': dict(sampler.stats),
            'reasons': dict(reasons), 'strictly_increasing': bool(len(ts)>1 and np.all(np.diff(ts)>0)),
            'max_camera_skew_ms': max((abs(c['receive_ns']-s['reference_receive_ns'])/1e6
                for s in samples for c in s['cameras'].values()), default=None),
            'example': samples[-1] if samples else None}
