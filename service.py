from contextlib import asynccontextmanager
import asyncio
from pathlib import Path

from assoc_index import AssocIndex
from assoc_recall import AssociativeRecall


@asynccontextmanager
async def open_service(*, state_dir, moments, buckets, raw_events, embeddings, model, list_edges=tuple,
                       list_edges_async=None):
    """Open one persistent recall service for exactly one authorized memory collection."""
    state = Path(state_dir).expanduser().resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    config = {"state_dir": str(state), "buckets_dir": str(getattr(buckets, "base_dir", "")),
              "list_edges": list_edges, "list_edges_async": list_edges_async}
    index = AssocIndex(config, moments, buckets, raw_events, embeddings, model)
    recall = AssociativeRecall(config, index, model, embeddings)
    await recall._ensure_worker()
    try:
        yield index, recall
    finally:
        if recall._worker:
            recall._worker.cancel()
            await asyncio.gather(recall._worker, return_exceptions=True)
        if index._refresh_task:
            await asyncio.gather(index._refresh_task, return_exceptions=True)
