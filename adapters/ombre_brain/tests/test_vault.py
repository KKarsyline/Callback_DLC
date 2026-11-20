import asyncio
import hashlib
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml

from adapters.ombre_brain import OmbreVault, host_recall, local_recall
from adapters.ombre_brain.demo import EXAMPLE, QUOTE, fixture_model, validation_request
from adapters.ombre_brain.vault import PolicyUnavailable


class PlainHeader(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def snapshot(root):
    return {p.relative_to(root): (p.stat().st_mode, p.stat().st_mtime_ns,
                                   p.read_bytes() if p.is_file() else None)
            for p in [root, *root.rglob("*")]}


class VaultTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.root = self.temp / "buckets"
        self.folder = self.root / "dynamic" / "example"
        self.folder.mkdir(parents=True)
        self.vault = OmbreVault(self.root)

    def write(self, filename="sample.md", *, folder=None, body=QUOTE, **meta):
        folder = folder or self.folder
        folder.mkdir(exist_ok=True, parents=True)
        path = folder / filename
        header = {"id": "example-event", "type": "dynamic", **meta}
        path.write_text("---\n" + yaml.dump(header, Dumper=PlainHeader, allow_unicode=True) +
                        "---\n\n" + body + "\n", encoding="utf-8")
        return path

    async def test_literal_body_and_stable_id_after_rename(self):
        path = self.write(body="A [[workbench]]\n@Example: labelled words.",
                          title="Hidden title", quotes=[{"text": "Hidden quote"}])
        before = await self.vault.get("example-event")
        self.assertNotIn("Hidden", str(before))
        moment, = self.vault.moments.list_all(limit=1)
        self.assertEqual(before["content"], moment["text"])
        self.assertNotIn("source_ref", moment["metadata"])
        path.rename(path.with_name("renamed.md"))
        self.assertEqual(moment, self.vault.moments.get(moment["moment_id"]))
        self.assertIsNone(await self.vault.get("../sample.md"))
        self.assertEqual([], self.vault.moments.list_all(limit=0))

    async def test_visibility_agrees_across_every_port(self):
        normal = self.write()
        old, = (await self.vault.get("example-event"))["metadata"]["source_raw_event_ids"]
        for change in [
            {"type": t} for t in ("archived", "tombstone", "permanent", "letter", "plan", "feel", "self", "i", "unknown")
        ] + [
            {"source_tool": "letter"}, {"tags": "demo, __letter__"}, {"locked_by": "human"},
            {"tags": ["__i_candidate__"]}, {"domain": ["self"]}, {"source_tool": "I"},
            {"lock_type": "permanent"}, {"deleted_at": "yesterday"}, {"tombstone": True},
            {"protected": True}, {"digested": True}, {"dont_surface": True}, {"pinned": True},
            {"anchor": True}, {"resolved": True}, {"dont_surface": []}, {"tags": {}},
            {"created": "private text"}, {"future_policy_field": True},
        ]:
            with self.subTest(change=change):
                self.write(**change)
                self.assertIsNone(await self.vault.get("example-event"))
                self.assertEqual([], await self.vault.list_all(include_archive=True))
                self.assertIsNone(self.vault.moments.get("example-event:body"))
                self.assertIn(old, self.vault.superseded_ids())
        normal.unlink()
        self.assertIn(old, self.vault.superseded_ids())
        self.write()
        self.assertNotIn(old, self.vault.superseded_ids())

    async def test_detach_has_bucket_scope_and_conflicts_fail_closed(self):
        ref = {"ref": "src_" + "a" * 64, "ranges": [[2, 4]]}
        active = {**ref, "status": "active"}
        self.write(source_links=[active], source_refs=[ref])
        other = self.write("other.md", id="other", source_refs=[ref])
        key, = (await self.vault.get("example-event"))["metadata"]["source_raw_event_ids"]
        other_key, = (await self.vault.get("other"))["metadata"]["source_raw_event_ids"]
        for links, refs in [
            ([{**active, "status": "detached"}], []),
            ([active, {**active, "ranges": [[8, 9]], "status": "detached"}], [ref]),
            ([active], []), ([ref], [ref]), ([], [ref]),
            ([{**active, "ref": "../../outside"}], []), ([active, active], [ref]),
            ([{**active, "ranges": [[True, 2]]}], [ref]), (None, []),
        ]:
            with self.subTest(links=links, refs=refs):
                self.write(source_links=links, source_refs=refs)
                self.assertIsNone(await self.vault.get("example-event"))
                self.assertEqual({key}, self.vault.superseded_ids())
                self.assertIsNotNone(await self.vault.get("other"))
        archive = self.root / "archive"
        archive.mkdir()
        other.rename(archive / other.name)
        self.assertEqual({key, other_key}, self.vault.superseded_ids())

    async def test_private_trees_links_and_ambiguous_files(self):
        for folder in ("letters", "plans", "_sources", "_ledger", "archive", "feel", "permanent",
                       "dynamic/.backup", "dynamic/example/nested"):
            self.write(folder=self.root / folder)
        external = self.write(folder=self.temp / "outside")
        (self.folder / "symbolic.md").symlink_to(external)
        (self.root / "dynamic" / "alias").symlink_to(external.parent, target_is_directory=True)
        os.link(external, self.folder / "hard.md")
        os.mkfifo(self.folder / "pipe.md")
        self.assertEqual([], self.vault.moments.list_all())
        first = self.write()
        duplicate = self.write("second.md", dont_surface=True)
        self.assertEqual([], self.vault.moments.list_all())
        duplicate.unlink()
        for data in (b"no header", b"---\nid: sample\ntype: dynamic\n",
                     b"---\nid: x\ntype: dynamic\ndont_surface: true\ndont_surface: false\n---\nbody",
                     b"---\nid: &a x\ntype: dynamic\nname: *a\n---\nbody",
                     b"---\n!!python/object:fixture {}\n---\nbody", b"\xff"):
            first.write_bytes(data)
            self.assertEqual([], self.vault.moments.list_all())

    async def test_real_core_revalidates_edits_suppression_and_removal(self):
        event = self.write(body=QUOTE + " during the blackout.")
        async with local_recall(self.root, scratch_parent=self.temp, dehydrator=fixture_model()) as (index, recall):
            await index.warm()
            self.assertEqual(1, (await index.rebuild(limit=5))["indexed"])
            await index.warm()
            hit, = await index.search(["amber lantern"])
            old = validation_request([hit])
            delivered, = (await recall.validate(old))["candidates"]
            self.assertIn(QUOTE, delivered["source_window"])
            self.write(body=QUOTE + " until Thursday.")
            self.assertEqual([], (await recall.validate(old))["candidates"])
            self.assertEqual([], await index.search(["amber lantern"]))
            self.assertEqual(1, (await index.rebuild(limit=5))["indexed"])
            await index.warm()
            updated, = await index.search(["amber lantern"])
            self.assertNotEqual(hit["evidence_version"], updated["evidence_version"])
            self.assertIn("Thursday", updated["source_window"])
            self.write(body=QUOTE + " until Thursday.", dont_surface=True)
            self.assertEqual([], (await recall.validate(validation_request([updated])))["candidates"])
            self.assertEqual([], await index.search(["amber lantern"]))
            event.unlink()
            self.assertEqual(1, (await index.rebuild(limit=5))["stale_removed"])

    async def test_real_core_on_read_only_fixture(self):
        root = self.temp / "fixture"
        shutil.copytree(EXAMPLE, root)
        for path in [*root.rglob("*"), root]:
            mode = stat.S_IMODE(path.stat().st_mode)
            path.chmod(0o555 if path.is_dir() else 0o444)
            self.addCleanup(path.chmod, mode)
        before = snapshot(root)
        async with local_recall(root, scratch_parent=self.temp, dehydrator=fixture_model()) as (index, recall):
            db = Path(index.db_path)
            self.assertNotIn(root, db.parents)
            await index.warm()
            report = await index.rebuild(limit=10)
            self.assertEqual((1, 1, 0), (report["scanned"], report["indexed"], report["failed"]))
            await index.warm()
            hit, = await index.search(["amber lantern"])
            row, = (await recall.validate(validation_request([hit])))["candidates"]
            self.assertEqual(QUOTE, row["excerpt"])
            self.assertNotIn("Metadata-only", row["source_window"])
            self.assertEqual([], await index.search(["silent"]))
            await recall.prepare(validation_request([]))
        self.assertFalse(db.parent.exists())
        self.assertTrue(recall._worker.done())
        self.assertEqual(before, snapshot(root))

    async def test_withdrawal_while_generating_cues(self):
        self.write()
        model = fixture_model()
        completions = model.client.chat.completions
        original = completions.create
        started = asyncio.Event()
        resume = asyncio.Event()

        async def delayed(**args):
            started.set()
            await resume.wait()
            return await original(**args)

        completions.create = delayed
        async with local_recall(self.root, scratch_parent=self.temp, dehydrator=model) as (index, _):
            task = asyncio.create_task(index.rebuild(limit=1))
            try:
                await asyncio.wait_for(started.wait(), 2)
                self.write(digested=True)
            finally:
                resume.set()
            report = await task
            self.assertEqual((0, 0), (report["indexed"], report["failed"]))
            self.assertEqual([], await index.search(["amber lantern"]))

    async def test_scratch_inside_vault_is_rejected(self):
        with self.assertRaises(ValueError):
            async with local_recall(self.root, scratch_parent=self.folder):
                self.fail("accepted state directory in vault")
        self.assertEqual([], list(self.folder.iterdir()))

    async def test_durable_authorized_archive_and_authored_update(self):
        from assoc_plans import refresh_plan_states

        plan = "I will replace the amber lantern bulb on Friday."
        outcome = "I replaced the amber lantern bulb on Friday."
        self.write(id="repair", body=plan, created="2024-01-15")
        self.write("other.md", id="unapproved", body=QUOTE)
        path = self.write("repair.md", folder=self.root / "archive" / "example",
                          id="record", type="archived", body=outcome, created="2024-01-16")
        policy = {"dynamic_ids": ["repair"], "archives": {"example/repair.md": {
            "id": "record", "original_type": "dynamic", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }}, "updates": [["repair", "record"]]}
        policy_path = self.temp / "audience.json"
        policy_path.write_text(json.dumps(policy))
        state = self.temp / "recall-state"

        def ports():
            return OmbreVault(self.root, read_policy=lambda: json.loads(policy_path.read_text()))

        model = fixture_model()

        async def respond(*, messages, **options):
            prompt = messages[-1]["content"]
            if "Return {\"cues\"" in prompt:
                quote = plan if plan in prompt else outcome
                payload = {"cues": [{"cue_type": "unfinished" if quote == plan else "detail",
                                     "quote": quote, "cue_text": "lantern repair",
                                     "plan_state": "planned", "plan_quote": plan}]}
            elif '"keep"' in prompt:
                payload = {"keep": [0], "plans": [{"id": 0, "state": "planned", "quote": plan}]}
            else:
                self.assertIn(outcome, prompt)
                payload = {"items": [{"id": "0", "plan_state": "planned", "plan_quote": plan,
                                      "outcome_state": "completed", "outcome_quote": outcome,
                                      "outcome_moment_id": "record:body"}]}
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))])

        model.client.chat.completions.create = respond
        vault = ports()
        self.assertIsNone(await self.vault.get("record"))
        self.assertIsNone(await vault.get("unapproved"))
        async with host_recall(vault, state_dir=state, model=model, embeddings=None) as (index, recall):
            await index.warm()
            self.assertEqual(2, (await index.rebuild(limit=10))["indexed"])
            self.assertEqual(1, (await refresh_plan_states(index))["reviewed"])
            await index.warm()
            hits = await index.search(["lantern"])
            request = validation_request(hits)
            rows = (await recall.validate(request))["candidates"]
            ongoing, = [r for r in rows if r["event_id"] == "repair"]
            archived, = [r for r in rows if r["event_id"] == "record"]
            self.assertTrue(archived["archived"])
            self.assertEqual((outcome, "completed"), (ongoing["outcome_quote"], ongoing["outcome_state"]))

        # New port objects and no model: resolved evidence comes from durable state plus live source.
        async with host_recall(ports(), state_dir=state, model=None, embeddings=None) as (_, recall):
            self.assertEqual(2, len((await recall.validate(request))["candidates"]))
            policy["updates"] = []
            policy_path.write_text(json.dumps(policy))
            self.assertEqual([], (await recall.validate(request))["candidates"])
            policy["updates"] = [["repair", "record"]]
            archive_grants = policy.pop("archives")
            policy_path.write_text(json.dumps(policy))
            self.assertIsNone(await vault.get("record"))
            self.assertEqual([], (await recall.validate(request))["candidates"])
            policy["archives"] = archive_grants
            policy_path.write_text(json.dumps(policy))
            self.assertEqual(2, len((await recall.validate(request))["candidates"]))
            path.write_text(path.read_text() + "\nAnother entry.\n")
            self.assertIsNone(await vault.get("record"))
            self.assertEqual([], vault.list_edges())
            self.assertEqual([], (await recall.validate(request))["candidates"])
            policy_path.unlink()
            with self.assertRaises(PolicyUnavailable):
                vault.moments.list_all()
