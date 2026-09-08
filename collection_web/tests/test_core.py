import concurrent.futures
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import sys
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import grpc
import h5py
import numpy as np
from protocol import Vendor, MetaRequest, MetaData
from episodes import EpisodeStore, atomic_json, validate_finished
from tasks import validate_task,parse_targets,dataset_name,DEFAULT_PROMPT
from device_monitor import DeviceMonitor
from collector import Collector

UUID='a'*32

def episode(dataset,uid=UUID,finished=True):
    p=dataset/uid;p.mkdir(parents=True)
    with h5py.File(p/'episode.hdf5','w') as f:
        f.attrs.update(total_frames=8,total_subtasks=2,completed_subtasks=2,control_frequency=30)
        f.create_dataset('timestamp/t',data=1788851000000+np.arange(8)*1000/30)
        for prefix in ('action','observation/state'):
            for part,width in [('arm/position',14),('effector/position',2),('waist/position',3),('head/position',2),('base/velocity',2)]:
                f.create_dataset(prefix+'/'+part,data=np.zeros((8,width)))
        for name in ['cam_high','cam_left_wrist','cam_right_wrist']:
            f.create_dataset('observation/images/rs/'+name+'/color',data=np.zeros((8,1),dtype='u1'))
            path=p/'videos'/'rs';path.mkdir(parents=True,exist_ok=True);(path/(name+'.mp4')).write_bytes(b'0'*100)
    atomic_json(p/'episode_meta.json',{'episode_id':uid,'step_index':[{'start_frame_id':0,'end_frame_id':3},{'start_frame_id':4,'end_frame_id':7},{'start_frame_id':8,'end_frame_id':7}]})
    (p/'collection.log').write_text('采集会话启动\n子任务 1 保存完毕 | 分界帧: 4\n[采集结束]\n'+('子任务 2 保存完毕 | 分界帧: 8\n采集流结束\n' if finished else ''))
    return p

class TaskTests(unittest.TestCase):
    def test_directory_name_height_and_unchanged_vendor_config(self):
        original,targets=validate_task({'task_name':DEFAULT_PROMPT})
        config,with_height=validate_task({'task_name':DEFAULT_PROMPT,'lift_height':'00.800'})
        self.assertEqual(config,original)
        self.assertEqual(dataset_name(with_height,with_height['lift_height']),('DahongpaoMilkTea_Ifcoconut_0.8','0.8'))
        self.assertEqual(dataset_name({'left':'左 商品','right':'右商品'},'0')[0],'左商品_右商品_0')
        for height in ['-1','NaN','Infinity','1e2','../0.8',True,'0.1234567']:
            with self.subTest(height=height),self.assertRaises(ValueError):validate_task({'task_name':DEFAULT_PROMPT,'lift_height':height})
        for target in ['','../tea','a/b','a_b','a\x00b']:
            with self.subTest(target=target),self.assertRaises(ValueError):validate_task({'task_name':DEFAULT_PROMPT,'left':target,'lift_height':'0.8'})
    def test_targets_and_descriptions(self):
        prompt='Grasp Dahongpao Milk Tea with the left hand and then grasp If coconut with the right hand'
        config,targets=validate_task({'task_name':prompt})
        self.assertEqual(targets,{'left':'Dahongpao Milk Tea','right':'If coconut'})
        self.assertNotIn('AD Calcium',config['action_desc']);self.assertIn('If coconut',config['scene_desc'])
    def test_task_path_and_numeric_validation(self):
        for payload in [{'task_name':'../bad'},{'task_name':'a/b'},{'task_name':'ok','frequency':float('nan')},{'task_name':'ok','subtask_num':True},{'task_name':'ok','frequency':30.5}]:
            with self.subTest(payload=payload),self.assertRaises(ValueError):validate_task(payload)
        self.assertEqual(parse_targets('picotest'),{'left':'','right':''})

