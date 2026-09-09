"""RGB-only camera acquisition and writer mode; no robot control commands."""
from dataclasses import replace
import functools
import types


def color_streams(streams):
    result = [s for s in streams if s.type == 'color']
    if not result:
        raise ValueError('RGB-only mode requires a configured color stream')
    return result


def color_specs(specs):
    if specs is None:
        raise ValueError('RGB-only mode requires explicit camera configuration')
    return [replace(s, align_to=None, desired_streams=color_streams(s.desired_streams))
            for s in specs]


def install_cameras(module):
    cls = module.ImageRecorder
    original_init, original_reinit = cls.__init__, cls.reinit_camera

    @functools.wraps(original_init)
    def initialize(self, camera_specs=None):
        specs = color_specs(camera_specs)
        module.logger.info('[RGBOnly] 相机只启用 color；depth/IR 和深度对齐已关闭')
        original_init(self, camera_specs=specs)

    @functools.wraps(original_reinit)
    def reinitialize(self, camera_name, streams=None):
        if streams:
            streams = color_streams(streams)
        return original_reinit(self, camera_name, streams=streams)

    cls.__init__, cls.reinit_camera = initialize, reinitialize


def get_color_images(self):
    images = {}
    for camera in self.image_recorder.list_all():
        color = self.image_recorder.get_image(camera, 'color')
        images[camera] = {'color': color} if color else {}
    return images


def install_collector(module, env_module):
    method = module.EpisodeDataCollector._capture_episode
    changes = []
    def change(code):
        values = []
        for value in code.co_consts:
            if isinstance(value, types.CodeType):
                value = change(value)
            elif code.co_name == 'save_worker' and value == ('color', 'depth', 'ir1', 'ir2'):
                value = ('color',)
                changes.append(code.co_name)
            values.append(value)
        return code.replace(co_consts=tuple(values))
    replacement = change(method.__code__)
    if changes != ['save_worker']:
        raise RuntimeError('Factory image type layout changed')
    method.__code__ = replacement
    env_module.Real_Env.get_images = get_color_images
    module.logger.info('[RGBOnly] 采集输入与文件写入仅保留 color，关节及时间戳不变')
