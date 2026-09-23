from __future__ import annotations

import asyncio
import http.client
import json
import threading
import unittest

import numpy as np

from chinese_speech.asr_service import ChineseASRBackend, StreamState
from chinese_speech.tts_service import ChineseTTSApplication, ChineseTTSServer


class FakeGenerateModel:
    def __init__(self, values):
        self.values = list(values)

    def generate(self, **_kwargs):
        value = self.values.pop(0) if self.values else ""
        if isinstance(value, list):
            return [{"value": value}]
        return [{"text": value}]


class ChineseASRServiceTests(unittest.TestCase):
    def test_partial_then_paraformer_final_without_qwen(self) -> None:
        async def scenario():
            backend = ChineseASRBackend()
            backend.online = FakeGenerateModel(["小达", "前进"])
            backend.vad = FakeGenerateModel([[]])
            backend.punc = FakeGenerateModel(["小达前进。"])
            backend.qwen = None
            backend.hotwords = ["小达"]
            state = StreamState()
            partial, ended = await backend.feed(state, b"\x00\x00" * 4800)
            self.assertEqual(partial, "小达")
            self.assertFalse(ended)
            result = await backend.finalize(state)
            self.assertEqual(result["text"], "小达前进。")
            self.assertEqual(result["source"], "paraformer")

        asyncio.run(scenario())


class FakeTTSBackend:
    output_sample_rate = 24000
    default_speaker = "Serena"

    def synthesize(self, _text, _speaker):
        return np.linspace(-0.2, 0.2, 2400, dtype=np.float32), 24000


class ChineseTTSServiceTests(unittest.TestCase):
    def test_pcm_is_available_before_generation_finishes(self) -> None:
        emitted, resume, completed = threading.Event(), threading.Event(), threading.Event()
        class Backend(FakeTTSBackend):
            def synthesize_stream(self, text, speaker, emit, cancelled):
                emit(np.full(2400, 0.25, dtype=np.float32), 24000)
                emitted.set()
                if not resume.wait(2):
                    raise RuntimeError("consumer did not receive the first audio")
                emit(np.full(2400, 0.25, dtype=np.float32), 24000)
                completed.set()
        app = ChineseTTSApplication(Backend())
        server = ChineseTTSServer(("127.0.0.1", 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            connection.request("POST", "/v1/speech", body=json.dumps({"request_id": "stream", "text": "测试语音"}), headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            first = response.read(1024)
            self.assertTrue(first)
            self.assertTrue(emitted.wait(1))
            self.assertFalse(completed.is_set())
            resume.set()
            audio = first + response.read()
            self.assertEqual(len(audio), 9600)
            # Internal packet boundaries must not introduce fades or missing samples.
            self.assertTrue(np.all(np.frombuffer(audio, dtype="<i2") == 8191))
        finally:
            resume.set()
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_presegmented_text_is_not_split_twice(self) -> None:
        backend = FakeTTSBackend()
        calls = []
        def synthesize(text, speaker):
            calls.append(text)
            return np.zeros(2400, dtype=np.float32), 24000
        backend.synthesize = synthesize
        app = ChineseTTSApplication(backend)
        text = "这是一段已经由上游分好的中文。它应当交给模型一次完成生成。"
        app.stream_pcm("prepared", text, "Serena", lambda _pcm: None, presegmented=True)
        self.assertEqual(calls, [text])

    def test_cancel_during_stream_releases_model_for_next_request(self) -> None:
        class Backend(FakeTTSBackend):
            def synthesize_stream(self, text, speaker, emit, cancelled):
                from chinese_speech.qwen_streaming import SynthesisCancelled
                for _ in range(3):
                    if cancelled():
                        raise SynthesisCancelled()
                    emit(np.zeros(2400, dtype=np.float32), 24000)
        app = ChineseTTSApplication(Backend())
        chunks = []
        def consume(pcm):
            chunks.append(pcm)
            app.cancel("cancelled")
        app.stream_pcm("cancelled", "测试", "Serena", consume)
        self.assertEqual(len(chunks), 1)
        self.assertTrue(app.last_metrics["cancelled"])
        app.stream_pcm("next", "下一句", "Serena", chunks.append)
        self.assertEqual(len(chunks), 4)
        self.assertFalse(app.last_metrics["cancelled"])

    def test_chunked_pcm_response(self) -> None:
        server = ChineseTTSServer(("127.0.0.1", 0), ChineseTTSApplication(FakeTTSBackend()))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            payload = json.dumps(
                {"request_id": "test", "text": "你好，我是小达。", "language": "Chinese"}
            ).encode()
            connection.request(
                "POST", "/v1/speech", body=payload, headers={"Content-Type": "application/json"}
            )
            response = connection.getresponse()
            audio = response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("X-Audio-Sample-Rate"), "24000")
            self.assertEqual(len(audio), 4800)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
