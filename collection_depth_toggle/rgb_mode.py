"""Per-session depth capture, default off. Camera operations never command motors."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import functools
import json
from pathlib import Path
import threading
import time


def depth_option(config):
    value = config.get('record_depth', False)
    if type(value) is not bool:
        raise ValueError('record_depth 必须为 true 或 false')
    return value


def selected_streams(streams, enabled):
    kinds = {'color', 'depth'} if enabled else {'color'}
    result = [s for s in streams if s.type in kinds]
    if {s.type for s in result} != kinds:
        raise ValueError('相机未配置所需的彩色/深度流')
    return result


def install_cameras(module):
    cls = module.ImageRecorder
    original_init = cls.__init__

    @functools.wraps(original_init)
    def initialize(self, camera_specs=None):
        if not camera_specs:
            raise ValueError('请选择明确的相机配置')
        self._depth_specs = {s.name: s for s in camera_specs}
        self._depth_mode = False
        self._depth_mode_lock = threading.Lock()
        specs = [replace(s, align_to=None, desired_streams=selected_streams(s.desired_streams, False))
                 for s in camera_specs]
        original_init(self, camera_specs=specs)
        module.logger.info('[RecordingDepth] 默认仅启用 color；按会话选择是否启用 depth')
    cls.__init__ = initialize


def configure_depth(env, enabled, timeout=15):
    recorder = env.image_recorder.rs
    if recorder is None:
        raise ValueError('没有可切换的 RealSense 相机')
    with recorder._depth_mode_lock:
        plans = {name: selected_streams(spec.desired_streams, enabled)
                 for name, spec in recorder._depth_specs.items()}
        if recorder._depth_mode is not enabled:
            # Mark a partial transition as unknown so a failure forces rollback.
            recorder._depth_mode = None
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(recorder.reinit_camera, name, streams=streams)
                           for name, streams in plans.items()]
                for future in futures:
                    if not future.result():
                        raise RuntimeError('相机切换未提交成功')
        deadline = time.monotonic()+timeout
        expected = {'color', 'depth'} if enabled else {'color'}
        while True:
            ready = True
            for name in plans:
                with recorder._lock:
                    state = recorder.valid_cameras.get(name)
                    streams = getattr(state, 'actual_streams', ())
                    matched = state is not None and state.is_matched()
                if ({s.type for s in streams} != expected or not matched or
                        any(s.last_frame is None or not s.last_frame.size for s in streams)):
                    ready = False
                    break
            if ready:
                recorder._depth_mode = enabled
                env._record_depth = enabled
                return {'record_depth': enabled, 'cameras': sorted(plans),
                        'streams': sorted(expected), 'frames_ready': True}
            if time.monotonic() >= deadline:
                raise RuntimeError('等待相机彩色/深度帧超时，未启动录制')
            time.sleep(.05)


def get_images(self):
    kinds = ('color', 'depth') if getattr(self, '_record_depth', False) else ('color',)
    images = {}
    for camera in self.image_recorder.list_all():
        images[camera] = {}
        for kind in kinds:
            frame = self.image_recorder.get_image(camera, kind)
            if frame:
                images[camera][kind] = frame
    return images


def install_collector(module, env_module):
    original_init = module.EpisodeDataCollector.__init__
    @functools.wraps(original_init)
    def initialize(self, event_queue, config, env):
        enabled = depth_option(config)
        if enabled != getattr(env, '_record_depth', False):
            raise RuntimeError('相机模式与录制选择不一致')
        original_init(self, event_queue, config, env)
        path = Path(self._dataset_dir)/'task_meta.json'
        metadata = json.loads(path.read_text())
        metadata['record_depth'] = enabled
        temporary = path.with_name('task_meta.depth.tmp')
        temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
        temporary.replace(path)
    module.EpisodeDataCollector.__init__ = initialize
    env_module.Real_Env.get_images = get_images


async def apply_mode(env, enabled):
    task = asyncio.create_task(asyncio.to_thread(configure_depth, env, enabled))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Do not let camera restart work race with rollback when a client leaves.
        await task
        raise


def install_service(module):
    original = module.RobotService.MetaTransfer
    @functools.wraps(original)
    async def meta(self, request, context):
        if not hasattr(self, '_depth_session_lock'):
            self._depth_session_lock = asyncio.Lock()
        if self._depth_session_lock.locked() or self.collector_poll is not None:
            context.set_code(module.grpc.StatusCode.ALREADY_EXISTS)
            context.set_details('MetaTransfer already running')
            return
        async with self._depth_session_lock:
            configured = False
            try:
                config = json.loads(request.json_config)
                enabled = depth_option(config)
                configured = True
                result = await apply_mode(self.hub, enabled)
                module.logger.info('[RecordingDepth] 会话模式就绪: record_depth=%s', enabled)
                # Diagnostic checks cameras only, without creating a collector,
                # writing task files, starting capture, or sending robot commands.
                if config.get('_probe_depth_mode') is True:
                    yield module.pb.MetaData(json_data=json.dumps(result))
                    return
                stream = original(self, request, context)
                try:
                    async for reply in stream:
                        yield reply
                finally:
                    await stream.aclose()
            except (ValueError, RuntimeError) as exc:
                context.set_code(module.grpc.StatusCode.FAILED_PRECONDITION)
                context.set_details(str(exc))
            finally:
                if configured:
                    try:
                        await apply_mode(self.hub, False)
                    except Exception:
                        module.logger.exception('[RecordingDepth] 会话结束后恢复彩色模式失败')
    module.RobotService.MetaTransfer = meta
