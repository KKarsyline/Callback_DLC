"""Regression coverage for quote windows and the persisted review decision."""
import asyncio
import hashlib
import json
from threading import get_ident
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from assoc_index import AssocIndex
from assoc_plans import refresh_plan_states


CUE = "The tiles are on the workbench."
PLAN = "I will catalogue the tiles on Friday."
OUTCOME = "I finished the tile catalogue."
CORRECTION = "Correction: I postponed the tile catalogue until Monday."
PADDING = " Other workshop notes." * 80


def decision(plan=PLAN, outcome=OUTCOME, mid="own", state="completed"):
    return {"id": "0", "plan_state": "planned" if plan else "unknown", "plan_quote": plan,
            "outcome_state": state if outcome else "unknown", "outcome_quote": outcome,
            "outcome_moment_id": mid if outcome else ""}


class Review:
    def __init__(self, root):
        self.moments, self.buckets, self.edges, self.retired = {}, {}, [], set()
        self.source("own", CUE + PADDING + PLAN + PADDING + OUTCOME)
        self.index = AssocIndex(
            {"state_dir": str(root), "list_edges": lambda: self.edges},
            SimpleNamespace(get=self.moments.get, list_all=lambda: list(self.moments.values())),
            SimpleNamespace(get=AsyncMock(side_effect=self.buckets.get),
                            list_all=AsyncMock(side_effect=lambda **kw: list(self.buckets.values()))),
            SimpleNamespace(superseded_ids=self.retired.copy), None, None)
        draft = dict(decision(), cue_text="catalogue", quote=CUE, cue_type="unfinished")
        checked = {"keep": [0], "plans": [{"id": 0, "state": "planned", "quote": PLAN}],
                   "outcomes": [{"id": 0, "state": "completed", "quote": OUTCOME}]}
        self.index._complete = AsyncMock(side_effect=map(json.dumps, [{"cues": [draft]}, checked]))
        assert asyncio.run(self.index.rebuild(1))["indexed"] == 1
        self.cid = self.row()["cue_id"]

    def source(self, mid, text):
        self.moments[mid] = {"moment_id": mid, "bucket_id": mid, "text": text, "section": "body",
                             "created_at": "2026-01-01" if mid == "own" else "2026-01-02"}
        self.buckets[mid] = {"id": mid, "content": text, "metadata": {}}
        if mid != "own":
            self.edges.append({"source": "own", "target": mid, "relation_type": "updates"})

    def row(self):
        with self.index._connect() as db:
            return dict(db.execute("SELECT * FROM assoc_plans ORDER BY rowid LIMIT 1").fetchone())

    def refresh(self, **kw):
        return asyncio.run(refresh_plan_states(self.index, **kw))

    def respond(self, *answers):
        self.index._complete = AsyncMock(side_effect=[json.dumps({"items": x}) for x in answers])

    def evidence(self):
        calls = self.index._complete.call_args_list
        first = json.loads(calls[0].args[0].split("Evidence:\n")[1])
        second = json.loads(calls[1].args[0].split("EVIDENCE:\n")[1].split("\nPROPOSALS:")[0])
        assert first == second
        return first[0]

    def resolve(self):
        return asyncio.run(self.index.resolve("own"))


@pytest.fixture
def review(tmp_path):
    return Review(tmp_path)


def test_separate_windows_survive_index_and_repeated_review(review):
    assert review.resolve()["outcome_state"] == "completed"
    review.respond([decision()], [decision()])
    assert review.refresh()["reviewed"] == 1
    evidence = review.evidence()
    assert any(CUE in w for w in evidence["plan_excerpts"])
    assert any(PLAN in w for w in evidence["plan_excerpts"])
    windows = evidence["later_sources"][0]["excerpts"]
    assert any(OUTCOME in w for w in windows)
    assert all(len(w) <= 1200 for w in windows)
    assert review.resolve()["outcome_quote"] == OUTCOME
    assert review.refresh()["reviewed"] == 0
    assert review.index._complete.await_count == 2


def test_linked_prior_uses_current_source_and_allows_author_correction(review):
    review.source("follow", PADDING + OUTCOME)
    version = review.index._snapshot(review.moments["follow"], review.buckets["follow"])
    with review.index._connect() as db:
        db.execute("UPDATE assoc_plans SET outcome_moment_id='follow', outcome_revision=?", (version,))
    review.respond([decision(mid="follow")], [decision(mid="follow")])
    assert review.refresh()["reviewed"] == 1
    assert any(OUTCOME in w for w in review.evidence()["later_sources"][1]["excerpts"])
    assert review.resolve()["outcome_source"] == "follow"
    review.source("correction", CORRECTION)
    corrected = decision(outcome=CORRECTION, mid="correction", state="rescheduled")
    review.respond([corrected], [corrected])
    assert review.refresh()["reviewed"] == 1
    assert review.resolve()["outcome_quote"] == CORRECTION


