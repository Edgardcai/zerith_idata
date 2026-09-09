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
        payload={'task_name':'test','left':'Dahongpao Milk Tea','right':'If coconut','lift_height':'0.8'}
        code,raw=self.request('/api/task/directory',payload);self.assertEqual(code,200)
        paths=json.loads(raw);dest=Path(paths['dataset']);source=Path(paths['source_dataset'])
        self.assertEqual(dest.name,'DahongpaoMilkTea_Ifcoconut_0.8');self.assertFalse(dest.exists());self.assertFalse(source.exists())
        self.assertEqual(self.app.collector.phase,'idle')
        self.app.store.new_session('routed',paths['config'],paths['targets'],dest,source)
        episode(source);self.app.store.observe({'id':'routed','dataset':str(dest),'source_dataset':str(source),'baseline':[]})
        self.assertEqual(self.request('/api/video/1/head')[0],200)
        self.assertEqual(self.request('/api/episode/rate',{'id':1,'grade':'B'})[0],200)
        self.assertEqual(self.request('/api/episode/delete',{'id':1,'confirm':'delete'})[0],200)
        self.assertFalse((dest/'episode_000001').exists())

if __name__=='__main__':unittest.main()
