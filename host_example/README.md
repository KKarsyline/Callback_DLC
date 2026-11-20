# Connect associative recall to a chat application

Run these commands in the package root, the directory containing `assoc_index.py`. Use Python 3.12 on macOS or Linux. The released index and plan refresher use POSIX file locks.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m host_example.demo
.venv/bin/python -m pytest -q tests/test_host_example.py
```

The demo builds the released SQLite cue index from a fictional workshop log. One completed chat reply prepares a shortlist. The next user asks how to keep loose pieces inside a tilted display case. The gateway revalidates the source, renders it, and the example reply quotes the brass latch detail. The demo reads the resulting `mentioned` receipt from SQLite. Asking again on the direct route returns “The display case has a hinged lid.”

The source, visible chat rows, embeddings and model responses in `demo.py` are synthetic. The indexer, ranking, plan and source checks, selection ledger, gateway cache, selection, rendering and settlement use the package modules. The demo needs no credentials or network access after installation. It removes its temporary state on exit.

## Wire the memory service

Create one long-lived index and service per authorized source collection. Keep the same embedding engine instance for the ordinary literal lookup and the service. All components that use a state directory must agree on its sources and model configuration.

```python
from embedding_engine import EmbeddingEngine
from pathlib import Path
from providers import GatewayJudge, RecallModel
from host_example.coordination import ChatHost, LocalRegistry
from service import open_service

# openai_client is an AsyncOpenAI-compatible client. http_client is an
# authenticated httpx.AsyncClient with the gateway model API as its base URL.
model = RecallModel(openai_client, recall_model_name)
embeddings = EmbeddingEngine(openai_client, embedding_model_name)
judge = GatewayJudge(http_client, gateway_model_name,
                     budget_db=Path(state_dir) / "gateway-budget.sqlite3")
async with open_service(state_dir=state_dir, moments=moments, buckets=buckets,
                        raw_events=raw_events, embeddings=embeddings, model=model,
                        list_edges=list_authorized_edges) as (index, service):
    host = ChatHost(chat_storage, LocalRegistry(service), judge)
    try:
        await serve_chat(host)  # the application's existing request loop
    finally:
        await host.drain()
