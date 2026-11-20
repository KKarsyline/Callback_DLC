# mcp-lifespan · 原宿主新增钩子

这里仅列这两期新增的调用，供已有宿主比对生命周期，片段依赖原宿主的局部变量。独立运行入口见根目录 README。

### 接入点 1 · 初始化已授权的记忆集合

```python
# The independent associative channel is opt-in and has no disabled-import side effects.
# AssocIndex is supplied by the evidence-index implementation.
assoc_recall_enabled = os.environ.get("OMBRE_ASSOC_RECALL_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
assoc_recall = None
if assoc_recall_enabled:
    from assoc_index import AssocIndex
    from assoc_recall import AssociativeRecall
    assoc_index = AssocIndex(config, memory_moment_store, bucket_mgr, raw_event_store,
                             embedding_engine, dehydrator)
    assoc_recall = AssociativeRecall(config, assoc_index, dehydrator, embedding_engine)

```

### 接入点 2 · 注册内部召回方法

```python
if assoc_recall_enabled:
    @mcp.tool()
    async def assoc_prepare(body: dict) -> dict:
        """Prepare up to six evidence-backed associative candidates."""
        return await assoc_recall.prepare(body)

    @mcp.tool()
    async def assoc_validate(body: dict) -> dict:
        """Recheck candidate evidence and reserve valid selections."""
        return await assoc_recall.validate(body)

    @mcp.tool()
    async def assoc_query(body: dict) -> dict:
        """Search using the exact literal query and only an existing query vector."""
        return await assoc_recall.query(body)

    @mcp.tool()
    async def assoc_finish(body: dict) -> dict:
        """Settle an associative selection idempotently."""
        return assoc_recall.finish(body)


```

### 接入点 3 · `if __name__ == "__main__":`

```python
        if assoc_recall_enabled:
            from contextlib import asynccontextmanager, suppress
            previous_lifespan = _app.router.lifespan_context
            @asynccontextmanager
            async def assoc_lifespan(app):
                async with previous_lifespan(app) as state:
                    await assoc_recall._ensure_worker()
                    try:
                        yield state
                    finally:
                        worker=assoc_recall._worker
                        if worker:
                            worker.cancel()
                            with suppress(asyncio.CancelledError):
                                await worker
            _app.router.lifespan_context = assoc_lifespan
```
