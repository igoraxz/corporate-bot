# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""bot.mcp — Custom MCP tool servers for Claude Agent SDK.

Package extraction from bot/mcp_tools.py (Phase 1B refactor). Pure extraction — no behavior changes.

Sub-modules:
  telegram.py     — Telegram send/edit/delete, media, buttons, group management
  teams.py        — MS Teams send messages
  memory_tools.py — Unified search, facts, media, RAG, conversation context
  services.py     — Phone, image gen, flights, model/effort/oauth, scheduling, harness
  deploy.py       — deploy_bot, deploy_review, staging_qa_run, deploy_status, staging
  relay.py        — Employee workstation control tool (Mac Claude Code sessions via SSH+Tailscale)
"""

import asyncio
import json
import logging
import os  # noqa: F401 — re-exported for MCP tool modules
import time as _time
from typing import Any

from claude_agent_sdk import tool, create_sdk_mcp_server  # noqa: F401 — tool re-exported

from config import MAX_TOOL_RESULT_CHARS, TOOL_TIMEOUT

log = logging.getLogger(__name__)


# ============================================================
# SHARED HELPERS (imported by sub-modules)
# ============================================================

def _safe_int(val: Any, default: int) -> int:
    """Coerce a value to int, handling string inputs from MCP tool calls.

    LLMs sometimes serialize integer params as strings (e.g. "10" instead of 10).
    Schema coercion (_coerce_integer_schemas) lets these through validation — this
    converts them safely in the handler. See AP-16: never use bare int(args[...]).
    """
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _to_bool(val: Any, default: bool = False) -> bool:
    """Coerce MCP tool arg to bool. SDK passes XML 'false'/'true' as strings."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes")
    return bool(val) if val is not None else default


def _classify_error(e: Exception) -> str:
    """Classify an exception into a user-friendly category."""
    err_str = str(e).lower()
    err_type = type(e).__name__
    if "invalid_scope" in err_str or "invalid_grant" in err_str:
        return "AUTH_ERROR: Google OAuth credentials are invalid or expired. Tell the user that Gmail/Calendar access needs re-authorization."
    if "refresh" in err_str and ("token" in err_str or "credential" in err_str):
        return "AUTH_ERROR: Authentication credentials expired. Tell the user this service needs re-authorization."
    if "rate limit" in err_str or "429" in err_str or "too many requests" in err_str:
        return "RATE_LIMIT: Service is rate-limited. Wait a moment and the user can try again."
    if any(x in err_str for x in ["connection refused", "connect timeout", "name resolution", "unreachable"]):
        return f"SERVICE_DOWN: The service is unreachable ({err_type}). Tell the user and answer from what you already know."
    if "timeout" in err_str or "timed out" in err_str:
        return "TIMEOUT: The service took too long to respond. Tell the user and try to answer from memory/context."
    if "permission" in err_str or "forbidden" in err_str or "403" in err_str:
        return f"PERMISSION_ERROR: Access denied ({err_str[:100]}). Tell the user."
    if "not found" in err_str or "404" in err_str:
        return f"NOT_FOUND: Resource not found ({err_str[:100]})."
    return f"ERROR: {err_str[:200]}"


def _resolve_access_ctx(args: dict[str, Any]):
    """Resolve AccessContext from session key injected into tool args.

    MCP tool wrappers inject _session_key into args. This resolves it to
    an AccessContext via the hooks registry. Returns None if not available
    (callers treat None as NO_ACCESS — safe default).
    """
    session_key = args.pop("_session_key", "")
    if session_key:
        from bot.hooks import get_access_context
        return get_access_context(session_key)
    # No session_key — return None (NO_ACCESS, safe default)
    log.warning("_resolve_access_ctx: no _session_key in args — returning None (NO_ACCESS)")
    return None


def _text_response(data: Any) -> dict:
    """Format any data as an MCP text content response."""
    if isinstance(data, dict):
        text = json.dumps(data, ensure_ascii=False, default=str)
    else:
        text = str(data)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + "\n... [truncated]"
    return {"content": [{"type": "text", "text": text}]}


