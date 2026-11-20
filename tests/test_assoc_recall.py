"""Focused protocol and ledger tests for associative recall."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from assoc_recall import AssociativeRecall, SCHEMA_VERSION


class FakeIndex:
    def __init__(self):
        self.revision = "r1"

    async def rebuild(self, limit=12):
        return {"indexed": 0}

    async def resolve(self, candidate_id, expected_revision=None, quote=None):
        if candidate_id != "m1" or expected_revision != self.revision:
            return None
        return {"candidate_id": "m1", "event_id": "b1", "evidence_version": self.revision,
                "source_window": "verified original excerpt", "excerpt": "distinctive detail",
                "date": "2025-01-02", "archived": False}

    async def search(self, texts, limit=12, *, vector=None, allow_embed=True):
        if not allow_embed and vector is None:
            return [{"candidate_id": "m1", "event_id": "b1", "evidence_version": "r1",
                     "excerpt": "distinctive detail", "score": 0.9}]
        return []


class FakeDehydrator:
    api_available = False


class FakeEmbedding:
    def __init__(self):
        self.timeout_ms = None

    async def wait_query_embedding(self, text, *, timeout_ms=5):
        self.timeout_ms = timeout_ms
        return None


class AssocRecallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.index = FakeIndex()
        self.service = AssociativeRecall({"buckets_dir": self.tmp.name, "state_dir":self.tmp.name}, self.index,
                                         FakeDehydrator(), FakeEmbedding())
        self.scope = {"session_id": "s", "branch_id": "b", "user_message_id": "u1",
                      "message_version": "1", "request_id": "q1"}

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def body(self, **extra):
        return {"schema_version": SCHEMA_VERSION, "scope": self.scope, **extra}

    async def test_validate_checks_revision_and_reserves_source(self):
        result = await self.service.validate(self.body(candidates=[{
            "candidate_id": "m1", "evidence_version": "r1", "excerpt": "distinctive detail"}]))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["candidates"][0]["source_window"], "verified original excerpt")
        self.assertTrue(result["candidates"][0]["selection_token"])
        stale = await self.service.validate(self.body(candidates=[{
            "candidate_id": "m1", "evidence_version": "old"}]))
        self.assertEqual(stale["candidates"], [])

    async def test_finish_is_idempotent_and_requires_scope_on_prepare(self):
        result = await self.service.validate(self.body(candidates=[{
            "candidate_id": "m1", "evidence_version": "r1", "excerpt": "distinctive detail"}]))
        token = result["candidates"][0]["selection_token"]
        finish = {"schema_version": SCHEMA_VERSION, "selection_token": token,
                  "assistant_message_id": "a1", "outcome": "mentioned", "scope": self.scope}
        self.assertEqual(self.service.finish(finish)["status"], "ok")
        self.assertEqual(self.service.finish(finish)["status"], "already_settled")
        self.assertEqual((await self.service.prepare({"schema_version": SCHEMA_VERSION}))[
            "status"], "invalid")

    async def test_query_does_not_require_or_create_embedding(self):
        result = await self.service.query(self.body(query_text="literal", context_text="resting",
                                                   remaining_ms=20))
        # No candidate is selected without a judgment from the configured provider.
        self.assertIsNone(result["selected"])
        self.assertEqual(result["status"], "ok")
        self.assertLessEqual(self.service.embedding.timeout_ms, 20)

    async def test_background_prepare_does_not_judge_current_turn(self):
        class Index(FakeIndex):
            async def search(self, texts, limit=12, *, vector=None, allow_embed=True):
                return [{"candidate_id": f"m{i}", "event_id": f"b{i}",
                         "evidence_version": "r1", "excerpt": "detail", "reason": "cue"}
                        for i in range(8)]
        class Service(AssociativeRecall):
            async def _predict(self, context): return ["situation"]
            async def _judge(self, context, candidates, *, query_mode):
                self.query_mode = query_mode
                return candidates[:6]
        service = Service({"buckets_dir": self.tmp.name, "state_dir":self.tmp.name}, Index(), FakeDehydrator(), FakeEmbedding())
        result = await service.prepare(self.body(context_text="recent context"))
        self.assertFalse(service.query_mode)
        self.assertEqual(len(result["candidates"]), 6)
        service._worker.cancel()


if __name__ == "__main__":
    unittest.main()
