"""Gateway-side helpers for associative recall.

The module deliberately treats the MCP gateway and the SLM as untrusted
boundaries. Malformed replies, timeouts, and transport errors all fail open by
returning no recall item. The only recall data exposed to callers is a gateway
candidate that passed assoc_validate.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "assoc.v1"
MAX_CACHE_KEYS = 64
MAX_CACHE_CANDIDATES = 6
CACHE_TTL_S = 15 * 60
CACHE_MAX_ROUNDS = 8
MAX_TEXT_CHARS = 12_000
MAX_CANDIDATE_ID_CHARS = 512
_ALLOWED_USE = frozenset({"mention", "understand"})


@dataclass
class _CacheEntry:
    candidates: list[dict[str, Any]]
    ancestry: tuple[str, ...]
    created_at: float
    basis_id: str


# Bounded process-local shortlist cache. The cache is an optimization only;
# all returned candidates must still be gateway validated.
_CACHE: OrderedDict[tuple[str, str], _CacheEntry] = OrderedDict()


def _scope(scope: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(scope, dict):
        raise ValueError("scope must be a dict")
    keys = ("session_id", "branch_id", "user_message_id", "message_version", "request_id")
    result: dict[str, Any] = {}
    for key in keys:
        value = scope.get(key)
        if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value).strip():
            raise ValueError(f"invalid scope field: {key}")
        result[key] = str(value)
    if "last_assistant_ids" in scope:
        values = scope["last_assistant_ids"]
        if not isinstance(values, (list, tuple)):
            raise ValueError("invalid scope field: last_assistant_ids")
        result["last_assistant_ids"] = list(dict.fromkeys(
            str(value).strip() for value in values
            if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip()
        ))[-6:]
    return result


def _ancestry(scope: dict[str, Any]) -> tuple[str, ...]:
    """Read optional active-path ancestry while keeping the wire scope stable."""
    values = scope.get("active_path_ancestry", scope.get("active_path_ids", ()))
    if not isinstance(values, (list, tuple)):
        return ()
    return tuple(str(value)[:256] for value in values if isinstance(value, (str, int)))[:64]


def _wire_scope(scope: dict[str, Any]) -> dict[str, Any]:
    # Keep the optional path field intact; an empty list is distinct from a
    # missing field because it disables legacy TTL fallback for this request.
    return _scope(scope)


def _body(scope: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "scope": _wire_scope(scope), **fields}


def _text(value: Any) -> str:
    """Normalize the registry's dict/string/MCP content-list response forms."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        if isinstance(value.get("structuredContent"), dict):
            return _text(value["structuredContent"])
        if isinstance(value.get("content"), list):
            return _text(value["content"])
        return ""
    if isinstance(value, list):
        return "".join(
            str(block.get("text") or "")
            for block in value
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
    return ""


def _payload(value: Any) -> Optional[dict[str, Any]]:
    if isinstance(value, dict):
        if isinstance(value.get("structuredContent"), dict):
            value = value["structuredContent"]
        elif "result" in value:
            value = value["result"]
        elif isinstance(value.get("content"), list):
            value = _text(value["content"])
        if isinstance(value, dict):
            return value
    raw = _text(value)
    if not raw:
        return None
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(decoded, dict):
        return decoded
    return None


async def _registry_call(registry: Any, server: str, tool: str, body: dict[str, Any]) -> Any:
    """Call an available registry transport, passing one body argument."""
    if registry is None or not isinstance(server, str) or not server.strip():
        raise RuntimeError("registry/server unavailable")
    return await registry.call_tool(server, tool, {"body": body})


def _valid_candidates(response: Any, *, expected_status: str = "ok") -> list[dict[str, Any]]:
    payload = _payload(response)
    if not payload or payload.get("schema_version") != SCHEMA_VERSION or payload.get("status") != expected_status:
        return []
    values = payload.get("candidates")
    if not isinstance(values, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values[:MAX_CACHE_CANDIDATES]:
        if not isinstance(item, dict):
            continue
        candidate_id = item.get("candidate_id")
        evidence_version = item.get("evidence_version")
        if not isinstance(candidate_id, str) or not candidate_id.strip() or len(candidate_id) > MAX_CANDIDATE_ID_CHARS:
            continue
        if candidate_id in seen or not isinstance(evidence_version, (str, int)) or isinstance(evidence_version, bool):
            continue
        if not isinstance(item.get("event_id"), str) or not isinstance(item.get("excerpt"), str):
            continue
        seen.add(candidate_id)
        result.append(dict(item))
    return result


def _cache_put(scope: dict[str, Any], candidates: list[dict[str, Any]]) -> None:
    try:
        normalized = _scope(scope)
    except ValueError:
        return
    key = (normalized["session_id"], normalized["branch_id"])
    basis = str(scope.get("basis_assistant_message_id") or "")
    if not basis: return
    _CACHE[key] = _CacheEntry(
        candidates=[dict(candidate) for candidate in candidates[:MAX_CACHE_CANDIDATES]],
        ancestry=_ancestry(scope), created_at=time.monotonic(), basis_id=basis,
    )
    _CACHE.move_to_end(key)
    while len(_CACHE) > MAX_CACHE_KEYS:
        _CACHE.popitem(last=False)


def _cache_get(scope: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        normalized = _scope(scope)
    except ValueError:
        return []
    key = (normalized["session_id"], normalized["branch_id"])
    entry = _CACHE.get(key)
    if entry is None:
        return []
    expired = time.monotonic() - entry.created_at > CACHE_TTL_S
    if expired or entry.basis_id not in _ancestry(scope)[-4:]:
        _CACHE.pop(key, None)
        return []
    _CACHE.move_to_end(key)
    return [dict(candidate) for candidate in entry.candidates]


async def prepare_after(
    scope: dict[str, Any], ctx: str, registry: Any, server: str,
) -> None:
    """Fetch a background shortlist and cache it for the current branch."""
    if not isinstance(ctx, str) or not ctx.strip():
        return
    try:
        response = await _registry_call(
            registry, server, "assoc_prepare",
            _body(scope, context_text=ctx[:MAX_TEXT_CHARS]),
        )
        candidates = _valid_candidates(response)
        _cache_put(scope, candidates)
    except Exception:
        logger.debug("associative prepare unavailable", exc_info=True)


def _recent_text(recent: Any, latest: str) -> str:
    chunks: list[str] = []
    if isinstance(recent, list):
        for message in recent[-12:]:
            if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
                continue
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                chunks.append(f"{message['role']}: {content.strip()[:1800]}")
    if isinstance(latest, str) and latest.strip():
        chunks.append("latest: " + latest.strip()[:3000])
    return "\n".join(chunks)[-MAX_TEXT_CHARS:]


def _strict_judgment(raw: Any, allowed_ids: set[str]) -> Optional[tuple[str, str]]:
    """Accept exactly one JSON object with the fixed mode/pick/use contract."""
    value: Any = raw
    if isinstance(raw, dict):
        # SLM adapters may return a chat completion wrapper or JSON directly.
        if "choices" in raw:
            try:
                value = raw["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                return None
        elif "content" in raw and not {"mode", "pick", "use"}.issubset(raw):
            value = raw.get("content")
    if isinstance(value, str):
        value = value.strip()
        if value.startswith("```") and value.endswith("```"):
            value = value.strip("`").strip()
            if value.lower().startswith("json"):
                value = value[4:].strip()
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(value, dict) or set(value) != {"mode", "pick", "use"}:
        return None
    if value.get("mode") == "none" and value.get("pick") is None and value.get("use") is None:
        return ("", "")
    if value.get("mode") != "pick":
        return None
    pick, use = value.get("pick"), value.get("use")
    if type(pick) is int: pick=str(pick)
    if not isinstance(pick, str) or pick not in allowed_ids or use not in _ALLOWED_USE:
        return None
    return pick, use


async def _judge(slm: Any, messages: str, candidates: list[dict[str, Any]], latest: str = "") -> Optional[tuple[str, str]]:
    if slm is None or not candidates:
        return None
    rows = [{"id":str(i),"why":str(c.get("reason",""))[:100],"excerpt":str(c.get("evidence_context") or c["excerpt"])[:240],
             "archived":bool(c.get("archived"))} for i,c in enumerate(candidates)]
    for item, row in zip(candidates,rows):
        if item.get("plan_state")=="planned": row["计划结果"]=item.get("outcome_state") or "unknown"
    system = ("你是共享记忆的联想筛选员。current 是此刻用户说的话。若对方想安静、暂停或先讲完，一律选空。"
              "其余时候，逐项比较旧事原文和 current：提到的对象可以完全不同。若共有同一种做法、决定、关系或意义，"
              "指出那件往事能自然回应这句话，就允许选为 mention，最终说不说由对话模型决定。"
              "请区分仅同主题和真正的结构对应。只共同出现一个词或笼统情绪时选空；"
              "归档条目需要当前线索和过去的具体原文同时支持这种联系，不能拿旧状态推断现在。"
              'JSON 只含 mode、pick、use。空为 {"mode":"none","pick":null,"use":null}。'
              '若选中，mode=pick，pick=候选的短编号，use=mention 或 understand。')
    user_content=json.dumps({"current":latest,"recent":messages,"ranked_sources":rows},ensure_ascii=False)
    try:
        from pipeline.assoc_budget import reserve
        import os
        reserve(getattr(slm, "budget_db", None) or os.environ["ASSOC_GATEWAY_BUDGET_DB"],ceiling_usd=1.5,
                text=system+user_content,output=100)
        payload={"model":slm._model,"messages":[{"role":"system","content":system},{"role":"user","content":user_content}],
                 "max_tokens":100,"temperature":0,"reasoning":{"enabled":False}}
        if getattr(getattr(slm,"_client",None),"base_url",None) and slm._client.base_url.host=="openrouter.ai":
            payload["provider"]={"sort":"latency"}
        response=await slm._post("/chat/completions",json=payload,_retries=0)
        response.raise_for_status()
        answer=_strict_judgment(response.json(),{row["id"] for row in rows})
        if not answer or not answer[0]:return None
        index,use=answer
        return candidates[int(index)]["candidate_id"],use
    except Exception:
        logger.debug("associative SLM judgment unavailable", exc_info=True)
        return None


async def _validate_batch(scope: dict[str, Any], candidates: list[dict[str, Any]], registry: Any, server: str) -> list[dict[str, Any]]:
    """Validate all shortlisted IDs in one MCP round trip."""
    try:
        body = _body(
            scope, candidates=[{"candidate_id": c["candidate_id"], "evidence_version": c["evidence_version"], "excerpt": c["excerpt"]} for c in candidates],
        )
        response = await _registry_call(registry, server, "assoc_validate", body)
        values = _valid_candidates(response)
        expected = {(c.get("candidate_id"), str(c.get("evidence_version"))) for c in candidates}
        return [{**v, "_assoc_scope": body["scope"]} for v in values
                if (v.get("candidate_id"), str(v.get("evidence_version"))) in expected
                and isinstance(v.get("source_window"), str)
                and isinstance(v.get("selection_token"), str) and v["selection_token"].strip()]
    except Exception:
        logger.debug("associative validation unavailable", exc_info=True)
        return []


async def _shortlist(
    scope: dict[str, Any], recent: Any, latest: str, slm: Any, registry: Any, server: str,
) -> Optional[dict[str, Any]]:
    try:
        candidates = _cache_get(scope)
        if not candidates:
            return None
        messages = _recent_text(recent, latest)
        if getattr(slm, "trusted_cache_judge", False) is True:
            validated, judgment = await asyncio.gather(
                _validate_batch(scope, candidates, registry, server),
                _judge(slm, messages, candidates, latest))
        else:
            validated = await _validate_batch(scope, candidates, registry, server)
            reasons = {item["candidate_id"]: item.get("reason", "") for item in candidates}
            current = [{**item, "evidence_context": item["source_window"],
                        "reason": reasons.get(item["candidate_id"], "")} for item in validated]
            judgment = await _judge(slm, messages, current, latest)
        if not judgment:
            return None
        picked_id, use = judgment
        for item in validated:
            if item and item.get("candidate_id") == picked_id:
                result = dict(item)
                result["use"] = use
                return result
    except Exception:
        logger.debug("associative shortlist failed open", exc_info=True)
    return None


def start_shortlist(
    scope: dict[str, Any], recent: list[dict[str, Any]], latest: str,
    slm: Any, registry: Any, server: str,
) -> Optional[asyncio.Task[Optional[dict[str, Any]]]]:
    """Synchronously schedule asynchronous validation/judgment; never blocks."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return loop.create_task(_shortlist(scope, recent, latest, slm, registry, server))


async def query_current(
    scope: dict[str, Any], query_text: str, latest: str, registry: Any,
    server: str, remaining_ms: int,
) -> Optional[dict[str, Any]]:
    """Ask the gateway for a query-specific selection using its embeddings."""
    if not isinstance(query_text, str) or not query_text or not isinstance(latest, str):
        return None
    try:
        body = _body(
            scope,
            query_text=query_text,
            context_text=latest[:MAX_TEXT_CHARS],
            remaining_ms=max(0, int(remaining_ms)),
        )
        response = await _registry_call(registry, server, "assoc_query", body)
        payload = _payload(response)
        if not payload or payload.get("schema_version") != SCHEMA_VERSION or payload.get("status") != "ok":
            return None
        selected = payload.get("selected")
        if selected is None:
            return None
        item = _valid_candidates({"schema_version": SCHEMA_VERSION, "status": "ok", "candidates": [selected]})
        if not item:
            return None
        candidate = item[0]
        if (not isinstance(candidate.get("selection_token"), str)
                or not candidate["selection_token"].strip()
                or not isinstance(candidate.get("source_window"), str)
                or candidate.get("use") not in _ALLOWED_USE):
            return None
        return {**candidate, "_assoc_scope": body["scope"]}
    except Exception:
        logger.debug("associative query unavailable", exc_info=True)
        return None


async def final_admission(
    scope: dict[str, Any], item: dict[str, Any], registry: Any, server: str, deadline: float,
) -> Optional[dict[str, Any]]:
    """Recheck one reserved selection at handoff within the caller's monotonic deadline."""
    if time.monotonic() >= deadline:
        return None
    try:
        use = item["use"]
        if use not in _ALLOWED_USE:
            return None
        identity = {key: item[key] for key in (
            "selection_token", "candidate_id", "event_id", "evidence_version", "excerpt")}
        if type(identity["evidence_version"]) is int:
            identity["evidence_version"] = str(identity["evidence_version"])
        if not all(isinstance(value, str) and value.strip() for value in identity.values()):
            return None
        body = _body(scope, recheck_only=True, candidates=[identity])
        if item.get("_assoc_scope", body["scope"]) != body["scope"]:
            return None
        call = asyncio.create_task(_registry_call(registry, server, "assoc_validate", body))
        try:
            ready, _ = await asyncio.wait({call}, timeout=max(0, deadline - time.monotonic()))
            if not ready or call.cancelled():
                return None
            response = call.result()
            if isinstance(response, dict) and response.get("isError"):
                return None
            payload = _payload(response)
        finally:
            if not call.done():
                # Transport cancellation/cleanup may outlast the foreground budget.
                call.cancel()
            call.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        if (not payload or payload.get("schema_version") != SCHEMA_VERSION
                or payload.get("status") != "ok" or payload.get("recheck_only") is not True
                or payload.get("scope", body["scope"]) != body["scope"]):
            return None
        rows = payload.get("candidates")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            return None
        fresh = rows[0]
        if (any(fresh.get(key) != value for key, value in identity.items())
                or fresh.get("use", use) != use
                or not isinstance(fresh.get("source_window"), str)
                or identity["excerpt"] not in fresh["source_window"]
                or time.monotonic() >= deadline):
            return None
        return {**fresh, "use": use, "_assoc_origin": item.get("_assoc_origin")}
    except Exception:
        logger.debug("associative final admission unavailable", exc_info=True)
        return None


async def finish_selection(
    scope: dict[str, Any], item: dict[str, Any], assistant_message_id: str,
    outcome: str, registry: Any, server: str,
) -> bool:
    """Settle a validated selection after the assistant response is known."""
    if outcome not in {"mentioned", "unused", "cancelled"}:
        return False
    token = item.get("selection_token") if isinstance(item, dict) else None
    if not isinstance(token, str) or not token.strip() or not isinstance(assistant_message_id, str):
        return False
    try:
        response = await _registry_call(registry, server, "assoc_finish", _body(
            scope, selection_token=token, assistant_message_id=assistant_message_id, outcome=outcome,
        ))
        payload = _payload(response)
        # Do not invent success: require the explicit gateway contract status.
        return bool(payload and payload.get("schema_version") == SCHEMA_VERSION and payload.get("status") == "ok")
    except Exception:
        logger.debug("associative finish unavailable", exc_info=True)
        return False


# Names kept short and explicit for parent integration and typing.
