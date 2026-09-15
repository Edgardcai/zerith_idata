"""Playback transport and identity regressions, using isolated collection data."""
import concurrent.futures
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request
sys.path.insert(0,str(Path(__file__).resolve().parent))
import unittest
import test_http
from test_core import episode


class VideoReviewTests(unittest.TestCase):
    setUp=test_http.HttpTests.setUp
    tearDown=test_http.HttpTests.tearDown
    request=test_http.HttpTests.request
    def recording(self):
        dataset=self.root/'video_test';dataset.mkdir()
        self.app.store.new_session('video',{}, {},dataset)
        episode(dataset)
        self.app.store.observe({'id':'video','dataset':str(dataset),'baseline':[]})
        row=self.app.store.list()[0]
        return dataset,row,Path(row['path'])

    def test_renamed_recording_resolved_by_uuid_without_changing_history(self):
        dataset,row,original=self.recording();renamed=dataset/'episode99';original.rename(renamed)
        self.assertEqual(self.request(f"/api/video/{row['id']}/head")[0],200)
        streams=json.loads(self.request(f"/api/review/{row['id']}")[1])['streams']
        self.assertTrue(all('url' in v for v in streams.values()))
        self.assertEqual(self.app.store.list()[0],row)
        # Same pathname reused for a different episode must never serve the wrong video.
        original.mkdir();(original/'review.json').write_text(json.dumps({'episode_uuid':'b'*32}))
        self.assertEqual(self.app.store.video_directory(row),renamed)
        renamed.rename(dataset/'episode123')
        self.assertEqual(self.app.store.video_directory(row),dataset/'episode123')

    def test_missing_and_ambiguous_identity_are_reported(self):
        dataset,row,original=self.recording();original.rename(dataset/'episode99')
        other=dataset/'duplicate';other.mkdir();(other/'review.json').write_text(json.dumps({'episode_uuid':row['uuid']}))
        self.assertEqual(self.request(f"/api/video/{row['id']}/head")[0],400)
        streams=json.loads(self.request(f"/api/review/{row['id']}")[1])['streams']
        self.assertTrue(all('相同标识' in v['error'] for v in streams.values()))

    def test_dataset_archive_and_rename_keep_original_episode_identity(self):
        dataset,row,original=self.recording()
        archive=self.root/'raw_data'/'renamed_batch';archive.parent.mkdir()
        dataset.rename(archive)
        self.assertEqual(self.app.store.video_directory(row),archive/original.name)
        self.assertEqual(self.request(f"/api/video/{row['id']}/head")[0],200)
        self.assertEqual(self.app.store.list()[0],row)

    def test_preserved_archive_preferred_to_scene_copy(self):
        import shutil
        dataset,row,original=self.recording()
        archive=self.root/'raw_data'/dataset.name;archive.parent.mkdir()
        dataset.rename(archive)
        scene=self.root/'combined_scene';scene.mkdir()
        shutil.copytree(archive/original.name,scene/'episode1')
        self.assertEqual(self.app.store.video_directory(row),archive/original.name)

    def test_missing_camera_report_and_symlink_rejection(self):
        dataset,row,original=self.recording()
        head=original/'videos/rs/cam_high.mp4';head.unlink()
        streams=json.loads(self.request(f"/api/review/{row['id']}")[1])['streams']
        self.assertIn('error',streams['head']);self.assertIn('url',streams['left'])
        head.symlink_to(original/'videos/rs/cam_left_wrist.mp4')
        self.assertEqual(self.request(f"/api/video/{row['id']}/head")[0],400)

    def test_ranges_head_cache_and_three_concurrent_downloads(self):
        _,row,original=self.recording();data=bytes(range(256))*1024
        for path in original.glob('videos/rs/*.mp4'):path.write_bytes(data)
        url=self.url+f"/api/video/{row['id']}/head"
        def get(headers=None,method='GET',target=url):
            try:r=urllib.request.urlopen(urllib.request.Request(target,headers=headers or {},method=method))
            except urllib.error.HTTPError as exc:r=exc
            with r:return r.status,r.headers,r.read()
        for value,expected in [('bytes=0-31',data[:32]),('bytes=-64',data[-64:]),('bytes=256-',data[256:]),('bytes=12-999999',data[12:])]:
            code,headers,body=get({'Range':value});self.assertEqual(code,206);self.assertEqual(body,expected);self.assertIn('Content-Range',headers)
        for value in ['bytes=999999-','bytes=-0','bytes=-','bytes=9-1','bytes=0-1,4-5']:
            code,headers,body=get({'Range':value});self.assertEqual(code,416);self.assertEqual(headers['Content-Range'],f'bytes */{len(data)}')
        code,headers,body=get(method='HEAD');self.assertEqual(code,200);self.assertEqual(body,b'');self.assertEqual(int(headers['Content-Length']),len(data))
        self.assertEqual(get({'If-None-Match':headers['ETag']})[0],304)
        self.assertEqual(get({'If-Range':'"old"','Range':'bytes=0-10'})[2],data)
        with concurrent.futures.ThreadPoolExecutor(3) as pool:
            results=list(pool.map(lambda name:get({'Range':'bytes=0-'},target=self.url+f"/api/video/{row['id']}/{name}"),['head','left','right']))
        self.assertTrue(all(code==206 and body==data for code,_,body in results))

    def test_retained_counts_exclude_discard_without_removing_or_renumbering_rows(self):
        dataset,row,_=self.recording()
        episode(dataset,'b'*32);self.app.store.observe({'id':'video','dataset':str(dataset),'baseline':[]})
        self.app.store.delete(row['id'])
        group=self.app.store.groups()[0]
        self.assertEqual((group['total'],group['retained'],group['completed'],group['deleted']),(2,1,1,1))
        self.assertEqual([r['name'] for r in self.app.store.list()],['episode_000002','episode_000001'])
        self.assertEqual(self.app.store.counts('video')['completed'],1)
        # The next capture still reserves the next sequence, even after discard.
        episode(dataset,'c'*32);self.app.store.observe({'id':'video','dataset':str(dataset),'baseline':[]})
        self.assertEqual(self.app.store.list()[0]['name'],'episode_000003')
