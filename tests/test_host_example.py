"""Application-level checks with the released service, index, gateway and ledger."""
import asyncio
import datetime
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from host_example.coordination import DirectResult
from host_example.demo import DIRECT, EVENT, QUESTION, QUOTE, ExampleReceipt, Workshop
from host_example.direct_callback import CallbackItem, callback_result
from pipeline import association_scheduler


class HostExampleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.demo = Workshop(root)
        await self.demo.start()
        self.addAsyncCleanup(self.demo.close)

    async def prepare(self):
        await self.demo.turn("We are building a display case.")
        await self.demo.host.drain()

    async def test_prepared_mention_cools_both_paths_for_six_completed_replies(self):
        await self.prepare()
        assistant, reply = await self.demo.turn(QUESTION)
        self.assertEqual(reply, QUOTE)
        await self.demo.host.drain()
        self.assertEqual(self.demo.receipts(), [(assistant, EVENT, "mentioned")])
        _, reply = await self.demo.turn(QUESTION, route="direct")
        self.assertEqual(reply, DIRECT)
        self.assertEqual(self.demo.last_direct_receipt.accepted_ids, ("display-case",))
        self.assertFalse(self.demo.last_context)
        await self.demo.host.drain()
        for _ in range(4):
            await self.demo.turn("Continue.", allow_assoc=False)
        # Five replies have intervened. The sixth still sees the mention.
        _, reply = await self.demo.turn(QUESTION, route="direct")
        self.assertEqual(reply, DIRECT)
        await self.demo.host.drain()
        _, reply = await self.demo.turn(QUESTION, route="direct")
        self.assertIn(QUOTE, reply)
        await self.demo.host.drain()
        self.assertEqual(len(self.demo.receipts()), 2)

    async def test_cold_query_can_deliver_and_direct_duplicates_win(self):
        assistant, reply = await self.demo.turn(QUESTION, route="direct", mention=False)
        self.assertIn(QUOTE, self.demo.last_context)
        self.assertEqual(reply, DIRECT)
        await self.demo.host.drain()
        self.assertEqual(self.demo.receipts(), [(assistant, EVENT, "unused")])
        _, reply = await self.demo.turn(QUESTION, route="direct", direct_event=EVENT)
        self.assertEqual(reply, DIRECT)
        self.assertEqual(self.demo.last_context, "")
        self.assertEqual(self.demo.receipts(), [(assistant, EVENT, "unused")])

    async def test_changed_source_and_revoked_raw_record_disqualify_a_cached_hit(self):
        await self.prepare()
        old = self.demo.sources.content
        self.demo.sources.content += " The test has been revised."
        await self.demo.turn(QUESTION)
        self.assertFalse(self.demo.last_context)
        self.demo.sources.content = old
        await self.demo.host.drain()
        await self.prepare()
        self.demo.raw.revoked.add(101)
        _, reply = await self.demo.turn(QUESTION, route="direct")
        self.assertEqual(reply, DIRECT)
        self.assertFalse(self.demo.last_context)
        self.assertEqual(self.demo.receipts(), [])

    async def test_fork_requires_its_own_prepare(self):
        await self.prepare()
        self.demo.branch = "fork"
        await self.demo.turn(QUESTION)
        self.assertEqual(self.demo.last_context, "")
        self.assertEqual(self.demo.receipts(), [])

    async def test_missing_counter_uses_expiry_but_empty_branch_has_no_cooldown(self):
        await self.demo.turn(QUESTION, route="direct")
        await self.demo.host.drain()
        self.demo.branch = "independent"

        async def unavailable(*args):
            raise OSError("counter unavailable")

        with patch.object(self.demo.storage, "completed_assistant_path", unavailable):
            _, reply = await self.demo.turn(QUESTION, route="direct")
        self.assertEqual(reply, DIRECT)
        await self.demo.host.drain()
        self.demo.branch = "empty"
        _, reply = await self.demo.turn(QUESTION, route="direct")
        self.assertIn(QUOTE, reply)

    async def test_exhausted_core_and_gateway_allowances_preserve_direct_evidence(self):
        await self.prepare()
        month = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")
        for path in (self.demo.service.db_path, self.demo.models.budget_db):
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE IF NOT EXISTS assoc_budget "
                           "(month TEXT PRIMARY KEY, microdollars INTEGER NOT NULL)")
                db.execute("INSERT OR REPLACE INTO assoc_budget VALUES (?, ?)", (month, 9_000_000))
        _, reply = await self.demo.turn(QUESTION, route="direct")
        self.assertEqual(reply, DIRECT)
        self.assertEqual(self.demo.last_context, "")
        self.assertEqual(self.demo.receipts(), [])

    async def test_quiet_and_raw_archive_turns_keep_the_baseline(self):
        await self.prepare()
        await self.demo.turn(QUESTION, route="raw_archive")
        self.assertEqual(self.demo.last_context, "")
        await self.demo.host.drain()
        registry = self.demo.host.registry
        with patch.object(registry, "call_tool", wraps=registry.call_tool) as dispatch:
            _, reply = await self.demo.turn("先听我说完", route="direct")
            self.assertEqual(reply, DIRECT)
            await self.demo.host.drain()
            dispatch.assert_not_awaited()
        self.assertEqual(self.demo.last_context, "")
        self.assertEqual(self.demo.receipts(), [])

    async def test_active_path_failure_preserves_direct_delivery(self):
        async def offline(*args, **kwargs):
            raise OSError("chat path unavailable")

        with patch.object(self.demo.storage, "list_active_path", offline):
            _, reply = await self.demo.turn(QUESTION, route="direct")
        self.assertEqual(reply, DIRECT)
        self.assertFalse(self.demo.last_context)
        self.assertEqual(self.demo.receipts(), [])

    async def test_slow_initial_read_still_prepares_for_next_turn(self):
        storage = self.demo.storage
        original = storage.list_active_path
        first_cancelled = asyncio.Event()
        reads = 0

        async def slow_first(*args, **kwargs):
            nonlocal reads
            reads += 1
            if reads == 1:
                try:
                    await asyncio.Future()
                finally:
                    first_cancelled.set()
            return await original(*args, **kwargs)

        registry = self.demo.host.registry
        with patch.object(storage, "list_active_path", slow_first), patch.object(
                registry, "call_tool", wraps=registry.call_tool) as calls:
            assistant, reply = await asyncio.wait_for(self.demo.turn("We are building a case."), 2)
            await self.demo.host.drain()
            self.assertTrue(first_cancelled.is_set())
            self.assertEqual(reads, 2)
            body, = [call.args[2]["body"] for call in calls.await_args_list
                     if call.args[1] == "assoc_prepare"]
            user, committed = storage.paths[(self.demo.session, self.demo.branch)]
            self.assertEqual((body["scope"]["user_message_id"], body["scope"]["message_version"]),
                             (user["id"], assistant))
            self.assertEqual(committed["content"], reply)
            self.assertIn(reply, body["context_text"])
        _, reply = await self.demo.turn(QUESTION)
        self.assertEqual(reply, QUOTE)

    async def test_prepare_skips_excluded_replaced_and_foreign_committed_rows(self):
        storage = self.demo.storage
        original = storage.list_active_path
        registry = self.demo.host.registry
        for position, update in (
            (-2, {"metadata": {"private": True}}), (-1, {"visibility": "private"}),
            (-2, {"id": "replacement"}), (-1, {"id": "new-leaf"}),
            (-2, {"content": "edited question"}), (-1, {"content": "edited answer"}),
            (-2, {"channel_id": "other"}), (-1, {"role": "tool"}),
        ):
            with self.subTest(position=position, update=update):
                async def changed_after_commit(*args, **kwargs):
                    rows = await original(*args, **kwargs)
                    if rows[-1]["role"] == "assistant":
                        rows[position] = {**rows[position], **update}
                    return rows

                with patch.object(storage, "list_active_path", changed_after_commit), patch.object(
                        registry, "call_tool", wraps=registry.call_tool) as calls:
                    await self.demo.turn("We are building a case.")
                    await self.demo.host.drain()
                    self.assertNotIn("assoc_prepare", [c.args[1] for c in calls.await_args_list])

    async def test_pending_receipt_allows_immediate_direct_reply(self):
        await self.prepare()
        registry = self.demo.host.registry
        original = registry.call_tool
        release = asyncio.Event()

        async def until_released(server, tool, arguments):
            if tool == "assoc_finish":
                await release.wait()
            return await original(server, tool, arguments)

        with patch.object(registry, "call_tool", until_released):
            try:
                _, reply = await self.demo.turn(QUESTION)
                self.assertEqual(reply, QUOTE)
                self.assertEqual(self.demo.receipts(), [])
                _, reply = await self.demo.turn(QUESTION, route="direct")
                self.assertEqual(reply, DIRECT)
            finally:
                release.set()
            await self.demo.host.drain()
        self.assertEqual([r[2] for r in self.demo.receipts()], ["mentioned"])

    async def test_cancelled_reply_settles_without_a_mention(self):
        await self.prepare()
        user = self.demo.storage.append(self.demo.session, self.demo.branch, "user", QUESTION)

        async def plan():
            return "none", ""

        abandoned = asyncio.Event()

        async def abort_if_prepared():
            abandoned.set()

        async def lookup(route, query):
            return DirectResult(abort_if_prepared=abort_if_prepared)

        async def interrupted(direct, context):
            self.assertIn(QUOTE, context)
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await self.demo.host.turn(self.demo.session, self.demo.branch, user, QUESTION,
                                      plan=plan, lookup=lookup, reply_and_commit=interrupted)
        await self.demo.host.drain()
        self.assertTrue(abandoned.is_set())
        self.assertEqual(self.demo.receipts(), [(None, EVENT, "cancelled")])

    async def test_one_shared_grace_while_direct_work_runs(self):
        await self.prepare()
        started = {tool: asyncio.Event() for tool in ("assoc_validate", "assoc_query")}
        stopped = {tool: asyncio.Event() for tool in started}
        registry = self.demo.host.registry
        original = registry.call_tool

        async def slow(server, tool, arguments):
            if tool in started:
                started[tool].set()
                try:
                    await asyncio.Future()
                finally:
                    stopped[tool].set()
            return await original(server, tool, arguments)

        async def plan():
            await asyncio.wait_for(started["assoc_validate"].wait(), 1)
            return "direct", QUESTION

        direct_receipt = object()
        direct_finished = None

        async def lookup(route, query):
            nonlocal direct_finished
            await asyncio.wait_for(started["assoc_query"].wait(), 1)
            direct_finished = time.monotonic()
            return DirectResult(DIRECT, ("display-case",), direct_receipt)

        async def commit(direct, context):
            self.assertIs(direct.receipt, direct_receipt)
            self.assertEqual(context, "")
            row = self.demo.storage.append(self.demo.session, self.demo.branch, "assistant",
                                           direct.text, expected_leaf=user)
            return row, direct.text

        user = self.demo.storage.append(self.demo.session, self.demo.branch, "user", QUESTION)
        with patch.object(registry, "call_tool", slow), patch.object(
                association_scheduler, "choose", wraps=association_scheduler.choose) as choose:
            _, reply = await asyncio.wait_for(self.demo.host.turn(
                self.demo.session, self.demo.branch, user, QUESTION,
                plan=plan, lookup=lookup, reply_and_commit=commit), 1)
            choose.assert_awaited_once()
            deadline = choose.await_args.args[2]
            self.assertGreater(deadline, direct_finished)
            self.assertLessEqual(deadline - direct_finished, 0.200)
            self.assertEqual(reply, DIRECT)
            for event in stopped.values():
                await asyncio.wait_for(event.wait(), 1)
        self.assertEqual(self.demo.receipts(), [])


class DirectCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_and_accepted_receipt_have_exactly_the_included_bucket_ids(self):
        items = [CallbackItem("box", "detail_1", "Plain latch."),
                 CallbackItem("shelf", "shelf:0123456789abcdef", "Hinged lid."),
                 CallbackItem("stand", "detail_3", "Extra shelf.")]
        prompt = "[box] Plain latch.\n[shelf] Hinged lid."
        for limits in ({"max_items": 2}, {"max_items": 3, "max_chars": len(prompt)}):
            with self.subTest(limits=limits):
                receipt = ExampleReceipt()
                result = await callback_result(items, receipt, **limits)
                self.assertEqual(result.text, prompt)
                self.assertEqual(receipt.state, "prepared")
                self.assertIs(result.receipt, receipt)
                await result.receipt.accept(result.event_ids)
                await result.abort_if_prepared()
                self.assertEqual(receipt.state, "accepted")
                self.assertEqual(receipt.accepted_ids, ("box", "shelf"))

    async def test_invalid_item_beyond_prompt_limit_rejects_the_whole_batch(self):
        first = CallbackItem("box", "sample", "First entry.")
        for invalid in (CallbackItem("shelf", "box:0123456789abcdef", "Wrong owner."),
                        CallbackItem("shelf", "shelf:0123456789abcdeG", "Wrong suffix."),
                        CallbackItem("box", "other", "Duplicate bucket."),
                        CallbackItem("shelf", "sample", "Duplicate moment.")):
            with self.subTest(invalid=invalid):
                receipt = ExampleReceipt()
                result = await callback_result([first, invalid], receipt, max_items=1)
                self.assertEqual((result.text, result.event_ids, result.receipt), ("", (), None))
                self.assertEqual(receipt.state, "discarded")
                self.assertEqual(receipt.accepted_ids, ())

    async def test_prompt_budget_with_no_room_discards_the_prepared_receipt(self):
        receipt = ExampleReceipt()
        result = await callback_result([CallbackItem("box", "log_1", "Hinged lid.")],
                                       receipt, max_chars=0)
        self.assertFalse(result.event_ids)
        self.assertEqual(receipt.state, "discarded")
