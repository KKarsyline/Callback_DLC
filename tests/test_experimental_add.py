import sqlite3
import tempfile
import unittest
from pathlib import Path

import json
from experiments.add_one import add_one
from assoc_index import AssocIndex

class Fake:
    _connect=AssocIndex._connect
    _init_db=AssocIndex._init_db
    _parse_json=staticmethod(AssocIndex._parse_json)
    def __init__(self,root):
        self.db_path=str(root/"fixture.sqlite")
        self._init_db()
        self.rev="v1"
        self.embedding_engine=type("Engine",(),{"model":"test"})()
        self.rows=[dict(cue_text="卸载后复测材料形状",quote="先卸载，再测量样块的形状。"),dict(supported=True,novel=True)]
        self.calls=0
        with self._connect() as db:
            db.execute("insert into assoc_state values ('m','v1',1)")
            db.execute("insert into assoc_cues values ('old','m','b','v1','样块','有一个样块','detail',null,'test',1)")
    async def _fresh_moment(self,mid):return dict(bucket_id="b",text="有一个样块。先卸载，再测量样块的形状。")
    async def _bucket(self,bid):return {"id":bid}
    def _eligible(self,moment,bucket):return True
    def _snapshot(self,moment,bucket):return self.rev
    def _source_window(self,moment,bucket,quote):return moment["text"] if quote in moment["text"] else ""
    async def _complete(self,prompt,max_tokens):
        self.calls+=1
        return json.dumps(self.rows.pop(0),ensure_ascii=False)
    async def _embed(self,text):return [1,0]

class AddTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.idx=Fake(self.root)
    def rows(self):
        with self.idx._connect() as db:return [tuple(row) for row in db.execute("select * from assoc_cues order by rowid")]
    async def run_add(self):return (await add_one(self.idx,"m",scratch_root=self.root))["status"]
    async def test_append(self):
        baseline=self.rows();self.assertEqual(await self.run_add(),"added");self.assertEqual(self.rows()[:1],baseline)
        self.assertEqual(len(self.rows()),2);self.assertEqual(self.idx.calls,2)
    async def test_full(self):
        with self.idx._connect() as db:
            for val in ("2","3","4"):
                db.execute("insert into assoc_cues select ?,moment_id,bucket_id,evidence_version,cue_text,quote,cue_type,vector_json,model,created_at from assoc_cues where cue_id='old'",(val,))
        baseline=self.rows();self.assertEqual(await self.run_add(),"full");self.assertEqual(self.rows(),baseline);self.assertEqual(self.idx.calls,0)
    async def test_stale(self):
        self.idx.rev="v2";self.assertEqual(await self.run_add(),"stale_source");self.assertEqual(self.idx.calls,0)
    async def test_changed_source(self):
        async def embed(text):self.idx.rev="v2";return [1,0]
        self.idx._embed=embed;baseline=self.rows();self.assertEqual(await self.run_add(),"stale_source");self.assertEqual(self.rows(),baseline)
    async def test_verifier(self):
        self.idx.rows[1]={"supported":True,"novel":False}
        baseline=self.rows();self.assertEqual(await self.run_add(),"not_verified");self.assertEqual(self.rows(),baseline)
    async def test_bad_quote(self):
        self.idx.rows[0]["quote"]="没说过";self.assertEqual(await self.run_add(),"invalid_or_duplicate");self.assertEqual(len(self.rows()),1)
    async def test_truncated(self):
        async def complete(prompt,max_tokens):return '{"cue_text":'
        self.idx._complete=complete;self.assertEqual(await self.run_add(),"invalid_generation");self.assertEqual(len(self.rows()),1)
    async def test_concurrent(self):
        async def embed(text):
            with self.idx._connect() as db:db.execute("update assoc_state set indexed_at=2 where moment_id='m'")
            return [1,0]
        self.idx._embed=embed;baseline=self.rows();self.assertEqual(await self.run_add(),"concurrent_change");self.assertEqual(self.rows(),baseline)
    async def test_scoped(self):
        unrelated=self.root/"other"
        unrelated.mkdir()
        with self.assertRaises(ValueError):await add_one(self.idx,"m",scratch_root=unrelated)
