"""Gateway active-path assistant turn IDs passed to associative recall."""
import unittest

from pipeline import associative_recall
from pipeline.association_scheduler import scope_for


class AssocTurnScopeTests(unittest.TestCase):
    def test_scope_keeps_last_six_completed_nonempty_assistant_turns(self):
        rows = [{"id": "a0", "role": "assistant", "content": "old"}]
        rows.extend({"id": f"a{i}", "role": "assistant", "content": f"reply {i}"}
                    for i in range(1, 8))
        rows.extend([
            {"id": "a-empty", "role": "assistant", "content": "   "},
            {"id": "a-progress", "role": "assistant", "content": "draft", "status": "in_progress"},
            {"id": "a-tool", "role": "assistant", "content": "", "tool_calls": [{"name": "x"}]},
            {"id": "system-1", "role": "system", "content": "ignored"},
            {"id": "u1", "role": "user", "content": "question"},
        ])
        scope = scope_for("s", "branch", "u1", rows)
        self.assertEqual(scope["last_assistant_ids"], [f"a{i}" for i in range(2, 8)])

    def test_scope_rejects_rows_from_another_branch(self):
        rows = [
            {"id": "same-path", "role": "assistant", "content": "valid", "branch_id": "b1"},
            {"id": "other-path", "role": "assistant", "content": "fork", "branch_id": "b2"},
        ]
        scope = scope_for("s", "b1", "u1", rows)
        self.assertEqual(scope["last_assistant_ids"], ["same-path"])

    def test_scope_and_wire_preserve_empty_and_nonempty_ids(self):
        base = {"session_id": "s", "branch_id": "b", "user_message_id": "u",
                "message_version": "u", "request_id": "u"}
        self.assertEqual(associative_recall._scope({**base, "last_assistant_ids": []})[
            "last_assistant_ids"], [])
        self.assertEqual(associative_recall._wire_scope({**base, "last_assistant_ids": ["a1", "a2"]})[
            "last_assistant_ids"], ["a1", "a2"])
        self.assertNotIn("last_assistant_ids", associative_recall._scope(base))


if __name__ == "__main__":
    unittest.main()
