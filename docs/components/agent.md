# Component: Agent / SDK Session Management

**Purpose**: Manages persistent Claude SDK client sessions (one per chat), executes queries, handles
budget, rate limits, compaction, and session recovery.

**Key files**: `bot/agent.py`, `bot/core/session_state.py`, `config.py`

---

## Design Principles

- **One `ClaudeSDKClient` per chat** — sessions are NEVER shared between chats (no isolation in SDK)
- **Permanent sessions** — SESSION_TIMEOUT = 365d (effectively forever); resume via `--resume UUID`
- **`permission_mode="bypassPermissions"`** — never pass `allow_dangerously_skip_permissions=True` (crashes SDK)
- **Per-session state via dict, NOT ContextVars** — `_session_state[session_key]` dict; ContextVars don't propagate to SDK hook tasks
- **No process-global session state** — `_active_session_key` was removed (AP-15); all session identity via explicit `session_key` params or `_session_key` in tool args
- **Budget auto-reset** — on USD cap hit: clear session_id, notify user to retry
- **`build_hooks(session_key)`** — creates per-client wrapper closures that eagerly register `session_id → session_key` in `_sid_to_skey` before any hook fires

## Key APIs

```python
client_pool.query(session_key, prompt, ...)   # main entry point
build_hooks(session_key)                       # returns per-client hook closures
get_session_state(session_key)                 # per-session dict
set_reply_threading(session_key, ...)          # MUST be called before every query
_connect_with_retry()                          # 2 retries, 3s/6s backoff
extend_budget(delta_usd)                       # raise cap without losing context (keyword-gated)
```

## Config Values

| Var | Default | Purpose |
|-----|---------|---------|
| MAX_TURNS | 75 | Per-query tool-call limit |
| MAX_BUDGET_DEFAULT | $5 sonnet / $50 opus | Session USD cap |
| QUERY_OUTPUT_CAP_DEFAULT | 25k sonnet / 50k opus | Per-query output token cap |
| SDK_CONNECT_RETRIES | 2 | Retry count on connect() failure |
| SDK_CONNECT_BACKOFF_BASE | 3.0s | Backoff base (doubles each retry) |
| SDK_INIT_TIMEOUT | 180s | Fatal init timeout |

## Anti-Patterns

- **NEVER** pass `allow_dangerously_skip_permissions=True` → immediate SDK crash
- **NEVER** share one `ClaudeSDKClient` across two chats → no session isolation
- **NEVER** use process-global state for session identity → race condition under parallel tasks (AP-15)
- **NEVER** use ContextVars for per-session state → not propagated to SDK hook tasks
- **NEVER** auto-invoke `extend_budget` → keyword-gated, must see "extend budget" in user message

## Pitfalls

- `receive_response()` takes **NO arguments** — don't pass `include_partial` or any params
- `callable` is not valid as a type hint in Python 3.12 — use `typing.Callable`
- `_sid_to_skey` mapping is populated by `build_hooks()` closures BEFORE hooks fire — this is the session resolution mechanism. Missing from `_sid_to_skey` = hook returns detached defaults, not an error
- Rate limit handling: SDK disconnects CLI subprocess but PRESERVES session_id for resume — do NOT reset session on rate limit
- Budget cap hit → FULL session reset (session_id cleared); extend_budget → deferred disconnect, session preserved
- Compaction: `pending_compaction` flag shown in TG streaming placeholder; token metrics sent to user after compaction

## Session State Isolation (Critical)

Three isolation layers — understand before modifying any session-related code:
1. **Hooks**: per-client closures via `build_hooks(session_key)`, resolve via `_sid_to_skey` only
2. **Send tools**: per-client wrappers via `create_messaging_server(session_key)`
3. **Memory tools**: per-client wrappers via `create_memory_server(session_key)`

ALL THREE must stay in sync. If you add a new tool that reads session state, wrap it per-session.

## Flow: Query Lifecycle

```
set_reply_threading(session_key)   ← MUST be first
build_hooks(session_key)           ← eager _sid_to_skey registration
client.query(prompt)               ← SDK executes tool calls
  → on_stream_chunk → TG placeholder edits
  → on_tool_status  → hooks PreToolUse/PostToolUse
  → AssistantMessage.usage → output token cap check
  → ResultMessage.cost → usage_log write (awaited)
_persist_memory_updates(all_text_blocks)  ← extract ALL FACTS_UPDATE from all turns
```