@pytest.mark.parametrize("stage", ["extract", "verify"])
@pytest.mark.parametrize("bad", [
    [], [{}], [dict(decision(), id="1")], [decision(), decision()],
    [dict(decision(), plan_quote="")], [dict(decision(), outcome_state=None)],
    [dict(decision(), outcome_state="done")], [dict(decision(), outcome_moment_id=[])],
    [{k: v for k, v in decision().items() if k != "outcome_quote"}],
])
def test_malformed_item_keeps_verified_row_retryable(review, stage, bad):
    before = review.row()
    good = [decision()]
    review.respond(bad if stage == "extract" else good, bad if stage == "verify" else good)
    result = review.refresh()
    assert (result["failed"], result["remaining"], result["reviewed"]) == (1, 1, 0)
    assert review.row() == before
    unknown = [decision(plan="", outcome="")]
    review.respond(unknown, unknown)
    assert review.refresh()["reviewed"] == 1
    assert review.row()["plan_state"] == "unknown"
    assert review.refresh()["reviewed"] == 0
    assert review.index._complete.await_count == 2


def test_partial_batch_only_signs_the_complete_item(review):
    with review.index._connect() as db:
        db.execute("""INSERT INTO assoc_cues SELECT 'other',moment_id,bucket_id,evidence_version,
            cue_text,quote,cue_type,vector_json,model,created_at FROM assoc_cues""")
    other = dict(decision(plan="", outcome=""), id="1")
    review.respond([decision(), other], [other])
    result = review.refresh()
    assert (result["failed"], result["reviewed"]) == (1, 1)
    assert review.row()["review_sig"] == ""
    review.respond([decision()], [decision()])
    assert review.refresh()["reviewed"] == 1
    assert review.refresh()["remaining"] == 0


@pytest.mark.parametrize("recover", [True, False])
def test_repair_legacy_unknown_only_once_and_by_id(review, recover):
    # Recreate an old signed decision after both quote offsets were erased.
    old_sig = hashlib.sha256(json.dumps([review.cid, review.row()["evidence_version"], [], []],
                                        separators=(",", ":")).encode()).hexdigest()
    with review.index._connect() as db:
        db.execute("""UPDATE assoc_plans SET plan_state='unknown',plan_quote='',outcome_state='unknown',
            outcome_quote='',outcome_moment_id='',outcome_revision='',review_sig=?""", (old_sig,))
    review.respond()
    assert review.refresh()["reviewed"] == 0
    assert review.refresh(repair_unknown_ids={"unrelated"})["reviewed"] == 0
    assert review.index._complete.await_count == 0
    review.respond([])
    assert review.refresh(repair_unknown_ids={review.cid})["failed"] == 1
    assert review.row()["review_sig"] == old_sig
    result = decision() if recover else decision(plan="", outcome="")
    review.respond([result], [result])
    assert review.refresh(repair_unknown_ids={review.cid})["reviewed"] == 1
    evidence = review.evidence()
    assert any(PLAN in w for w in evidence["plan_excerpts"])
    assert any(OUTCOME in w for w in evidence["later_sources"][0]["excerpts"])
    assert review.resolve()["outcome_state"] == result["outcome_state"]
    assert review.refresh(repair_unknown_ids={review.cid})["reviewed"] == 0
    assert review.refresh()["reviewed"] == 0
    assert review.index._complete.await_count == 2


@pytest.mark.parametrize("change", ["edit", "revoke", "unlink", "ambiguous", "own_edit"])
def test_stale_review_cannot_publish_or_sign(review, change):
    review.source("follow", CORRECTION)
    review.buckets["follow"]["metadata"]["source_raw_event_ids"] = [42]
    before = review.row()
    answer = decision(outcome=CORRECTION, mid="follow", state="rescheduled")

    def respond(prompt, **kw):
        if "PROPOSALS:" in prompt:
            if change == "edit":
                review.buckets["follow"]["content"] += " Updated."
            elif change == "revoke":
                review.retired.add(42)
            elif change == "unlink":
                review.edges.clear()
            elif change == "ambiguous":
                review.source("second", "Another later account.")
                review.source("third", "Yet another later account.")
            else:
                review.buckets["own"]["content"] += " Updated."
        return json.dumps({"items": [answer]})

    review.index._complete = AsyncMock(side_effect=respond)
    assert review.refresh()["failed"] == 1
    assert review.row() == before
    if change == "own_edit":
        assert review.resolve() is None
    else:
        review.respond([answer], [answer])
        assert review.refresh()["reviewed"] == 1
        if change in {"revoke", "unlink", "ambiguous"}:
            assert len(review.evidence()["later_sources"]) == 1
            assert review.resolve()["outcome_state"] == "unknown"