class StoreTests(unittest.TestCase):
    def routed(self):
        destination=self.root/'Left_Right_0.8';destination.mkdir()
        baseline=self.store.new_session('routed',{'task_name':'original prompt'},{'left':'Left','right':'Right','lift_height':'0.8'},destination,self.dataset)
        return {'id':'routed','dataset':str(destination),'source_dataset':str(self.dataset),'baseline':baseline}
    def test_routed_episode_moves_only_after_final_signal(self):
        old=episode(self.dataset,'b'*32)
        session=self.routed();p=episode(self.dataset,finished=False)
        self.store.observe(session);self.assertTrue(p.exists());self.assertFalse((Path(session['dataset'])/'episode_000001').exists())
        with (p/'collection.log').open('a') as f:f.write('子任务 2 保存完毕 | 分界帧: 8\n采集流结束\n')
        self.store.observe(session);row=self.store.list()[0];dest=Path(row['path'])
        self.assertEqual(dest,Path(session['dataset'])/'episode_000001');self.assertFalse(p.exists());self.assertTrue(old.exists())
        metadata=json.loads((dest/'collection_task.json').read_text());self.assertEqual(metadata['targets']['lift_height'],'0.8');self.assertEqual(metadata['config']['task_name'],'original prompt')
        self.store.observe(session);self.assertEqual(len(self.store.list()),1)
        self.store.rate(row['id'],'F');self.assertEqual(json.loads((dest/'review.json').read_text())['grade'],'F')
        self.store.delete(row['id']);self.assertFalse(dest.exists());self.assertTrue(old.exists())
    def test_routed_numbering_skips_existing_and_survives_restart(self):
        session=self.routed();dest=Path(session['dataset']);(dest/'episode_000009').mkdir()
        episode(self.dataset);self.store.observe(session);self.assertEqual(self.store.list()[0]['seq'],10)
        self.store.close();self.store=EpisodeStore(self.runtime,self.root)
        self.assertEqual(self.store.last_session()['source_dataset'],str(self.dataset))
        episode(self.dataset,'b'*32);self.store.observe(session);self.assertEqual(self.store.list()[0]['seq'],11)
        self.assertTrue((dest/'episode_000009').exists())
    def test_routed_crash_after_move_recovers(self):
        session=self.routed();episode(self.dataset)
        with patch.object(self.store,'_finish_index',side_effect=RuntimeError('crash')):self.store.observe(session)
        self.store.close();self.store=EpisodeStore(self.runtime,self.root)
        row=self.store.list()[0];self.assertEqual(row['state'],'completed')
        self.assertTrue((Path(row['path'])/'collection_task.json').exists())
    def test_routed_move_failure_preserves_original(self):
        session=self.routed();p=episode(self.dataset)
        with patch.object(Path,'rename',side_effect=OSError('cross-device or permissions')):self.store.observe(session)
        self.assertTrue(p.exists());self.assertEqual(self.store.list()[0]['state'],'saving')
        self.assertFalse((Path(session['dataset'])/'episode_000001').exists())
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'data';self.root.mkdir();self.runtime=Path(self.tmp.name)/'runtime'
        self.dataset=self.root/'1_test';self.dataset.mkdir()
        self.store=EpisodeStore(self.runtime,self.root)
        self.session={'id':'session','dataset':str(self.dataset),'baseline':[],'config':{'subtask_num':2}}
        self.store.new_session('session',{'subtask_num':2},{},self.dataset)
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def test_finalization_number_rating_and_delete(self):
        original=episode(self.dataset);events=self.store.observe(self.session)
        r=self.store.list()[0];self.assertEqual(r['state'],'completed');self.assertEqual(r['grade'],'A');self.assertFalse(original.exists())
        path=self.dataset/'episode_000001';self.assertTrue(path.exists());self.assertEqual(len(r['detail']['steps']),2)
        self.assertAlmostEqual(r['detail']['actual_hz'],30,places=3)
        self.store.observe(self.session);self.assertEqual(len(self.store.list()),1)
        self.store.rate(r['id'],'B');self.assertEqual(json.loads((path/'review.json').read_text())['grade'],'B')
        self.store.rate(r['id'],'F');self.assertEqual(self.store.list()[0]['grade'],'F')
        self.store.delete(r['id']);self.assertFalse(path.exists());self.assertEqual(self.store.list()[0]['state'],'deleted')
        episode(self.dataset,'b'*32);self.store.observe(self.session);self.assertEqual(self.store.list()[0]['seq'],2)
    def test_active_cannot_delete_or_rate(self):
        original=episode(self.dataset,finished=False);self.store.observe(self.session);r=self.store.list()[0]
        self.assertTrue(original.exists());self.assertEqual(r['state'],'saving')
        with self.assertRaises(ValueError):self.store.delete(r['id'])
        with self.assertRaises(ValueError):self.store.rate(r['id'],'A')
    def test_existing_baseline_is_untouched(self):
        original=episode(self.dataset);self.session['baseline']=[UUID];self.store.observe(self.session)
        self.assertTrue(original.exists());self.assertEqual(self.store.list(),[])
    def test_symlinks_and_root_rejected(self):
        outside=Path(self.tmp.name)/'outside';outside.mkdir();link=self.dataset/'link';link.symlink_to(outside,target_is_directory=True)
        for path in [self.root,self.dataset,link,outside]:
            with self.subTest(path=path),self.assertRaises(ValueError):self.store.safe(path)
    def test_partial_metadata_keeps_pending_then_recovers(self):
        original=episode(self.dataset);(original/'episode_meta.json').write_text('{')
        self.store.observe(self.session);self.assertEqual(self.store.list()[0]['state'],'saving');self.assertTrue(original.exists())
        atomic_json(original/'episode_meta.json',{'step_index':[]});self.store.observe(self.session);self.assertEqual(self.store.list()[0]['state'],'completed')
    def test_nonfinite_data_cannot_finalize(self):
        p=episode(self.dataset)
        with h5py.File(p/'episode.hdf5','a') as f:f['action/arm/position'][0,0]=np.nan
        self.store.observe(self.session);self.assertEqual(self.store.list()[0]['state'],'saving');self.assertIsNone(self.store.list()[0]['grade'])
    def test_crash_after_rename_recovers(self):
        p=episode(self.dataset)
        with patch.object(self.store,'_finish_index',side_effect=RuntimeError('crash')):
            self.store.observe(self.session)
        self.store.close();self.store=EpisodeStore(self.runtime,self.root)
        self.assertEqual(self.store.list()[0]['state'],'completed');self.assertTrue((self.dataset/'episode_000001'/'review.json').exists())
    def test_grade_file_recovers_after_database_interruption(self):
        episode(self.dataset);self.store.observe(self.session);r=self.store.list()[0]
        path=Path(r['path'])/'review.json';review=json.loads(path.read_text());review['grade']='F';atomic_json(path,review)
        self.store.close();self.store=EpisodeStore(self.runtime,self.root)
        self.assertEqual(self.store.list()[0]['grade'],'F')
    def test_pending_log_offset_survives_restart(self):
        p=episode(self.dataset,finished=False)
        with (p/'collection.log').open('a') as f:f.write('WARNING waiting\n')
        self.store.observe(self.session);self.assertEqual(self.store.list()[0]['progress']['warnings'],1)
        self.store.close();self.store=EpisodeStore(self.runtime,self.root)
        self.store.observe(self.session);self.assertEqual(self.store.list()[0]['progress']['warnings'],1)
    def test_rename_failure_is_not_marked_completed(self):
        p=episode(self.dataset)
        with patch.object(Path,'rename',side_effect=PermissionError('denied')):self.store.observe(self.session)
        self.assertTrue(p.exists());self.assertEqual(self.store.list()[0]['state'],'saving')
    def test_preview_disconnect_is_separate_from_recording_warning(self):
        p=episode(self.dataset)
        with (p/'collection.log').open('a') as f:
            f.write('WARNING [WebRTC] 视频 ICE 断开，强制清理连接\nWARNING 数据写入等待\n')
        self.store.observe(self.session);row=self.store.list()[0]
        self.assertEqual(row['progress']['preview_warnings'],1)
        self.assertEqual(row['progress']['warnings'],1)
        self.assertIn('写入告警 1 次',row['detail']['warnings'])

