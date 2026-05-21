---
paths:
  - "bot/**/*.py"
  - "integrations/**/*.py"
  - "main.py"
  - "config.py"
---

# Python Bot Code Rules

- Python 3.12 typing: use `dict`, `list`, `str | None` — NOT `Dict`, `List`, `Optional`. Use `Callable` from typing (not `callable`)
- NEVER use literal newlines in f-strings — use `\n` escape sequences. SyntaxError = bot crash with no recovery
- Async patterns: use `asyncio.create_task()` for fire-and-forget. Use `asyncio.Lock` per session key
- Error handling: report errors to chat (never fail silently). Use `log.error(..., exc_info=True)`
- Edit in `/host-repo/`, NOT `/app/` (read-only image copy)
- Always verify syntax after editing: `python3 -c "import ast; ast.parse(open('/host-repo/<file>').read())"`
- No instance-specific data (names, IDs, schedules) in source code — use config files
- Per-session state: use `_session_state` dict keyed by `session_key`, NOT ContextVars
- Hook closures: `build_hooks(session_key)` creates per-client wrappers — never use process-global state for session identity (AP-15)
- MCP tools: `@tool` decorator + `create_sdk_mcp_server()`. Session-aware wrappers via factory functions
- Structured logging: key=value pairs, include timing for slow operations
- Pin all dependencies — never update versions without explicit approval
