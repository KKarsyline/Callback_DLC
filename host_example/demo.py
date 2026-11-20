"""Offline ports for a fictional exhibit workshop. Run from the package root."""
from __future__ import annotations

import asyncio
import json as codec
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from host_example.coordination import ChatHost, DirectResult, LocalRegistry
from host_example.direct_callback import CallbackItem, callback_result
from service import open_service

EVENT = "exhibit-crate"
QUOTE = "The brass latch kept the glass tiles inside the crate."
SOURCE = "During a tilt test at the exhibit workshop, we tipped the crate. " + QUOTE
QUESTION = "How could we keep loose pieces inside a tilted display case?"
DIRECT = "The display case has a hinged lid."


class Moments:
    def list_all(self):
        return [self.get("tilt-test")]

    def get(self, moment_id):
        if moment_id == "tilt-test":
            return {"moment_id": moment_id, "bucket_id": EVENT, "section": "body",
                    "source_id": "workshop-log", "text": SOURCE}


class Buckets:
    def __init__(self, root):
        self.base_dir = str(root)
        self.content = SOURCE

    async def list_all(self, include_archive=True):
        return [await self.get(EVENT)]

    async def get(self, event):
        if event == EVENT:
            return {"id": EVENT, "content": self.content, "type": "dynamic",
                    "metadata": {"type": "dynamic", "date": "2023-04-01",
                                 "source_raw_event_ids": [101]}}


class RawEvents:
    def __init__(self):
        self.revoked = set()

    def superseded_ids(self):
        return self.revoked.copy()


class Embeddings:
    enabled = True
    model = "synthetic-tilt-v1"

    def __init__(self):
        self.literal_vectors = {}

    @staticmethod
    def vector(text):
        return [1.0, 0.0] if "tilt" in text.lower() else [0.0, 1.0]

    async def _generate_embedding(self, text, *, kind):
        return self.vector(text)

    async def generate_query_embeddings(self, texts):
        return [self.vector(text) for text in texts]

    async def wait_query_embedding(self, text, *, timeout_ms):
        return self.literal_vectors.get(text)


class ScriptedModel:
    """Only the external completion calls are scripted. Unknown inputs abstain."""

    model = _model = "scripted-example"
    api_available = True

    def __init__(self, budget_db):
        self.budget_db = budget_db
        self.client = self
        self.chat = SimpleNamespace(completions=self)

    def with_options(self, **kwargs):
        return self

    def _completion_options(self, **kwargs):
        return kwargs

    async def create(self, *, messages, **kwargs):
        system, user = (entry["content"] for entry in messages)
        if '"cues"' in user and SOURCE in user:
            answer = {"cues": [{"cue_text": "contain loose pieces when tilted",
                                 "cue_type": "detail", "quote": QUOTE}]}
        elif '"keep"' in user and QUOTE in user:
            answer = {"keep": [0]}
        elif '"situations"' in system:
            answer = {"situations": ["tilted container"], "possible_questions": ["latch for loose pieces"]}
        elif '"selections"' in system:
            rows = codec.loads(user)["candidates"]
            relevant = '"use"' not in system or codec.loads(user)["context"] == QUESTION
            answer = {"selections": [{"id": row["id"], "use": "mention"} for row in rows
                                     if relevant and QUOTE in row["excerpt"]][:1]}
        else:
            answer = {}
        message = SimpleNamespace(content=codec.dumps(answer))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    async def _post(self, path, *, json, _retries):
        payload = codec.loads(json["messages"][-1]["content"])
        answer = {"mode": "none", "pick": None, "use": None}
        for row in payload.get("ranked_sources", []):
            if payload.get("current") == QUESTION and QUOTE in row["excerpt"]:
                answer = {"mode": "pick", "pick": row["id"], "use": "mention"}
                break
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"choices": [{"message": {"content": codec.dumps(answer)}}]})


