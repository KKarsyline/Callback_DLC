"""Per-turn scheduling for optional chat associative recall."""
import asyncio
import json
import logging
import os
import re
import time
from difflib import SequenceMatcher

from pipeline import associative_recall as assoc

log = logging.getLogger(__name__)
GRACE_S = .19  # 10 ms headroom for scheduling inside the 200 ms allowance
_PREPARING = {}


def enabled():
    return os.getenv("OMBRE_ASSOC_RECALL_ENABLED", "").lower() in {"1", "true"}


def scope_for(conv, channel, user, rows, basis=None):
    # list_active_path is chronological. Cooldown uses only completed assistant
    # turns on this exact path; ordinary assistant replies count even when they
    # did not trigger retrieval. Empty and tool-only assistant rows do not.
    ancestors = [str(row.get("id")) for row in rows if row.get("id") and row.get("id") != user]
    assistant_ids = []
    for row in rows:
        if not isinstance(row, dict) or row.get("role") != "assistant":
            continue
        row_id = row.get("id")
        content = row.get("content")
        if isinstance(content,list):
            content="".join(str(v.get("text") or "") for v in content
                            if isinstance(v,dict) and v.get("type")=="text")
        if not row_id or not isinstance(content,str) or not content.strip():
            continue
        row_channel = row.get("channel_id", row.get("branch_id"))
        if channel and row_channel is not None and str(row_channel) != str(channel):
            continue
        status = str(row.get("status") or row.get("state") or "").lower()
        if status in {"in_progress", "in-progress", "streaming", "pending", "running"}:
            continue
        if row.get("completed") is False or row.get("is_complete") is False:
            continue
        assistant_ids.append(str(row_id))
    return {"session_id": conv, "branch_id": channel or conv, "user_message_id": user,
            "message_version": basis or user, "request_id": user, "basis_assistant_message_id": basis,
            "active_path_ancestry": ancestors[-4:], "last_assistant_ids": assistant_ids[-6:]}


def quiet(text):
    return any(marker in text for marker in ("先让我缓缓", "让我缓一缓", "让我先缓缓", "听我讲完", "先听我说完", "先别打断", "先不用给建议"))


def submit_background(coro, tasks):
    task = asyncio.create_task(coro)
    tasks.add(task)
    def done(t):
        tasks.discard(t)
        if not t.cancelled() and t.exception():
            log.warning("assoc background failed type=%s",type(t.exception()).__name__)
    task.add_done_callback(done)
    return task


async def prepare_completed(storage, registry, server, conv, channel, user, assistant):
    from pipeline.memory_gateway_planner import select_recent_planner_messages
    key = (conv, channel)
    task = asyncio.current_task()
    previous = _PREPARING.get(key)
    if previous and previous is not task:
        previous.cancel()
    _PREPARING[key] = task
    try:
        rows = await storage.list_active_path(conv,channel_id=channel,limit=16,latest=True)
        if not rows or rows[-1].get("id") != assistant:
            return
        scope = scope_for(conv,channel,user,rows,assistant)
        visible = select_recent_planner_messages(rows)
        context = "\n".join(f"{r['role']}: {r['content']}" for r in visible)[-6000:]
        await asyncio.wait_for(assoc.prepare_after(scope,context,registry,server),35)
    except Exception as exc:
        log.info("assoc prepare skipped user=%s type=%s",user,type(exc).__name__)
    finally:
        if _PREPARING.get(key) is task:
            _PREPARING.pop(key,None)


async def choose(tasks, literal, deadline):
    # Existing retrieval is already complete. Wait only for a useful completion
    # within the shared grace; an empty result cannot hide a ready sibling.
    pending = {t for t in tasks if t}
    while pending:
        ready, pending = await asyncio.wait(pending,timeout=max(0,deadline-time.monotonic()),
                                             return_when=asyncio.FIRST_COMPLETED)
        if not ready: break
        for t in tasks:
            if t not in ready or t.cancelled(): continue
            try: item=t.result()
            except Exception: continue
            if (item and item.get("source_window") and item.get("event_id") not in literal
                    and item.get("use") in {"mention","understand"}):
                return {**item,"_assoc_origin":"shortlist" if t is tasks[0] else "current_query"}
    return None


def lookup_status():
    return "这轮已经查过。"


