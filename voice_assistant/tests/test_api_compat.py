import unittest
from unittest.mock import patch
import httpx
import numpy as np
from voice_assistant.config import ApiProfile
from voice_assistant.llm import ResponsesConversation, LlmError, Message
from voice_assistant.asr import OpenAICompatibleASR, SpeechRecognitionError
from voice_assistant.tts import OpenAICompatibleTTS, SpeechSynthesisError

class ApiCompatibilityTests(unittest.TestCase):
    def test_stream_http_error_remains_readable(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(
            400, json={'error': {'message': 'unsupported reasoning effort'}}))
        client = httpx.Client(transport=transport)
        with patch('voice_assistant.llm.httpx.Client', return_value=client):
            llm = ResponsesConversation([ApiProfile('test','http://localhost','test-secret')], 'test')
            with self.assertRaisesRegex(LlmError, 'unsupported reasoning effort'):
                list(llm.stream_reply([Message('user','test')]))

    def test_missing_cloud_key_never_sends_audio_requests(self):
        def unexpected(request):
            self.fail('unconfigured cloud credential must not be sent')
        with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
            asr = OpenAICompatibleASR('http://localhost', '', client=client)
            tts = OpenAICompatibleTTS('http://localhost', '', 'tts', 'tts', 'voice', client=client)
            with self.assertRaises(SpeechRecognitionError):
                asr.transcribe(np.zeros(16000,dtype=np.float32))
            with self.assertRaises(SpeechSynthesisError):
                tts.synthesize('test')

    def test_local_english_client_sends_language_and_speaker(self):
        from voice_assistant.chinese_speech import ChineseSpeechClient
        import json
        requests = []
        def handle(request):
            requests.append(request)
            if request.url.path == '/v1/transcribe':
                return httpx.Response(200,json={'text':'Hello world.'})
            return httpx.Response(200,content=b'\x00\x00'*100,
                                  headers={'X-Audio-Sample-Rate':'24000'})
        client = ChineseSpeechClient('http://localhost:8770','http://localhost:8771',
                                     language='en',speaker='Ryan')
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(handle))
        try:
            self.assertEqual(client.transcribe(np.zeros(16000),16000), 'Hello world.')
            self.assertIn(b'name="language"\r\n\r\nen', requests[0].content)
            self.assertTrue(b''.join(client.stream_speech('Hello world.')))
            body = json.loads(requests[1].content)
            self.assertEqual(body['language'], 'English')
            self.assertEqual(body['speaker'], 'Ryan')
        finally:
            client.close()
