---
description: "Bot design patterns: JsonConfigStore, session isolation, OAuth profiles, harness system, message flow, burst coalescing."
---

## Key Patterns

### JsonConfigStore — Unified JSON Config Management
`bot/config_store.py` provides a single class for all small JSON config files (<1000 entries).
Replaces duplicated load/save/cache/lock boilerplate. Features:
- **Atomic writes**: tmp file + rename (no partial writes on crash)
- **Thread safety**: `threading.Lock` guards all mutations
- **Mtime-based reload**: detects external edits (throttled to once per second)
- **Lazy init**: optional `lazy=True` defers disk I/O until first access
- **Key type coercion**: `key_type=int` for stores with integer keys (e.g. TG chat IDs)

Usage:
```python
from bot.config_store import JsonConfigStore
store = JsonConfigStore("my_config", DATA_DIR / "my_config.json")
store.get("key", default="fallback")  # read (lock-free)
store.set("key", "value")             # write + atomic save
store.delete("key")                   # remove + save
store.update({"a": 1, "b": 2})       # batch set + single save
store.replace({"a": 1})              # atomic replace all entries
store.all()                           # snapshot dict
"key" in store                        # membership check
```

Used by all bot config stores:
- `config_overrides.py` — model_overrides.json, oauth_overrides.json
- `bot/hooks.py` — external_tool_overrides.json
- `config.py` — unified chat registry (via bot/chat_registry.py)
- `bot/scheduler.py` — scheduled_tasks.json (migrated from array to dict-keyed format)
- `bot/harness.py` — harnesses.json (definitions), chat_harnesses.json (assignments)
- Only relay registry retains its existing pattern (nested session state).

### Docker Volume Mounts
- `bot-data:/app/data` — persistent bot data (SQLite DB, state files, knowledge, facts)
- `media-cache:/app/data/media_cache` — cached media files (permanent)
- `claude-sessions:/home/botuser/.claude` — Agent SDK session persistence
- `.:/host-repo` — git repo mount for self-upgrade

**Host file mounts** (bind mounts from repo to container):
- `./data/prompts:/app/data/prompts:ro` — prompt fragments (editable without rebuild)
- `./data/org_config.json:/app/data/org_config.json:ro` — organization identity
- `./data/google-workspace-creds:/app/data/google-workspace-creds:rw` — Google OAuth

**IMPORTANT — Docker mount gotcha**: When docker-compose specifies a file bind mount (`./data/file.json:/app/data/file.json`) and the host path doesn't exist, Docker creates it as a **directory**, not a file. This causes OCI runtime errors on restart. Always ensure host-mounted files exist as actual files before deploying. All mutable bot data (facts, knowledge, goals) is stored in the `bot-data` named volume via SQLite — no host file mounts needed for those.