def render(item):
    if item.get("use") == "understand":
        return "（这轮留意对方当下的处境，顺着对方的话回应即可。）"
    # Only source-verified quotes become historical detail. Plan and outcome
    # claims travel separately so an unknown outcome cannot read as completed.
    source = str(item.get("source_window") or "")
    quote = str(item.get("excerpt") or "")
    if not quote or quote not in source:
        return ""
    date = str(item.get("date") or "")[:10]
    stamp = f"[Atelier · {date} · 已核对存档]"
    position = source.index(quote)
    start = max(0, position - 70)
    window = source[start:start + 220].strip()
    plan_state = str(item.get("plan_state") or "unknown")
    outcome_state = str(item.get("outcome_state") or "unknown")
    plan_quote = str(item.get("plan_quote") or "")[:220]
    outcome_quote = str(item.get("outcome_quote") or "")[:220]
    # The server has separately re-read the source revision for each quotation.
    # Preserve its cutoff and the factual-only lookup receipt even on long text.
    if plan_state == "planned" and plan_quote:
        window="当时的计划："+plan_quote[:100]
        labels={"completed":"已完成","cancelled":"已取消","rescheduled":"已改期"}
        if outcome_state in labels and outcome_quote:
            window+="\n"+labels[outcome_state]+"，后续原文："+outcome_quote[:100]
    return stamp+"\n"+window[:220]+"\n"+lookup_status()


def actually_mentioned(item, user_text, response):
    if item.get("use")!="mention" or not response: return False
    evidence=re.sub(r"\s+","",str(item.get("source_window") or item.get("excerpt") or ""))
    answer=re.sub(r"\s+","",response)
    markers=r"(?:\d{1,2}月\d{1,2}[日号]?|(?<!\d)\d{1,2}[:：]\d{2}(?!\d)|[A-Za-z][A-Za-z0-9_-]{2,})"
    shared = set(re.findall(markers,evidence)) & set(re.findall(markers,answer))
    if len({value for value in shared if value not in user_text})>=2:
        return True
    blocks=SequenceMatcher(None,evidence,answer,autojunk=False).get_matching_blocks()
    matches=[evidence[b.a:b.a+b.size] for b in blocks if b.size>=3 and evidence[b.a:b.a+b.size] not in user_text]
    return any(len(t)>=7 for t in matches) or len(set(matches))>=2


async def paraphrased(slm,item,user_text,response):
    if slm is None or item.get("use")!="mention" or not response: return False
    from pipeline.assoc_budget import reserve
    original=str(item.get("source_window") or item.get("excerpt") or "")[:480]
    payload=json.dumps({"original":original,"user":user_text[-500:],"reply":response[:1600]},ensure_ascii=False)
    system=('核对回复是否实际说出了 original 中的那一件往事，允许改述。仅话题相似、仅用了用户刚说的内容或泛泛建议都不算。'
            '明确讲到该事件时，返回 {"source":"原文连续片段","reply":"回复中对应的连续片段"}。不确定返回 {}。')
    reserve(getattr(slm, "budget_db", None) or os.environ["ASSOC_GATEWAY_BUDGET_DB"],
            ceiling_usd=1.5,text=system+payload,output=160)
    reply=await slm._post("/chat/completions",json={"model":slm._model,"messages":[{"role":"system","content":system},
                 {"role":"user","content":payload}],"max_tokens":160,"temperature":0},_retries=0)
    reply.raise_for_status()
    obj=json.loads(reply.json()["choices"][0]["message"]["content"])
    a,b=obj.get("source"),obj.get("reply")
    return (isinstance(a,str) and isinstance(b,str) and len(a.strip())>=4 and len(b.strip())>=5
            and a in original and b in response and b not in user_text)


async def settled(bundle, assistant_id, user_text, response, registry, server, slm=None):
    scope,item = bundle
    matched=actually_mentioned(item,user_text,response)
    if not matched:
        try:
            matched=await asyncio.wait_for(paraphrased(slm,item,user_text,response),4)
        except Exception:
            matched=False
    outcome="mentioned" if matched else "unused"
    await asyncio.wait_for(assoc.finish_selection(scope,item,assistant_id,outcome,registry,server),5)
    log.info("assoc settlement user=%s assistant=%s event=%s outcome=%s",scope["user_message_id"],assistant_id,item["event_id"],outcome)
