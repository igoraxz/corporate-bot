# Component: Task Queue & Message Processing

**Purpose**: SQLite-backed persistent task queue (PersistentQueue) ensures crash-safe,
sequential per-chat message processing with burst coalescing and mid-task injection.

**Key files**: `bot/pipeline/task_queue.py`, `bot/pipeline/dispatch.py`, `bot/pipeline/handlers.py`, `bot/pipeline/streaming.py`

---

## Design Principles

- **SQLite-backed** — crash-safe; tasks survive bot restarts (no lost messages)
- **Sequential per chat** — one active task per chat at a time; extras queued behind it
- **Burst coalescing** — rapid-fire messages merged into one task (decision order: merge → inject → enqueue)
- **Mid-task injection** — text-only messages written to stdin of the live SDK subprocess (no new task created)
- **Global cap** — MAX_TOTAL_TASKS=15 across all chats; MAX_PARALLEL_TASKS=3 per chat

## Decision Order for Incoming Message

```
1. MERGE INTO PENDING — if a pending PQ row exists for this chat:
   append text to parsed_json.text + media to parsed_json.merged_media
   Soft caps: 50KB text, 10 media items. Atomic SELECT+UPDATE with status='pending' guard.

2. MID-TASK INJECT — text-only + SDK actively processing this chat:
   write to subprocess stdin via transport.write() + _write_lock
   Skip if has_pending_teardown(session_key) is True (session about to be wiped)

3. ENQUEUE FRESH ROW — default path when neither above applies
```

## Dispatch Debounce

Rows are NOT dispatched immediately — claimed after `PQ_DISPATCH_DEBOUNCE_MS` (default 150ms) of
silence. Every merge bumps `updated_at`, extending the window. Once the chat goes quiet, row dispatches.
Set `PQ_DISPATCH_DEBOUNCE_MS=0` to disable (pre-v5 behavior).

## Streaming UX (TG)

- **Deferred placeholder** — "🧠 Thinking..." placeholder created 300ms after task starts (avoids flash for instant responses)
- **Streaming edits** — placeholder updated with tool chain state every 3s while model works
- **Deleted on reply** — when model calls `telegram_send_message`, placeholder is deleted and reply is sent as fresh message
- **Fallback salvage** — if model ran tools but never called a send tool, `response_text` is delivered from the `finally` block (prevents silent failures)

## TG Placeholder Invariant

The "lost-placeholder" branch recreates with literal "🧠 Thinking..." string — NEVER stream content.
`_ph_deferred` task is cancelled eagerly from 3 sites:
1. `on_tool_status` when a SEND_TOOL fires
2. `on_stream_chunk` when lost-placeholder is recreated
3. `finally` block cleanup

## Anti-Patterns

- **NEVER** inject media messages — only text-only messages can be injected; media needs full download + description processing
- **NEVER** inject when teardown is pending — check `has_pending_teardown(session_key)` first; if True, enqueue normally
- **NEVER** skip the debounce window — it exists to prevent premature dispatch of partial bursts

## Pitfalls

- **user_msg_id for threading** — for burst-coalesced messages, `user_msg_id = merged_message_ids[-1]` (latest
  message in the burst). Reply threads to the MOST RECENT user message in the burst, not the first.
- **External chat catch-up** — stale messages from active/active_plus chats are enqueued with catch-up debounce
  on bot restart. `_was_deploy_restart()` checks a marker file to skip catch-up after intentional deploys.
- **MAX_TOTAL_TASKS is global** — if 15 tasks are running across all chats, new tasks queue and wait.
  This cap prevents resource exhaustion but can cause visible delays during high-traffic periods.
- **Merged media processing** — `handle_telegram_message` iterates `parsed["merged_media"]`, downloads + describes
  each item, appends `[Merged message #N — PHOTO attached: /path]` blocks to the prompt text.
