import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

import numpy as np


async def add_one(index, mid, *, scratch_root):
    root = Path(scratch_root).resolve(strict=True)
    path = Path(index.db_path).resolve(strict=True)
    if root == Path("/") or not path.is_relative_to(root) or path.is_relative_to("/state"):
        raise ValueError("requires an isolated index copy")

    def snapshot(db):
        state = db.execute("SELECT evidence_version,indexed_at FROM assoc_state WHERE moment_id=?", (mid,)).fetchone()
        rows = db.execute("SELECT * FROM assoc_cues WHERE moment_id=? ORDER BY cue_id", (mid,)).fetchall()
        plans = db.execute("SELECT * FROM assoc_plans WHERE moment_id=? ORDER BY cue_id", (mid,)).fetchall()
        return tuple(state) if state else (), [dict(row) for row in rows], [dict(row) for row in plans]

    async def source(revision):
        moment = await index._fresh_moment(mid)
        bucket = await index._bucket(moment["bucket_id"]) if moment else None
        if not bucket or not index._eligible(moment, bucket) or index._snapshot(moment, bucket) != revision:
            return None, None
        return moment, bucket

    with index._connect() as db:
        db.execute("BEGIN")
        state, rows, plans = snapshot(db)
    if not state or not rows or any(row["evidence_version"] != state[0] for row in rows):
        return {"status": "missing_or_stale"}
    if len(rows) >= 4:
        return {"status": "full"}
    moment, bucket = await source(state[0])
    if not moment:
        return {"status": "stale_source"}
    old = [row["cue_text"] for row in rows]
    data = "SOURCE:\n"+moment["text"][:6000]+"\nEXISTING KEYS:\n"+json.dumps(old,ensure_ascii=False)
    try:
        candidate = index._parse_json(await index._complete(
            'Read the SOURCE and the existing index keys separately. Find one explicit action or constraint in the source '
            'that a reader could not learn from the keys alone. Sharing the same topic does not make two keys redundant. '
            'Quote that action verbatim first, then write a short Chinese search key for the same action using functional roles '
            'rather than object names. Preserve who did what; desires must stay desires. Return JSON {"quote":"...","cue_text":"..."}. '
            'If no such evidence exists return {"quote":"","cue_text":""}.\n'+data,max_tokens=240))
        if not isinstance(candidate, dict):
            return {"status":"invalid_generation"}
        cue, quote = candidate.get("cue_text"), candidate.get("quote")
        if not isinstance(cue,str) or not isinstance(quote,str) or not cue.strip() or not quote.strip():
            return {"status":"no_new_cue"}
        cue = cue.strip()
        if len(cue)>120 or len(quote)>220 or cue in old or quote not in moment["text"][:6000] or not index._source_window(moment,bucket,quote):
            return {"status":"invalid_or_duplicate"}
        source_text=moment["text"][:6000]
        offset=source_text.index(quote)
        stop=offset+len(quote)
        starts=[match.end() for match in re.finditer(r"[。！？\n]",source_text[:offset])]
        end=re.search(r"[。！？\n]",source_text[stop:]) if quote[-1] not in "。！？\n" else None
        sentence=source_text[starts[-1] if starts else 0:stop+(end.end() if end else 0)].strip()
        if quote in sentence and len(sentence)<=220 and index._source_window(moment,bucket,sentence):
            quote=sentence
        if any(row["quote"]==quote and row["cue_type"]=="unfinished" for row in rows):
            return {"status":"plan_overlap"}
        checked = index._parse_json(await index._complete(
            '逐项检查线索里的角色、每个动作和约束，仅凭引文是否全部成立？需要或愿望不能写成已经实现，'
            '建议不能写成已经决定；任何一项缺依据就判不支持。是否补充了旧线索没有充分表达的关系？'
            '只输出形如 {"supported":true,"novel":true} 的单个 JSON 对象，两个值各自按实际核验结果填写布尔值。\n' +
            json.dumps({"线索":cue,"引文":quote,"已有线索":old},ensure_ascii=False), max_tokens=48))
        if not isinstance(checked,dict) or not all(type(checked.get(key)) is bool for key in ("supported","novel")):
            return {"status":"invalid_verification"}
        if not checked["supported"] or not checked["novel"]:
            return {"status":"not_verified","supported":checked["supported"],"novel":checked["novel"]}
        vector = np.asarray(await index._embed(cue), dtype="<f4")
        if vector.ndim != 1 or not vector.size or not np.isfinite(vector).all():
            return {"status":"bad_vector"}
        moment, bucket = await source(state[0])
        if not moment or not index._source_window(moment,bucket,quote):
            return {"status":"stale_source"}
        key = hashlib.sha256(json.dumps([mid,state[0],"situation",quote,cue],ensure_ascii=False).encode()).hexdigest()
        with index._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if snapshot(db) != (state, rows, plans):
                return {"status":"concurrent_change"}
            now=time.time()
            db.execute("INSERT INTO assoc_cues VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (key,mid,bucket["id"],state[0],cue,quote,"situation",vector.tobytes(),index.embedding_engine.model,now))
            db.execute("UPDATE assoc_state SET indexed_at=? WHERE moment_id=?", (now,mid))
        return {"status":"added","cue_id":key,"cue":cue,"quote":quote}
    except (ValueError,RuntimeError,TypeError,sqlite3.Error) as exc:
        return {"status":"failed","error":type(exc).__name__}
