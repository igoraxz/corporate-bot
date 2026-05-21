---
description: "Bot system architecture: module map, data flow, Docker volumes, key patterns. Auto-loaded for admin/coding harnesses."
---

## Architecture Overview

```
${COMPOSE_PROJECT_NAME}/  # e.g. corporate-bot, my-org-bot
  main.py           — FastAPI app + lifespan + webhook routes + re-exports (1229 lines, decomposed Apr 2026)
  config.py         — Loads org_config.json + env vars, builds runtime config + re-exports from config_overrides
  config_overrides.py — Model/OAuth/effort override persistence (backed by JsonConfigStore)
  Dockerfile        — Python 3.12 + Node.js 22 + Playwright Chromium
  docker-compose.yml — bot-core + camofox-browser services, volumes, networks
  entrypoint.sh     — Volume permission fixes, SSH/git setup, skills symlink, drops to botuser
  bot/
    agent.py        — ClientPool: one ClaudeSDKClient per chat, persistent sessions (no timeout),
                      compaction notifications, session metadata injection + re-exports from bot.core.*
    hooks.py        — PreToolUse/PostToolUse/PreCompact hooks: flat RBAC access control
                      (per-chat role enforcement), file/bash blocklists,
                      deploy gating, harness allowed_tools whitelist (Layer 1b2),
                      coding sandbox containment (Layer 2/3)
                      + re-exports from bot.core.session_state + bot.tool_labels
    harness.py      — Per-chat config profiles. CRUD, per-chat overrides, project dirs,
                      copy_from, SDK isolation via settings.json, thread-safe lazy loading
    chat_registry.py — Unified chat registry (JCS-backed). Modes: active_plus/active/silent.
                      Auth check, boot migration from legacy, forum topic support
    task_queue.py   — PersistentQueue: SQLite-backed task queue, crash-safe, FIFO eviction,
                      catch-up debounce for active chats, deploy loop prevention
    mcp_tools.py    — Thin shim: re-exports from bot.mcp package
    desktop_relay.py — Thin shim: re-exports from bot.relay (employee workstation control) package
    handlers.py     — handle_telegram_message, handle_teams_message (extracted from main.py)
    dispatch.py     — triage_and_enqueue, _dispatch_task, DC throttling (extracted from main.py)
    commands.py     — 26 lightweight slash command handlers, 30+ regex patterns (extracted from main.py)
    streaming.py    — TG placeholder/ticker, Teams status updates (extracted from main.py)
    staging.py      — Staging test endpoints, response buffering, client cleanup (extracted from main.py)
    relay_ux.py     — Relay chat UX, catchup rendering, relay message handler (extracted from main.py)
    task_manager.py — ActiveTask dataclass, TaskManager class (extracted from main.py)
    config_store.py — JsonConfigStore: unified JSON config file class with atomic writes,
                      threading.Lock, mtime-based external edit detection. Used by config_overrides
                      (model/OAuth overrides) and hooks (per-harness tool gating)
    tool_labels.py  — Security constants: SEND_TOOLS, TOOL_LABELS, _ADMIN_ONLY_TOOLS,
                      blocklists (_BASH_BLOCKLIST, _ADMIN_BASH_BLOCKLIST, _CODING_SANDBOX_BASH_BLOCKLIST,
                      _BLOCKED_PATH_PATTERNS, _ADMIN_BLOCKED_PATH_PATTERNS) (extracted from hooks.py)
    facts.py        — Fact CRUD, search, prompt injection (extracted from memory.py)
    media_cache.py  — Media caching, search, descriptions (extracted from memory.py)
    core/           — Shared core modules (extracted from agent.py, hooks.py, memory.py)
      access.py     — AccessContext dataclass, build_access_context(), SQL filters
      session_state.py — Per-session state management, 48+ getter/setter functions
      language.py   — Language detection
      safety.py     — Suspicious response detection
      fact_persistence.py — Facts regex extraction and persistence
    mcp/            — MCP tool package (extracted from mcp_tools.py, 84 tools across 9 modules)
      __init__.py   — Shared helpers, server factories, tool registry, integer schema coercion
      telegram.py   — 24 TG MCP tools
      teams.py      — 1 Teams MCP tool
      memory_tools.py — 10 memory/facts/RAG tools
      services.py   — 18 service tools (phone, image, flights, model/effort, scheduling)
      deploy.py     — 6 deploy tools + QA runner with session cleanup
      relay.py      — desktop_relay MCP tool
    pm/             — PM Agent Bus: structured task execution (v5 in-session)
      __init__.py   — Package init + create_pm_server() factory (uses shared coercion from bot.mcp)
      db.py         — PM DB singleton (own aiosqlite, pm.db, WAL + FK enforcement)
      schema.py     — DDL v3, status constants, transition rules, init_pm_db(), deploy tables
      store.py      — Async CRUD: projects, sprints, tickets, transitions, agent_runs, deploys, rolling sprints
      tools.py      — 20 MCP tools (harness-gated): data model (11) + orchestration (5) + convenience (2) + retro/summary (2)
      budget.py     — Budget tracking: atomic roll-up ticket→sprint→project, budget checks, JOIN summary
      retro.py      — Sprint retro (cost/velocity/findings analysis) + sprint summary (demo/reporting)
      roles.py      — Agent role definitions: PM, QA (+ legacy Engineer, Verifier, Reviewer)
      tcd.py        — Ticket Context Document: checkpoint + file content capture
      orchestrator.py — TCD helpers, ad-hoc agent dispatch (legacy launch_pm_session removed in v5)
    relay/          — Employee workstation control package (extracted from desktop_relay.py)
      ssh.py        — SSH transport: execute(), fetch_file(), push_file()
      registry.py   — JSON-backed session registry, JSONL management
      oauth.py      — Mac OAuth token management
      sessions.py   — Daemon/tmux session management
      watchdog.py   — Mac health monitoring, daemon respawn
      core.py       — High-level API: ping(), status(), authenticate()
    mcp_config.py   — External MCP server configs: Playwright (SSE→stdio bridge), Camoufox (stdio wrap), Google Workspace (stdio), MS365/Softeria (stdio, npx), Kiwi Flights (remote HTTP)
    mcp_size_guard.py — Response size guard proxy: truncates MCP responses >500KB to prevent SDK 1MB JSON buffer crash. Two modes: stdio wrap + SSE→stdio bridge
    media.py        — Background media description orchestration (Gemini Flash, semaphore-limited)
    db.py           — DB singleton: shared aiosqlite connection with WAL mode, busy_timeout, lazy-init lock
    memory.py       — SQLite storage: messages, DB init, usage tracking + re-exports from facts.py, media_cache.py
    rag.py          — RAG v4: per-chat isolated chunks (chat_id column, grouped by chat before chunking),
                      size-aware (char budget 2000, long msg splitting, 2-msg overlap),
                      Gemini embeddings (3072-dim), paginated cosine sim search (heap merge, AccessContext SQL filter),
                      atomic backfill (swap table pattern)
    prompts.py      — System prompt builder (loads data/prompts/*.txt, substitutes dynamic context)
    scheduler.py    — Flexible scheduled tasks (JSON-backed, MCP-managed, chat_id-scoped AccessContext)
  integrations/
    telegram.py   — Telegram MTProto userbot (Pyrogram/pyrotgfork): sending, client management
                      + re-exports from tg_parser.py, tg_media.py
    tg_parser.py  — _parse_pyrogram_message (extracted from telegram.py)
    tg_media.py   — download_media, _safe_filename (extracted from telegram.py)
    gemini.py     — Gemini API: image generation, media descriptions (Flash)
    phone.py      — Vapi phone calls: initiate calls, retrieve transcripts
  .claude/
    skills/       — Claude Code skills (17 SKILL.md files, loaded on-demand by SDK)
    agents/       — Named subagent definitions (6: code-researcher, product-researcher, architect, security-reviewer, quality-reviewer, qa-tester)
    rules/        — Path-scoped rules (3: python-bot, prompts, skills)
    settings.json — Project-level SDK settings (currently empty)
  data/prompts/     — System prompt fragments (mounted as volume, editable without rebuild)
  scripts/desktop-relay/ — Mac-side scripts for employee workstation control (guard.sh, auth.sh, setup_totp.py)
  docs/DESKTOP_RELAY_SETUP.md — Full setup guide for employee workstation control
```

