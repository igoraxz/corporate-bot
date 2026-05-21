---
description: "Employee Workstation Control — manage Claude Code sessions on employee Mac workstations via dedicated Telegram channels. TRIGGER when: user asks to create/manage workstation channels, link TG groups to employee Mac sessions, check workstation status, manage workstation sessions, or any desktop_relay MCP tool usage. Also trigger for 'mac ping', 'relay status', 'create relay group', 'delete relay group'."
---

# Employee Workstation Control — Mac Development Environment Management

Manage Claude Code sessions on employee Mac workstations via dedicated Telegram
channels. Each workstation channel is a TG group pinned to an employee's Mac
Claude Code session identified by its JSONL UUID. The transport is a **persistent
SDK daemon per channel** on the employee's Mac — one long-lived Python process that hosts a `ClaudeSDKClient` resumed onto the
pinned UUID. Streamed stream-json flows back through the same SSH pipe. The
legacy tmux-TUI transport was removed in Session C (commit `ad6a496`,
2026-04-16, −2,541 LOC).

## Architecture

```
TG workstation channel -> Bot -> RelayClientPool.acquire(chat_id)
                         │
                         ▼
                    ssh -T mac relay-sdk-persistent <chat_id> <uuid> <cwd_b64>
                         │
                         ▼
                    ~/.relay/sdk_persistent_daemon.py (Mac, long-lived)
                         │
                         ▼
                    ClaudeSDKClient(resume=<uuid>) ⇄ stream-json over stdio
```

- Security: zero-trust workstation-side auth. guard.sh validates session before every
  SSH command. Session expires after 2 h sliding. Bot server is untrusted.
- Transport: SSH + `relay-sdk-persistent` on employee Mac spawns the persistent daemon.
  One daemon per workstation channel, pidfile at `~/.relay/daemons/<chat_id>.pid`. Subsequent
  queries reuse the same daemon (no subprocess-spawn cost per turn).
- State machine (per `RelayPersistentClient`):
  DISCONNECTED → CONNECTING → READY ⇄ BUSY → ERROR → RETRY → DEAD
  (exp-backoff 1/3/9/27/60s, max 5). Query correlation via uuid4 ids.
- Output: stream-json entries parsed in real time — `stream_event` text-delta
  chunks accumulate into the TG placeholder at a 1 Hz edit throttle. Dedupe
  logic skips the redundant final `assistant` block when deltas already
  captured the text.
- Files: `relay-fetch` command transfers binary files base64-over-SSH (50 MB cap).
  `relay-push-file` uploads from bot→Mac (inbound media).
- History: the daemon resumes the SAME JSONL that Desktop Claude.app writes
  to. Shared-session model: both sides contribute to one conversation.

## `desktop_relay` MCP tool actions (Employee Workstation Control)

- `authenticate` — pass code to authenticate the SSH session (2 h expiry).
- `status` — Mac connection + session list + auth status.
- `list_all_jsonls` — enumerate ALL JSONL sessions across ALL Mac projects,
  sorted newest-first. Returns uuid + project_path + size + mtime per entry.
  Used from admin DM to pick which project/session to link a relay chat to.
- `create_relay_group` — create TG workstation channel, link to employee Mac project, set up tmux
  holder (for OAuth `/login` flow), pin the current JSONL UUID, inject
  RELAY_FILE protocol into the project `CLAUDE.md`.
- `delete_relay_group` — unlink + disable + leave TG workstation channel. Employee Mac session untouched.
- `set_relay_mode` — enable/disable relay mode on a chat.
- `link_session` / `unlink_session` — manage chat-to-session mappings.
- `fetch` — fetch a file from Mac by path (used for RELAY_FILE delivery).
- `install_daemon` — push updated `sdk_persistent_daemon.py` to Mac. Use after
  daemon code changes. Validates size + shebang, writes atomically to
  `~/.relay/sdk_persistent_daemon.py`. Kill active daemons after to respawn with new code.
- `install_skills` — push mac-skills/ to Mac (coding-principles + review agents + CLAUDE.md snippet).
- `lock` — revoke auth session immediately.
- `ping` — health check (no auth).

Removed in Session C: `send_keys`, `capture`, `discover` — replaced by
the persistent daemon pool.

## Bot-side slash commands (workstation channel only)

- `/session` or `/session info` — show current binding (project path,
  pinned UUID, session name) and any active overrides.
