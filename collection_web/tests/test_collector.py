import concurrent.futures
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import grpc
from protocol import Vendor,MetaRequest,MetaData
from collector import Collector
from episodes import EpisodeStore,atomic_json
from test_core import episode

class ReadyMonitor:
    def snapshot(self):return {'checks':[{'key':'fake','label':'测试设备','ok':True,'detail':'fixture'}]}

def eventually(fn):
    end=time.monotonic()+3
    while time.monotonic()<end:
        if fn():return
        time.sleep(.03)
    raise AssertionError('condition did not become true')

class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'data';self.root.mkdir()
        self.runtime=Path(self.tmp.name)/'runtime';self.store=EpisodeStore(self.runtime,self.root)
        self.cancelled=threading.Event();self.fail_event=threading.Event()
        def meta(request,context):
            config=json.loads(request.json_config);p=self.root/f"{config['task_id']}_{config['task_name']}";p.mkdir(exist_ok=True);atomic_json(p/'task_meta.json',config)
            context.add_callback(self.cancelled.set)
            yield MetaData(json_data='{"message":"accepted"}')
            while context.is_active():
                if self.fail_event.is_set():context.abort(grpc.StatusCode.UNAVAILABLE,'simulated transport loss')
                time.sleep(.02)
        self.server=grpc.server(concurrent.futures.ThreadPoolExecutor(max_workers=2))
        h=grpc.unary_stream_rpc_method_handler(meta,request_deserializer=MetaRequest.FromString,response_serializer=MetaData.SerializeToString)
        self.server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler('robot.RobotService',{'MetaTransfer':h}),))
        port=self.server.add_insecure_port('127.0.0.1:0');self.server.start()
        self.collector=Collector(self.store,ReadyMonitor(),self.runtime,vendor_factory=lambda:Vendor(f'127.0.0.1:{port}'))
    def tearDown(self):self.collector.close();self.store.close();self.server.stop(0).wait();self.tmp.cleanup()
    def start(self):
        self.collector.start({'task_name':'test'});eventually(lambda:self.collector.status()['accepted'])
    def test_submit_ack_then_cancel(self):
        self.start();self.assertEqual(self.collector.status()['phase'],'waiting')
        self.collector.end();self.assertTrue(self.cancelled.wait(2));self.assertEqual(self.collector.status()['phase'],'closed')
        with self.assertRaises(ValueError):self.collector.preflight({'task_name':'bad/path'})
    def test_depth_choice_reaches_rpc_and_session_metadata(self):
        self.collector.start({'task_name':'test','record_depth':True})
        eventually(lambda:self.collector.status()['accepted'])
        self.assertIs(self.collector.status()['session']['config']['record_depth'],True)
        actual=json.loads((self.root/'1_test'/'task_meta.json').read_text())
        self.assertIs(actual['record_depth'],True)
        self.collector.end();self.start()
        self.assertIs(self.collector.status()['session']['config']['record_depth'],False)
    def test_default_two_stages_and_live_progress(self):
        self.start()
        actual=json.loads((self.root/'1_test'/'task_meta.json').read_text())
        self.assertEqual(actual['subtask_num'],2)
        self.assertEqual(actual['step_list'],[])
        p=episode(self.root/'1_test',finished=False)
        (p/'collection.log').write_text('采集会话启动\n')
        eventually(lambda:self.collector.status()['current'] is not None)
        uid=self.collector.status()['current']['uuid']
        with self.collector.lock:
            self.collector.responses.append({'at':time.time(),'value':{
                'event_type':'EPISODE_PROGRESS','uid':uid,'completed_subtask_index':1}})
        status=self.collector.status()
        self.assertEqual(status['current']['progress']['completed_steps'],1)
        self.assertEqual(status['current']['state'],'recording')
        self.assertEqual(status['phase'],'waiting')
    def test_duplicate_start_and_active_end_rejected(self):
        self.start()
        with self.assertRaises(ValueError):self.collector.start({'task_name':'other'})
        p=episode(self.root/'1_test',finished=False)
        with self.assertRaises(ValueError):self.collector.end()
        self.assertTrue(p.exists());self.assertEqual(self.collector.status()['phase'],'waiting')
    def test_connection_loss_does_not_reconnect_or_claim_external_episode(self):
        self.start();self.fail_event.set();eventually(lambda:self.collector.status()['phase']=='disconnected')
        episode(self.root/'1_test');time.sleep(.7)
        self.assertEqual(self.store.list(),[])
        self.assertEqual(self.collector.status()['phase'],'disconnected')
    def test_completed_episode_and_no_watch_after_end(self):
        self.start();episode(self.root/'1_test');eventually(lambda:self.collector.status()['counts']['completed']==1)
        self.collector.end();episode(self.root/'1_test','b'*32);time.sleep(.7)
        self.assertEqual(len(self.store.list()),1)
        self.assertTrue((self.root/'1_test'/('b'*32)).exists())
    def test_synchronous_start_failure_can_retry(self):
        with patch.object(self.collector,'vendor_factory',side_effect=RuntimeError('transport construction failed')):
            with self.assertRaises(RuntimeError):self.collector.start({'task_name':'test'})
        self.assertEqual(self.collector.status()['phase'],'rejected')
        self.start();self.assertTrue(self.collector.status()['connected'])
    def test_named_destination_preserves_rpc_and_source_ack(self):
        payload={'task_name':'test','left':'Left Tea','right':'Right Tea','lift_height':'0.80'}
        dest=self.root/'LeftTea_RightTea_0.8';dest.mkdir();(dest/'task_meta.json').write_text('{"history":true}')
        result=self.collector.start(payload);eventually(lambda:self.collector.accepted)
        self.assertEqual(result['session']['dataset'],str(dest))
        actual=json.loads((self.root/'1_test'/'task_meta.json').read_text())
        self.assertEqual(actual['task_name'],'test');self.assertNotIn('lift_height',actual)
        self.assertEqual(json.loads((dest/'task_meta.json').read_text()),{'history':True})
        episode(self.root/'1_test');eventually(lambda:self.collector.status()['counts']['completed']==1)
        self.assertTrue((dest/'episode_000001').exists());self.collector.end()
        second=self.collector.start({**payload,'lift_height':'0.9'});eventually(lambda:self.collector.accepted)
        self.assertTrue(second['session']['dataset'].endswith('LeftTea_RightTea_0.9'))
        episode(self.root/'1_test','b'*32);eventually(lambda:self.collector.status()['counts']['completed']==1)
        self.assertTrue((self.root/'LeftTea_RightTea_0.9'/'episode_000001').exists())

if __name__=='__main__':unittest.main()
