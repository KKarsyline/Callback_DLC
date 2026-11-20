def select_recent_planner_messages(rows, *, current_message_id=""):
    visible = []
    for row in rows or ():
        if not isinstance(row, dict) or row.get("role") not in {"assistant", "user"}:
            continue
        metadata = row.get("metadata") or {}
        if not isinstance(metadata, dict) or row.get("id") == current_message_id:
            continue
        if any(row.get(key) or metadata.get(key) for key in (
            "private", "is_private", "injected", "is_injected", "memory_injected", "tool_calls",
            "tool_call_id", "is_proactive", "cancelled", "hidden",
        )):
            continue
        if any(str(record.get("visibility") or "visible").lower() not in {"visible", "public", "normal"}
               for record in (row, metadata)):
            continue
        if row.get("role") == "assistant" and any(
            record.get(key) not in {None, "completed", "complete", "final"} for record in (row, metadata)
            for key in ("status", "state")
        ):
            continue
        if any(record.get(key, True) is not True for record in (row, metadata) for key in ("completed", "is_complete")):
            continue
        content = row.get("content")
        if isinstance(content, list):
            content = "\n".join(block["text"] for block in content if isinstance(block, dict)
                                and block.get("type") == "text" and isinstance(block.get("text"), str)
                                and not block.get("private") and not block.get("hidden")
                                and block.get("visibility") in {None, "visible", "public", "normal"})
        if isinstance(content, str) and content.strip() and "<atelier_context>" not in content:
            visible.append({"role": row["role"], "content": content.strip()})
    return visible[-10:]
