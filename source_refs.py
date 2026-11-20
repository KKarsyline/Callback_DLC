def source_ref_window(moment, *, body, max_chars, context_lines):
    """Map document line coordinates to the authorized body; never reopen a suggested path."""
    metadata = moment.get("metadata") or {}
    ref = metadata.get("source_ref") or {}
    if not isinstance(ref, dict) or not body:
        return ""
    first = int(ref.get("content_start_line") or 1)
    start = int(ref.get("start_line") or 0) - first
    stop = int(ref.get("end_line") or ref.get("start_line") or 0) - first + 1
    if not 0 <= start < stop:
        return ""
    lines = body.splitlines()
    return "\n".join(lines[max(0, start-context_lines):stop+context_lines])[:max_chars]