def test_ambiguous_prior_link_loses_only_outcome_even_on_provider_failure(review):
    review.source("follow", CORRECTION)
    corrected = [decision(mid="follow", state="rescheduled", outcome=CORRECTION)]
    review.respond(corrected, corrected)
    assert review.refresh()["reviewed"] == 1
    assert review.resolve()["outcome_source"] == "follow"
    review.source("second", "Another related record.")
    review.source("third", "Another related record.")
    # The adapter may share an event loop with foreground chat.
    connect = review.index._connect
    owner = get_ident()

    def worker_connection():
        assert get_ident() != owner
        return connect()

    review.respond([])
    with patch.object(review.index, "_connect", worker_connection):
        assert review.refresh()["failed"] == 1
    assert review.resolve()["plan_quote"] == PLAN
    assert review.resolve()["outcome_state"] == "unknown"
    assert review.row()["review_sig"] == ""
    del review.edges[1:]
    review.respond(corrected, corrected)
    with patch.object(review.index, "_connect", worker_connection):
        assert review.refresh()["reviewed"] == 1
    assert review.resolve()["outcome_source"] == "follow"


def test_competing_writer_after_verification_leaves_row_for_retry(review):
    before = review.row()
    with review.index._connect() as writer:
        def respond(prompt, **kw):
            if "PROPOSALS:" in prompt:
                writer.execute("BEGIN IMMEDIATE")
            return json.dumps({"items": [decision()]})

        review.index._complete = AsyncMock(side_effect=respond)
        result = review.refresh()
        assert (result["reviewed"], result["failed"], result["remaining"]) == (0, 1, 1)
        assert review.row() == before
    review.respond([decision()], [decision()])
    assert review.refresh()["reviewed"] == 1
    assert review.resolve()["outcome_quote"] == OUTCOME


def test_stale_link_revision_does_not_supply_an_old_quote_window(review):
    review.source("follow", OUTCOME)
    answer = [decision(mid="follow")]
    review.respond(answer, answer)
    assert review.refresh()["reviewed"] == 1
    review.moments["follow"]["text"] = review.buckets["follow"]["content"] = PADDING + OUTCOME
    assert review.resolve()["outcome_state"] == "unknown"
    review.respond(answer, answer)
    assert review.refresh()["reviewed"] == 1
    linked = review.evidence()["later_sources"][1]
    assert all(OUTCOME not in w for w in linked["excerpts"])
    assert review.resolve()["outcome_state"] == "unknown"


def test_ombre_vault_scans_once_per_port_per_snapshot(tmp_path):
    from adapters.ombre_brain.demo import EXAMPLE, QUOTE
    from adapters.ombre_brain.vault import OmbreVault

    bid = "a0b1c2d3e4f5"
    threads = []

    def policy():
        threads.append(get_ident())
        return {"dynamic_ids": [bid]}

    vault = OmbreVault(EXAMPLE, read_policy=policy)
    index = AssocIndex({"state_dir": str(tmp_path), "list_edges_async": vault.list_edges_async},
                       vault.moments, vault, vault, None, None)
    assert asyncio.run(refresh_plan_states(index))["scanned"] == 0
    assert threads == []
    moment = vault.moments.get(bid + ":body")
    revision = index._snapshot(moment, asyncio.run(vault.get(bid)))
    with index._connect() as db:
        db.executemany("""INSERT INTO assoc_cues (cue_id,moment_id,bucket_id,evidence_version,
            cue_text,quote,cue_type,created_at) VALUES (?,?,?,?,?,?,'unfinished',0)""",
            [(str(i), moment["moment_id"], bid, revision, QUOTE, QUOTE) for i in range(6)])
    answer = [dict(decision(plan="", outcome=""), id=str(i)) for i in range(6)]
    index._complete = AsyncMock(return_value=json.dumps({"items": answer}))
    threads.clear()
    assert asyncio.run(refresh_plan_states(index))["reviewed"] == 6
    assert len(threads) == 8
    assert get_ident() not in threads
    threads.clear()
    assert asyncio.run(refresh_plan_states(index))["remaining"] == 0
    assert len(threads) == 4
    assert get_ident() not in threads
    assert index._complete.await_count == 2
