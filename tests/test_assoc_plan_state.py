"""Plan and outcome evidence stay literal, revision-bound, and conservative."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from assoc_index import AssocIndex


class _Moments:
    def __init__(self, row): self.row = row
    def list_all(self, limit=10000): return [self.row][:limit]
    def get(self, moment_id): return self.row if moment_id == self.row["moment_id"] else None


class _Buckets:
    def __init__(self, root, bucket): self.base_dir, self.bucket = str(root), bucket
    async def list_all(self, include_archive=True): return [self.bucket]
    async def get(self, bucket_id): return self.bucket if bucket_id == self.bucket["id"] else None


class _Client:
    def __init__(self, answers):
        self.answers, self.chat, self.completions = iter(answers), self, self
    def with_options(self, **kwargs): return self
    async def create(self, **kwargs):
        value = next(self.answers)
        message = type("Message", (), {"content": value})()
        return type("Response", (), {"choices": [type("Choice", (), {"message": message})()]})()


class _Dehydrator:
    model = "test-model"
    def __init__(self, answers): self.client = _Client(answers)


class _Embeddings:
    enabled, model = False, "test"


class AssocPlanStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.text = "我计划周五去看展。后来我已经看完了展览。那天展厅比我原来想象的要宽敞，回家时天已经黑了。"
        self.path = root / "event.md"
        self.path.write_text(self.text, encoding="utf-8")
        self.bucket = {"id": "event-1", "type": "dynamic", "content": self.text,
                       "path": str(self.path), "content_start_line": 1,
                       "metadata": {"type": "dynamic", "date": "2026-01-02"}}
        self.moment = {"moment_id": "moment-1", "bucket_id": "event-1", "section": "body",
                       "text": self.text, "source_id": "body", "metadata": {
                           "source_ref": {"path": str(self.path), "content_start_line": 1,
                                          "start_line": 1, "end_line": 1}}}
        self.answers = [
            json.dumps({"cues": [{"cue_text": "看展计划及后续", "cue_type": "unfinished",
                                  "quote": "我计划周五去看展。", "plan_state": "planned",
                                  "plan_quote": "我计划周五去看展。", "outcome_state": "completed",
                                  "outcome_quote": "我已经看完了展览。"}]}),
            json.dumps({"keep": [0], "plans": [{"id": 0, "state": "planned", "quote": "我计划周五去看展。"}],
                        "outcomes": [{"id": 0, "state": "completed", "quote": "我已经看完了展览。"}]}),
        ]
        self.index = AssocIndex({"state_dir": str(root / "state"), "buckets_dir": str(root)},
                                _Moments(self.moment), _Buckets(root, self.bucket), object(),
                                _Embeddings(), _Dehydrator(self.answers))

    def tearDown(self): self.temp.cleanup()

    def test_plan_and_result_quotes_are_stored_and_returned(self):
        report = asyncio.run(self.index.rebuild(5))
        self.assertEqual(report["indexed"], 1)
        hit = asyncio.run(self.index.resolve("moment-1"))
        self.assertEqual(hit["plan_state"], "planned")
        self.assertEqual(hit["outcome_state"], "completed")
        self.assertIn(hit["plan_quote"], self.text)
        self.assertIn(hit["outcome_quote"], self.text)
        self.assertEqual(hit["source_revision"], hit["evidence_version"])

    def test_unverified_legacy_unfinished_is_never_promoted(self):
        with self.index._connect() as db:
            db.execute("INSERT INTO assoc_cues "
                       "(cue_id,moment_id,bucket_id,evidence_version,cue_text,quote,cue_type,created_at) "
                       "VALUES(?,?,?,?,?,?,?,?)", ("old", "moment-1", "event-1", "rev", "cue", "quote", "unfinished", 1))
        with self.index._connect() as db:
            row = db.execute("SELECT plan_state FROM assoc_plans WHERE cue_id='old'").fetchone()
        self.assertIsNone(row)

    def test_source_revision_change_invalidates_previous_plan(self):
        asyncio.run(self.index.rebuild(5))
        previous = asyncio.run(self.index.resolve("moment-1"))
        self.bucket["content"] += " 原计划后来取消。"
        self.assertIsNone(asyncio.run(self.index.resolve("moment-1", previous["evidence_version"])))


if __name__ == "__main__":
    unittest.main()
