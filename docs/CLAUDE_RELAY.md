# Employee Workstation Control — Persistent SDK Daemon Pool

**Status:** Live. Always-on (no fallback — rollback = `git revert`).

The employee workstation control lets dedicated Telegram channels drive Claude
Code sessions on employee Mac workstations. Each channel maps to one long-lived
daemon on the employee's Mac, which hosts a `ClaudeSDKClient` resumed onto the
channel's pinned JSONL UUID. Queries flow over a persistent SSH pipe; stream-json
flows back.

## Architecture

```
TG relay query
    │
    ▼
bot/main.py _handle_relay_message
    │
    ▼
bot/relay/pool.py  RelayClientPool.acquire(chat_id, uuid, cwd)
    │
    ▼
ssh -T mac relay-sdk-persistent <chat_id> <uuid> <cwd_b64>
    │
    ▼
~/.relay/sdk_persistent_daemon.py (Mac, long-lived)
    │
    ▼
claude-agent-sdk ClaudeSDKClient (resume UUID) ⇄ stream-json over stdio
```

The first message per chat spawns the daemon. Subsequent messages reuse it,
skipping the ~1–3 s subprocess-spawn cost per turn and keeping SDK session
state warm between turns.

## State Machine (per `RelayPersistentClient`)

```
DISCONNECTED ──► CONNECTING ──► READY ⇄ BUSY
     ▲                │              │
     │                ▼              ▼
     └── RETRY ──── ERROR ◄──────────┘
             ▲
             │ exp backoff 1/3/9/27/60s, max 5 tries
             ▼
             DEAD (manual reset via /relay-kill + next query)
```

Query correlation via uuid4 ids — each `query()` call gets its own id and
filters the shared reader stream for matching events.

## Mac-side resources

`~/.relay/daemons/<chat_id>.{pid,state,log}` — pidfile + state JSON + rotating log (10MB × 3).
Orphan defence: on every `relay-sdk-persistent` spawn, daemon checks its own pidfile and
refuses to start if another live daemon already owns that chat_id. `relay-reap-orphans`
sweeps stale pidfiles whose PIDs are dead.

## Guard.sh commands (Mac-side)

| Command | Purpose |
|---|---|
| `relay-sdk-persistent <chat_id> <uuid> <cwd_b64> [label] [model] [effort]` | Spawn/attach daemon, stream stdio both ways. Optional overrides for model (e.g. `opus[1m]`) and effort (`low`/`medium`/`high`/`xhigh`). |
| `relay-ping-daemon <chat_id>` | Liveness probe: `ALIVE:<pid>:<heartbeat_age>s` or `DEAD` |
| `relay-list-daemons` | Dump all pidfiles: `<chat_id> <pid> <status> <heartbeat_age>s <uuid> [model] [effort]` |
| `relay-kill-daemon <chat_id>` | TERM→KILL specific daemon |
| `relay-reap-orphans` | Sweep dead pidfiles |
All guarded by TOTP session (existing auth flow unchanged).

The ephemeral `relay-sdk-query` path was removed alongside the Claude relay
collapse — there is no longer any non-persistent Claude invocation.

## Bot-side surface

- `bot/relay/pool.py` — `RelayClientPool` (chat_id → client) +
  `RelayPersistentClient` (state machine, reader_loop, query(id) correlation).
- `main.py._handle_relay_message` — relay dispatch: acquires a pool client,
  streams stream-json back into the TG placeholder.
- `desktop_relay` MCP tool actions: `daemons`, `kill_daemon`, `reap_orphans`.
- Admin slash commands (relay chats + admin DM): `/relays`, `/relay-kill <chat_id>`, `/relay-reap`.

## Config

No feature flag — the pool path is always-on. Rollback is `git revert`.

Env vars relevant to the relay:

```
DESKTOP_ENABLED=true           # master feature flag (entire relay)
DESKTOP_HOST=...               # Tailscale MagicDNS hostname of the Mac
DESKTOP_USER=youruser              # Mac username
```

## Observability

- Pool health: `desktop_relay(action="daemons")` returns
  `{mac_daemons: [...], pool_clients: [...]}`.
- Logs: `bot/relay/pool.py` logs at DEBUG for per-entry yields and
  INFO for lifecycle events (spawn, state change, retry, error, terminate).
- Mac-side: `~/.relay/daemons/<chat_id>.log` (rotating).

## Related docs

- `DESKTOP_RELAY_SETUP.md` — full setup guide (TOTP, SSH keys, Tailscale).
- `CLAUDE.md` → "Claude relay" section for architecture overview.
- `.claude/skills/relay-chats/SKILL.md` — bot-facing skill with dispatch details.
