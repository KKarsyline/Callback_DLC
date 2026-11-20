import asyncio
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from threading import Event, get_ident
from unittest.mock import Mock, patch

from adapters.ombre_brain import OmbreVault, host_recall
from adapters.ombre_brain.demo import EXAMPLE, fixture_model, validation_request
from adapters.ombre_brain.vault import PolicyUnavailable


BID = "a0b1c2d3e4f5"


class PolicyTest(unittest.IsolatedAsyncioTestCase):
    async def test_async_ports_scan_once_off_the_event_loop(self):
        policy = {"dynamic_ids": [BID]}
        read_threads = []

        def read():
            read_threads.append(get_ident())
            return policy

        vault = OmbreVault(EXAMPLE, read_policy=read)
        with patch.object(vault, "_scan", wraps=vault._scan) as scan:
            bucket = await vault.get(BID)
            self.assertEqual([bucket], await vault.list_all(include_archive=True))
            moment = await vault.moments.get_async(BID + ":body")
            self.assertEqual([moment], await vault.moments.list_all_async(limit=1))
            self.assertEqual(set(), await vault.superseded_ids_async())
            self.assertEqual([], await vault.list_edges_async())
            self.assertEqual(6, scan.call_count)
            self.assertEqual(6, len(read_threads))
            self.assertNotIn(get_ident(), read_threads)

            policy.clear()
            self.assertIsNone(await vault.moments.get_async(BID + ":body"))
            for enumerate_rows in (vault.list_all, vault.moments.list_all_async,
                                   vault.superseded_ids_async, vault.list_edges_async):
                with self.subTest(port=enumerate_rows.__name__), self.assertRaises(PolicyUnavailable):
                    await enumerate_rows()
            self.assertEqual(6, scan.call_count)

    async def test_incomplete_snapshots_deny_reads_and_abort_enumeration(self):
        grant = {"id": BID, "original_type": "dynamic", "sha256": "a" * 64}
        cases = [
            None, [], {}, {"updates": []}, {"dynamic_ids": None}, {"dynamic_ids": [""]},
            {"dynamic_ids": [], "schema_version": 2}, {"dynamic_ids": [BID], "deny": [BID]},
            {"dynamic_ids": [BID], "archives": []}, {"dynamic_ids": [BID], "updates": {}},
            {"dynamic_ids": [BID], "updates": [[BID]]}, {"dynamic_ids": [BID], "updates": [[BID, 7]]},
            {"dynamic_ids": [BID], "archives": {"one.md": {}}},
            {"dynamic_ids": [BID], "archives": {"../one.md": grant}},
            {"dynamic_ids": [BID], "archives": {"one\x00.md": grant}},
            {"dynamic_ids": [BID], "archives": {"one.md": {**grant, "sha256": ""}}},
            {"dynamic_ids": [BID], "archives": {"one.md": {**grant, "revoked": True}}},
        ]
        for policy in cases:
            with self.subTest(policy=policy):
                vault = OmbreVault(EXAMPLE, read_policy=lambda: policy)
                self.assertIsNone(await vault.get(BID))
                self.assertIsNone(vault.moments.get(BID + ":body"))
                with self.assertRaises(PolicyUnavailable):
                    vault.moments.list_all()
                with self.assertRaises(PolicyUnavailable):
                    await vault.list_all(include_archive=True)
                with self.assertRaises(PolicyUnavailable):
                    vault.superseded_ids()
                with self.assertRaises(PolicyUnavailable):
                    vault.list_edges()

    async def test_rebuild_preserves_saved_hit_until_confirmed_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "policy.json"
            allowed = {"dynamic_ids": [BID]}
            path.write_text(json.dumps(allowed))
            reader = Mock(side_effect=lambda: json.loads(path.read_text()))
            vault = OmbreVault(EXAMPLE, read_policy=reader)
            async with host_recall(vault, state_dir=root / "state", model=fixture_model(),
                                   embeddings=None) as (index, recall):
                self.assertEqual(vault.list_edges_async, index.config["list_edges_async"])

                def records():
                    with closing(index._connect()) as db:
                        return [list(db.execute(f"SELECT * FROM {table} ORDER BY rowid"))
                                for table in ("assoc_cues", "assoc_state")]

                self.assertEqual(1, (await index.rebuild(limit=10))["indexed"])
                await index.warm()
                hit, = await index.search(["amber lantern"])
                request = validation_request([hit])
                saved = records()
                self.assertEqual([1, 1], list(map(len, saved)))

                for content in (None, "", "{", "{}", '{"dynamic_ids": [], "schema": 2}'):
                    with self.subTest(content=content):
                        if content is None:
                            path.unlink()
                        else:
                            path.write_text(content)
                        self.assertEqual([], (await recall.validate(request))["candidates"])
                        with self.assertRaises(PolicyUnavailable):
                            await index.rebuild(limit=10)
                        self.assertEqual(saved, records())

                # Fail only the moment, bucket, or retired-source enumeration.
                for position in range(3):
                    with self.subTest(position=position):
                        reader.side_effect = [allowed] * position + [TypeError("reader failed"), allowed]
                        with self.assertRaises(PolicyUnavailable):
                            await index.rebuild(limit=10)
                        self.assertEqual(saved, records())

                reader.side_effect = lambda: json.loads(path.read_text())
                path.write_text(json.dumps(allowed))
                row, = (await recall.validate(request))["candidates"]
                self.assertEqual(hit["evidence_version"], row["evidence_version"])

                path.write_text('{"dynamic_ids": []}')
                self.assertEqual([], (await recall.validate(request))["candidates"])
                self.assertEqual(1, (await index.rebuild(limit=10))["stale_removed"])
                self.assertEqual([[], []], records())

    async def test_overlapping_readers_keep_their_own_policy_snapshot(self):
        policy = {"dynamic_ids": [BID]}
        vault = OmbreVault(EXAMPLE, read_policy=lambda: policy)
        scanning, resume = Event(), Event()
        scan = vault._scan

        def paused_scan(*args):
            scanning.set()
            if not resume.wait(5):
                raise TimeoutError("test did not release scan")
            return scan(*args)

        with patch.object(vault, "_scan", side_effect=paused_scan):
            listing = asyncio.create_task(vault.moments.list_all_async())
            try:
                self.assertTrue(await asyncio.to_thread(scanning.wait, 5))
                policy.clear()
                self.assertIsNone(await vault.get(BID))
                with self.assertRaises(PolicyUnavailable):
                    await vault.list_all()
            finally:
                resume.set()
                moments = await listing
        self.assertEqual([BID], [m["bucket_id"] for m in moments])
        with self.assertRaises(PolicyUnavailable):
            vault.moments.list_all()
