# Component: Harness System

**Purpose**: Named per-chat configuration profiles that control model, tools,
budget, project directory, domain access, RBAC role, and SDK settings for each chat independently.

**Key files**: `bot/harness.py`, `bot/hooks.py` (Layer 1b2), `bot/agent.py`

---

## Design Principles

- **Per-chat config profiles** — harnesses are named definitions assigned to chats
- **Two JCS-backed storage files**: `DATA_DIR/config/harnesses.json` (definitions) + `DATA_DIR/config/chat_harnesses.json` (per-chat assignments + overrides). Thread-safe, atomic writes, mtime auto-reload via `JsonConfigStore`
- **SDK settings.json controls tool VISIBILITY** (Layer 1a — before hooks run; deny=[] in settings.json removes the tool from the model's available list)
- **hooks.py Layer 1b2 controls tool ACCESS** (after SDK; prefix whitelist via `allowed_tools`)
- **Session reset** triggered automatically on harness change

## Harness Fields

| Field | Purpose | Notes |
|-------|---------|-------|
| model | Claude model | claude-sonnet-4-6, claude-opus-4-6, [1m] suffix = 1M context |
| effort | Thinking depth | low/medium/high/max (max = Opus only) |
| max_turns | Per-query tool-call limit | 0 = use default (75) |
| max_budget_usd | Session USD cap | 0 = use default |
| output_token_cap | Per-query output cap | 0 = use default |
| domains | Active knowledge domains | list of domain IDs (core always implicit) |
| write_domain | R/W domain for fact routing | single domain ID |
| default_role | RBAC role auto-assigned on chat | e.g. "teamwork", "employee" |
| prompt_modules | Domain prompt module filter | list of module names (domain:component notation) |
| claude_md | Custom CLAUDE.md path | overrides /host-repo/CLAUDE.md |
| claude_md_modules | Auto-loaded skills | list of skill names (loaded from `.claude/skills/<name>/SKILL.md`) |
| custom_instructions | Extra system prompt text | appended after main prompt |
| facts_visible | Show facts in prompt | bool |
| facts_categories | Filter fact categories | list or null (all) |
| settings_json | SDK settings overlay | merged with project settings.json |
| allowed_tools | Tool whitelist | list of prefixes, null = all |
| allowed_credentials | Credential allowlist | ["*"]=unrestricted (admin only), []=deny-all, ["x@y"]=only listed. Layer 1b3 |
| project_dir | Sandbox directory | enables coding sandbox mode |
| query_timeout_s | Zombie detection timeout | 0 = disabled (use for PM/long coding tasks) |
| fast_mode | Opus fast mode | 2.5x speed, 6x cost, Opus 4.6 only |
| pm_project_id | PM project link | int, links harness to PM project |

## Canonical Harnesses (6)

| Label | Default Role | Notes |
|-------|-------------|-------|
| admin | bot_admin | Full access, all tools, Opus/1M |
| coding | bot_admin | Coding sessions with project_dir, Opus/1M |
| teamwork | teamwork | Team group chats, R/W teamwork domain |
| project-coding | teamwork | Team coding sessions, Opus/1M |
| personal | employee | Employee DMs, sandbox |
| external_chat | external | Third-party chats, restricted tools |

## Anti-Patterns

- **NEVER** store harness-specific runtime info in bot facts — harness is session config, not data
- **NEVER** bypass the harness tool whitelist by modifying hooks.py without a security audit
- **NEVER** confuse settings.json denies (SDK/visibility layer) with allowed_tools (hooks/access layer)
- **NEVER** add project_dir without testing bubblewrap sandbox (see hooks.md Layer 3b)

## Pitfalls

- **Per-volume storage** — harnesses.json lives in the Docker volume, not git. Staging has DIFFERENT
  harnesses than prod. Use `harness_config` parameter in `/test/inject` to auto-create on staging.
- **Relay chats have NO bot-local harness** — relay chats bypass the bot SDK entirely; they use Mac-side
  Claude config. Never show harness tags in `/relays` display.
- **Per-chat overrides persist separately** — stored in `chat_harnesses.json`; survive harness redefinition.
- **query_timeout_s=0** is correct for PM/coding tasks (default 900s kills long-running tool calls).
- **fast_mode** silently ignored for non-Opus models — no error, no warning.
- **allowed_tools prefix matching** — `mcp__google-workspace__` blocks all 20+ GW tools.
- **RBAC execution scope determines which bash blocklist applies** — determined by the RBAC role assigned to the chat, not a harness field.

## Session Reset Trigger

`/set-harness <label>` triggers `reset_session(chat_id)`:
1. Disconnect current SDK client
2. Clear session_id from session_map (SQLite)
3. Next query spawns fresh SDK client with new harness config
