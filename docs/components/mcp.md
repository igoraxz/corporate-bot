# Component: MCP Tool Servers

**Purpose**: Custom MCP servers expose bot capabilities as tools to the Claude SDK.
Per-session isolation prevents cross-chat state contamination.

**Key files**: `bot/mcp/` (7 modules), `bot/mcp_tools.py` (thin shim re-exporter),
`bot/mcp_config.py`, `bot/mcp_size_guard.py`

---

## Design Principles

- **Per-session isolation for stateful servers** — `create_messaging_server(session_key)` and
  `create_memory_server(session_key)` are factories; each session gets its own wrapper
- **Shared services server** — deploy, image, flight, model/effort tools are stateless and shared
- **Size guard wraps all browser MCPs** — 500KB truncation limit + image-to-file conversion
- **Tool registry** — `@tool` decorator + typed args; session-aware tools inject `_session_key` into args and pop it in the handler
- **External servers** — Playwright (SSE→stdio bridge), Camoufox (stdio), Google Workspace (stdio),
  MS365 (stdio via npx), Kiwi Flights (remote HTTP)

## Server Map

| Server | Factory | Session-aware | Key tools |
|--------|---------|--------------|-----------|
| messaging | `create_messaging_server(session_key)` | YES | telegram_send_*, teams_send_* |
| memory | `create_memory_server(session_key)` | YES | memory_search, manage_fact, search_facts |
| services | shared singleton | NO | deploy_bot, phone_call, generate_image, google_flights_search |
| pm | `create_pm_server(session_key)` | YES + harness-gated | pm_run_ticket, pm_board, ... |
| Playwright | SSE→stdio bridge | tab-per-session | browser_navigate, browser_snapshot, browser_click |
| Camoufox | stdio wrap | shared tab model | camofox_navigate, camofox_snapshot |
| Google Workspace | stdio | per-user_google_email | get_events, search_gmail_messages |
| MS365/Softeria | stdio (npx auto-install) | shared | get-calendar-view, list-mail-messages |
| Kiwi Flights | remote HTTP | stateless | search-flight |

## Size Guard (`mcp_size_guard.py`)

- **Text truncation** at 500KB (`MCP_MAX_RESPONSE_BYTES`) — prevents SDK 1MB JSON buffer crash
- **Image-to-file** — ALL base64 images extracted, saved to `data/tmp/<prefix>-<ts>.png`, replaced with path reference
- **Request injection** — auto-injects viewport 1920x1080 + light colorScheme into Camoufox `create_tab`
- **Two modes**: `wrap` for stdio servers (Camoufox), `sse` for SSE→stdio bridging (Playwright)
- **Chunked pipe reads** — 64KB reads prevent stdio deadlocks on large responses

## Anti-Patterns

- **NEVER** share messaging/memory MCP servers between sessions — reply threading state is per-session
- **NEVER** read files >500KB via browser MCP — SDK crashes with 1MB JSON buffer overflow
- **NEVER** bypass the size guard when adding new browser MCP integrations
- **NEVER** use process-global state for session identity in MCP tool handlers — use explicit `_session_key` popped from tool args (AP-15)
- **NEVER** use MS365 `--preset` flag — it overwrites `--enabled-tools` in Softeria's cli.js (preset pattern runs after Commander parse, silently discarding the tools restriction)

## Pitfalls

- **bot/mcp_tools.py is a thin shim** — actual implementations live in `bot/mcp/` package
  (7 modules: `__init__.py`, `telegram.py`, `teams.py`, `memory_tools.py`, `services.py`,
  `deploy.py`, `relay.py`). Always edit the package module, not the shim.
- **Playwright = SSE→stdio bridge** — size_guard runs in `sse` mode; Playwright server at port 3001.
  Multiple SDK sessions get separate tabs in the SAME shared browser instance.
- **Camoufox = stdio wrap** — size_guard runs in `wrap` mode; separate Docker service at port 9377.
  Profile isolation: admin chats use `/app/data/camofox-profiles/team/`, non-admin chats use
  `/app/data/camofox-profiles/{chat_id}/` (path-traversal sanitized).
- **Google Workspace multi-account** — route via `user_google_email` parameter;
  credentials at `/app/data/google-workspace-creds/<email>.json`.
- **PM tools harness-gated** — `_EXPLICIT_HARNESS_TOOLS` in hooks.py; non-admin coding chats
  need `allowed_tools` list containing the PM MCP prefix in their harness.
- **MS365 MSAL cache format** — must be Softeria envelope: `{_cacheEnvelope: true, data: "<MSAL JSON>", savedAt: <ms>}`.
  `selected_account.json` must use `{accountId: "..."}` (not `homeAccountId`). Wrong format = silent auth failure.
- **Tool count in context** — each registered tool adds ~200-500 tokens to context window.
  Avoid registering tools that are never needed for the session type.
