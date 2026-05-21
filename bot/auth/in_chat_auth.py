# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""In-chat authentication MCP tools.

Provides auth_claude and auth_credentials tools that run inside
the conversation — no external web UI needed. Users paste tokens
directly in chat; tokens are stored per-user in the OAuth flow module.

Security:
- Tokens are NEVER logged (only metadata like provider, user_id prefix)
- Token storage is per-user isolated
- Tools use _session_key for session identity (AP-15 compliant)
"""

import logging
from typing import Any

from claude_agent_sdk import tool

from bot.mcp import _run_with_timeout

log = logging.getLogger(__name__)


@tool("auth_claude", "Authenticate your Claude Code account for this session. Use action='start' with a token to store it, 'status' to check, or 'revoke' to remove.", {
    "type": "object",
    "properties": {
        "action": {"type": "string", "description": "start | status | revoke"},
        "token": {"type": "string", "description": "OAuth token (for direct token input)"},
    },
    "required": ["action"],
})
async def auth_claude_tool(args: dict[str, Any]) -> dict:
    """In-chat Claude OAuth authentication."""
    async def _do():
        action = args.get("action", "")
        token = args.get("token", "")
        _session_key = args.pop("_session_key", "")

        # Get user context — use bare platform ID for token storage keys
        # (not RBAC-prefixed user_id, which would fragment the token table)
        from bot.core.session_state import _get_or_create_state
        state = _get_or_create_state(_session_key)
        user_ctx = state.get("current_user_ctx") or {}
        user_id = user_ctx.get("platform_user_id", "") or user_ctx.get("user_id", "")
        _chat_id = state.get("origin_chat_id", "")

        if not user_id:
            return {"error": "Cannot determine user identity. Please try again from your DM session."}

        if action == "start":
            if token:
                # Direct token paste
                from bot.auth.oauth_flow import store_user_oauth_token
                await store_user_oauth_token(user_id, "claude", token)
                return {
                    "success": True,
                    "message": "Claude OAuth token stored. Session will use your personal quota on next query.",
                }
            else:
                return {
                    "message": (
                        "To authenticate your Claude account, please paste your OAuth token.\n\n"
                        "Get it from: https://console.anthropic.com/settings/keys\n\n"
                        "Then call: auth_claude(action='start', token='your-token-here')"
                    ),
                }

        elif action == "status":
            from bot.auth.oauth_flow import list_user_tokens
            tokens = await list_user_tokens(user_id)
            claude_tokens = [t for t in tokens if t["provider"] == "claude"]
            if claude_tokens:
                return {"authenticated": True, "updated_at": claude_tokens[0]["updated_at"]}
            return {"authenticated": False, "message": "No Claude token stored. Use auth_claude(action='start') to authenticate."}

        elif action == "revoke":
            from bot.auth.oauth_flow import delete_user_oauth_token
            await delete_user_oauth_token(user_id, "claude")
            return {"success": True, "message": "Claude OAuth token revoked. Session will use shared/system token."}

        return {"error": f"Unknown action: {action}. Use: start, status, revoke"}

    return await _run_with_timeout(_do(), "auth_claude")


@tool("auth_credentials", "Authenticate external service credentials (M365, Gmail, etc). Use action='start' with provider and token, 'status' to check, or 'revoke' to remove.", {
    "type": "object",
    "properties": {
        "action": {"type": "string", "description": "start | status | revoke"},
        "provider": {"type": "string", "description": "m365 | google"},
        "token": {"type": "string", "description": "OAuth token (for direct input)"},
    },
    "required": ["action", "provider"],
})
async def auth_credentials_tool(args: dict[str, Any]) -> dict:
    """In-chat credential authentication for M365, Gmail, etc."""
    async def _do():
        action = args.get("action", "")
        provider = args.get("provider", "")
        token = args.get("token", "")
        _session_key = args.pop("_session_key", "")

        from bot.core.session_state import _get_or_create_state
        state = _get_or_create_state(_session_key)
        user_ctx = state.get("current_user_ctx") or {}
        user_id = user_ctx.get("platform_user_id", "") or user_ctx.get("user_id", "")

        if not user_id:
            return {"error": "Cannot determine user identity. Please try again from your DM session."}

        if provider not in ("m365", "google"):
            return {"error": f"Unsupported provider: {provider}. Use 'm365' or 'google'."}

        if action == "start":
            if token:
                from bot.auth.oauth_flow import store_user_oauth_token
                await store_user_oauth_token(user_id, provider, token)
                return {"success": True, "message": f"{provider} credential stored for your account."}
            else:
                if provider == "m365":
                    return {
                        "message": (
                            "To authenticate M365, you'll need to complete a device code flow.\n"
                            "Contact your admin or paste your token directly."
                        ),
                    }
                elif provider == "google":
                    return {
                        "message": (
                            "To authenticate Google, paste your OAuth refresh token.\n"
                            "Contact your admin for the token."
                        ),
                    }

        elif action == "status":
            from bot.auth.oauth_flow import list_user_tokens
            tokens = await list_user_tokens(user_id)
            provider_tokens = [t for t in tokens if t["provider"] == provider]
            if provider_tokens:
                return {"authenticated": True, "provider": provider, "updated_at": provider_tokens[0]["updated_at"]}
            return {"authenticated": False, "provider": provider}

        elif action == "revoke":
            from bot.auth.oauth_flow import delete_user_oauth_token
            await delete_user_oauth_token(user_id, provider)
            return {"success": True, "message": f"{provider} credential revoked."}

        return {"error": f"Unknown action: {action}. Use: start, status, revoke"}

    return await _run_with_timeout(_do(), "auth_credentials")


# Tool list for server registration
AUTH_TOOLS = [auth_claude_tool, auth_credentials_tool]
