"""Cooldown is based on completed assistant turns on the active path."""
import tempfile
import unittest
from unittest.mock import patch

from assoc_recall import AssociativeRecall, SCHEMA_VERSION


class FakeIndex:
    async def resolve(self, candidate_id, expected_revision=None, quote=None):
        if candidate_id != "c1" or expected_revision != "v1":
            return None
        return {"candidate_id": "c1", "event_id": "event1", "evidence_version": "v1",
                "source_window": "verified source", "excerpt": "distinctive detail",
                "date": "2025-01-02", "archived": False}

    async def search(self, texts, limit=12, *, vector=None, allow_embed=True):
        return []


class FakeDehydrator:
    api_available = False


class FakeEmbedding:
    async def wait_query_embedding(self, text, *, timeout_ms=5):
        return None


class AssocCooldownTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = AssociativeRecall({"buckets_dir": self.tmp.name, "state_dir": self.tmp.name},
                                         FakeIndex(), FakeDehydrator(), FakeEmbedding())
        self.base = {"session_id": "s", "branch_id": "b", "user_message_id": "u",
                     "message_version": "u", "request_id": "u"}

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def mention(self, assistant_id, *, branch="b", event="event1", expired=False):
        scope = {**self.base, "branch_id": branch, "request_id": "request-" + assistant_id}
        token = self.service._reserve(scope, {"candidate_id": "c1", "event_id": event,
                                              "evidence_version": "v1"})
        body = {"schema_version": SCHEMA_VERSION, "scope": scope, "selection_token": token,
                "assistant_message_id": assistant_id, "outcome": "mentioned"}
        self.assertEqual(self.service.finish(body)["status"], "ok")
        if expired:
            with self.service._db() as db:
                db.execute("UPDATE selections SET expires_at=0 WHERE selection_token=?", (token,))

    def test_sixth_turn_is_cooled_but_seventh_is_not(self):
        self.mention("a1")
        self.assertTrue(self.service._is_cooled({**self.base, "last_assistant_ids": [
            "a1", "a2", "a3", "a4", "a5", "a6"]}, "event1"))
        self.assertFalse(self.service._is_cooled({**self.base, "last_assistant_ids": [
            "a2", "a3", "a4", "a5", "a6", "a7"]}, "event1"))

    def test_time_gap_does_not_expire_turn_cooldown(self):
        self.mention("a1", expired=True)
        self.assertTrue(self.service._is_cooled({**self.base, "last_assistant_ids": ["a1"]}, "event1"))

    def test_short_time_does_not_cool_a_mention_outside_path(self):
        self.mention("a1")
        self.assertFalse(self.service._is_cooled({**self.base, "last_assistant_ids": ["a2"]}, "event1"))

    def test_fork_uses_exact_active_path_ids(self):
        self.mention("fork-a1", branch="fork-a")
        self.assertTrue(self.service._is_cooled({**self.base, "branch_id": "fork-b",
                                                 "last_assistant_ids": ["fork-a1"]}, "event1"))
        self.assertFalse(self.service._is_cooled({**self.base, "branch_id": "fork-b",
                                                  "last_assistant_ids": ["other-a1"]}, "event1"))

    def test_duplicate_ids_are_deduplicated_and_unused_does_not_cool(self):
        self.mention("a1")
        scope = {**self.base, "last_assistant_ids": ["a1", "a1"]}
        self.assertTrue(self.service._is_cooled(scope, "event1"))
        token = self.service._reserve({**self.base, "request_id": "unused"},
                                      {"candidate_id": "c1", "event_id": "event2",
                                       "evidence_version": "v1"})
        self.assertEqual(self.service.finish({"schema_version": SCHEMA_VERSION,
            "scope": {**self.base, "request_id": "unused"}, "selection_token": token,
            "assistant_message_id": "a-unused", "outcome": "unused"})["status"], "ok")
        self.assertFalse(self.service._is_cooled({**self.base, "last_assistant_ids": ["a-unused"]}, "event2"))

    def test_empty_path_is_meaningful_and_missing_path_uses_legacy_ttl(self):
        self.mention("a1")
        self.assertFalse(self.service._is_cooled({**self.base, "last_assistant_ids": []}, "event1"))
        self.assertTrue(self.service._is_cooled(self.base, "event1"))


if __name__ == "__main__":
    unittest.main()
