"""Local Responses fixture; the suite must never spend real inference credits."""
import json
import os
import sys
import tempfile
from pathlib import Path

# The test suite must never initialize the production database.
_test_runtime = tempfile.TemporaryDirectory(prefix="dataqc-tests-")
os.environ["DATAQC_HOME"] = _test_runtime.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vendor"))
import re
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import pytest
from dataqc import config
from dataqc.motion_review import CRITERIA

@pytest.fixture(scope='session')
def local_model_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            kind=payload['text']['format']['name']
            if kind in ('MotionReview','BatchMotionReview'):
                result=dict(summary='测试数值分析完成，物理风险不可由轨迹排除',findings=[
                    dict(criterion=c,status='not_observable' if c.endswith('风险') else 'pass',reason='根据提供的指标',
                        evidence_ids=[] if c.endswith('风险') else ['trajectory:summary']) for c in CRITERIA])
                if kind=='BatchMotionReview':
                    packet=json.loads(payload['input'][1]['content'][1]['text'])
                    result=dict(episodes=[dict(episode_id=e['episode_id'],needs_detail=False,summary=result['summary'],
                        statuses=[f['status'] for f in result['findings']],evidence_ids=['trajectory:summary'],issues=[]) for e in packet['episodes']])
            elif kind=='MatchingReview':
                content=payload['input'][1]['content'];text=content[0]['text']
                entries=json.loads(text[text.index('[{'):])
                result=dict(hands=[dict(hand=e['hand'],status='pass',observed_item=e['expected'],reason='fixture image match',
                    evidence_ids=[f"cam_{e['hand']}_wrist:{e['moments'][1]['frame']}"]) for e in entries])
            else:
                self.send_error(400,'Unexpected test schema');return
            data=json.dumps(dict(status='completed',output=[dict(content=[dict(type='output_text',text=json.dumps(result))])],
                usage=dict(input_tokens=10,output_tokens=10,total_tokens=20))).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield server.server_port
    server.shutdown();server.server_close();thread.join()

@pytest.fixture(autouse=True)
def local_model_credentials(tmp_path,monkeypatch,local_model_server):
    path=tmp_path/'api.txt'
    path.write_text(f'base_url="http://127.0.0.1:{local_model_server}/v1"\nOPENAI_API_KEY="fixture"\n')
    monkeypatch.setattr(config,'CONFIG',tmp_path/'test-settings.json')
    monkeypatch.setitem(config.DEFAULTS,'api_file',str(path))
