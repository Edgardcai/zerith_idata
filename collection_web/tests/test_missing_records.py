import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from episodes import EpisodeStore
from test_core import episode

class MissingRecordsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'data';self.root.mkdir()
        self.dataset=self.root/'test';self.dataset.mkdir()
        self.store=EpisodeStore(Path(self.tmp.name)/'runtime',self.root)
        self.store.new_session('s',{}, {},self.dataset)
        self.session={'id':'s','dataset':str(self.dataset),'baseline':[]}
        self.source=episode(self.dataset,finished=False)
        self.store.observe(self.session);self.ident=self.store.list()[0]['id']
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def test_missing_directory_becomes_error_after_grace(self):
        shutil.rmtree(self.source)
        self.assertEqual(self.store.reconcile_missing('s',now=100),[])
        self.store.reconcile_missing('s',now=129)
        self.assertEqual(self.store.get(self.ident)['state'],'saving')
        self.store.reconcile_missing('s',now=131)
        row=self.store.list()[0]
        self.assertEqual(row['state'],'error');self.assertIsNone(row['grade'])
        self.assertIn('目录已不存在',row['detail']['error'])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM audit WHERE action='missing_directory'").fetchone()[0],1)
    def test_existing_recording_is_never_timed_out(self):
        self.store.reconcile_missing('s',now=100000000000)
        self.assertEqual(self.store.get(self.ident)['state'],'saving');self.assertTrue(self.source.exists())
    def test_destination_exists_during_rename_is_protected(self):
        self.source.rename(self.dataset/'episode_000001')
        self.store.reconcile_missing('s',now=100)
        self.store.reconcile_missing('s',now=1000)
        self.assertEqual(self.store.get(self.ident)['state'],'saving')
    def test_reappearing_directory_clears_grace(self):
        temporary=self.source.with_name('temporary');self.source.rename(temporary)
        self.store.reconcile_missing('s',now=100)
        temporary.rename(self.source);self.store.reconcile_missing('s',now=120)
        self.assertNotIn('missing_since',self.store.list()[0]['progress'])
