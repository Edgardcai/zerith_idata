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
