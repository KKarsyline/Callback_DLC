import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from assoc_recall import AssociativeRecall


class TokenRecheck(unittest.IsolatedAsyncioTestCase):
    async def test_scope_expiry_settlement_and_read_only_recheck(self):
        scope = dict(session_id="s", branch_id="b", user_message_id="u", message_version="v", request_id="q")
        claim = dict(candidate_id="m", event_id="e", evidence_version="1", excerpt="sample")
        row = dict(**claim, source_window="sample text")
        with tempfile.TemporaryDirectory() as root:
            service = AssociativeRecall({"state_dir":root}, SimpleNamespace(resolve=AsyncMock(return_value=row)), None, None)
            req = dict(schema_version="assoc.v1", scope=scope, candidates=[claim])
            selection, = (await service.validate(req))["candidates"]
            bound = dict(**claim, selection_token=selection["selection_token"])
            req.update(recheck_only=True, candidates=[bound])
            self.assertEqual((await service.validate(req))["candidates"][0]["selection_token"], selection["selection_token"])
            for field in ("branch_id", "request_id", "session_id"):
                self.assertFalse((await service.validate({**req,"scope":{**scope,field:"other"}}))["candidates"])
            for field in ("selection_token", "candidate_id", "event_id", "excerpt", "evidence_version"):
                self.assertFalse((await service.validate({**req,"candidates":[{**bound,field:"other"}]}))["candidates"])
            with sqlite3.connect(service.db_path) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM selections").fetchone()[0], 1)
                db.execute("UPDATE selections SET expires_at=0")
            self.assertFalse((await service.validate(req))["candidates"])
            with sqlite3.connect(service.db_path) as db:
                db.execute("UPDATE selections SET expires_at=9999999999, outcome='unused'")
            self.assertFalse((await service.validate(req))["candidates"])
