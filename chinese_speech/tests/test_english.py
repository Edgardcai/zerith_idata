import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import numpy as np
from chinese_speech.asr_service import ChineseASRBackend
from chinese_speech.tts_service import ChineseTTSApplication

class EnglishTests(unittest.TestCase):
    def test_asr_uses_english_qwen_without_chinese_fallback(self):
        backend = ChineseASRBackend()
        backend.qwen = Mock()
        backend.qwen.transcribe.return_value = [SimpleNamespace(text='Hello world.')]
        result = asyncio.run(backend.transcribe_english(b'\x00\x00' * 16000))
        self.assertEqual(result['text'], 'Hello world.')
        self.assertEqual(backend.qwen.transcribe.call_args.kwargs['language'], 'English')
        self.assertEqual(backend.qwen.transcribe.call_args.kwargs['context'], '')
        backend.qwen = None
        with self.assertRaises(RuntimeError):
            asyncio.run(backend.transcribe_english(b'\x00\x00'))

    def test_tts_preserves_english_numbers_and_passes_language(self):
        backend = SimpleNamespace(output_sample_rate=24000)
        calls = []
        def synth(text, speaker, emit, cancelled, language='Chinese'):
            calls.append((text, speaker, language))
            emit(np.zeros(2400), 24000)
        backend.synthesize_stream = synth
        app = ChineseTTSApplication(backend)
        chunks = []
        app.stream_pcm('en-test', 'Save 20% on 2026-09-16.', 'Ryan', chunks.append, language='English')
        self.assertEqual(calls, [('Save 20% on 2026-09-16.', 'Ryan', 'English')])
        self.assertTrue(chunks)
