from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from chinese_speech.qwen_streaming import QwenCodecStreamer, SynthesisCancelled


class Codes:
    def __init__(self, frame):
        self.values = np.full((1, 16), frame, dtype=np.int64)

    def detach(self):
        return self

    def __getitem__(self, key):
        return self.values[key]


class FakeModel:
    def __init__(self, frames=19):
        self.frames = frames
        self.generated = 0
        self.hook = None
        self.removed = False
        self.model = SimpleNamespace(
            talker=SimpleNamespace(register_forward_hook=self.register),
            config=SimpleNamespace(talker_config=SimpleNamespace(codec_eos_token_id=2150)),
            speech_tokenizer=SimpleNamespace(decode=self.decode),
        )

    def register(self, hook):
        self.hook = hook
        return SimpleNamespace(remove=self.remove)

    def remove(self):
        self.hook = None
        self.removed = True

    @staticmethod
    def decode(items):
        codes = items[0]["audio_codes"]
        return [np.repeat(codes[:, 0].astype(np.float32), 1920)], 24000

    def generate_custom_voice(self, **_kwargs):
        self.hook(None, None, SimpleNamespace(hidden_states=(None, None)))
        for frame in range(self.frames):
            self.generated += 1
            self.hook(None, None, SimpleNamespace(hidden_states=(None, Codes(frame))))
        return [np.repeat(np.arange(self.frames, dtype=np.float32), 1920)], 24000


class CodecStreamingTests(unittest.TestCase):
    def setUp(self):
        fake_torch = SimpleNamespace(
            inference_mode=nullcontext,
            cat=lambda frames, dim: np.concatenate([f.values for f in frames], axis=dim),
        )
        self.torch_patch = patch.dict("sys.modules", {"torch": fake_torch})
        self.torch_patch.start()
        self.addCleanup(self.torch_patch.stop)

    def test_emits_before_generation_finishes_and_keeps_every_sample_once(self):
        model = FakeModel()
        pieces, generated_at_emit = [], []
        def emit(audio, rate):
            self.assertEqual(rate, 24000)
            pieces.append(audio)
            generated_at_emit.append(model.generated)
        QwenCodecStreamer(model).synthesize("测试", "Serena", emit, lambda: False)
        self.assertEqual(generated_at_emit, [8, 16, 19])
        self.assertEqual([len(p) for p in pieces], [15360, 15360, 5760])
        np.testing.assert_array_equal(np.concatenate(pieces), np.repeat(np.arange(19), 1920))
        self.assertTrue(model.removed)

    def test_cancel_interrupts_generation_and_removes_hook(self):
        model = FakeModel()
        emitted = []
        with self.assertRaises(SynthesisCancelled):
            QwenCodecStreamer(model).synthesize("测试", "Serena", lambda *_: emitted.append(True), lambda: bool(emitted))
        self.assertLess(model.generated, model.frames)
        self.assertTrue(model.removed)

    def test_disconnected_consumer_removes_hook(self):
        model = FakeModel()
        with self.assertRaises(BrokenPipeError):
            QwenCodecStreamer(model).synthesize("测试", "Serena", Mock(side_effect=BrokenPipeError), lambda: False)
        self.assertTrue(model.removed)

    def test_short_audio_is_not_lost(self):
        model = FakeModel(frames=3)
        pieces = []
        QwenCodecStreamer(model).synthesize("你好", "Serena", lambda audio, _rate: pieces.append(audio), lambda: False)
        self.assertEqual(len(pieces), 1)
        self.assertEqual(len(pieces[0]), 3 * 1920)