class ChatRows:
    """One serial conversation per branch. Only committed visible messages enter rows."""

    def __init__(self):
        self.paths = {}

    def append(self, session, branch, role, content, *, expected_leaf=None):
        path = self.paths.setdefault((session, branch), [])
        if expected_leaf is not None and (not path or path[-1]["id"] != expected_leaf):
            raise ValueError("the active leaf changed before commit")
        row_id = uuid4().hex
        path.append({"id": row_id, "channel_id": branch, "role": role, "content": content})
        return row_id

    async def list_active_path(self, session, *, channel_id, limit, latest):
        return list(self.paths[(session, channel_id)][-limit:])

    async def completed_assistant_path(self, session, branch, leaf):
        rows = self.paths[(session, branch)]
        if rows[-1]["id"] != leaf:
            raise ValueError("stale leaf")
        return [row["id"] for row in rows if row["role"] == "assistant" and row["content"].strip()][-6:]


class ExampleReceipt:
    """Synthetic native receipt for one prepared direct lookup."""

    def __init__(self):
        self.state = "prepared"
        self.accepted_ids = ()

    async def accept(self, event_ids):
        if self.state == "prepared":
            self.state = "accepted"
            self.accepted_ids = tuple(event_ids)

    async def abort_if_prepared(self):
        if self.state == "prepared":
            self.state = "discarded"


class Workshop:
    def __init__(self, root):
        self.session = uuid4().hex
        self.branch = "main"
        self.storage = ChatRows()
        self.sources, self.raw = Buckets(root), RawEvents()
        self.models, self.embeddings = ScriptedModel(root / "gateway-budget.sqlite3"), Embeddings()
        self.runtime = open_service(state_dir=root, moments=Moments(), buckets=self.sources,
                                    raw_events=self.raw, embeddings=self.embeddings, model=self.models)

    async def start(self):
        self.index, self.service = await self.runtime.__aenter__()
        self.host = ChatHost(self.storage, LocalRegistry(self.service), self.models)
        report = await self.index.rebuild(limit=10)
        if report["failed"] or not report["complete"]:
            raise RuntimeError(f"index build incomplete: {report}")
        await self.index.warm()

    async def close(self):
        await self.host.drain()
        await self.runtime.__aexit__(None, None, None)

    async def turn(self, text, *, route="none", direct_event="display-case",
                   mention=True, allow_assoc=True):
        user_id = self.storage.append(self.session, self.branch, "user", text)

        async def plan():
            return route, text

        async def lookup(route, query):
            if route == "direct":
                self.embeddings.literal_vectors[query] = self.embeddings.vector(query)
                item = CallbackItem(direct_event, f"{direct_event}:0123456789abcdef", DIRECT)
                self.last_direct_receipt = ExampleReceipt()
                return await callback_result([item], self.last_direct_receipt)
            return DirectResult()

        async def reply_and_commit(direct, extra):
            if direct.receipt:
                # The synthetic reply model has accepted the assembled prompt.
                await direct.receipt.accept(direct.event_ids)
            visible = [DIRECT] if DIRECT in direct.text else []
            if mention and QUOTE in extra:
                visible.append(QUOTE)
            reply = " ".join(visible) or "Let's check the display case."
            assistant = self.storage.append(self.session, self.branch, "assistant", reply,
                                            expected_leaf=user_id)
            self.last_context = extra
            return assistant, reply

        return await self.host.turn(self.session, self.branch, user_id, text, plan=plan,
                                    lookup=lookup, reply_and_commit=reply_and_commit,
                                    allow_assoc=allow_assoc)

    def receipts(self):
        with sqlite3.connect(self.service.db_path) as db:
            return db.execute("SELECT assistant_message_id, event_id, outcome FROM selections "
                              "WHERE outcome IS NOT NULL ORDER BY rowid").fetchall()


async def main(root):
    demo = Workshop(root)
    try:
        await demo.start()
        await demo.turn("We are designing a display case.")
        await demo.host.drain()
        assistant, reply = await demo.turn(QUESTION)
        await demo.host.drain()
        if (assistant, EVENT, "mentioned") not in demo.receipts():
            raise RuntimeError("prepared recall did not produce a mention receipt")
        print("Prepared reply:", reply)
        _, reply = await demo.turn(QUESTION, route="direct")
        if reply != DIRECT:
            raise RuntimeError("expected the direct answer during cooldown")
        print("Next reply during cooldown:", reply)
        print("Settled receipts:", demo.receipts())
    finally:
        await demo.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        asyncio.run(main(Path(directory)))
