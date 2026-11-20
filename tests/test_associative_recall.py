"""Focused async tests for the associative recall gateway adapter."""
from __future__ import annotations

import asyncio
import json
import unittest

from pipeline import associative_recall as recall


class FakeRegistry:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def call_tool(self, server, tool, body):
        self.calls.append((server, tool, body))
        response = self.responses.get(tool)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            response = response(body)
        return response


class FakeSLM:
    def __init__(self, response):
        self.response = response
        self._model = "test"
        self.calls = 0

    async def _post(self, *args, **kwargs):
        self.calls += 1
        return self

    def json(self):
        return {"choices":[{"message":{"content":self.response}}]}

    def raise_for_status(self): pass


def scope(message_id="u1"):
    return {
        "basis_assistant_message_id":"a0","active_path_ancestry":["a0"],
        "session_id": "conversation", "branch_id": "channel",
        "user_message_id": message_id, "message_version": "1", "request_id": "r1",
    }


def candidate(candidate_id="m1"):
    return {
        "candidate_id": candidate_id, "event_id": "b1", "evidence_version": "v1",
        "excerpt": "We planned a trip.", "reason": "shared plan", "date": "2026-01-01",
        "archived": False,
    }


def ok(candidates):
    return {"schema_version": "assoc.v1", "status": "ok", "candidates": candidates}


class AssociativeRecallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import os,tempfile
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        from unittest.mock import patch
        env=patch.dict(os.environ, {"ASSOC_GATEWAY_BUDGET_DB":self.tmp.name+"/budget.db"});env.start();self.addCleanup(env.stop)
        recall._CACHE.clear()

    async def test_prepare_caches_shortlist_and_wire_body_has_one_payload(self):
        registry = FakeRegistry({"assoc_prepare": ok([candidate()])})
        await recall.prepare_after(scope(), "recent context", registry, "atelier")
        self.assertEqual(len(registry.calls), 1)
        _, tool, body = registry.calls[0]
        self.assertEqual(tool, "assoc_prepare")
        self.assertEqual(body["body"]["schema_version"], "assoc.v1")
        self.assertEqual(body["body"]["context_text"], "recent context")
        self.assertEqual([item["candidate_id"] for item in recall._cache_get(scope())], ["m1"])

    async def test_shortlist_validates_and_judges_concurrently_then_returns_validated_item(self):
        source = candidate()
        validated = {**source, "source_window": "earlier messages", "selection_token": "token-1"}
        registry = FakeRegistry({
            "assoc_prepare": ok([source]),
            "assoc_validate": ok([validated]),
        })
        recall._cache_put(scope(), [source])
        slm = FakeSLM(json.dumps({"mode": "pick", "pick": "0", "use": "mention"}))
        slm.trusted_cache_judge = True
        task = recall.start_shortlist(scope(), [{"role": "user", "content": "Remember the trip?"}], "Remember?", slm, registry, "atelier")
        self.assertIsNotNone(task)
        result = await task
        self.assertEqual(result["selection_token"], "token-1")
        self.assertEqual(result["use"], "mention")
        self.assertEqual(slm.calls, 1)

    async def test_revoked_cache_never_reaches_default_judge(self):
        recall._cache_put(scope(), [candidate()])
        slm = FakeSLM('{"mode":"none","pick":null,"use":null}')
        registry = FakeRegistry({"assoc_validate": ok([])})
        self.assertIsNone(await recall.start_shortlist(scope(), [], "question", slm, registry, "memory"))
        self.assertEqual(slm.calls, 0)

    async def test_malformed_judgment_and_gateway_failure_fail_open(self):
        source = candidate()
        validated = {**source, "source_window": "earlier", "selection_token": "token-1"}
        registry = FakeRegistry({"assoc_validate": ok([validated])})
        recall._cache_put(scope(), [source])
        slm = FakeSLM('{"mode":"pick","pick":"unknown","use":"mention"}')
        task = recall.start_shortlist(scope(), [], "question", slm, registry, "atelier")
        self.assertIsNone(await task)
        failed = FakeRegistry({"assoc_prepare": RuntimeError("offline")})
        await recall.prepare_after(scope(), "context", failed, "atelier")
        self.assertEqual(await recall.query_current(scope(), "exact query", "latest", failed, "atelier", 100), None)

    async def test_query_preserves_exact_query_text_and_requires_validated_shape(self):
        selected = {**candidate(), "source_window": "matching window", "selection_token": "t", "use": "understand"}
        registry = FakeRegistry({"assoc_query": {"schema_version": "assoc.v1", "status": "ok", "selected": selected}})
        query = "  exact literal query  "
        result = await recall.query_current(scope(), query, "latest message", registry, "atelier", 500)
        self.assertEqual(result["candidate_id"], "m1")
        body = registry.calls[0][2]
        self.assertEqual(body["body"]["query_text"], query)
        self.assertEqual(body["body"]["remaining_ms"], 500)

    async def test_finish_requires_gateway_success_and_sends_outcome(self):
        success = {"schema_version": "assoc.v1", "status": "ok"}
        registry = FakeRegistry({"assoc_finish": success})
        item = {"selection_token": "t"}
        self.assertTrue(await recall.finish_selection(scope(), item, "a1", "mentioned", registry, "atelier"))
        body = registry.calls[0][2]
        self.assertEqual(body["body"]["outcome"], "mentioned")
        self.assertEqual(body["body"]["assistant_message_id"], "a1")
        self.assertFalse(await recall.finish_selection(scope(), item, "a1", "unknown", registry, "atelier"))

    async def test_scope_cache_is_bounded_by_branch_ancestry_and_ttl(self):
        s = {**scope(), "basis_assistant_message_id":"branch-a","active_path_ancestry":["root","branch-a"]}
        recall._cache_put(s, [candidate()])
        self.assertEqual(len(recall._cache_get(s)), 1)
        self.assertEqual(recall._cache_get({**s,"active_path_ancestry":["root","branch-b"]}), [])
        self.assertEqual(recall._cache_get({**s, "active_path_ancestry": ["elsewhere"]}), [])
        recall._cache_put(scope(), [candidate()])
        entry = recall._CACHE[("conversation", "channel")]
        entry.created_at -= recall.CACHE_TTL_S + 1
        self.assertEqual(recall._cache_get(scope()), [])


if __name__ == "__main__":
    unittest.main()