async def _run_with_timeout(coro, tool_name: str, timeout: int = 0) -> dict:
    """Execute a coroutine with timeout and error classification. Returns MCP response.

    Telegram MTProto tools auto-get the longer TG_TOOL_TIMEOUT to outlast
    FloodWait pre-sleeps (TG can flood-block the userbot for 100-300s). Without
    this, the MCP wrapper times out at TOOL_TIMEOUT (90s) while integrations/
    telegram.py is mid-sleep, and the message is silently dropped — the model
    sees TIMEOUT and never retries, so the user never gets a reply.
    """
    if timeout:
        effective_timeout = timeout
    elif tool_name.startswith("telegram_"):
        from config import TG_TOOL_TIMEOUT
        effective_timeout = TG_TOOL_TIMEOUT
    else:
        effective_timeout = TOOL_TIMEOUT
    start = _time.time()
    try:
        result = await asyncio.wait_for(coro, timeout=effective_timeout)
        elapsed = _time.time() - start
        log.debug(f"Tool {tool_name} returned ({elapsed:.1f}s)")
        return _text_response(result)
    except asyncio.TimeoutError:
        log.error(f"Tool {tool_name} timed out after {effective_timeout}s")
        return _text_response({"error": f"TIMEOUT: {tool_name} did not respond within {effective_timeout}s."})
    except PermissionError as e:
        return _text_response({"error": str(e)})
    except Exception as e:
        elapsed = _time.time() - start
        log.error(f"Tool {tool_name} failed after {elapsed:.1f}s: {e}", exc_info=True)
        return _text_response({"error": _classify_error(e)})


async def _cache_and_describe_bot_media(file_path: str, media_type: str, source: str, caption: str = ""):
    """Cache and auto-describe media sent by the bot (non-blocking background task).

    This ensures bot-generated/sent media (images, documents, etc.) is indexed
    in the media cache and searchable via search_media, just like user-sent media.
    """
    try:
        import os
        from pathlib import Path
        if not os.path.exists(file_path):
            return
        from bot.storage.memory import cache_media_file
        cached = await cache_media_file(
            source_path=file_path,
            media_type=media_type,
            source=source,
            sender_name="Bot",
            original_filename=Path(file_path).name,
            description=caption,
        )
        if cached and cached.get("id") and cached.get("file_path"):
            from config import MEDIA_DESCRIPTION_ENABLED
            if MEDIA_DESCRIPTION_ENABLED:
                from bot.storage.media import generate_media_description
                asyncio.create_task(generate_media_description(
                    cached["id"], cached["file_path"], cached.get("filename", "")
                ))
                log.info(f"Bot media cached and queued for description: {Path(file_path).name}")
    except Exception as e:
        log.warning(f"Failed to cache bot media {file_path}: {e}")


# ============================================================
# TOOL SCHEMA HELPERS
# ============================================================

def _coerce_int_type_recursive(obj: dict | list | str) -> bool:
    """Recursively walk a JSON Schema and coerce "type": "integer" to accept strings.

    Returns True if any coercion was applied (caller needs deepcopy first).
    Handles nested properties, items.properties, and array items.
    """
    changed = False
    if isinstance(obj, dict):
        if obj.get("type") == "integer":
            obj["type"] = ["integer", "string"]
            changed = True
        # Recurse into properties
        for prop_def in obj.get("properties", {}).values():
            if isinstance(prop_def, dict):
                changed |= _coerce_int_type_recursive(prop_def)
        # Recurse into items (array element schema)
        items = obj.get("items")
        if isinstance(items, dict):
            changed |= _coerce_int_type_recursive(items)
    return changed


