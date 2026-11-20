import asyncio
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from host_example import demo
from pipeline import associative_recall as gateway
from pipeline import association_scheduler as association


class AdmissionWireTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scope = association.scope_for("session", "branch", "user", [])
        self.binding = dict(candidate_id="moment", event_id="event", evidence_version="v1",
                            excerpt="Plan a trip.", selection_token="reserved")
        self.fresh = dict(self.binding, source_window="Plan a trip.", plan_state="planned",
                          plan_quote="Plan a trip.", outcome_state="unknown")
        self.item = dict(self.fresh, source_window="Old window. Plan a trip.", use="mention",
                         outcome_state="completed", outcome_quote="Made it to Zurich.",
                         _assoc_origin="current_query")
        self.response = dict(schema_version="assoc.v1", status="ok", recheck_only=True,
                             candidates=[self.fresh])
        self.registry = AsyncMock()

    async def admit(self, response, **changes):
        self.registry.call_tool.return_value = response
        return await gateway.final_admission(self.scope, {**self.item, **changes}, self.registry,
                                             "memory", time.monotonic() + .19)

    async def test_same_binding_current_evidence_and_local_use(self):
        result = await self.admit(self.response)
        self.registry.call_tool.assert_awaited_once_with("memory", "assoc_validate", {"body": {
            "schema_version": "assoc.v1", "scope": {
                "session_id": "session", "branch_id": "branch", "request_id": "user",
                "user_message_id": "user", "message_version": "user", "last_assistant_ids": []},
            "recheck_only": True, "candidates": [self.binding]}})
        self.assertEqual(result, {**self.fresh, "use": "mention", "_assoc_origin": "current_query"})
        self.assertNotIn("Zurich", association.render(result))

    async def test_unconfirmed_or_substituted_response_is_rejected(self):
        for change in (
            {"recheck_only": None}, {"recheck_only": 1}, {"status": "invalid"},
            {"schema_version": "assoc.v2"}, {"scope": {"branch_id": "other"}},
            {"candidates": []}, {"candidates": [self.fresh, self.fresh]},
            *({"candidates": [{**self.fresh, key: "different"}]} for key in self.binding),
            {"candidates": [{**self.fresh, "source_window": "unrelated"}]},
            {"candidates": [{**self.fresh, "use": "mention"}]},
        ):
            with self.subTest(change=change):
                self.assertIsNone(await self.admit({**self.response, **change}, use="understand"))

    async def test_expired_deadline_and_invalid_purpose_skip_rpc(self):
        self.assertIsNone(await self.admit(self.response, use="other"))
        self.assertIsNone(await gateway.final_admission(
            self.scope, self.item, self.registry, "memory", time.monotonic()))
        self.registry.call_tool.assert_not_awaited()

    async def test_foreign_message_version_rejected_for_both_selection_paths(self):
        self.registry.call_tool.return_value = dict(schema_version="assoc.v1", status="ok",
                                                    candidates=[self.fresh])
        short, = await gateway._validate_batch(self.scope, [self.binding], self.registry, "memory")
        self.registry.call_tool.return_value = dict(schema_version="assoc.v1", status="ok",
                                                    selected={**self.fresh, "use": "mention"})
        live = await gateway.query_current(self.scope, "trip", "trip", self.registry, "memory", 100)
        self.registry.call_tool.reset_mock()
        for item in (short, live):
            result = await gateway.final_admission(
                {**self.scope, "message_version": "edited"}, {**item, "use": "mention"},
                self.registry, "memory", time.monotonic() + .19)
            self.assertIsNone(result)
        self.registry.call_tool.assert_not_awaited()


class AdmissionHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workshop = demo.Workshop(Path(directory.name))
        await self.workshop.start()
        self.addAsyncCleanup(self.workshop.close)

    async def prepare(self):
        await self.workshop.turn("We are designing a display case.")
        await self.workshop.host.drain()

    async def test_revocation_during_shortlist_judgment(self):
        await self.prepare()
        judge = self.workshop.models
        original = judge._post

        async def revoke_during_judgment(*args, **kwargs):
            self.workshop.raw.revoked.add(101)
            return await original(*args, **kwargs)

        with patch.object(judge, "_post", side_effect=revoke_during_judgment) as call:
            _, reply = await self.workshop.turn(demo.QUESTION)
            call.assert_awaited_once()
        self.assertEqual(self.workshop.last_context, "")
        self.assertNotIn(demo.QUOTE, reply)

    async def test_revocation_while_ordinary_lookup_waits_for_current_query(self):
        registry = self.workshop.host.registry
        original = registry.call_tool
        query_done = asyncio.Event()
        direct_result = demo.callback_result

        async def observe(server, tool, arguments):
            response = await original(server, tool, arguments)
            if tool == "assoc_query" and response["selected"]:
                query_done.set()
            return response

        async def slow_lookup(*args, **kwargs):
            await asyncio.wait_for(query_done.wait(), 1)
            self.workshop.raw.revoked.add(101)
            return await direct_result(*args, **kwargs)

        with patch.object(registry, "call_tool", observe), patch.object(demo, "callback_result", slow_lookup):
            _, reply = await self.workshop.turn(demo.QUESTION, route="direct")
        self.assertEqual(reply, demo.DIRECT)
        self.assertEqual(self.workshop.last_context, "")
        self.assertEqual(self.workshop.last_direct_receipt.accepted_ids, ("display-case",))

    async def assert_terminal_token(self, route, outcome):
        registry = self.workshop.host.registry
        with patch.object(registry, "call_tool", wraps=registry.call_tool) as calls:
            assistant, reply = await self.workshop.turn(demo.QUESTION, route=route)
            await self.workshop.host.drain()
        recheck, = [c.args[2]["body"] for c in calls.await_args_list
                    if c.args[1] == "assoc_validate" and c.args[2]["body"].get("recheck_only")]
        token = recheck["candidates"][0]["selection_token"]
        with sqlite3.connect(self.workshop.service.db_path) as db:
            rows = db.execute("SELECT selection_token, assistant_message_id, outcome FROM selections").fetchall()
        self.assertEqual(rows, [(token, assistant, outcome)])
        return reply

    async def test_cold_direct_mention_reuses_only_reservation(self):
        reply = await self.assert_terminal_token("direct", "mentioned")
        self.assertIn(demo.QUOTE, reply)
        self.assertIn(demo.DIRECT, reply)

    async def test_understand_settles_unused_without_exposing_quote(self):
        await self.prepare()
        with patch.object(gateway, "_judge", return_value=("tilt-test", "understand")):
            reply = await self.assert_terminal_token("none", "unused")
        self.assertEqual(self.workshop.last_context, association.render({"use": "understand"}))
        self.assertNotIn(demo.QUOTE, reply)

    async def test_timeout_preserves_direct_without_waiting_for_transport_cleanup(self):
        registry = self.workshop.host.registry
        original = registry.call_tool
        release_cleanup = asyncio.Event()
        cleaned = asyncio.Event()

        async def stalled(server, tool, arguments):
            if arguments["body"].get("recheck_only"):
                try:
                    await asyncio.Future()
                finally:
                    await release_cleanup.wait()
                    cleaned.set()
            return await original(server, tool, arguments)

        try:
            with patch.object(registry, "call_tool", stalled), patch.object(
                    association, "choose", wraps=association.choose) as choose, patch.object(
                    gateway, "final_admission", wraps=gateway.final_admission) as admit:
                _, reply = await asyncio.wait_for(self.workshop.turn(demo.QUESTION, route="direct"), 1)
                self.assertEqual(admit.await_args.args[-1], choose.await_args.args[-1])
                self.assertEqual(reply, demo.DIRECT)
                self.assertEqual(self.workshop.last_context, "")
                self.assertFalse(cleaned.is_set())
        finally:
            release_cleanup.set()
        await asyncio.wait_for(cleaned.wait(), 1)

    async def test_transport_error_keeps_direct_receipt(self):
        registry = self.workshop.host.registry
        original = registry.call_tool

        async def offline(server, tool, arguments):
            if arguments["body"].get("recheck_only"):
                raise OSError("memory service unavailable")
            return await original(server, tool, arguments)

        with patch.object(registry, "call_tool", offline):
            _, reply = await self.workshop.turn(demo.QUESTION, route="direct")
        self.assertEqual(reply, demo.DIRECT)
        self.assertEqual(self.workshop.last_context, "")
        self.assertEqual(self.workshop.last_direct_receipt.accepted_ids, ("display-case",))
