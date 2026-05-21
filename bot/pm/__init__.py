# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""bot.pm — PM Agent Bus for structured task execution.

v5 (in-session): The calling session IS the PM+Engineer. pm_run_ticket and
pm_run_sprint return TCD + instructions for in-session execution. Review
quality comes from coding-principles stages (security-reviewer + quality-reviewer
agents). No separate orchestrator session is spawned.

19 MCP tools, own SQLite DB (pm.db), separate from conversations.db.
Legacy roles (Engineer, Verifier, Reviewer) kept for backward compat with
pm_dispatch_agent direct calls.

Hierarchy: Coding Chat → Project → Sprint → Ticket
Features: ticket prioritization, estimation/velocity tracking, sprint retro, sprint summary.
"""

from claude_agent_sdk import create_sdk_mcp_server

from bot.pm.tools import PM_TOOLS


def create_pm_server(session_key: str = ""):
    """Create the bot-pm MCP server with all PM tools.

    When session_key is provided, PM tools are wrapped to inject _session_key
    into args so they can resolve the calling chat's harness (project_dir, etc.).

    Integer schema coercion now shared via bot.mcp._coerce_integer_schemas().
    """
    # Coerce integer schemas to accept strings (LLM serialization fix)
    from bot.mcp import _coerce_integer_schemas
    tools = _coerce_integer_schemas(list(PM_TOOLS))
    if session_key:
        from bot.mcp import _wrap_tools_with_session_key
        tools = _wrap_tools_with_session_key(tools, session_key)
    return create_sdk_mcp_server(
        name="bot-pm",
        version="0.1.0",
        tools=tools,
    )


__all__ = ["create_pm_server", "PM_TOOLS"]