def _coerce_integer_schemas(tools: list) -> list:
    """Coerce integer schema fields to accept both integer and string types.

    LLMs sometimes serialize integer params as strings (e.g. "limit": "10"
    instead of 10). The MCP library's jsonschema.validate() rejects this
    before the handler runs. Fix: modify schemas to accept both types.
    Handler code already does int() conversion via _safe_int().

    Applied to ALL MCP servers (memory, services, PM) — not just PM.
    Handles TWO schema formats:
    1. Full JSON Schema: {"type": "object", "properties": {"limit": {"type": "integer"}}}
       → Recursively coerces "type": "integer" to ["integer", "string"]
    2. Shorthand (SDK @tool): {"chat_id": int, "limit": int}
       → Replaces Python `int` with `[int, str]` so the SDK generates
         a permissive JSON Schema. Used by most telegram.py tools.
    """
    import copy
    from claude_agent_sdk import SdkMcpTool
    coerced = []
    for t in tools:
        schema = t.input_schema
        if not isinstance(schema, dict):
            coerced.append(t)
            continue
        if "properties" in schema:
            # Full JSON Schema format
            probe = copy.deepcopy(schema)
            if _coerce_int_type_recursive(probe):
                coerced.append(SdkMcpTool(
                    name=t.name, description=t.description,
                    input_schema=probe, handler=t.handler,
                    annotations=getattr(t, "annotations", None),
                ))
                continue
        else:
            # Shorthand format: {param_name: python_type, ...}
            # Check if any values are bare `int` type and coerce to [int, str]
            has_int = any(v is int for v in schema.values())
            if has_int:
                probe = copy.deepcopy(schema)
                for k, v in probe.items():
                    if v is int:
                        probe[k] = [int, str]
                coerced.append(SdkMcpTool(
                    name=t.name, description=t.description,
                    input_schema=probe, handler=t.handler,
                    annotations=getattr(t, "annotations", None),
                ))
                continue
        coerced.append(t)
    return coerced


# ============================================================
# TOOL WRAPPING HELPER
# ============================================================

def _wrap_tools_with_session_key(tools: list, session_key: str) -> list:
    """Wrap MCP tools to inject _session_key into args for per-session isolation.

    Uses default-arg capture (_orig=t.handler, _sk=session_key) to bind loop
    variables correctly — direct closure capture would use the last iteration's values.
    """
    from claude_agent_sdk import SdkMcpTool
    wrapped = []
    for t in tools:

        async def _wrapped_handler(args: dict, _orig=t.handler, _sk=session_key) -> dict:
            args["_session_key"] = _sk
            return await _orig(args)

        wrapped.append(SdkMcpTool(
            name=t.name, description=t.description,
            input_schema=t.input_schema, handler=_wrapped_handler,
            annotations=getattr(t, "annotations", None),
        ))
    return wrapped


# ============================================================
# IMPORTS FROM SUB-MODULES
# ============================================================

# Telegram tools
from bot.mcp.telegram import (  # noqa: E402
    _get_reply_threading_id, _update_reply_threading_state,
    telegram_send_message, telegram_send_photo, telegram_send_document,
    telegram_send_video, telegram_send_audio, telegram_send_voice,
    telegram_send_location,
    telegram_edit_message, telegram_delete_message,
    telegram_forward_message, telegram_pin_message, telegram_unpin_message,
    telegram_get_chat_members, telegram_get_chat_history,
    telegram_set_profile_photo, telegram_update_profile,
    telegram_set_chat_photo, telegram_set_chat_title,
    telegram_create_group, telegram_leave_chat,
    SESSION_TG_TOOLS, NON_SESSION_TG_TOOLS,
)

# Teams tools
from bot.mcp.teams import (
    teams_send_message,
    SESSION_TEAMS_TOOLS, NON_SESSION_TEAMS_TOOLS,
)

# Memory tools
from bot.mcp.memory_tools import (  # noqa: E402
    memory_search, get_recent_conversation, search_facts_tool, get_document_tool,
    get_message_context, get_messages_by_range, search_media,
    rag_backfill_tool, rag_stats_tool, manage_fact_tool,
    MEMORY_TOOLS,
)

# Services tools
from bot.mcp.services import (  # noqa: E402
    _GFLIGHTS_SEM, _format_flight_result,
    phone_call, phone_get_transcript,
    generate_image, edit_image,
    google_flights_search, google_flights_search_dates,
    switch_model, switch_effort, switch_oauth, manage_oauth,
    extend_budget, usage_report, reset_session_tool,
    manage_harness, manage_external_chat_tool, manage_chat_tool,
    list_scheduled_tasks, manage_scheduled_task,
    SERVICE_TOOLS,
)

# Deploy tools
from bot.mcp.deploy import (  # noqa: E402
    deploy_bot, deploy_review, staging_qa_run, deploy_status,
    deploy_staging, deploy_staging_status, staging_command,
    _staging_trigger_dir, _staging_qa_run_impl,
    DEPLOY_TOOLS,
)

