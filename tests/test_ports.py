import asyncio
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest

from callback_ids import valid_moment_id
from embedding_engine import EmbeddingEngine
from memory_edges import MemoryEdgeStore
from pipeline.memory_gateway_planner import select_recent_planner_messages
from service import open_service


class Provider:
    def __init__(self):
        self.embeddings = self
        self.calls = []

    async def create(self, **args):
        self.calls.append(args)
        await asyncio.sleep(.01)
        return SimpleNamespace(data=[SimpleNamespace(embedding=[1., 0.])])


class HostPortTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_query_joins_existing_request_only(self):
        provider = Provider()
        engine = EmbeddingEngine(provider, "fixture")
        self.assertIsNone(await engine.wait_query_embedding("absent", timeout_ms=1))
        self.assertEqual(provider.calls, [])
        literal = asyncio.create_task(engine._generate_embedding("subject", kind="query"))
        self.assertEqual(await engine.wait_query_embedding("subject", timeout_ms=1000), [1., 0.])
        self.assertEqual(await literal, [1., 0.])
        self.assertEqual(engine.peek_query_embedding("subject"), [1., 0.])
        self.assertEqual(len(provider.calls), 1)

    async def test_monthly_budget_and_reservations_persist_on_reopen(self):
        from assoc_budget import reserve
        with TemporaryDirectory() as root:
            ports = dict(state_dir=root, moments=None, buckets=None, raw_events=None, embeddings=None, model=None)
            async with open_service(**ports) as (index, service):
                reserve(index.db_path, ceiling_usd=1, text="fixture")
                token = service._reserve({"session_id":"s","branch_id":"b","request_id":"q"},
                                         {"candidate_id":"m","event_id":"e","evidence_version":"v"})
            async with open_service(**ports) as (index, service):
                with index._connect() as db:
                    self.assertGreater(db.execute("SELECT microdollars FROM assoc_budget").fetchone()[0], 0)
                    self.assertEqual(db.execute("SELECT count(*) FROM selections WHERE selection_token=?", (token,)).fetchone()[0], 1)

    def test_simple_and_composite_ids(self):
        self.assertTrue(valid_moment_id("moment", "bucket"))
        parent = "x"*200
        self.assertTrue(valid_moment_id(parent+":0123456789abcdef", parent))
        self.assertFalse(valid_moment_id("other:0123456789abcdef", parent))
        self.assertFalse(valid_moment_id("x:0123456789abcdeG", "x"))

    def test_current_edges_and_recent_visible_only(self):
        rows = []
        port = MemoryEdgeStore({"list_edges": lambda: rows})
        self.assertEqual(port.list_edges(), [])
        rows.append({"relation_type": "updates", "source": "a", "target": "b"})
        self.assertEqual(port.list_edges(), rows)
        chat = [{"role":"user", "id":"1", "content":"public"},
                {"role":"user", "id":"2", "content":"hidden", "metadata":{"private":True}},
                {"role":"assistant", "id":"3", "status":"cancelled", "content":"partial"}]
        chat.extend([{"role":"assistant", "id":"4", "visibility":"visible", "metadata":{"visibility":"private"}, "content":"private"},
                     {"role":"assistant", "id":"5", "state":"streaming", "content":"unfinished"},
                     {"role":"assistant", "id":"6", "is_complete":False, "content":"unfinished"}])
        self.assertEqual(select_recent_planner_messages(chat), [{"role":"user", "content":"public"}])
