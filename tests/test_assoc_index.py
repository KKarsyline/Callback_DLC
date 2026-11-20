"""Focused tests for evidence grounding, idempotence, and stale-source rejection."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from assoc_index import AssocIndex


class _Moments:
    def __init__(self):
        self.rows = {}

    def list_all(self, limit=10000):
        return list(self.rows.values())[:limit]

    def get(self, moment_id):
        return self.rows.get(moment_id)


class _Buckets:
    def __init__(self, root, bucket):
        self.base_dir = str(root)
        self.bucket = bucket

    async def list_all(self, include_archive=True):
        return [self.bucket]

    async def get(self, bucket_id):
        return self.bucket if self.bucket["id"] == bucket_id else None


class _Chat:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.completions = self
        self.chat = self

    async def create(self, **kwargs):
        answer = next(self.answers)
        return type("Response", (), {"choices": [type("Choice", (), {"message": type("Message", (), {"content": answer})()})()]})()


class _Dehydrator:
    model = "configured-model"

    def __init__(self, answers):
        self.client = _Chat(answers)


class _Embeddings:
    enabled = False
    model = "test"


class AssocIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "event.md"
        self.content = "材料样品没有标注规格。检测员把两个样块放在相同载荷下，只有第二个样块恢复了原来的形状。"
        self.path.write_text(self.content, encoding="utf-8")
        self.bucket = {"id": "event-1", "type": "dynamic", "content": self.content,
                       "path": str(self.path), "content_start_line": 1,
                       "metadata": {"type": "dynamic", "date": "2026-01-02"}}
        self.moments = _Moments()
        self.moment = {"moment_id": "moment-1", "bucket_id": "event-1", "section": "body",
                       "text": self.content, "source_id": "body", "created_at": "2026-01-02",
                       "metadata": {"source_ref": {"path": str(self.path), "content_start_line": 1,
                                                     "start_line": 1, "end_line": 1}}}
        self.moments.rows[self.moment["moment_id"]] = self.moment
        self.answers = [
            json.dumps({"cues": [{"cue_text": "材料参数不明时，可以比较卸载后能否恢复", "cue_type": "situation",
                                  "quote": "只有第二个样块恢复了原来的形状。"}]}),
            '{"keep":[0]}',
        ]
        self.index = AssocIndex({"state_dir": str(self.root / "state"), "buckets_dir": str(self.root)},
                                self.moments, _Buckets(self.root, self.bucket), object(), _Embeddings(),
                                _Dehydrator(self.answers))

    def tearDown(self):
        self.tmp.cleanup()

    def test_rebuild_is_idempotent_and_search_returns_live_evidence(self):
        first = asyncio.run(self.index.rebuild(10))
        second = asyncio.run(self.index.rebuild(10))
        self.assertEqual(first["indexed"], 1)
        self.assertEqual(second["unchanged"], 1)
        self.index._load_features()
        hits = asyncio.run(self.index.search(["材料卸载后怎样判断能否恢复？"], allow_embed=False))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["candidate_id"], "moment-1")
        self.assertIn(hits[0]["excerpt"], hits[0]["source_window"])

    def test_changed_bucket_invalidates_old_revision(self):
        asyncio.run(self.index.rebuild(10))
        old = asyncio.run(self.index.resolve("moment-1"))
        self.bucket["content"] += " 后来试验步骤调整了。"
        self.assertIsNone(asyncio.run(self.index.resolve("moment-1", old["evidence_version"])))
        self.assertEqual(asyncio.run(self.index.search(["样块恢复"], allow_embed=False)), [])

    def test_mixed_superseded_bucket_fails_closed(self):
        self.bucket["metadata"]["supersede_needs_review"] = True
        self.assertIsNone(asyncio.run(self.index.resolve("moment-1")))
        self.assertEqual(asyncio.run(self.index.rebuild(10))["ineligible"], 1)

    def test_model_quote_must_be_literal(self):
        self.answers = [json.dumps({"cues": [{"cue_text": "新增故事", "cue_type": "detail",
                                              "quote": "他们很喜欢"}]})]
        self.index.dehydrator = _Dehydrator(self.answers)
        result = asyncio.run(self.index.rebuild(10))
        self.assertEqual(result["indexed"], 1)
        self.assertEqual(asyncio.run(self.index.search(["新增故事"], allow_embed=False)), [])


if __name__ == "__main__":
    unittest.main()