```

Supply these application objects to the constructors.

| Port | What the host supplies |
| --- | --- |
| `moments.list_all()`, `moments.get(moment_id)` | Synchronous, current records with stable `moment_id`, `bucket_id`, `section` and literal `text`. Use an evidence section such as `body`. Return the complete authorized collection from `list_all`. The index uses absence to retire rows during a full scan. |
| `await buckets.get(id)`, `await buckets.list_all(include_archive=True)` | Current records with `id`, authoritative `content`, `type` and a metadata dictionary. Return missing records as `None`. Preserve dates, archive status, exclusions and `source_raw_event_ids`. Supply the text as it exists now, including edits and withdrawals. The released index binds a cue to a revision and checks the literal quote against current content before delivery. |
| `raw_events.superseded_ids()` | Synchronous set of retired numeric raw-source IDs in the same namespace as bucket metadata. A matching ID invalidates the evidence. |
| `list_edges()` | Synchronous current relations with `source`, `target` and `relation_type`. The plan refresher considers explicit `same_event` and `updates` links for later outcomes. Omit this option for a collection without links. |
| Model and embeddings | The core requests JSON completions and vector embeddings. The gateway requests a separate judgment over a prepared shortlist and can check a paraphrase after commit. `providers.py` and `embedding_engine.py` supply the client bindings. Set credentials, model names, request timeouts and client shutdown in the application. |

`open_service` starts the incremental cue/plan worker and joins it on shutdown. To build a new collection before serving chat, run bounded batches of `await index.rebuild(limit=12)` and inspect `indexed`, `failed`, `busy` and `complete`. Retry contention and investigate recurring failures before running another batch. Warm the feature cache with `await index.warm()`. A one-shot host can also call `refresh_plan_states(index, limit=12)` from `assoc_plans`. Each source edit or withdrawal should immediately be visible to the read ports, including between shortlist creation and validation.

Give `GatewayJudge` a writable `budget_db` path as shown, or set `ASSOC_GATEWAY_BUDGET_DB` before starting the application. Gateway model calls require one of these settings. Keep the gateway file separate from `state_dir/assoc_recall.sqlite3`, which holds the core budget, cue index and selection ledger. Gateway and core reservations enforce independent monthly estimates across concurrent callers sharing each file. Their released ceilings are USD 1.50 and USD 8.50 respectively. Check the configured rates against the providers you select.

## Connect the chat lifecycle

`ChatHost.turn` expects a committed, visible user message. Use a globally unique session ID, a stable branch ID, and an immutable user-message ID. An edit, regeneration or fork must identify a distinct request or branch so previous receipts retain their meaning. The small host uses the immutable user ID for request and message version through the release’s `scope_for`. If your system reuses a user ID for multiple attempts, map the attempt to a distinct branch or extend the scope mapping with your persisted request and version IDs.

```python
assistant_id, visible_reply = await host.turn(
    session_id, branch_id, user_message_id, latest_text,
    plan=plan_this_turn,
    lookup=ordinary_lookup,
    reply_and_commit=generate_and_store_reply,
    allow_assoc=eligible_for_optional_recall,
)
```

1. **Read the active path.** Implement `await chat_storage.list_active_path(session, channel_id=branch, limit=16, latest=True)` in oldest-to-newest order on the selected branch. Include row IDs, roles, content and visibility metadata. The current user row must be the leaf. The preparation hook later requires the newly committed assistant to be the leaf. Supply only authorized rows and keep injected notes, private content, tools and partial responses out of visible dialogue. Session and branch IDs scope the cache and receipts. The source adapters determine which records the user may read.
2. **Count completed replies.** Implement `await chat_storage.completed_assistant_path(session, branch, leaf_id)` from the persisted parent chain, returning the last six visible, completed assistant IDs in order. Count ordinary replies even when recall was disabled. Skip drafts, cancellations, tool-only rows and proactive messages. Read beyond the recent-message display limit when necessary. An empty list means the path has zero such replies. On an unavailable path counter the host omits the field and the core uses its legacy receipt-expiry check. The package includes a SQLite implementation for hosts with a compatible message/run schema.
3. **Run the normal planner and direct lookup.** `await plan()` returns `(route, exact_query)`. Use `direct` for ordinary literal retrieval and `none` for ordinary chat. Routes such as `raw_archive` or `router_error` bypass associative delivery. The host calls `await lookup(route, exact_query)` for the baseline on every turn. Return a `DirectResult` carrying the normal context, IDs of events actually injected and any native receipt. For the direct route, the literal search may generate its ordinary vector through `await embeddings._generate_embedding(exact_query, kind="query")`. The released current-query arm can then wait for that same vector. Without a vector it can search the warm lexical index.
4. **Assemble and commit the reply.** `await reply_and_commit(direct, extra)` receives the original direct result and at most one optional context block. Add both at the host’s existing model-context boundary. A direct event wins a duplicate. Set `DirectResult.lookup_attempted=True` on an attempted direct lookup if the model should receive the release’s factual lookup status on an empty result. Accept the native direct receipt at the host’s normal delivery boundary. If direct retrieval reserves a receipt, supply its async `abort_if_prepared` callback on the result. The coordinator schedules it when interrupted after lookup, on generation failure, or for a suppressed answer. Have this callback check the native terminal state, since delivery may already have succeeded. The lookup itself owns cleanup until it returns its result. Return `(assistant_id, final_visible_text)` only after the reply has been committed against the expected user leaf. Propagate cancellation or generation failure. A suppressed or empty answer earns no mention.
5. **Settle and prepare after commit.** The gateway checks the final reply against the selected source and uses a bounded paraphrase check when needed. Settlement writes `mentioned` or `unused`. A failed delivery with a selected token requests `cancelled`. Only `mentioned` activates cooldown. Unselected validation reservations age out. Each eligible commit triggers a fresh background read, including when the initial active-path read missed the foreground deadline. Preparation requires the same user ID and text, followed on that branch by the committed assistant ID and visible reply at the leaf. Both messages must pass the visibility filter. An intervening tool row is allowed. A changed, hidden or superseded pair skips preparation. The gateway prepares up to six candidates and checks the basis assistant against active ancestry when the cache is read.

The lookup planner runs alongside scope loading and shortlist validation/judgment. On the direct route, current-query work starts alongside the existing literal lookup. Both optional arms share one final wait, with 190 ms of scheduled grace within the 200 ms target. The 15-second current-query service limit allows overlap with existing retrieval. The coordinator cancels unfinished optional tasks as soon as selection ends. Application I/O and model generation retain their own timeouts. Keep synchronous adapters fast enough to let the event loop enforce the deadline.

Set `allow_assoc=False` for disabled features, private or suppressed turns, proactive events, rewinds and requests for quiet. The release also recognizes a small set of quiet phrases. On an unavailable source, rejected judgment, expired budget or timeout, the original direct result still reaches the reply callback. If a baseline callback fails, the application’s existing error policy applies.

`ChatHost` owns background tasks in one event loop. Keep one instance for the conversation worker. While a mention receipt is settling, a following turn on that branch can continue immediately via direct lookup. Call `await host.drain()` at shutdown or at a deterministic test boundary. A multi-worker host should route successive turns consistently or coordinate pending receipts across workers. A process exit between commit and the background finish can lose that receipt. Hosts needing crash recovery can persist pending finish work with the reply and replay it within the token lifetime. The core accepts a scoped token once and retains its first terminal outcome.

## Adapt an existing direct callback

For the SQLite example in [turn_store.py](../storage/turn_store.py), map your message and completion records to the example's `messages` and `assistant_runs` columns, or implement `completed_assistant_path` against your own schema. No production table needs to be renamed.

`host_example.direct_callback.callback_result` is optional. Use it after the host has authenticated the transport, authorized access to every selected source, and parsed the entire callback envelope. In a parser that checks moment IDs during decoding, use `valid_moment_id(moment_id, bucket_id)` there too so valid composite IDs reach the helper. Keep the existing checks for schema, status, request binding, item count, required fields, text and scores. Only pass a receipt the host has already verified belongs to this request. If parsing fails, the host owns safe disposal of any independently verified reservation.

```python
from host_example.direct_callback import CallbackItem, callback_result

# Inside ordinary_lookup, after the native parser returns an authorized batch.
items = [CallbackItem(row.bucket_id, row.moment_id, row.content) for row in parsed_items]
return await callback_result(items, prepared_receipt, max_items=2, max_chars=1200)
```

The helper calls `callback_ids.valid_moment_id(moment_id, bucket_id)` for the batch before applying prompt limits. Ordinary moment IDs and IDs with the owning bucket’s prefix and 16 lowercase hexadecimal digits can coexist. An invalid pair or a repeated bucket or moment ID discards the whole batch, including when the offending item falls beyond the prompt limit. If nothing fits, the helper discards the verified reservation and returns empty direct context.

The helper renders each included entry with its bucket ID and returns those same IDs, in order, as `direct.event_ids`. On model acceptance, the delivery callback calls `await direct.receipt.accept(direct.event_ids)` if a receipt is present. Map `accept` and `abort_if_prepared` to the native receipt’s idempotent commit/discard operations. The accepted IDs feed the direct receipt and associative event deduplication. The runnable demo exercises this path with a synthetic native receipt.