# Employee workstation control tool
from bot.mcp.relay import desktop_relay_tool  # noqa: E402

# Domain management tools (corporate knowledge domains)
from bot.mcp.domains import (  # noqa: E402
    manage_domain, search_knowledge, index_document_tool,
)
DOMAIN_TOOLS = [manage_domain, search_knowledge, index_document_tool]

# RBAC management tools (admin-only)
from bot.mcp.rbac_tools import manage_rbac, RBAC_TOOLS  # noqa: E402

# In-chat auth tools (B5e — user-facing OAuth flows)
from bot.auth.in_chat_auth import auth_claude_tool, auth_credentials_tool, AUTH_TOOLS  # noqa: E402


# ============================================================
# AGGREGATED TOOL LISTS (matching original mcp_tools.py)
# ============================================================

# Tools that need session-aware reply threading (use _session_key from args)
_SESSION_AWARE_TOOLS = list(SESSION_TG_TOOLS) + list(SESSION_TEAMS_TOOLS)

# Tools that don't need session identity (no reply threading)
_NON_SESSION_TOOLS = list(NON_SESSION_TG_TOOLS) + list(NON_SESSION_TEAMS_TOOLS)

# Memory tools (same as MEMORY_TOOLS from memory_tools.py)
_MEMORY_TOOLS = list(MEMORY_TOOLS)


# ============================================================
# MCP SERVER FACTORIES
# ============================================================

def create_messaging_server(session_key: str = ""):
    """Create the bot-messaging MCP server.

    When session_key is provided, session-aware tools (send_message etc.)
    are wrapped to inject _session_key into args for race-safe reply threading.
    Non-session tools are included as-is.

    Integer schemas coerced to accept strings (LLM serialization fix).
    """
    # Coerce integer schemas on all tools before session wrapping
    session_tools = _coerce_integer_schemas(list(_SESSION_AWARE_TOOLS))
    non_session_tools = _coerce_integer_schemas(list(_NON_SESSION_TOOLS))
    if session_key:
        # Wrap ALL tools with session_key — non-session tools also need it for
        # thread_id auto-injection (CB-21: edit/delete/pin/unpin/forward).
        tools = _wrap_tools_with_session_key(session_tools + non_session_tools, session_key)
    else:
        tools = session_tools + non_session_tools

    return create_sdk_mcp_server(
        name="bot-messaging",
        version="1.0.0",
        tools=tools,
    )


def create_services_server(session_key: str = ""):
    """Create the bot-services MCP server (Voice assistant + Image gen + Deploy + Scheduler + Workstation control).

    When session_key is provided, tools that need session context (switch_model,
    switch_oauth, extend_budget, deploy_bot, deploy_review, staging_qa_run, etc.)
    are wrapped to inject _session_key into args. Tools that don't need it ignore it.

    Integer schemas coerced to accept strings (same LLM serialization fix as PM/memory).
    """
    # Use canonical SERVICE_TOOLS + DEPLOY_TOOLS lists to avoid missing any registered tools.
    all_tools = list(SERVICE_TOOLS) + list(DEPLOY_TOOLS) + [desktop_relay_tool] + DOMAIN_TOOLS + RBAC_TOOLS + AUTH_TOOLS
    # Coerce integer schemas BEFORE wrapping
    all_tools = _coerce_integer_schemas(all_tools)
    if session_key:
        all_tools = _wrap_tools_with_session_key(all_tools, session_key)
    return create_sdk_mcp_server(
        name="bot-services",
        version="1.0.0",
        tools=all_tools,
    )


def create_memory_server(session_key: str = ""):
    """Create the bot-memory MCP server.

    When session_key is provided, all memory tools are wrapped to inject
    _session_key into args for race-safe AccessContext resolution.
    Eliminates dependence on any process-global session state.

    Integer schemas are coerced to accept strings — LLMs sometimes serialize
    int params as strings (e.g. "limit": "10"), causing jsonschema rejection
    before the handler runs. Same fix as PM tools (AP: integer coercion).
    """
    # Coerce integer schemas BEFORE wrapping (wrapper preserves input_schema)
    tools = _coerce_integer_schemas(list(_MEMORY_TOOLS))
    if session_key:
        tools = _wrap_tools_with_session_key(tools, session_key)

    return create_sdk_mcp_server(
        name="bot-memory",
        version="1.0.0",
        tools=tools,
    )


