import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from http.server import ThreadingHTTPServer
from app import Application, Handler
from test_core import episode

class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'data';self.root.mkdir()
        self.app=Application(self.root,Path(self.tmp.name)/'runtime',start_devices=False)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.app=self.app
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.url=f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):self.server.shutdown();self.thread.join();self.server.server_close();self.app.close();self.tmp.cleanup()
    def request(self,path,body=None,token=True,origin=None):
        headers={}
        if body is not None:
            headers['Content-Type']='application/json'
            if token:headers['X-Collection-Token']=self.app.csrf
        if origin:headers['Origin']=origin
        req=urllib.request.Request(self.url+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        try:
            with urllib.request.urlopen(req) as response:return response.status,response.read()
        except urllib.error.HTTPError as exc:return exc.code,exc.read()
    def test_static_bootstrap_and_status(self):
        self.assertEqual(self.request('/')[0],200);self.assertEqual(self.request('/static/app.js')[0],200)
        self.assertEqual(json.loads(self.request('/api/bootstrap')[1])['csrf'],self.app.csrf)
        status=json.loads(self.request('/api/status')[1]);self.assertEqual(status['collection']['phase'],'idle');self.assertFalse(status['device']['ready'])
    def test_directory_groups_include_multiple_sessions_and_filter_details(self):
        first=self.root/'batch_one';second=self.root/'batch_two'
        first.mkdir();second.mkdir()
        for session,dataset,uid in [('s1',first,'a'*32),('s2',first,'b'*32),('s3',second,'c'*32)]:
            baseline=self.app.store.new_session(session,{}, {},dataset)
            episode(dataset,uid)
            self.app.store.observe({'id':session,'dataset':str(dataset),'baseline':baseline})
        groups=json.loads(self.request('/api/episode-groups')[1])['groups']
        self.assertEqual({g['dataset']:g['total'] for g in groups},{str(first):2,str(second):1})
        from urllib.parse import quote
        rows=json.loads(self.request('/api/episodes?dataset='+quote(str(first),safe=''))[1])['episodes']
        self.assertEqual({r['session_id'] for r in rows},{'s1','s2'})
        self.assertTrue(all(r['dataset']==str(first) for r in rows))
        self.assertEqual(json.loads(self.request('/api/episodes?dataset=missing')[1])['episodes'],[])
    def test_missing_token_cross_origin_and_invalid_start(self):
        self.assertEqual(self.request('/api/session/start',{'task_name':'test'},token=False)[0],403)
        self.assertEqual(self.request('/api/session/start',{'task_name':'test'},origin='http://evil.invalid')[0],403)
        self.assertEqual(self.request('/api/session/start',{'task_name':'test'})[0],400)
        self.assertEqual(self.app.collector.phase,'idle')
    def test_rating_delete_via_api_and_confirmation(self):
        dataset=self.root/'1_test';dataset.mkdir();self.app.store.new_session('s',{}, {}, dataset)
        p=episode(dataset);self.app.store.observe({'id':'s','dataset':str(dataset),'baseline':[]})
        row=self.app.store.list()[0]
        self.assertEqual(self.request('/api/episode/rate',{'id':row['id'],'grade':'F'})[0],200)
        self.assertEqual(self.app.store.list()[0]['grade'],'F')
        self.assertEqual(self.request('/api/episode/delete',{'id':row['id']})[0],400)
        self.assertTrue(Path(row['path']).exists())
        self.assertEqual(self.request('/api/episode/delete',{'id':row['id'],'confirm':'delete'})[0],200)
        self.assertFalse(Path(row['path']).exists())
    def test_static_traversal_and_incomplete_video(self):
        self.assertEqual(self.request('/static/../../app.py')[0],400)
        self.assertEqual(self.request('/api/video/999/head')[0],400)
    def test_directory_preview_is_readonly_and_matches_archived_video(self):
        payload={'task_id':20260911,'task_name':'test','left':'Dahongpao Milk Tea','right':'If coconut','lift_height':'0.8'}
        code,raw=self.request('/api/task/directory',payload);self.assertEqual(code,200)
        paths=json.loads(raw);dest=Path(paths['dataset']);source=Path(paths['source_dataset'])
        self.assertEqual(dest.name,'20260911_scene1');self.assertFalse(dest.exists());self.assertFalse(source.exists())
        self.assertEqual(self.app.collector.phase,'idle')
        self.app.store.new_session('routed',paths['config'],paths['targets'],dest,source)
        episode(source);self.app.store.observe({'id':'routed','dataset':str(dest),'source_dataset':str(source),'baseline':[]})
        self.assertEqual(self.request('/api/video/1/head')[0],200)
        self.assertEqual(self.request('/api/episode/rate',{'id':1,'grade':'B'})[0],200)
        self.assertEqual(self.request('/api/episode/delete',{'id':1,'confirm':'delete'})[0],200)
        self.assertFalse((dest/'episode_000001').exists())

    def test_apply_scene_without_prompt_creates_directory_and_persists(self):
        code,raw=self.request('/api/scene/apply',{'task_id':20260914,'scene_id':'001'})
        self.assertEqual(code,200)
        result=json.loads(raw);self.assertEqual(result['scene_id'],1)
        self.assertTrue((self.root/'20260914_scene1').is_dir())
        self.assertEqual(json.loads(self.request('/api/bootstrap')[1])['selected_scene'],result)
        self.assertEqual(json.loads((self.app.runtime/'selected_scene.json').read_text()),result)
        self.assertEqual(self.app.collector.phase,'idle')
        marker=self.root/'20260914_scene1'/'keep.txt';marker.write_text('history')
        self.assertEqual(self.request('/api/scene/apply',{'task_id':20260914,'scene_id':1})[0],200)
        self.assertEqual(marker.read_text(),'history')
        for payload in [{'scene_id':'../1'},{'scene_id':True},{'scene_id':0},{'scene_id':1.5}]:
            self.assertEqual(self.request('/api/scene/apply',payload)[0],400)
        self.assertEqual(self.request('/api/scene/apply',{'scene_id':2},token=False)[0],403)
        self.app.collector.phase='waiting'
        self.assertEqual(self.request('/api/scene/apply',{'scene_id':2})[0],400)
        self.assertEqual(self.app.collector.selected_scene,result)
        self.app.collector.phase='idle'

    def test_scene_symlink_rejected(self):
        outside=Path(self.tmp.name)/'outside';outside.mkdir()
        (self.root/'20260914_scene1').symlink_to(outside,target_is_directory=True)
        self.assertEqual(self.request('/api/scene/apply',{'task_id':20260914,'scene_id':1})[0],400)
        self.assertEqual(list(outside.iterdir()),[])

    def test_compact_status_keeps_live_episode_without_history(self):
        dataset=self.root/'compact';dataset.mkdir()
        self.app.store.new_session('compact',{'subtask_num':2},{},dataset)
        episode(dataset,'a'*32)
        session={'id':'compact','dataset':str(dataset),'baseline':[],'config':{'subtask_num':2}}
        self.app.store.observe(session)
        episode(dataset,'b'*32,finished=False);self.app.store.observe(session)
        self.app.collector.session=session
        full=json.loads(self.request('/api/status')[1])['collection']
        compact=json.loads(self.request('/api/status?compact=1')[1])['collection']
        self.assertEqual(len(full['episodes']),2)
        self.assertNotIn('episodes',compact)
        self.assertEqual(compact['current'],full['current'])
        self.assertEqual(compact['counts'],full['counts'])
        self.assertEqual(compact['current']['uuid'],'b'*32)

    def test_timing_warning_defaults_A_and_manual_rating_is_unrestricted(self):
        import h5py
        dataset=self.root/'quality';dataset.mkdir()
        self.app.store.new_session('quality',{}, {},dataset)
        p=episode(dataset)
        with h5py.File(p/'episode.hdf5','a') as f:f['timestamp/t'][3]=f['timestamp/t'][2]
        self.app.store.observe({'id':'quality','dataset':str(dataset),'baseline':[]})
        row=self.app.store.list()[0];self.assertEqual(row['grade'],'A')
        for grade in ('F','B','A'):
            self.assertEqual(self.request('/api/episode/rate',{'id':row['id'],'grade':grade})[0],200)
            self.assertEqual(self.app.store.list()[0]['grade'],grade)
        self.assertEqual(self.app.store.list()[0]['detail']['timing_qc']['status'],'failed')

if __name__=='__main__':unittest.main()