## Multi-Instance Design

All instance-specific data lives outside the code:

| Data | Location | How it's loaded |
|------|----------|-----------------|
| Organization identity, members, goals | `data/org_config.json` | `config.py` → `ORG_CONTEXT`, `ADMIN_NAMES`, etc. |
| Secrets (API keys, tokens) | `.env` | Environment variables |
| User IDs, chat IDs | `.env` | `ADMIN_USERS`, `TG_CHAT_ID` (notification only) |
| Prompts (always-on) | `data/prompts/*.txt` | Loaded by `prompts.py`, placeholders filled from config. |
| Skills (on-demand) | `.claude/skills/*/SKILL.md` | 17 skills discovered by SDK at startup, loaded via Skill tool when triggered by description match |
| Agents (subagents) | `.claude/agents/*.md` | 6 named agents: code-researcher, product-researcher, architect, security-reviewer, quality-reviewer, qa-tester. PM pipeline uses in-session execution (v5). |
| Rules (path-scoped) | `.claude/rules/*.md` | 3 path-scoped rule files: auto-loaded when SDK reads matching files (Python, prompts, skills) |
| Facts | SQLite `message_facts` table | Graph triples (subject/key/value), versioned, free-form categories, doc_pointer, RAG chunk linkage, auto-injected into system prompt |
| Media cache | `media-cache` Docker volume | Permanent (99yr), TG re-fetch via tg_file_id, graceful degradation |
| Google credentials | `data/google-workspace-creds/` | Volume-mounted |
