"""Grounding, event identity, retries, and revision rollover for plan follow-up."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assoc_plans import refresh_plan_states


class Moments:
    def __init__(self, rows):
        self.rows = rows

    def list_all(self):
        return list(self.rows.values())

    def get(self, moment_id):
        return self.rows.get(moment_id)


class Buckets:
    def __init__(self, rows):
        self.rows = rows

    async def list_all(self, include_archive=True):
        return list(self.rows.values())


class Index:
    def __init__(self, root, rows, buckets, answers):
        self.config = {"state_dir": str(root), "buckets_dir": str(root)}
        self.db_path = str(root / "index.sqlite")
        self.moment_store = Moments(rows)
        self.bucket_mgr = Buckets(buckets)
        self.answers = iter(answers)
        self.calls = 0
        with self._connect() as conn:
            conn.execute("CREATE TABLE assoc_cues(cue_id TEXT,moment_id TEXT,bucket_id TEXT,evidence_version TEXT,quote TEXT,cue_type TEXT)")
            for moment in rows.values():
                bucket = buckets[moment["bucket_id"]]
                if moment["moment_id"] == "plan":
                    conn.execute("INSERT INTO assoc_cues VALUES(?,?,?,?,?,?)",
                                 ("cue", "plan", "origin", self._snapshot(moment, bucket), "I plan to call the clinic.", "unfinished"))

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    async def _bucket(self,bid):return self.bucket_mgr.rows.get(bid)

    async def _all_buckets(self):
        return await self.bucket_mgr.list_all()

    @staticmethod
    def _eligible(moment, bucket):
        return bool(moment.get("text") and bucket.get("active", True))

    def _snapshot(self, moment, bucket):
        return moment["revision"]

    def _source_window(self, moment, bucket, quote):
        return moment["text"] if quote and quote in moment["text"] and quote in bucket["content"] else None

    async def _fresh_moment(self, mid):
        return self.moment_store.get(mid)

    async def _complete(self, prompt, max_tokens=700):
        self.calls += 1
        return next(self.answers)

    @staticmethod
    def _parse_json(value):
        try:
            return json.loads(value)
        except Exception:
            return None


def replies(plan_quote="I plan to call the clinic.", state="completed", outcome_quote="I called the clinic today.", outcome_id="later"):
    proposal = {"items": [{"id": "0", "plan_state": "planned", "plan_quote": plan_quote,
                           "outcome_state": state, "outcome_quote": outcome_quote,
                           "outcome_moment_id": outcome_id}]}
    return [json.dumps(proposal), json.dumps(proposal)]


class PlanFollowupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        plan_text = "I plan to call the clinic."
        later_text = "I called the clinic today."
        self.buckets = {
            "origin": {"id": "origin", "content": plan_text},
            "later": {"id": "later", "content": later_text},
            "other": {"id": "other", "content": "I called a different clinic today."},
        }
        self.rows = {
            "plan": {"moment_id": "plan", "bucket_id": "origin", "text": plan_text,
                     "created_at": "2026-01-01", "revision": "p1"},
            "later": {"moment_id": "later", "bucket_id": "later", "text": later_text,
                      "created_at": "2026-01-03", "revision": "l1"},
            "other": {"moment_id": "other", "bucket_id": "other", "text": self.buckets["other"]["content"],
                      "created_at": "2026-01-03", "revision": "o1"},
        }
        self.index = Index(root, self.rows, self.buckets, replies())
        self.edge_patch = patch("memory_edges.MemoryEdgeStore.list_edges", return_value=[
            {"source": "origin", "target": "later", "relation_type": "same_event", "created_at": "2026-01-04"}])
        self.edge_patch.start()
        self.addCleanup(self.edge_patch.stop)

    def tearDown(self):
        self.temp.cleanup()

    def state(self):
        with self.index._connect() as conn:
            row = conn.execute("SELECT * FROM assoc_plans WHERE cue_id='cue'").fetchone()
            return dict(row) if row else None

    def test_explicit_later_event_can_be_confirmed(self):
        result = asyncio.run(refresh_plan_states(self.index))
        self.assertEqual(result["reviewed"], 1)
        self.assertEqual(self.state()["outcome_state"], "completed")
        self.assertEqual(self.index.calls, 2)

    def test_different_event_is_rejected_by_independent_verifier(self):
        bad = replies(state="completed", outcome_quote="I called a different clinic today.", outcome_id="other")
        self.index.answers = iter([bad[0], replies(state="unknown",outcome_quote="",outcome_id="")[1]])
        self.edge_patch.stop()
        self.edge_patch = patch("memory_edges.MemoryEdgeStore.list_edges", return_value=[
            {"source": "origin", "target": "other", "relation_type": "updates", "created_at": "2026-01-04"}])
        self.edge_patch.start()
        self.addCleanup(self.edge_patch.stop)
        result = asyncio.run(refresh_plan_states(self.index))
        self.assertEqual(result["reviewed"], 1)
        self.assertEqual(self.state()["outcome_state"], "unknown")

    def test_conflicting_linked_events_fail_closed(self):
        self.rows["later"]["text"] = "I called the clinic today, but the appointment is still pending."
        self.buckets["later"]["content"] = self.rows["later"]["text"]
        self.index.answers = iter(replies(outcome_quote="I called the clinic today."))
        # Two linked later moments are intentionally ambiguous; only two are
        # gathered and the model must conservatively return unknown.
        self.index.moment_store.rows["second"] = {
            "moment_id": "second", "bucket_id": "later", "text": "I did not call the clinic.",
            "created_at": "2026-01-04", "revision": "l2"}
        self.edge_patch.stop()
        self.edge_patch = patch("memory_edges.MemoryEdgeStore.list_edges", return_value=[
            {"source": "origin", "target": "later", "relation_type": "same_event", "created_at": "2026-01-04"}])
        self.edge_patch.start()
        self.addCleanup(self.edge_patch.stop)
        result = asyncio.run(refresh_plan_states(self.index))
        self.assertEqual(result["reviewed"], 1)
        # It cannot publish a quote that was only verified against a conflicting source revision.
        self.assertEqual(self.state()["outcome_state"], "unknown")

    def test_missing_or_nonliteral_quotes_do_not_publish_claims(self):
        self.index.answers = iter(replies(plan_quote="I intend to call", outcome_quote="The visit went well."))
        result = asyncio.run(refresh_plan_states(self.index))
        self.assertEqual(result["reviewed"], 1)
        self.assertEqual(self.state()["plan_state"], "unknown")
        self.assertEqual(self.state()["outcome_state"], "unknown")

    def test_unchanged_unknown_is_skipped_then_rollover_is_rechecked(self):
        self.index.answers = iter(replies(state="unknown", outcome_quote="", outcome_id=""))
        asyncio.run(refresh_plan_states(self.index))
        self.assertEqual(asyncio.run(refresh_plan_states(self.index))["reviewed"], 0)
        self.rows["later"]["revision"] = "l2"
        self.rows["later"]["text"] = "The clinic called me back; I have not called them."
        self.buckets["later"]["content"] = self.rows["later"]["text"]
        self.index.answers = iter(replies(state="unknown", outcome_quote="", outcome_id=""))
        result = asyncio.run(refresh_plan_states(self.index))
        self.assertEqual(result["reviewed"], 1)


if __name__ == "__main__":
    unittest.main()
