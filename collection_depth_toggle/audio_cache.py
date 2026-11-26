"""Prepare fixed collector announcements before recording, not at stage edges."""
import functools
import threading
import types
from types import SimpleNamespace


class PreparedAudio:
    def __init__(self, audio_module):
        self.module = audio_module
        self.original = audio_module.play_text
        self.cache = {}
        self.lock = threading.Lock()
        self.broadcast_code = next(c for c in self.original.__code__.co_consts
                                   if isinstance(c, types.CodeType) and c.co_name == '_broadcast_arr')

    def prepare(self, text):
        with self.lock:
            if text in self.cache:
                return
            arrays = []
            # Clone only Python globals. Synthesis/decoding feeds a private sink,
            # never live speakers/VR streams; real module globals remain intact.
            class Sink:
                def put_nowait(self, frame):
                    arrays.append(frame.to_ndarray().copy())
            class Target:
                def __init__(self):
                    self.sync_q = Sink()
            namespace = dict(self.original.__globals__, stream_registry={Target()},
                             registry_lock=threading.Lock(), _play_text_lock=threading.Lock())
            render = types.FunctionType(self.original.__code__, namespace,
                                        closure=self.original.__closure__)
            render(text)
            if not arrays:
                raise RuntimeError('采集提示音预生成失败：' + text)
            self.cache[text] = tuple(arrays)

    def play(self, text):
        arrays = self.cache.get(text)
        if arrays is None:
            # Preserve vendor warning/error announcements outside fixed stages.
            return self.original(text)
        broadcast = types.FunctionType(self.broadcast_code, self.original.__globals__)
        with self.module._play_text_lock:
            pts = 0
            for array in arrays:
                pts = broadcast(array, pts)


def install(collector_module, audio_module):
    prepared = PreparedAudio(audio_module)
    original_init = collector_module.EpisodeDataCollector.__init__

    @functools.wraps(original_init)
    def initialize(self, event_queue, config, env):
        # Constructor precedes the factory capture thread. Failure prevents
        # starting an unprepared collector instead of synthesizing while moving.
        count = int(config.get('subtask_num', 2))
        if not 1 <= count <= 100:
            raise ValueError('采集阶段数量超出提示音预生成范围')
        for text in ['开始采集', '数据采集完成'] + [f'步骤{i}已完成' for i in range(1, count + 1)]:
            prepared.prepare(text)
        original_init(self, event_queue, config, env)

    collector_module.EpisodeDataCollector.__init__ = initialize
    collector_module.play_text = prepared.play
    return prepared
