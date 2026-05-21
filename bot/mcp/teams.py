# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Teams MCP tools — send messages to Teams chats.

Follows the same pattern as telegram.py:
- Session-aware reply threading via _session_key injection
- FACTS_UPDATE stripping before send
- Bot response storage for context continuity
- External chat auto-registration
- Staging buffer support

Teams message formatting: markdown (bold, italic, code, links).
No HTML parse_mode — Teams uses its own markdown subset.
"""

import logging
from typing import Any

from claude_agent_sdk import tool

from bot.mcp import _run_with_timeout

log = logging.getLogger(__name__)


# ============================================================
# Teams-specific helpers
# ============================================================

def _get_teams_reply_threading_id(
    target_chat_id: str,
    session_key: str = "",
) -> str | None:
    """Get reply-to activity ID for Teams threading.

    Returns activity_id to reply to, or None for standalone message.
    Follows same logic as TG: first send → reply to user's message,
    subsequent → standalone unless interleaved.
    """
    from bot.hooks import (
        get_reply_to_msg_id, get_task_last_sent_id,
        get_current_task_id, get_origin_chat_id,
        get_chat_last_task,
    )
    sk = session_key
    if str(target_chat_id) != get_origin_chat_id(session_key=sk):
        return None
    task_id = get_current_task_id(session_key=sk)
    my_last = get_task_last_sent_id(session_key=sk)
    if my_last is None:
        # First send — reply to user's original message
        reply_id = get_reply_to_msg_id(session_key=sk)
        return str(reply_id) if reply_id else None
    last_task = get_chat_last_task(str(target_chat_id))
    if last_task and last_task != task_id:
        # Another task interleaved — thread to user's message
        reply_id = get_reply_to_msg_id(session_key=sk)
        return str(reply_id) if reply_id else None
    return None


def _update_teams_reply_threading_state(
    target_chat_id: str,
    msg_id: str | None,
    session_key: str = "",
) -> None:
    """Update reply-threading state after a successful Teams send."""
    from bot.hooks import (
        set_task_last_sent_id, get_current_task_id,
        get_origin_chat_id, mark_chat_activity,
    )
    sk = session_key
    if msg_id and str(target_chat_id) == get_origin_chat_id(session_key=sk):
        set_task_last_sent_id(msg_id, session_key=sk)
        task_id = get_current_task_id(session_key=sk)
        if task_id:
            mark_chat_activity(str(target_chat_id), task_id)


# ============================================================
# TEAMS TOOLS
# ============================================================

@tool("teams_send_message", "Send a message to a Teams chat", {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "Message text (markdown supported: **bold**, *italic*, `code`, ```code blocks```, [links](url))"},
        "chat_id": {"type": "string", "description": "Teams conversation ID. Leave empty to send to the current chat."},
    },
    "required": ["text"],
})
async def teams_send_message(args: dict[str, Any]) -> dict:
    """Send a message to a Teams conversation."""
    async def _do():
        sk = args.pop("_session_key", "")
        from config import IS_STAGING

        if IS_STAGING:
            from bot.pipeline.staging import staging_buffer_response
            target = args.get("chat_id") or "staging-test"
            await staging_buffer_response(
                str(target), args.get("text", ""), "teams"
            )
            return {
                "success": True, "message_id": "",
                "note": "staging: buffered (not sent)",
            }

        from integrations.teams import get_client
        from bot.agent import strip_facts_update
        from bot.core.session_state import _get_or_create_state

        client = get_client()
        if not client:
            return {"success": False, "error": "Teams client not initialized (TEAMS_ENABLED=false?)"}

        # Strip FACTS_UPDATE from visible text
        raw_text = args.get("text", "")
        clean_text = strip_facts_update(raw_text)
        if not clean_text or not clean_text.strip():
            return {
                "success": True,
                "note": "Message contained only FACTS_UPDATE, nothing to send",
            }

        # Resolve chat_id from args or session state
        chat_id = args.get("chat_id", "")
        if not chat_id:
            state = _get_or_create_state(sk)
            chat_id = state.get("origin_chat_id", "")
        if not chat_id:
            return {"success": False, "error": "No chat_id provided and none in session"}

        # Get reply-to for threading
        reply_to = _get_teams_reply_threading_id(chat_id, sk)

        try:
            result = await client.send_message(
                chat_id, clean_text,
                text_format="markdown",
                reply_to_id=reply_to,
            )
            msg_id = result.get("id", "")

            # Update reply-threading state
            _update_teams_reply_threading_state(chat_id, msg_id, sk)

            # Store bot response for context continuity
            try:
                from bot.storage.memory import store_message as store_msg
                await store_msg(
                    "teams", "Bot", clean_text, "assistant",
                    chat_id=str(chat_id),
                )
            except Exception as e:
                log.warning("Failed to store Teams bot message: %s", e)

            # Track bot send time for external chat throttling
            from bot.pipeline.dispatch import update_ec_bot_send_time
            update_ec_bot_send_time(str(chat_id))

            return {"success": True, "message_id": msg_id}

        except Exception as e:
            log.error("teams_send_message failed: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}

    return await _run_with_timeout(_do(), "teams_send_message")


# ============================================================
# teams_edit_message
# ============================================================

@tool("teams_edit_message", "Edit an existing Teams message.", {
    "type": "object",
    "properties": {
        "message_id": {"type": "string", "description": "Activity ID of the message to edit"},
        "text": {"type": "string", "description": "New message text (markdown format)"},
        "chat_id": {"type": "string", "description": "Conversation ID (Teams chat/channel ID)"},
    },
    "required": ["message_id", "text", "chat_id"],
})
async def teams_edit_message(args: dict[str, Any]) -> dict:
    args.pop("_session_key", "")

    async def _do():
        try:
            from integrations.teams import get_client
            client = get_client()
            if not client:
                return {"success": False, "error": "Teams client not available"}
            result = await client.update_message(
                conversation_id=str(args["chat_id"]),
                activity_id=str(args["message_id"]),
                text=str(args["text"]),
            )
            return {"success": True, "result": result}
        except Exception as e:
            log.error("teams_edit_message failed: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}

    return await _run_with_timeout(_do(), "teams_edit_message")


# ============================================================
# teams_delete_message
# ============================================================

@tool("teams_delete_message", "Delete a Teams message.", {
    "type": "object",
    "properties": {
        "message_id": {"type": "string", "description": "Activity ID of the message to delete"},
        "chat_id": {"type": "string", "description": "Conversation ID (Teams chat/channel ID)"},
    },
    "required": ["message_id", "chat_id"],
})
async def teams_delete_message(args: dict[str, Any]) -> dict:
    args.pop("_session_key", "")

    async def _do():
        try:
            from integrations.teams import get_client
            client = get_client()
            if not client:
                return {"success": False, "error": "Teams client not available"}
            await client.delete_activity(
                conversation_id=str(args["chat_id"]),
                activity_id=str(args["message_id"]),
            )
            return {"success": True}
        except Exception as e:
            log.error("teams_delete_message failed: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}

    return await _run_with_timeout(_do(), "teams_delete_message")


# ============================================================
# teams_send_document
# ============================================================

@tool("teams_send_document", "Send a file/document to a Teams chat.", {
    "type": "object",
    "properties": {
        "file_path": {"type": "string", "description": "Local path to the file to send"},
        "chat_id": {"type": "string", "description": "Conversation ID"},
        "file_name": {"type": "string", "description": "Display filename (optional, defaults to basename)"},
        "caption": {"type": "string", "description": "Message text to accompany the file"},
    },
    "required": ["file_path", "chat_id"],
})
async def teams_send_document(args: dict[str, Any]) -> dict:
    args.pop("_session_key", "")

    async def _do():
        try:
            from bot.teams_files import send_file_to_dm
            from bot.auth.token_vault import get_user_token
            from integrations.teams import get_client
            from pathlib import Path

            file_path = str(args["file_path"])
            chat_id = str(args["chat_id"])
            file_name = args.get("file_name") or Path(file_path).name
            caption = args.get("caption", "")

            # SECURITY: Path confinement — only allow files from safe directories
            _resolved = Path(file_path).resolve()
            _SAFE_DIRS = (Path("/app/data/tmp"), Path("/app/data/reports"), Path("/app/data/media_cache"))
            if not any(_resolved.is_relative_to(d) for d in _SAFE_DIRS):
                return {"success": False, "error": "File path not in allowed directories (data/tmp, reports, media_cache)"}

            if not _resolved.exists():
                return {"success": False, "error": f"File not found: {file_path}"}

            # Try user token first (for DM uploads to OneDrive)
            user_token = None
            try:
                user_token = await get_user_token(chat_id, "ms365")
            except Exception:
                pass

            if user_token:
                result = await send_file_to_dm(
                    user_token=user_token,
                    conversation_id=chat_id,
                    file_path=file_path,
                    file_name=file_name,
                )
                # Also send caption as text message
                if caption:
                    client = get_client()
                    if client:
                        await client.send_message(chat_id, caption)
                return {"success": True, **result}
            else:
                # Fallback: send as link in message
                client = get_client()
                if client:
                    msg = f"\U0001f4ce **{file_name}**"
                    if caption:
                        msg += f"\n{caption}"
                    msg += f"\n\n_File available at: `{file_path}`_"
                    await client.send_message(chat_id, msg)
                    return {"success": True, "method": "text_link"}
                return {"success": False, "error": "No Teams client or user token available"}
        except Exception as e:
            log.error("teams_send_document failed: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}

    return await _run_with_timeout(_do(), "teams_send_document")


# ============================================================
# Tool lists for server registration
# ============================================================

# Session-aware Teams tools (need _session_key injection)
SESSION_TEAMS_TOOLS = [
    teams_send_message,
]

# Non-session Teams tools (no reply threading needed)
NON_SESSION_TEAMS_TOOLS: list = [
    teams_edit_message,
    teams_delete_message,
    teams_send_document,
]

__all__ = [
    # Tools
    "teams_send_message", "teams_edit_message", "teams_delete_message",
    "teams_send_document",
    # Helpers
    "_get_teams_reply_threading_id",
    "_update_teams_reply_threading_state",
    # Tool lists
    "SESSION_TEAMS_TOOLS", "NON_SESSION_TEAMS_TOOLS",
]