- `/session list` — list recent JSONL sessions for this project (top 15,
  sorted by mtime). A 📌 marks the currently-pinned session.
- `/session pin <N|uuid-prefix>` — re-pin by list number (from `/session list`)
  or by uuid-prefix (≥4 hex chars, must match uniquely).
- `/session model <name>` — set model override (e.g. `opus`, `sonnet`). `clear` to remove.
- `/session effort <level>` — set effort override (`low`/`medium`/`high`/`xhigh`). `clear` to remove.
- `/session context 1m|200k` — toggle 1M context (appends `[1m]` to model override).
- `/session settings` — show effective settings (overrides + live daemon state).
- `/session reset-settings` — clear all overrides (back to Mac defaults).

**Bot-side admin commands** (intercepted BEFORE forwarding to Mac daemon):

- `/relays` — list active workstation channels with linked sessions and status.
- `/relay-reset [N]` — restart daemon (kill + immediate respawn). N from `/relays`, or no-arg = current workstation channel.
- `/relay-reap` — sweep dead workstation pidfiles on employee Mac.
- `/create-relay N` — create workstation channel for project #N from `/mac-sessions`.
- `/delete-relay N` — remove workstation channel #N from `/relays`.
- `/mac` / `/claude-auth [label]` / `/lock` — connection + auth mgmt.
- `/mac-sessions` — numbered employee Mac project list (also `/sessions`, `/macsessions`).

**Pass-through to Mac Claude** (anything else): `/login`, `/cost`, `/clear`,
`/status`, `/resume`, `/model`, etc. — forwarded unchanged.

Legacy `/usage`, `/reset`, `/relay-follow` were removed in Session C.

## File delivery protocol (RELAY_FILE)

Mac Claude outputs `[RELAY_FILE: /absolute/path]` in its response text. The bot
detects the tag in streaming output, fetches the file via SSH `relay-fetch`
(base64), saves to `/app/data/tmp/`, and sends via `telegram_send_document`.

## Creating a workstation channel

1. Check Mac is online: `desktop_relay(action="status")`.
2. Authenticate if needed: `desktop_relay(action="authenticate", totp_code="123456")`.
3. `desktop_relay(action="create_relay_group", session_name="...",
   project_path="...", group_title="...")`.
   Automatic: TG group + bot added, `active_plus` + `relay_mode` +
   `sdk_relay=true`, tmux holder session, pinned JSONL UUID, RELAY_FILE
   protocol injected.

## Workstation channel runtime behaviour

Text messages route to `_handle_relay_message` in `main.py` (handles workstation channel I/O):

1. Placeholder created: `🧠 Thinking... (Xs)` (or `🧠 Noted...` for mid-task
   injection — see below).
2. `RelayClientPool.acquire(chat_id, uuid, cwd)` returns the live daemon's client.
   First call spawns the daemon via `relay-sdk-persistent`; subsequent calls reuse it.
3. `stream_event / content_block_delta / text_delta` entries accumulate into
   the placeholder at a 1 Hz edit throttle.
4. If no text for 60 s, the placeholder flips to `⏳ Still working... (Xs)` —
   proves the daemon is alive.
5. On `result` entry: send final text + cost footer, delete placeholder.
6. On `daemon_error`: show error in placeholder, exit.

Mid-task behaviour (stdin-open injection, current as of Apr 18 2026):
when a new message arrives while a prior daemon task is still running for
the same workstation channel, the new user frame is written to the SAME live claude
subprocess's stdin (which is kept open across queries). Claude picks it up
at the next input checkpoint between tool rounds. Prior query keeps
running; tools are preserved. Daemon-side FIFO correlation
(`_pending_qids` deque + per-qid `asyncio.Event`) routes stream/result
entries to the correct qid.

CLI merge semantics (verified empirically Apr 18 2026):

- Case A (single-turn text-only parent turn): CLI emits a SECOND `result`
  event for the injected frame. Bot delivers 2 separate replies.
- Case B (multi-turn tool-using parent turn, the COMMON case): CLI
  absorbs the injected frame into the current AgentLoop between tool
  rounds and emits ONE `result` with `turns=N+1`. FIX SHIPPED: daemon
  tracks `_user_text_in_window` and on `result`, drains extra qids with
  synthetic `result` of `subtype="merged_into_prior_qid"`; bot deletes
  the merged placeholder. Case A's 2-reply pattern is cosmetic (deferred).

## guard.sh commands (Mac-side whitelist)

