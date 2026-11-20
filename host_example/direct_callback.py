"""Bridge a parsed, authorized direct callback batch to the chat host."""
from dataclasses import dataclass

from callback_ids import valid_moment_id
from host_example.coordination import DirectResult


@dataclass(frozen=True)
class CallbackItem:
    bucket_id: str
    moment_id: str
    content: str


async def callback_result(items, receipt, *, max_items=2, max_chars=1200):
    """Check all IDs before slicing. A receipt is bound by the caller to this batch."""
    items = tuple(items)
    buckets, moments = set(), set()
    for item in items:
        if (not valid_moment_id(item.moment_id, item.bucket_id)
                or item.bucket_id in buckets or item.moment_id in moments):
            await receipt.abort_if_prepared()
            return DirectResult(lookup_attempted=True)
        buckets.add(item.bucket_id)
        moments.add(item.moment_id)

    lines, included = [], []
    remaining = max_chars
    for item in items[:max(0, max_items)]:
        entry = f"[{item.bucket_id}] {item.content}"
        cost = len(entry) + (1 if lines else 0)
        if cost > remaining:
            break
        remaining -= cost
        lines.append(entry)
        included.append(item.bucket_id)
    if not included:
        await receipt.abort_if_prepared()
        return DirectResult(lookup_attempted=True)
    return DirectResult(text="\n".join(lines), event_ids=tuple(included), receipt=receipt,
                        lookup_attempted=True, abort_if_prepared=receipt.abort_if_prepared)
