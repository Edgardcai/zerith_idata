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
        date_default=patch('tasks.default_task_id',return_value=1)
        date_default.start();self.addCleanup(date_default.stop)
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
        port=self.server.add_insecure_port('127.0.0.1:0');self.port=port;self.server.start()
        self.collector=Collector(self.store,ReadyMonitor(),self.runtime,vendor_factory=lambda:Vendor(f'127.0.0.1:{port}'))
        self.collector.apply_scene({'task_id':1,'scene_id':1})
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

    def test_previous_stream_ending_cannot_disconnect_current_session(self):
        self.start()
        before=self.collector.status()
        self.collector._receive('previous-session',iter(()))
        after=self.collector.status()
        self.assertEqual(after['phase'],before['phase'])
        self.assertEqual(after['connected'],before['connected'])
        self.assertEqual(after['session']['id'],before['session']['id'])

    def test_previous_stream_failure_cannot_disconnect_current_session(self):
        self.start()
        before=self.collector.status()
        def old_stream():
            raise RuntimeError('late failure from previous RPC')
            yield
        self.collector._receive('previous-session',old_stream())
        after=self.collector.status()
        self.assertEqual(after['phase'],before['phase'])
        self.assertEqual(after['error'],before['error'])
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
        dest=self.root/'1_scene1';(dest/'task_meta.json').write_text('{"history":true}')
        result=self.collector.start(payload);eventually(lambda:self.collector.accepted)
        self.assertEqual(result['session']['dataset'],str(dest))
        actual=json.loads((self.root/'1_test'/'task_meta.json').read_text())
        self.assertEqual(actual['task_name'],'test');self.assertNotIn('lift_height',actual)
        self.assertEqual(json.loads((dest/'task_meta.json').read_text()),{'history':True})
        episode(self.root/'1_test');eventually(lambda:self.collector.status()['counts']['completed']==1)
        self.assertTrue((dest/'episode_000001').exists());self.collector.end()
        self.assertFalse((self.root/'1_test').exists())
        self.assertEqual(json.loads((dest/'task_meta.json').read_text()),{'history':True})
        backup=self.runtime/f"source_task_meta_{result['session']['id']}.json"
        self.assertEqual(json.loads(backup.read_text()),actual)
        second=self.collector.start({**payload,'lift_height':'0.9'});eventually(lambda:self.collector.accepted)
        self.assertEqual(second['session']['dataset'],str(dest))
        episode(self.root/'1_test','b'*32);eventually(lambda:self.collector.status()['counts']['completed']==1)
        self.assertTrue((dest/'episode_000002').exists())
    def test_date_directories_keep_days_separate_and_preserve_source_mapping(self):
        payload={'task_name':'test','left':'Yili Peach Yogurt','right':'Coca-Cola','lift_height':'0.8'}
        for date in (20260911,20260912):
            self.collector.apply_scene({'task_id':date,'scene_id':1})
            paths=self.collector.start({**payload,'task_id':date})['session']
            eventually(lambda:self.collector.accepted)
            source=self.root/f'{date}_test'
            self.assertEqual(paths['dataset'],str(self.root/f'{date}_scene1'))
            self.assertEqual(paths['source_dataset'],str(source))
            episode(source)
            eventually(lambda:self.collector.status()['counts']['completed']==1)
            meta=json.loads((Path(paths['dataset'])/'episode_000001'/'collection_task.json').read_text())
            self.assertEqual(meta['config']['task_id'],date)
            self.collector.end()
        self.assertEqual(len(self.store.list()),2)

    def test_empty_named_session_cleanup(self):
        result=self.collector.start({'task_name':'test','left':'Left','right':'Right','lift_height':'0.8'})
        eventually(lambda:self.collector.accepted)
        source=Path(result['session']['source_dataset']);dest=Path(result['session']['dataset'])
        self.collector.end()
        self.assertFalse(source.exists());self.assertTrue(dest.is_dir());self.assertFalse((dest/'task_meta.json').exists())
        self.assertEqual(self.collector.status()['phase'],'closed')

    def test_cleanup_preserves_unfinished_and_unknown_files(self):
        for name in ('unfinished','notes.txt'):
            with self.subTest(name=name):
                result=self.collector.start({'task_name':name,'left':'Left','right':'Right','lift_height':'0.8'})
                eventually(lambda:self.collector.accepted)
                source=Path(result['session']['source_dataset'])
                if name=='unfinished':
                    pending=source/('a'*32);pending.mkdir();(pending/'episode.hdf5').write_bytes(b'unfinished data')
                else:(source/name).write_text('keep me')
                self.collector.end()
                self.assertTrue((source/'task_meta.json').exists())
                self.assertTrue((source/('a'*32)/'episode.hdf5').exists() if name=='unfinished' else (source/name).exists())

    def test_cleanup_preserves_changed_metadata_and_symlinks(self):
        for kind in ('changed','symlink'):
            with self.subTest(kind=kind):
                result=self.collector.start({'task_name':kind,'left':'Left','right':'Right','lift_height':'0.8'})
                eventually(lambda:self.collector.accepted)
                source=Path(result['session']['source_dataset']);meta=source/'task_meta.json'
                if kind=='changed':atomic_json(meta,{'task_id':999,'task_name':'another task'})
                else:
                    target=self.root/'external.json';meta.rename(target);meta.symlink_to(target)
                self.collector.end()
                self.assertTrue(meta.exists());self.assertTrue(source.exists())

    def test_cleanup_failure_does_not_fail_end(self):
        self.collector.start({'task_name':'test','left':'Left','right':'Right','lift_height':'0.8'})
        eventually(lambda:self.collector.accepted)
        with patch('collector.atomic_json',side_effect=PermissionError('test backup failure')):
            self.collector.end()
        self.assertEqual(self.collector.status()['phase'],'closed')
        self.assertTrue((self.root/'1_test'/'task_meta.json').exists())

    def test_cleanup_keeps_legacy_source_when_it_is_final_dataset(self):
        source=self.root/'1_test';source.mkdir();atomic_json(source/'task_meta.json',{'task_name':'test'})
        self.assertFalse(self.collector._cleanup_source({'dataset':str(source),'source_dataset':str(source),'config':{'task_id':1,'task_name':'test'}}))
        self.assertTrue((source/'task_meta.json').exists())

    def test_prompts_share_scene_numbering_and_each_episode_keeps_its_task(self):
        def capture(prompt,scene,uid,seq):
            self.collector.apply_scene({'task_id':1,'scene_id':scene})
            paths=self.collector.start({'task_name':prompt,'scene_id':scene})['session']
            eventually(lambda:self.collector.accepted)
            episode(Path(paths['source_dataset']),uid)
            eventually(lambda:self.collector.status()['counts']['completed']==1)
            target=self.root/f'1_scene{scene}'/f'episode_{seq:06d}'
            meta=json.loads((target/'collection_task.json').read_text())
            self.assertEqual(meta['config']['task_name'],prompt)
            self.assertEqual(meta['config']['scene_id'],scene)
            self.collector.end()
            return target
        first=capture('first product',1,'a'*32,1)
        capture('second product',1,'b'*32,2)
        capture('first product',2,'c'*32,1)
        row=next(row for row in self.store.list() if row['path']==str(first))
        self.store.delete(row['id'])
        self.collector.close()
        self.collector=Collector(self.store,ReadyMonitor(),self.runtime,vendor_factory=lambda:Vendor(f'127.0.0.1:{self.port}'))
        self.assertEqual(self.collector.selected_scene['scene_id'],2)
        capture('third product',1,'d'*32,3)
        self.assertEqual({g['dataset']:g['total'] for g in self.store.groups()},
                         {str(self.root/'1_scene1'):3,str(self.root/'1_scene2'):1})

    def test_apply_rejected_during_session_and_start_requires_applied_scene(self):
        with self.assertRaisesRegex(ValueError,'先应用'):
            self.collector.start({'task_name':'test','scene_id':2})
        self.start()
        with self.assertRaisesRegex(ValueError,'结束当前会话'):
            self.collector.apply_scene({'scene_id':2})
        self.assertEqual(self.collector.selected_scene['scene_id'],1)

if __name__=='__main__':unittest.main()
