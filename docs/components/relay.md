# Component: Employee Workstation Control

**Purpose**: Routes TG messages to persistent Claude SDK daemons running on employee Mac
workstations, enabling full Desktop Claude sessions to be controlled from dedicated
Telegram channels.

**Key files**: `bot/relay/` (ssh.py, registry.py, oauth.py, sessions.py, watchdog.py, core.py, pool.py, ux.py, mac_resolver.py),
`bot/commands.py`
**Mac-side**: `scripts/desktop-relay/guard.sh`, `~/.relay/sdk_persistent_daemon.py`

---

## Design Principles

- **Persistent daemon per workstation channel** — one long-lived Python daemon on employee Mac per TG workstation channel
- **SSH transport** — bot → SSH → guard.sh → daemon commands (guard.sh validates TOTP on every command)
- **TOTP auth** — 2h sliding TTL; EVERY SSH command validated, not just session start
- **Pool state machine** — DISCONNECTED → CONNECTING → READY ⇄ BUSY → ERROR → RETRY → DEAD
  (exp-backoff: 1s/3s/9s/27s/60s, max 5 retries before DEAD)
- **Workstation channels bypass bot SDK entirely** — messages go to employee Mac daemon directly; hooks Layer 1b2 does NOT apply
- **Workstation channels have NO bot-local harness** — employee Mac-side Claude config controls model/effort/context

## Key Flow

```
TG message → triage_and_enqueue → relay chat? → relay.pool.acquire(chat_id)
  → daemon.run_query(prompt) via SSH pipe
  → stream-json events → bot relays to TG (streaming placeholder)
  → result event → final reply sent as fresh TG message
  → daemon returns to READY state in pool
```

## Mid-Task Injection

Text messages arriving mid-query are injected via stdin into the live daemon subprocess.
Old placeholder deleted, new "🧠 Noted..." placeholder created, threaded to the injected message.

## Subprocess Lifecycle & Drain

After 5 min idle (`_SUBPROCESS_IDLE_TIMEOUT_S`), a drain task closes subprocess stdin.
Two safety mechanisms prevent the zombie-alive state (FB-53, May 2026):
1. **Post-close kill watchdog** in `_drain_and_close`: waits 30s (`_SUBPROCESS_STDIN_CLOSE_KILL_S`)
   for the process to exit after stdin close, then force-kills if it lingers.
2. **Inline force-kill** in `_ensure_proc_and_write`: if a new query detects `stdin.is_closing()`,
   force-kills the lingering process and spawns fresh (instead of returning an error).
Both use identity-guarded `if self._proc is proc` pattern to avoid clobbering a
concurrently-spawned subprocess.

## Catchup (3 Scenarios on Daemon Spawn)

| Scenario | Trigger | Content |
|---------|---------|---------|
| first_run | No saved offset for chat+uuid | Last 10 query/response pairs from full JSONL |
| delta | Saved offset exists | Last 10 NEW pairs since saved offset |
| repinned | `/session pin` to new UUID | Last 10 pairs from new JSONL (deduped vs last delivered) |

Bot-side dedup via `_catchup_last_delivered_ts` (per-chat ephemeral dict) prevents
duplicate delivery when repinning to a JSONL with overlapping history.

## Realtime Desktop→TG Catchup

Daemon tails the pinned JSONL every 500ms and emits `desktop_turn` events for Desktop Claude activity.
Bot-side listener routes these to TG. Three-layer dedup:
1. Daemon-side `message.id` set (bounded FIFO, cap 10K)
2. Offset-range check (own writes vs Desktop writes)
3. Bot-side `BoundedDedup` per chat (cap 10K, ephemeral)

## Anti-Patterns

- **NEVER** carry `mac_desktop` status from prior messages — always use LIVE CONTEXT header value
- **NEVER** delete JSONL files without explicit admin confirmation (needed for `--resume` and investigation)
- **NEVER** show harness tags in `/relays` display — relay chats have no bot-local harness
- **NEVER** rely on in-memory pool state across bot restarts — pool rebuilds from scratch on restart

## Pitfalls

- **base64 on macOS requires `-i` flag** — `base64 FILE` (Linux) vs `base64 -i FILE` (Mac).
  Already fixed in guard.sh. Do NOT revert.
- **guard.sh hardened (FB-32, FB-40)** — only `relay-*` prefix commands accepted. Generic `tmux *` /
  `claude *` catch-all removed. `relay-fetch` has path allowlist. No `eval "$CMD"` anywhere.
  **Mac install approval gate (FB-40)**: `relay-install-skills` and `relay-install-daemon` write
  to `~/.relay/pending-installs/<type>/` — operator must run `~/.relay/approve-install.sh`
  (root-owned, NOT in guard.sh whitelist) to review and approve. `relay-install-status` checks
  pending state. Bot's `install_status` action auto-respawns dead daemons after approval.
  After bot deploy, sync guard.sh + approve-install.sh to Mac:
  `cp scripts/desktop-relay/guard.sh ~/.relay/ && sudo cp scripts/desktop-relay/approve-install.sh ~/.relay/ && sudo chown root:staff ~/.relay/approve-install.sh`
- **JSONL is the session** — `--resume UUID` lets daemon resume exactly where Desktop Claude.app left off.
  UUID comes from the JSONL filename pinned to the relay chat.
- **Pool stuck in CONNECTING** — use `/relay-reset [N]` to kill + immediate respawn.
- **Daemon-side FIFO correlation** — `_pending_qids` deque + per-qid `asyncio.Event` routes stream/result
  events to correct query ID. On `result`, head qid pops and its event fires. Mismatched qids cause
  stuck placeholders — see injection merge semantics (fact `relay_injection_semantics_verified`).
- **Relay file attachments** — bot strips `[RELAY_FILE: /abs/path]` tags from daemon replies,
  fetches via `relay-fetch` SSH command → sends as TG document. guard.sh rejects `..` and symlinks.
- **Inbound media push** — TG photos/docs sent to relay chat are pushed to Mac via `relay-push-file`
  SSH command; path injected into daemon query as `[Attached <type>: /Users/youruser/.relay/inbox/<sha>.<ext>]`.

## Guard.sh SSH Interface

```
relay-status                   # check daemon health and pid
relay-fetch <path>             # read file from Mac (base64 encoded output)
relay-push-file <sha256> <ext> # receive file upload (base64 via stdin → ~/.relay/inbox/)
relay-push-oauth <label>       # push OAuth profile JSON to Mac
relay-backup-put/list/prune    # backup management (TOTP-exempt for cron)
```