Authenticated (require valid session):

- `relay-list-jsonls [project]` — list JSONL sessions under ~/.claude/projects/.
- `relay-read-jsonl-header <path>` — read first 16KB of JSONL (cwd extraction).
- `relay-sdk-persistent <chat_id> <uuid> <cwd_b64>` — spawn/attach the
  persistent daemon for this chat, forward stdio. (Live transport.)
- `relay-ping-daemon <chat_id>` / `relay-list-daemons` / `relay-kill-daemon <chat_id>` /
  `relay-reap-orphans` — persistent-daemon management.
- `relay-push-file <sha256> <ext>` — bot-to-Mac inbound media upload (50 MB cap).
- `relay-fetch PATH` — fetch binary file as base64 (50 MB cap, path-validated).
- `relay-status` — session info + tmux sessions.
- `relay-claude-login` — spawn `claude setup-token` helper (v7 OAuth reauth).
- `relay-claude-login-paste` — deliver OAuth code (stdin, strict regex, 0600 file).
- `relay-claude-login-status` — read helper state file (URL + terminal state).
- `relay-claude-login-kill` — terminate in-flight helper.
- `relay-lock` — immediate session revocation.

No auth required:
- `relay-ping` — health check, returns `PONG:timestamp`.

Removed in Session C + Claude-relay collapse: `tail`, `tail -f`, `wc`,
`fswatch --event Created`, `relay-send`, `relay-sdk-query` (ephemeral path).

Blocked: everything else — logged to audit file.

## Updating guard.sh on Mac

`guard.sh` lives at `~/.relay/guard.sh` on the Mac. The repo copy is at
`scripts/desktop-relay/guard.sh`. Deploying the bot only updates the repo
copy — the Mac copy must be refreshed separately (scp from the Mac). After
Session C, any legacy commands still whitelisted on the Mac are harmless
because the bot no longer issues them.

## Employee Mac health watchdog

The scheduler pings the employee's Mac every ~5 min via `relay-ping`. On offline >5 min,
an alert goes to each workstation channel. On reconnect, a single online notification
is sent. No catch-up replay — the daemon is reconnected on next query;
interim Mac-side turns are replayed via the resumed JSONL.

## Key files

- `bot/desktop_relay.py` — SSH transport, registry, workstation watchdog,
  `list_project_jsonls`, `update_pinned_jsonl`.
- `bot/relay/pool.py` — `RelayClientPool` + `RelayPersistentClient`
  (state machine, reader loop, query correlation per workstation channel).
- `bot/mcp_tools.py` — `desktop_relay` MCP tool dispatch.
- `main.py` — workstation channel dispatch (`_handle_relay_message`), streaming placeholder
  UX, mid-task queue (`_pending_relay_queue`), `/session` picker, RELAY_FILE
  tag detection.
- `scripts/desktop-relay/guard.sh` — Employee Mac-side SSH command gatekeeper (repo copy).
- `scripts/desktop-relay/auth.sh` — Mac-side code verification.
- `scripts/desktop-relay/sdk_persistent_daemon.py` — Employee Mac-side long-lived
  daemon host (one per workstation channel).
- `data/config/relay_sessions.json` — session registry (workstation channel chat_id to session_name,
  project_path, pinned_jsonl_path, sdk_relay flag, last_synced_offset).

## Config

- `DESKTOP_ENABLED=true` — feature flag.
- `DESKTOP_HOST=employee-mac` — Tailscale MagicDNS hostname for employee workstation.
- `DESKTOP_USER=employee-user` — Employee Mac username.
- `DESKTOP_SESSION_TIMEOUT=7200` — session duration (seconds).

## Troubleshooting

- Employee Mac offline: check Tailscale on employee workstation. `status` action for diagnostics.
- Auth expired: new code via `authenticate` action.
- Daemon error: placeholder shows a daemon error message. Common
  causes: stale auth, `claude` binary not in Mac PATH (guard.sh enumerates
  `~/.nvm/versions/node/*/bin`), corrupted JSONL UUID.
- No UUID in jsonl path: relay entry missing `pinned_jsonl_path` — re-link
  via `create_relay_group` or use `/session pin <uuid>`.
- Rate limited on Mac: daemon `result` entry carries `rate_limit_event` —
  bot notifies TG and waits for reset.
- File delivery fails: check guard.sh `relay-fetch` whitelist. Files must
  be under 50 MB.
