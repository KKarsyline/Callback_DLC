"""Run from the package root: python -B -m adapters.ombre_brain.demo"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from .vault import local_recall


EXAMPLE = Path(__file__).with_name("example_vault")
QUOTE = "The amber lantern stayed on the workbench"


class FixtureCompletions:
    """Offline model boundary. Only the bundled example sentence has a cue."""

    async def create(self, *, messages, **options):
        prompt = messages[-1]["content"]
        if "EVIDENCE:\n" in prompt:
            cue = {"cue_type": "detail", "cue_text": "amber lantern", "quote": QUOTE}
            data = {"cues": [cue] if QUOTE in prompt else []}
        else:
            data = {"keep": [0] if QUOTE in prompt else []}
        message = SimpleNamespace(content=json.dumps(data))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def fixture_model():
    chat = SimpleNamespace(completions=FixtureCompletions())
    return SimpleNamespace(model="offline-fixture", client=SimpleNamespace(chat=chat),
                           api_available=False)


def validation_request(candidates):
    return {"schema_version": "assoc.v1", "scope": {
        "session_id": "sample-session", "branch_id": "main", "user_message_id": "sample-turn",
        "message_version": "1", "request_id": "sample-request", "last_assistant_ids": [],
    }, "candidates": candidates}


async def main():
    async with local_recall(EXAMPLE, dehydrator=fixture_model()) as (index, recall):
        await index.warm()
        report = await index.rebuild(limit=10)
        await index.warm()
        hits = await index.search(["amber lantern"])
        delivered = await recall.validate(validation_request(hits))
        rows = delivered["candidates"]
        if report["failed"] or len(rows) != 1 or rows[0]["excerpt"] != QUOTE:
            raise RuntimeError("fixture build or validated recall failed")
        print(json.dumps({key: rows[0][key] for key in
                          ("event_id", "excerpt", "source_window", "evidence_version")}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
