"""End-to-end record identity, discard recovery and restart regressions."""
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from episodes import EpisodeStore,atomic_json
import test_http
from test_core import episode

class LifecycleTests(unittest.TestCase):
    setUp=test_http.HttpTests.setUp
    tearDown=test_http.HttpTests.tearDown
    request=test_http.HttpTests.request

    def recording(self):
        dataset=self.root/'batch';dataset.mkdir()
        self.app.store.new_session('lifecycle',{}, {},dataset)
        episode(dataset);self.app.store.observe({'id':'lifecycle','dataset':str(dataset),'baseline':[]})
        row=self.app.store.list()[0]
        return dataset,row,Path(row['path'])

    def reopen(self):
        # A second store tests recovery without starting any device or collector.
        store=EpisodeStore(self.app.runtime,self.root)
        self.addCleanup(store.close)
        return store

    def test_discard_renamed_record_does_not_delete_reused_old_path(self):
        dataset,row,old=self.recording();actual=dataset/'episode42';old.rename(actual)
        old.mkdir();atomic_json(old/'review.json',{'episode_uuid':'b'*32,'grade':'F'})
        (old/'keep.txt').write_text('different recording')
        code,_=self.request('/api/episode/delete',{'id':row['id'],'confirm':'delete'})
        self.assertEqual(code,200);self.assertFalse(actual.exists());self.assertTrue((old/'keep.txt').exists())
        saved=self.app.store.list()[0]
        self.assertEqual((saved['seq'],saved['path'],saved['state']),(row['seq'],row['path'],'deleted'))
        self.assertEqual(self.app.store.groups()[0]['retained'],0)

    def test_discard_archived_original_preserves_scene_copy(self):
        dataset,row,old=self.recording();archive=self.root/'raw_data'/dataset.name;archive.parent.mkdir();dataset.rename(archive)
        copy=self.root/'combined'/'episode5';copy.parent.mkdir();shutil.copytree(archive/old.name,copy)
        self.app.store.delete(row['id'])
        self.assertFalse((archive/old.name).exists());self.assertTrue(copy.exists())

    def test_missing_record_is_not_falsely_marked_discarded(self):
        _,row,old=self.recording();shutil.rmtree(old)
        code,_=self.request('/api/episode/delete',{'id':row['id'],'confirm':'delete'})
        self.assertEqual(code,400);self.assertEqual(self.app.store.get(row['id'])['state'],'completed')

    def test_partial_removal_recovers_without_metadata_or_reused_path_deletion(self):
        dataset,row,old=self.recording();actual=dataset/'episode12';old.rename(actual)
        def partial(path):
            (Path(path)/'review.json').unlink();(Path(path)/'episode_meta.json').unlink()
            raise OSError('simulated disk interruption')
        with patch('episodes.shutil.rmtree',side_effect=partial):
            self.assertEqual(self.request('/api/episode/delete',{'id':row['id'],'confirm':'delete'})[0],500)
        self.assertEqual(self.app.store.get(row['id'])['state'],'deleting')
        self.assertFalse(actual.exists());self.assertEqual(len(list(dataset.glob('.collection-discard-*'))),1)
        actual.mkdir();(actual/'keep.txt').write_text('new data after old directory moved')
        reopened=self.reopen()
        self.assertEqual(reopened.get(row['id'])['state'],'deleted')
        self.assertTrue((actual/'keep.txt').exists());self.assertEqual(list(dataset.glob('.collection-discard-*')),[])
        self.assertEqual(reopened.db.execute('select count(*) from pending_deletions').fetchone()[0],0)

    def test_delete_rename_failure_can_retry_and_replaced_target_is_not_removed(self):
        dataset,row,old=self.recording()
        with patch.object(Path,'rename',side_effect=OSError('rename denied')):
            self.assertEqual(self.request('/api/episode/delete',{'id':row['id'],'confirm':'delete'})[0],500)
        moved=dataset/'real_original';old.rename(moved);old.mkdir();(old/'keep.txt').write_text('replacement')
        code,_=self.request('/api/episode/delete',{'id':row['id'],'confirm':'delete'})
        self.assertEqual(code,200);self.assertTrue((old/'keep.txt').exists());self.assertFalse(moved.exists())

    def test_in_progress_delete_does_not_remove_replacement_with_copied_metadata(self):
        dataset,row,old=self.recording()
        with patch.object(Path,'rename',side_effect=OSError('rename denied')):
            self.assertEqual(self.request('/api/episode/delete',{'id':row['id'],'confirm':'delete'})[0],500)
        outside=self.root/'saved_original';old.rename(outside)
        shutil.copytree(outside,old)
        code,_=self.request('/api/episode/delete',{'id':row['id'],'confirm':'delete'})
        self.assertEqual(code,400);self.assertTrue(old.exists());self.assertTrue(outside.exists())

    def test_delete_retry_completes_after_temporary_filesystem_failure(self):
        _,row,old=self.recording()
        with patch('episodes.shutil.rmtree',side_effect=OSError('temporarily busy')):
            self.assertEqual(self.request('/api/episode/delete',{'id':row['id'],'confirm':'delete'})[0],500)
        self.assertIn('delete_error',self.app.store.list()[0]['detail'])
        self.assertEqual(self.request('/api/episode/delete',{'id':row['id'],'confirm':'delete'})[0],200)
        self.assertNotIn('delete_error',self.app.store.list()[0]['detail'])
        self.assertFalse(old.exists())

    def test_restart_finishes_legacy_pending_delete_by_identity(self):
        dataset,row,old=self.recording();renamed=dataset/'episode99';old.rename(renamed)
        self.app.store.db.execute("update episodes set state='deleting' where id=?",(row['id'],));self.app.store.db.commit()
        reopened=self.reopen();self.assertEqual(reopened.get(row['id'])['state'],'deleted');self.assertFalse(renamed.exists())

    def test_grade_file_recovers_after_rename_and_interrupted_database_write(self):
        dataset,row,old=self.recording();renamed=dataset/'episode99';old.rename(renamed)
        review=json.loads((renamed/'review.json').read_text());review['grade']='F';atomic_json(renamed/'review.json',review)
        reopened=self.reopen();self.assertEqual(reopened.list()[0]['grade'],'F')
        self.assertEqual(reopened.list()[0]['seq'],row['seq'])

    def test_older_archive_grade_does_not_override_newer_manual_grade(self):
        dataset,row,old=self.recording();self.app.store.rate(row['id'],'B')
        renamed=dataset/'episode99';old.rename(renamed)
        review=json.loads((renamed/'review.json').read_text());review.update(grade='A',updated_at=0);atomic_json(renamed/'review.json',review)
        reopened=self.reopen();self.assertEqual(reopened.list()[0]['grade'],'B')

    def test_end_rejects_saving_episode_even_if_log_is_not_recent(self):
        dataset=self.root/'pending';dataset.mkdir()
        self.app.store.new_session('pending',{}, {},dataset)
        episode(dataset,finished=False)
        session={'id':'pending','dataset':str(dataset),'baseline':[]}
        self.app.store.observe(session);self.app.collector.session=session
        self.app.collector.phase='waiting'
        with patch.object(self.app.collector,'external_active',return_value=[]):
            code,body=self.request('/api/session/end',{})
        self.assertEqual(code,400);self.assertIn('正在采集或保存',json.loads(body)['error'])
        self.assertEqual(self.app.collector.phase,'waiting')