def create_pm_server(session_key: str = ""):
    """Create the bot-pm MCP server (PM Agent Bus — projects/sprints/tickets).

    When session_key is provided, PM tools are wrapped to inject _session_key
    so they can resolve the calling chat's harness (project_dir, etc.).
    """
    from bot.pm import create_pm_server as _create
    return _create(session_key)


def get_custom_mcp_servers(session_key: str = "") -> dict:
    """Create and return all custom in-process MCP servers.

    When session_key is provided, messaging tools get per-client wrappers
    that inject session identity for race-safe reply threading, and memory
    tools get wrappers for race-safe AccessContext resolution.
    """
    return {
        "bot-messaging": create_messaging_server(session_key),
        "bot-services": create_services_server(session_key),
        "bot-memory": create_memory_server(session_key),
        "bot-pm": create_pm_server(session_key),
    }


# ============================================================
# PUBLIC API
# ============================================================

__all__ = [
    # Shared helpers
    "_safe_int", "_to_bool", "_classify_error", "_resolve_access_ctx",
    "_text_response", "_run_with_timeout",
    "_cache_and_describe_bot_media",
    "_coerce_int_type_recursive", "_coerce_integer_schemas",
    "_wrap_tools_with_session_key",
    # Re-export from claude_agent_sdk
    "create_sdk_mcp_server",
    # TG tools + helpers
    "_get_reply_threading_id", "_update_reply_threading_state",
    "telegram_send_message", "telegram_send_photo", "telegram_send_document",
    "telegram_send_video", "telegram_send_audio", "telegram_send_voice",
    "telegram_send_location",
    "telegram_edit_message", "telegram_delete_message",
    "telegram_forward_message", "telegram_pin_message", "telegram_unpin_message",
    "telegram_get_chat_members", "telegram_get_chat_history",
    "telegram_set_profile_photo", "telegram_update_profile",
    "telegram_set_chat_photo", "telegram_set_chat_title",
    "telegram_create_group", "telegram_leave_chat",
    "SESSION_TG_TOOLS", "NON_SESSION_TG_TOOLS",
    # Teams tools
    "teams_send_message",
    "SESSION_TEAMS_TOOLS", "NON_SESSION_TEAMS_TOOLS",
    # Memory tools
    "memory_search", "get_recent_conversation", "search_facts_tool", "get_document_tool",
    "get_message_context", "get_messages_by_range", "search_media",
    "rag_backfill_tool", "rag_stats_tool", "manage_fact_tool",
    "MEMORY_TOOLS",
    # Services tools
    "_GFLIGHTS_SEM", "_format_flight_result",
    "phone_call", "phone_get_transcript",
    "generate_image", "edit_image",
    "google_flights_search", "google_flights_search_dates",
    "switch_model", "switch_effort", "switch_oauth", "manage_oauth",
    "extend_budget", "usage_report", "reset_session_tool",
    "manage_harness", "manage_external_chat_tool", "manage_chat_tool",
    "list_scheduled_tasks", "manage_scheduled_task",
    "SERVICE_TOOLS",
    # Deploy tools
    "deploy_bot", "deploy_review", "staging_qa_run", "deploy_status",
    "deploy_staging", "deploy_staging_status", "staging_command",
    "_staging_trigger_dir", "_staging_qa_run_impl",
    "DEPLOY_TOOLS",
    # Relay tool
    "desktop_relay_tool",
    # Domain tools
    "manage_domain", "search_knowledge", "index_document_tool",
    "DOMAIN_TOOLS",
    # RBAC tools
    "manage_rbac", "RBAC_TOOLS",
    # Auth tools (B5e)
    "auth_claude_tool", "auth_credentials_tool", "AUTH_TOOLS",
    # Aggregated lists
    "_SESSION_AWARE_TOOLS", "_NON_SESSION_TOOLS", "_MEMORY_TOOLS",
    # Server factories
    "create_messaging_server", "create_services_server",
    "create_memory_server", "create_pm_server", "get_custom_mcp_servers",
]