class MonitorTests(unittest.TestCase):
    def test_vr_dynamic_type_and_freshness(self):
        with tempfile.TemporaryDirectory() as d:
            m=DeviceMonitor(d)
            m._put('device',{'others':{'vr':'异常'}});self.assertFalse(m.snapshot()['vr_connected'])
            m._put('device',{'others':{'vr':dict.fromkeys(['head_world','left_joystick_world','right_joystick_world'],{})}});self.assertTrue(m.snapshot()['vr_connected'])
            m.values['device']['received']-=10;self.assertFalse(m.snapshot()['vr_connected']);m.close()

class RpcTests(unittest.TestCase):
    def test_real_grpc_serialization_and_cancel(self):
        seen=[];cancelled=threading.Event()
        def meta(request,context):
            seen.append(json.loads(request.json_config));context.add_callback(cancelled.set)
            yield MetaData(json_data=json.dumps({'message':'ready'}))
            while context.is_active():time.sleep(.01)
        server=grpc.server(concurrent.futures.ThreadPoolExecutor(max_workers=2))
        handler=grpc.unary_stream_rpc_method_handler(meta,request_deserializer=MetaRequest.FromString,response_serializer=MetaData.SerializeToString)
        server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler('robot.RobotService',{'MetaTransfer':handler}),))
        port=server.add_insecure_port('127.0.0.1:0');server.start()
        vendor=Vendor(f'127.0.0.1:{port}')
        stream=vendor.meta({'task_name':'test'})
        self.assertEqual(json.loads(next(stream).json_data),{'message':'ready'})
        stream.cancel();self.assertTrue(cancelled.wait(2));self.assertEqual(seen,[{'task_name':'test'}]);vendor.close();server.stop(0).wait()

if __name__=='__main__':unittest.main()