### Telegram (Pyrogram MTProto Userbot)
- Uses `pyrotgfork` (actively maintained Pyrogram fork) — NOT Bot API
- Pyrogram handles both sending AND receiving via MTProto (no webhooks)
- FastAPI still serves `/health`, scheduled tasks, and other HTTP APIs
- Session file (`DATA_DIR/tg_session/bot.session`) contains MTProto auth keys — treat as secret
- User accounts CANNOT create inline keyboards — buttons rendered as numbered text options
- First-time setup: `docker exec -it corporate-bot python scripts/setup_tg_userbot.py`
- Config: `TG_API_ID`, `TG_API_HASH` (from https://my.telegram.org/apps), `TG_SESSION_DIR`, `TG_SESSION_NAME`
- Graceful degradation: if no session file exists, bot starts without TG (health endpoint still works)

### Agent SDK Usage
- One `ClaudeSDKClient` per chat session (NOT shared — no session isolation in SDK)
- `permission_mode="bypassPermissions"` (do NOT pass `allow_dangerously_skip_permissions=True` — it crashes the SDK)
- `cwd="/app"` + `setting_sources=["project"]` — SDK discovers skills from `/app/.claude/skills/`
- Hooks via `HookMatcher` — `PreToolUse` for security, `PostToolUse` for status, `PreCompact` for compaction tracking. `build_hooks(session_key)` creates per-client wrapper closures that eagerly register `session_id → session_key` for race-safe hook resolution
- `thinking={"type": "adaptive"}` always on
- `max_turns=75` per query (with "continue" mechanism for tasks exceeding the limit)
- Custom MCP tools via `create_sdk_mcp_server()` + `@tool` decorator
- Session metadata (source, chat_id, is_admin) injected into system prompt — survives context compaction
- Compaction notifications sent to chat when SDK compacts context (with token metrics)
- **SDK resilience** (`agent.py`):
  - `_connect_with_retry()`: 2 retries with 3s/6s exponential backoff on connect failures. Config: `SDK_CONNECT_RETRIES`, `SDK_CONNECT_BACKOFF_BASE`
  - Session recovery: falls back to fresh session on corrupt state
  - Rate limit handling: preserves session (disconnects CLI subprocess only, keeps session_id for resume). Does NOT reset session on rate limit
  - `_build_error_detail()`: admin-aware structured error messages with diagnostics (stack traces for admins, user-friendly messages for non-admins)
  - SDK init timeout: 3 minutes (`SDK_INIT_TIMEOUT`), with user notification on fatal errors
  - Compaction status shown in TG streaming placeholder via `pending_compaction` flag
  - Lightweight `status` command bypasses SDK entirely for instant health check
- **Cost optimization & model routing** (`agent.py`, `config.py`):
  - Default model: Sonnet (`claude-sonnet-4-6`), ~5x cheaper than Opus
  - Session-level USD budget via SDK `max_budget_usd` ($5 sonnet, $50 opus) — cumulative per session. On cap hit: auto-reset session + notify user to retry
  - `extend_budget(delta_usd)` MCP tool (admin + keyword-gated by `extend budget` / `продли бюджет`) raises the cap mid-conversation WITHOUT losing context. Stores absolute new cap in `_session_budget_overrides[session_key]`, schedules a DEFERRED disconnect via `_pending_budget_disconnects` (NOT `_pending_session_resets` — that clears session_id) so the tool's own reply reaches the user before teardown. Next query spawns fresh CLI with `--resume UUID` + new cap → context preserved via JSONL replay. Hard caps: `MAX_EXTEND_DELTA_USD` (per-call, default $100) + `MAX_TOTAL_BUDGET_CAP` (per-session total, default $500). Single-shot per turn: hook consumes the keyword flag on first successful pass so the model cannot invoke the tool multiple times per user message. Config: `MAX_EXTEND_DELTA_USD`, `MAX_TOTAL_BUDGET_CAP` env vars.
  - Per-query output token cap (custom): 25k sonnet, 50k opus. Tracks `output_tokens` from `AssistantMessage.usage` intra-query, deduped by message_id. Calls `client.interrupt()` on overshoot + notifies user to continue
  - Config: `MAX_BUDGET_DEFAULT`, `MAX_BUDGET_HEAVY`, `QUERY_OUTPUT_CAP_DEFAULT`, `QUERY_OUTPUT_CAP_HEAVY`
  - `fallback_model="claude-sonnet-4-6"` on Opus sessions for graceful rate limit degradation
  - 4 model variants: `claude-sonnet-4-6`, `claude-opus-4-6`, `claude-sonnet-4-6[1m]`, `claude-opus-4-6[1m]`
  - Context window: 200k default (no suffix), 1M opt-in via `[1m]` suffix. Admin switches via `switch_model`
  - Per-chat model overrides persisted to `DATA_DIR/model_overrides.json` — survive container restarts. Loaded on module init, saved on every set/clear.
  - **Harness system** (`bot/harness.py`): named config profiles assigned per chat. Key fields: model, effort, max_turns, max_budget_usd, output_token_cap, prompt_modules, claude_md, claude_md_modules, custom_instructions, facts_visible, facts_categories, settings_json, allowed_tools, allowed_credentials, project_dir, query_timeout_s, fast_mode, domains, write_domain, default_role, pm_project_id. `prompt_modules` (replaces removed `prompt_includes/excludes`) filters domain-contributed system prompt modules. `domains` lists active knowledge domains; `write_domain` sets the R/W domain for fact routing; `default_role` auto-assigns RBAC role when harness is set on a chat; `pm_project_id` links to the PM project. Reference domains derived at runtime: `domains - write_domain - core`. `sandbox: true` activates bwrap kernel-level containment for all users in the chat (FB-48). `claude_md_modules` lists module/skill names to compose into CLAUDE.md (for project-dir chats) or inject inline into system prompt (for non-team chats in active mode). Skills loaded from `.claude/skills/<name>/SKILL.md` (with legacy fallback to `data/claude_md_modules/`). Shared loader: `load_module_content()` with allowlist regex validation and YAML frontmatter stripping. `query_timeout_s` controls zombie detection and checkpoint warnings (0=disabled for coding, default 900s). Relay chats use their own hardcoded timeout (Mac-side config). `fast_mode` enables SDK fast mode (Opus 4.6 only, 2.5x speed, 6x cost) — injected as `"fastMode": true` in per-harness settings.json; silently ignored for non-opus models. Storage: `DATA_DIR/harnesses.json` (definitions) + `DATA_DIR/chat_harnesses.json` (assignments with per-chat overrides). Per-chat overrides: `{"harness": "label", "overrides": {"field": "value"}}` — priority: chat override → harness → default. Per-harness project dirs at `DATA_DIR/harness-projects/<label>/` with `.claude/settings.json` (SDK permissions) and skills symlinked from `/app`. `create_harness(label, copy_from="source")` copies project dir contents. `allowed_tools` enforced in hooks.py Layer 1b2. MCP tool: `manage_harness` (admin-only). Slash commands: `/harnesses`, `/set-harness` (triggers session reset). **Auto-reset**: any harness update, per-chat override set/clear, harness assign/unassign/delete automatically schedules SDK session reset for affected chats — ensures changes take effect immediately without manual `/reset`. OAuth profiles remain independent. Canonical harnesses (7): admin, coding, teamwork, personal, external_chat, project-coding (+ __defaults__ internal, not in JCS).
  - Config: `MODEL_PRIMARY`, `MAX_BUDGET_DEFAULT`, `MAX_BUDGET_HEAVY`
- **Per-chat OAuth profiles** (`config.py`, `config_overrides.py`, `agent.py`, `mcp/services.py`):
  - Unified OAuth: same token works for bot SDK sessions AND Mac relay daemons
  - **All profiles are equal labeled files** at `OAUTH_PROFILES_DIR/<label>.json` with `accessToken` field. One is flagged as "default" — used by all sessions without a per-chat override.
  - **Default profile selection**: `get_default_oauth_label()` reads `DATA_DIR/oauth_default_label.json` (persisted via `set_default_oauth_label()`), falls back to `OAUTH_DEFAULT_LABEL` env var (default: `"default"`). Changeable at runtime via `manage_oauth(action="set-default", label="...")`.
  - **Token resolution** (`agent.py`): `get_oauth_token(label)` reads the profile file. If the profile file doesn't exist → falls back to `~/.claude/.credentials.json` (file-based, auto-refresh via SDK) → `CLAUDE_CODE_OAUTH_TOKEN` env var. All tokens injected via `CLAUDE_CODE_OAUTH_TOKEN` env var into SDK subprocess.
  - **`is_default_oauth(label)`**: Returns True for both `"default"` keyword and the actual default label — both are treated as the default profile. `switch_oauth` with `"default"` clears the per-chat override.
  - **Switching to a named profile**: `switch_oauth personal` (or `/claude-set-oauth personal` via lightweight command) writes a per-chat override to `oauth_overrides.json` and resets the session. Only works if the named profile JSON exists.
  - **`/claude-set-oauth` lightweight command** (no inference, pre-SDK): `/claude-set-oauth <label>` (this chat), `/claude-set-oauth <label> all` (all chats with overrides). Works even when rate-limited. Legacy `/switch-oauth` still accepted.
  - Per-chat overrides persisted to `DATA_DIR/oauth_overrides.json` — survive container restarts
  - Default label persisted to `DATA_DIR/oauth_default_label.json` — survives container restarts
  - **Boot cleanup**: On init, OAuth overrides pointing to the current default label are auto-removed (redundant)
  - MCP tools: `switch_oauth` (per-chat switch + session reset + relay daemon respawn), `manage_oauth` (list/add/remove/set-default profiles)
  - **OAuth switch → session reset rule**: `switch_oauth` auto-resets the current session. But `manage_oauth set-default` does NOT auto-reset any sessions — the OAuth token is injected at subprocess spawn time, so live sessions keep the old token until reset. After changing the default, ALWAYS suggest `/reset` for the current chat and mention that other active sessions without per-chat overrides also need reset to pick up the new default.
  - `usage_report(action="by_profile")` shows cost breakdown per OAuth profile
  - Relay integration: `switch_oauth` auto-updates relay `account_label` + kills daemon for respawn
  - Mac sync: `push_oauth_to_mac()` via `relay-push-token` SSH command writes `~/.relay/oauth_token.<label>`
  - Slash commands: `/chats` (unified view with OAuth per chat), `/usage [hours]` (per-profile cost report), `/claude-accounts` (list profiles with ⭐ default marker)
- **Rate limit monitoring** (`agent.py`):
  - Captures `RateLimitEvent` from SDK `receive_response()` stream
  - Fields: `utilization` (0.0-1.0), `status` (allowed/allowed_warning/rejected), `rate_limit_type` (five_hour/seven_day/seven_day_opus/seven_day_sonnet), `resets_at`
  - Alerts admin at 75% (warn) and 90% (alert) with 30min cooldown. Sent to originating chat, admin-only
  - State exposed via `get_rate_limit_state()` → `/health` endpoint + lightweight status
  - Config: `RATE_LIMIT_WARN_PCT`, `RATE_LIMIT_ALERT_PCT`, `RATE_LIMIT_ALERT_COOLDOWN_S`
- **Usage accounting** (`memory.py`, `agent.py`):
  - `usage_log` SQLite table: session_key, model, cost_usd, tokens, turns, duration per query
  - Written on every `ResultMessage` (awaited, not fire-and-forget)
  - Per-query anomaly logging: warns on cost > `QUERY_COST_WARN` ($2) or near max_turns
  - `usage_report` MCP tool (admin-only): summary, top queries, rate_limits actions
  - `get_usage_summary(hours)` with parameterized SQL, bounded 1h-365d

### Security Model — 4-Tier Access Control

**Tiers** (determined by RBAC role + harness `data_scope` + `project_dir`):

| Tier | Trigger | Bash | Write/Edit | Read/Grep/Glob | Path Scope |
|------|---------|------|-----------|----------------|------------|
| **bot_admin** | RBAC `bot_admin` role | ✅ | ✅ | ✅ | Full container |
| **employee** | RBAC `employee` role | ❌ | ❌ | ✅ (path-checked) | `_BLOCKED_PATH_PATTERNS` |

**Enforcement layers** (defense-in-depth):
1. **Container isolation**: Docker, non-root `botuser`, no secrets in image
2. **User auth**: Admin user IDs checked in hooks
3. **Layer 1b — Admin tool gating**: Bash/Write/Edit/deploy_bot/manage_harness require admin. **Sandbox bypass**: if `sandbox_dir` is set, `_SANDBOX_TOOLS` (Bash/Write/Edit/Read/Grep/Glob) bypass this gate for ALL users — containment enforced by Layer 2/3.
3b. **Layer 1b2 — Harness tool whitelist**: Per-harness `allowed_tools` prefix whitelist. Null = all tools.
4. **Layer 1c — Per-harness tool gating**: `allowed_tools` field in harness determines accessible tools. Non-admin chats without explicit grants are blocked from admin-only tools. **Sandbox exception**: chats with coding harness (`sandbox_dir` set) can use filesystem tools within sandbox.
5. **Layer 2 — File path protection**: Tier-specific blocklists. Sandbox uses `_check_sandbox_path()` with `os.path.realpath()` for traversal/symlink protection. Admin uses `_ADMIN_BLOCKED_PATH_PATTERNS`. Non-admin uses `_BLOCKED_PATH_PATTERNS`. `.env` is write-blocked for all users.
6. **Layer 3 — Bash command protection**: Tier-specific blocklists. Sandbox adds /proc, /sys, network exfiltration (wget/nc/ssh/curl POST), pip/npm install, symlink creation blocks. `_check_sandbox_bash()` has 4-phase check: blocklist → secret vars → path refs → media bash.
7. **Layer 3b — Bubblewrap kernel sandbox** (coding sessions only): After regex checks pass, Bash commands are wrapped in `bwrap` via `sandbox-exec.sh`. Provides kernel-level namespace isolation: `--clearenv` (no env vars/secrets), `--unshare-net` (no network), `--unshare-pid` (PID isolation), no /proc mounted (hides parent process info, env vars, PID enumeration), `--tmpfs /app` (hides all bot code/data), `--bind sandbox_dir` (only project dir writable). Requires custom seccomp profile (`seccomp-bwrap.json`) allowing user namespace creation (CLONE_NEWUSER|NEWPID|NEWNET|NEWNS) + mount/umount2/pivot_root syscalls, plus `apparmor=unconfined` — NO `cap_add: SYS_ADMIN` needed. Hard-denies Bash in sandbox mode if bwrap is unavailable — regex-only sandbox has known bypasses (script files, encoded commands, perl/ruby). Script: `scripts/sandbox-exec.sh` (referenced from `/host-repo/scripts/`, NOT copied into `/app/`).

**Coding sandbox model**: Designed for collaborative coding chats (multiple devs, same project). ALL users (admin + non-admin) get identical sandboxed permissions when `sandbox: true` is set in the harness config. Sandbox dir is set in `handlers.py:_set_sandbox_for_session()`. Two-layer defense: regex-based command checks (Layer 3) catch direct attempts, bubblewrap kernel sandbox (Layer 3b) prevents script-based escapes.

#### Host Watcher Isolation (IMPORTANT)
The deploy watcher (`host-watcher.sh`) should be installed OUTSIDE the repo at `/opt/corporate-bot-watcher/`.
The bot CANNOT modify it — file path blocklist blocks writes to watcher/service files,
bash blocklist blocks `systemctl`, `docker run`, and `git push --force`.
See `docs/HOST_SECURITY.md` for admin setup instructions.

#### Backup Script Isolation (IMPORTANT)
The hourly backup cron script should be installed OUTSIDE the repo at `/opt/corporate-bot-backup/backup.sh`.
Same isolation strategy as the deploy watcher: path blocklist blocks writes to any path containing
`corporate-bot-backup`, bash blocklist blocks shell commands referencing it, and `.backup-password` is
path-blocked so the bot cannot read or modify the encryption key.
See `docs/HOST_SECURITY.md` "Backup Script Isolation" section for one-time setup commands.

#### Employee Workstation guard.sh — hardened (FB-32, Apr 29 2026)
guard.sh accepts ONLY `relay-*` prefix commands with strict per-command validation.
Generic `tmux *` / `claude *` / `cat` / `ls` / `find` / `echo` catch-all REMOVED —
no `eval "$CMD"` anywhere. `relay-fetch` restricted to path allowlist (`~/dev/*`,
`~/.claude/*`, `~/.relay/inbox/*`, `~/.relay/outbox/*`, `/tmp/*`) with `realpath` symlink resolution.
`relay-install-daemon` logs SHA256. `relay-install-skills` validates tar entries.

### Message Flow
1. TG Pyrogram handler or Teams webhook receives message
2. `triage_and_enqueue()` validates sender, stores message, enqueues to `PersistentQueue` (SQLite-backed, crash-safe)
3. PersistentQueue dispatches tasks sequentially per chat (1 at a time per chat, MAX_TOTAL_TASKS global cap, FIFO eviction)
4. `process_incoming()` builds prompt (with session metadata), sets per-session reply-threading state (via `set_reply_threading()`), calls `client_pool.query()`
5. TG: streaming placeholder edits. Teams: proactive message replies
6. TG fallback salvage: if tools ran but no send_message called, `response_text` is delivered from the finally block
7. Post-processing: FACTS_UPDATE extraction from ALL text blocks (intermediate + final), memory storage, media caching

**TG streaming placeholder invariant — no content leak** (fix 4fd6a4b, Apr 19 2026):
The `on_stream_chunk` lost-placeholder branch (stream arrived before the deferred "🧠 Thinking..." placeholder fired or after a permanent edit error killed the original) creates the recreate with the LITERAL string `"🧠 Thinking..."` — NEVER `display_text`. The stream content is then applied via `_tg_safe_edit` on the SAME msg_id. This guarantees that finally's delete-on-`reply_sent` cleanly removes the placeholder when the model's `telegram_send_message` tool call lands the final content — no double-reply, no ghost message. The deferred placeholder task (`_ph_deferred`) is eagerly cancelled via `_cancel_if_pending()` from 3 sites: on_tool_status (SEND_TOOL fires), on_stream_chunk (lost-placeholder recreate), and finally (cleanup). Relay chats already follow this pattern — see `_handle_relay_message`.

### Reply Threading
- TG: first reply uses `reply_to_message_id` to thread to user's message; subsequent replies interleave naturally
- Per-session state dict in `hooks.py` (`_session_state` keyed by `session_key`): stores `reply_to_msg_id`, `task_last_sent_id`, `current_task_id`, `origin_chat_id`, WA equivalents. Hooks resolve state via `session_id → _sid_to_skey → session_key`. No process-global fallback (AP-15).
- **Per-client hook closures**: `build_hooks(session_key)` creates wrapper closures that eagerly register `session_id → session_key` in `_sid_to_skey` before the real hook fires. This ensures hooks always resolve to the correct session state, even when SDK fires hooks before yielding SystemMessage. `_get_state_for_hook()` returns safe detached defaults if mapping is missing.
- **Per-client MCP session isolation**: `create_messaging_server(session_key)`, `create_services_server(session_key)`, `create_memory_server(session_key)` are factories that wrap tools to inject `_session_key` into tool args. Each tool handler pops `_session_key` and passes it to state helpers via explicit `_get_or_create_state(session_key)`. No process-global state for session identity (AP-15).
- Interleave detection via `mark_chat_activity()` / `get_chat_last_task()`

### Sequential Processing
- Messages in each chat are processed sequentially through one persistent main session
- Multiple chats run in parallel, each with their own session
- Task queue: SQLite-backed PersistentQueue, crash-safe, FIFO

### Burst-Message Coalescing (v5, April 2026)
Rapid-fire bursts (forwarded multi-messages, typed-fast replies) are coalesced
into a single task instead of N sequential tasks. Triage decision order:

1. **Merge-into-pending wins over inject**: if a pending PQ row exists for
   this chat, append text to `parsed_json.text` and media raw to
   `parsed_json.merged_media` (list). Atomic SELECT+UPDATE with
   `status='pending'` guard. Soft caps: 50KB text, 10 media items.
2. **Mid-task inject**: text-only + SDK currently processing → stdin-write
   into the live subprocess.
3. **Enqueue fresh row** otherwise.

**Dispatch debounce** (`bot/pipeline/task_queue.py::_claim_next`): row claim is
deferred if `updated_at` is within `PQ_DISPATCH_DEBOUNCE_MS` (default 150ms).
Every merge bumps `updated_at`, extending the window. Once chat goes quiet
for the window, the row dispatches. Set `PQ_DISPATCH_DEBOUNCE_MS=0` to
disable (pre-v5 behavior).

**Dispatcher integration** (`handle_telegram_message`):
- Iterates `parsed["merged_media"]`: downloads + voice-transcribes or
  AI-describes each item, appends `[Merged message #N — PHOTO attached:
  /path]` blocks to the prompt text.
- `user_msg_id` (used for placeholder + final reply threading) =
  `merged_message_ids[-1]` if present, else primary `message_id`. UX
  invariant: each reply threads to the LATEST user message it included.

**Relay chats** (`desktop_relay`): eager `_relay_tasks[chat_id]` registration
at spawn site closes the ~50ms race where burst msg2 could miss msg1's
live task and spawn a non-injected sibling. Concurrent stdin-inject path
then handles all burst members reentrantly (fact `relay_cli_stdin_reentrant`).

**Tests**: `tests/test_pq_merge.py` covers 9 scenarios (empty pending, text
merge, media merge, text+media, soft caps, SELECT+UPDATE race, catchup skip,
msg_id dedup).

### Stale Message Handling
Messages arriving during bot downtime (restart/deploy) are detected as "stale" (timestamp < bot startup time).
- **Storage**: All stale messages stored in SQLite for context (both TG and WA)
- **Team messages**: Stored only — context is loaded via SESSION RESET mechanism (`get_recent_conversation`) when a fresh session starts
- **Chat catch-up**: Stale messages from active/active_plus chats enqueued to PersistentQueue with catch-up debounce
- **Deploy restart detection**: `_was_deploy_restart()` checks marker file to skip catch-up after intentional deploys

### Unified Chat Registry
All chats are managed via a single JCS-backed registry (`bot/chat_registry.py`).
Admin registers chats via `manage_chat` MCP tool. RBAC determines permissions.

**Chat modes** (comms style only — permissions are RBAC-managed):
- **`active_plus`** (default for registered): Full streaming UX — placeholders, thinking updates, ticker, parallel tasks.
- **`active`**: Bot participates, no streaming UI. For multi-party / 3P chats where streaming looks odd.
- **`silent`**: Listen only — messages stored for context, bot never responds.

**Forum topics** treated as separate chats: key format `telegram:<chat_id>:<thread_id>`.
Each topic can have its own harness, OAuth, and mode (falls back to parent group if unset).

**Key implementation:**
- `skip_streaming` derived from registry mode (`active_plus` = streaming, else no)
- Harness/OAuth/model lookups: topic key → parent group key → default (3-level fallback)
- Session key includes thread_id for per-topic SDK client isolation
- Boot migration populates registry from legacy `TG_ALLOWED_USERS` + `TG_EXTERNAL_CHATS`
- Webhook auth: `chat_registry.is_authorized()` checks registry + legacy fallback + admin

**Session isolation** (AP-15): `build_hooks(session_key)` creates per-client wrapper closures. All MCP servers use `_wrap_tools_with_session_key()`. No process-global state for session identity.

### Skills System
- 17 on-demand skills in `.claude/skills/*/SKILL.md` with YAML frontmatter descriptions
- SDK explicitly registers from `/app/.claude/skills/` (symlinked to `/host-repo/.claude/skills/`)
- Skills are loaded via the built-in `Skill` tool when the description matches user intent
- Skills awareness prompt (`data/prompts/19_skills_awareness.txt`) lists all skills in the always-on system prompt
- Skills: phone-calls, browser-automation, third-party-correspondence, email-operations, image-operations, file-sending, self-upgrade, coding-principles, travel-search, mcp-authentication, qa-testing, relay-chats, project-management, pptx-creation, docx-creation, diagram-creation, html-report
- Skills can be pre-loaded via harness `claude_md_modules` (composed into CLAUDE.md or injected inline for non-team chats). Chats in `active` mode auto-inject `third-party-correspondence` inline.
- Admin-only skills (self-upgrade, coding-principles) are protected by hook-level tool gating, not skill-level access control
- Skill files are writable by admin (via Bash/Edit tools) for self-upgrade; non-admin cannot modify them

### Agents (Named Subagents)
- 10 agent definitions in `.claude/agents/*.md` with YAML frontmatter (name, model, effort, tools, skills)
- Used by the coding-principles review pipeline, standalone product research, staging QA, and PM pipeline
- Coding agents: `code-researcher` (deep technical research), `product-researcher` (market/PMF/GTM/economics analysis), `architect` (plan design/review), `security-reviewer` (vulnerability audit), `quality-reviewer` (code quality + testing), `qa-tester` (staging test scenario generation)
- PM pipeline agents (LEGACY — v5 uses in-session execution): `pm-engineer`, `pm-verifier`, `pm-qa`, `pm-reviewer` kept for backward compat with `pm_dispatch_agent` direct calls. v5 pipeline uses security-reviewer + quality-reviewer from coding-principles instead.
- Coding agents use `model: opus`, `effort: high` for maximum quality
- Agent turns are independent from parent's turn limit (spawning = 1 parent turn)
- Agents share Anthropic rate limits and session USD budget with the parent

### Rules (Path-Scoped)
- 4 rule files in `.claude/rules/*.md` with `paths:` frontmatter for auto-activation
- Rules are auto-loaded by SDK when matching files are READ (not on Write/Edit — known SDK limitation)
- Rules: `python-bot.md` (bot/*.py, integrations/*.py), `prompts.md` (data/prompts/*.txt), `skills.md` (.claude/skills/*.md)
- Rules are additive to CLAUDE.md — they provide path-specific context, not replacements
