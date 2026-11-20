"""Application hooks around the released gateway. All storage rows are host-owned."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pipeline import associative_recall as gateway
from pipeline import association_scheduler as association
from pipeline.memory_gateway_planner import select_recent_planner_messages

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DirectResult:
    text: str = ""
    event_ids: tuple[str, ...] = ()
    receipt: object = None
    lookup_attempted: bool = False
    abort_if_prepared: Callable[[], Awaitable[None]] | None = None


class LocalRegistry:
    """In-process transport for one trusted application's memory service."""

    def __init__(self, service, server="memory"):
        self.server = server
        self.service = service

    async def call_tool(self, server, tool, arguments):
        if server != self.server:
            raise ValueError("unknown memory service")
        body = arguments["body"]
        if tool == "assoc_finish":
            return self.service.finish(body)
        handler = {"assoc_prepare": self.service.prepare,
                   "assoc_validate": self.service.validate,
                   "assoc_query": self.service.query}[tool]
        return await handler(body)


class CommittedTurnPath:
    """Give the release's prepare hook a single checked, post-commit read."""

    def __init__(self, storage, user_id, latest, assistant_id, response):
        self.storage = storage
        self.user_id = user_id
        self.assistant_id = assistant_id
        self.visible_pair = [{"role": "user", "content": latest.strip()},
                             {"role": "assistant", "content": response.strip()}]

    async def list_active_path(self, session, *, channel_id, limit, latest):
        rows = await self.storage.list_active_path(
            session, channel_id=channel_id, limit=limit, latest=latest)
        if not rows or rows[-1].get("id") != self.assistant_id:
            return []
        for row in rows:
            if any(row.get(key) not in (None, expected) for key, expected in (
                    ("session_id", session), ("conversation_id", session),
                    ("channel_id", channel_id), ("branch_id", channel_id))):
                return []
        # Tool activity may occur between the user and the committed answer.
        # Find the owning user before filtering, so a hidden user cannot drop
        # out and leave their assistant's response paired with an earlier user.
        for row in reversed(rows[:-1]):
            if row.get("role") == "user":
                if (row.get("id") == self.user_id and
                        select_recent_planner_messages([row, rows[-1]]) == self.visible_pair):
                    return rows
                break
            if select_recent_planner_messages([row]):
                break
        return []


class ChatHost:
    """Coordinate optional recall alongside the application's ordinary lookup."""

    def __init__(self, storage, registry, slm, server="memory"):
        self.storage = storage
        self.registry = registry
        self.slm = slm
        self.server = server
        self.background = set()
        self.settling = set()

    async def drain(self):
        """Join deferred receipts and preparation during tests or shutdown."""
        if self.background:
            await asyncio.gather(*tuple(self.background), return_exceptions=True)

    async def _snapshot(self, session, branch, user_id, latest):
        try:
            rows = await self.storage.list_active_path(session, channel_id=branch, limit=16, latest=True)
            if not rows or rows[-1]["id"] != user_id:
                return None, []
            if select_recent_planner_messages(rows[-1:]) != [{"role": "user", "content": latest.strip()}]:
                return None, []
            scope = association.scope_for(session, branch, user_id, rows)
            try:
                scope["last_assistant_ids"] = await self.storage.completed_assistant_path(
                    session, branch, user_id)
            except Exception:
                # Missing selects legacy expiry fallback. [] means a known empty path.
                scope.pop("last_assistant_ids", None)
            return scope, select_recent_planner_messages(rows, current_message_id=user_id)
        except Exception:
            log.info("Active-path recall unavailable", exc_info=True)
            return None, []

    async def turn(self, session, branch, user_id, latest, *, plan, lookup,
                   reply_and_commit, allow_assoc=True):
        """plan() -> (route, query), lookup(route, query) -> DirectResult.

        reply_and_commit(direct, optional_context) returns (stored assistant ID,
        final visible text). The application retains its direct delivery receipt.
        """
        short = live = snapshot = timer = bundle = direct = None
        branch_key = (session, branch)
        allowed = bool(allow_assoc and latest.strip() and not association.quiet(latest))

        async def shortlist():
            scope, recent = await asyncio.shield(snapshot)
            if scope:
                return await gateway.start_shortlist(scope, recent, latest, self.slm, self.registry, self.server)

        async def current(query):
            scope, _ = await asyncio.shield(snapshot)
            if scope:
                return await gateway.query_current(scope, query, latest, self.registry, self.server, 15000)

        if allowed and branch_key not in self.settling:
            snapshot = asyncio.create_task(self._snapshot(session, branch, user_id, latest))
            short = asyncio.create_task(shortlist())

        try:
            route, query = await plan()
            planner_done = time.monotonic()
            eligible = snapshot is not None and route in {"none", "direct"}
            if short:
                if eligible:
                    timer = asyncio.get_running_loop().call_later(association.GRACE_S, short.cancel)
                else:
                    short.cancel()
            if eligible and route == "direct" and query:
                live = asyncio.create_task(current(query))
            direct = await lookup(route, query)
            literal_done = time.monotonic()
            context = ""
            if eligible:
                deadline = (literal_done if route == "direct" else planner_done) + association.GRACE_S
                try:
                    item = await association.choose((short, live), direct.event_ids, deadline)
                    if item:
                        scope = snapshot.result()[0]
                        item = await gateway.final_admission(scope, item, self.registry, self.server, deadline)
                    if item:
                        context = association.render(item)
                        if context:
                            bundle = scope, item
                except Exception:
                    log.info("Optional evidence unavailable", exc_info=True)
                if route == "direct" and direct.lookup_attempted and not (direct.text or context):
                    context = association.lookup_status()
        except BaseException:
            self._abort_direct(direct)
            raise
        finally:
            if timer:
                timer.cancel()
            for task in (short, live, snapshot):
                if task and not task.done():
                    task.cancel()

        try:
            assistant_id, response = await reply_and_commit(direct, context)
        except BaseException:
            self._abort_direct(direct)
            if bundle:
                self._cancel(bundle)
            raise
        if not assistant_id or not response or not response.strip():
            self._abort_direct(direct)
            if bundle:
                self._cancel(bundle)
        elif allowed:
            if bundle:
                self.settling.add(branch_key)
                async def settle():
                    try:
                        await association.settled(bundle, assistant_id, latest, response,
                                                  self.registry, self.server, self.slm)
                    finally:
                        self.settling.discard(branch_key)
                association.submit_background(settle(), self.background)
            path = CommittedTurnPath(self.storage, user_id, latest, assistant_id, response)
            association.submit_background(asyncio.wait_for(association.prepare_completed(
                path, self.registry, self.server, session, branch, user_id, assistant_id),
                timeout=35), self.background)
        return assistant_id, response

    def _abort_direct(self, direct):
        if direct is not None and direct.abort_if_prepared:
            association.submit_background(direct.abort_if_prepared(), self.background)

    def _cancel(self, bundle):
        scope, item = bundle
        association.submit_background(asyncio.wait_for(gateway.finish_selection(
            scope, item, "", "cancelled", self.registry, self.server), 5), self.background)
