# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Telegram MCP tools — send/edit/delete messages, media, buttons, group management.

Extracted from bot/mcp_tools.py (Phase 1B refactor). Pure extraction — no behavior changes.
"""

import asyncio
import json
import logging
from typing import Any

from claude_agent_sdk import tool

from bot.mcp import (
    _safe_int, _run_with_timeout,
    _cache_and_describe_bot_media,
)

log = logging.getLogger(__name__)


# ============================================================
# TG-specific helpers (reply threading)
# ============================================================

def _get_thread_id(session_key: str = "") -> int | None:
    """Get the forum topic thread_id from session state for auto-injection."""
    if not session_key:
        return None
    from bot.core.session_state import get_message_thread_id
    return get_message_thread_id(session_key=session_key)


def _get_reply_threading_id(target_chat_id: int | str, session_key: str = "") -> int | None:
    """Compute reply_to_message_id for auto-threading.

    Returns the message_id to reply to, or None for standalone.
    Rules:
    - First send from task → reply to user's original message
    - Subsequent sends → reply-thread only if another task/user interleaved
    - Only applies when sending to the task's origin chat

    When session_key is provided, uses explicit state lookup (race-safe).
    """
    from bot.hooks import (get_reply_to_msg_id, get_task_last_sent_id,
                           get_current_task_id, get_origin_chat_id,
                           get_chat_last_task)
    sk = session_key
    if str(target_chat_id) != get_origin_chat_id(session_key=sk):
        return None
    task_id = get_current_task_id(session_key=sk)
    my_last = get_task_last_sent_id(session_key=sk)
    if my_last is None:
        return get_reply_to_msg_id(session_key=sk)
    last_task = get_chat_last_task(str(target_chat_id))
    if last_task and last_task != task_id:
        # Another task interleaved — reply to user's original message to form
        # a visible thread, not to bot's own last message
        return get_reply_to_msg_id(session_key=sk)
    return None


def _update_reply_threading_state(target_chat_id: int | str, msg_id: int | None,
                                  session_key: str = "") -> None:
    """Update reply-threading state after a successful send."""
    from bot.hooks import (set_task_last_sent_id, get_current_task_id,
                           get_origin_chat_id, mark_chat_activity)
    sk = session_key
    if msg_id and str(target_chat_id) == get_origin_chat_id(session_key=sk):
        set_task_last_sent_id(msg_id, session_key=sk)
        task_id = get_current_task_id(session_key=sk)
        if task_id:
            mark_chat_activity(str(target_chat_id), task_id)


# ============================================================
# TELEGRAM TOOLS
# ============================================================

@tool("telegram_send_message", "Send a message to a Telegram chat. Supports optional inline keyboard buttons.", {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "parse_mode": {"type": "string"},
        "chat_id": {"type": ["integer", "string"]},
        "reply_to_message_id": {"type": "integer", "description": "Reply to a specific message ID. Only set this when you need to reply to a SPECIFIC message in a multi-person chat. Leave unset for normal replies — auto-threading handles it."},
        "message_thread_id": {"type": ["integer", "string"], "description": "Forum topic thread ID. Routes message to a specific topic in a forum-enabled supergroup."},
        "inline_keyboard": {"type": "array", "description": "Optional inline keyboard. Array of rows, each row is array of button objects with 'text' and optional 'callback_data' or 'url'. Example: [[{\"text\":\"Yes\",\"callback_data\":\"yes\"},{\"text\":\"No\",\"callback_data\":\"no\"}]]"},
    },
    "required": ["text", "parse_mode", "chat_id"],
})
async def telegram_send_message(args: dict[str, Any]) -> dict:
    async def _do():
        sk = args.pop("_session_key", "")
        from config import IS_STAGING
        if IS_STAGING:
            # In staging mode, buffer the response instead of sending to TG
            from main import staging_buffer_response
            target = args.get("chat_id") or "staging-test"
            staging_buffer_response(str(target), "telegram_send_message", {k: v for k, v in args.items() if k != "_session_key"})
            return {"success": True, "message_id": 0, "note": "staging: buffered (not sent)"}
        from integrations.telegram import send_message
        from config import TG_CHAT_ID
        # Corporate mode: chat_id is required (no primary group fallback).
        # TG_CHAT_ID kept as last-resort fallback for backward compat only.
        target_chat_id = args.get("chat_id") or TG_CHAT_ID
        from bot.agent import strip_facts_update
        clean_text = strip_facts_update(args["text"])
        if not clean_text or not clean_text.strip():
            return {"success": True, "note": "Message contained only FACTS_UPDATE, nothing to send"}

        # Sanitize HTML: strip tags not supported by Telegram Bot API.
        # Models drift toward generic HTML (span, div, p, h1, br) during long sessions.
        # Supported: b, i, u, s, code, pre, a, blockquote, tg-spoiler
        if args.get("parse_mode", "").upper() == "HTML":
            import re as _re_tg
            _TG_ALLOWED = {"b", "i", "u", "s", "code", "pre", "a", "blockquote", "tg-spoiler"}
            # Replace <br> with newline before stripping (common drift tag)
            clean_text = _re_tg.sub(r'<br\s*/?\s*>', '\n', clean_text)
            # Strip unsupported tags, keep text content
            clean_text = _re_tg.sub(
                r'<(/?)(\w[\w-]*)[^>]*>',
                lambda m: m.group(0) if m.group(2).lower() in _TG_ALLOWED else "",
                clean_text,
            )

        # Explicit reply_to overrides auto-threading (used in external/group chats)
        explicit_reply = args.get("reply_to_message_id")
        # AP-16: never bare int() on MCP args — LLMs may send strings
        try:
            reply_id = int(explicit_reply) if explicit_reply else _get_reply_threading_id(target_chat_id, sk)
        except (ValueError, TypeError):
            reply_id = _get_reply_threading_id(target_chat_id, sk)

        # Forum topic: route message to the specified thread.
        # Auto-inject from session state if not explicitly provided by the model.
        thread_id = None
        raw_thread = args.get("message_thread_id")
        if raw_thread is not None:
            thread_id = _safe_int(raw_thread, None)
        elif sk:
            from bot.core.session_state import get_message_thread_id
            thread_id = get_message_thread_id(session_key=sk)

        # Inline keyboard: build reply_markup if provided
        reply_markup = None
        kb_data = args.get("inline_keyboard")
        if kb_data:
            try:
                from integrations.telegram_botapi import build_inline_keyboard
                # kb_data may be a JSON string from the LLM
                if isinstance(kb_data, str):
                    kb_data = json.loads(kb_data)
                reply_markup = build_inline_keyboard(kb_data)
            except Exception as kb_err:
                log.warning("Failed to build inline keyboard: %s", kb_err)

        result = await send_message(
            clean_text,
            chat_id=target_chat_id,
            parse_mode=args.get("parse_mode", "HTML"),
            reply_to_message_id=reply_id,
            message_thread_id=thread_id,
            reply_markup=reply_markup,
        )
        if result:
            msg_id = result.get("message_id")
            _update_reply_threading_state(target_chat_id, msg_id, sk)
            # Store bot response for context continuity across tasks
            try:
                from bot.storage.memory import store_message as store_msg
                await store_msg("telegram", "Bot", clean_text, "assistant",
                                chat_id=str(target_chat_id))
            except Exception as e:
                log.warning(f"Failed to store bot message: {e}")
            # Track bot send time for external chat throttling
            _ec_id = _safe_int(target_chat_id, 0)  # M6 fix: AP-16 safe int
            if target_chat_id != TG_CHAT_ID and _ec_id != 0:
                import bot.chat_registry as _cr
                _cr_entry = _cr.get(f"telegram:{_ec_id}")
                if _cr_entry and _cr_entry.get("mode") in ("active", "active_plus"):
                    from main import update_ec_bot_send_time
                    update_ec_bot_send_time(str(target_chat_id))
            return {"success": True, "message_id": msg_id}
        return {"success": False, "error": "Failed to send message"}
    return await _run_with_timeout(_do(), "telegram_send_message")


@tool("telegram_edit_message", "Edit an existing Telegram message", {
    "message_id": int, "text": str, "chat_id": int,
})
async def telegram_edit_message(args: dict[str, Any]) -> dict:
    async def _do():
        sk = args.pop("_session_key", "")
        from integrations.telegram import edit_message
        from bot.agent import strip_facts_update
        clean_text = strip_facts_update(args["text"])
        if not clean_text or not clean_text.strip():
            return {"success": True, "note": "Message contained only FACTS_UPDATE, nothing to edit"}
        target_chat_id = args.get("chat_id")
        if not target_chat_id:
            return {"success": False, "error": "chat_id is required"}
        thread_id = _get_thread_id(sk)
        result = await edit_message(
            args["message_id"], clean_text,
            chat_id=target_chat_id,
            message_thread_id=thread_id,
        )
        return {"success": result is not None}
    return await _run_with_timeout(_do(), "telegram_edit_message")


@tool("telegram_delete_message", "Delete a Telegram message", {
    "message_id": int, "chat_id": int,
})
async def telegram_delete_message(args: dict[str, Any]) -> dict:
    async def _do():
        sk = args.pop("_session_key", "")
        from integrations.telegram import delete_message
        target_chat_id = args.get("chat_id")
        if not target_chat_id:
            return {"success": False, "error": "chat_id is required"}
        thread_id = _get_thread_id(sk)
        result = await delete_message(args["message_id"], chat_id=target_chat_id,
                                      message_thread_id=thread_id)
        return {"success": result}
    return await _run_with_timeout(_do(), "telegram_delete_message")


@tool("telegram_send_photo", "Send a photo to a Telegram chat", {
    "photo_path": str, "caption": str, "chat_id": int,
})
async def telegram_send_photo(args: dict[str, Any]) -> dict:
    async def _do():
        sk = args.pop("_session_key", "")
        from integrations.telegram import send_photo
        target_chat_id = args.get("chat_id")
        if not target_chat_id:
            return {"success": False, "error": "chat_id is required"}
        reply_id = _get_reply_threading_id(target_chat_id, sk)
        thread_id = _get_thread_id(sk)
        result = await send_photo(
            args["photo_path"],
            args.get("caption", ""),
            chat_id=target_chat_id,
            reply_to_message_id=reply_id,
            message_thread_id=thread_id,
        )
        if result:
            msg_id = result.get("message_id")
            _update_reply_threading_state(target_chat_id, msg_id, sk)
            asyncio.create_task(_cache_and_describe_bot_media(
                args["photo_path"], "photo", "telegram", args.get("caption", "")
            ))
            return {"success": True, "message_id": msg_id}
        return {"success": False, "error": "Failed to send photo"}
    return await _run_with_timeout(_do(), "telegram_send_photo")


@tool("telegram_send_document", "Send a document to a Telegram chat", {
    "document_path": str, "caption": str, "chat_id": int,
})
async def telegram_send_document(args: dict[str, Any]) -> dict:
    async def _do():
        sk = args.pop("_session_key", "")
        from integrations.telegram import send_document
        target_chat_id = args.get("chat_id")
        if not target_chat_id:
            return {"success": False, "error": "chat_id is required"}
        reply_id = _get_reply_threading_id(target_chat_id, sk)
        thread_id = _get_thread_id(sk)
        result = await send_document(
            args["document_path"],
            args.get("caption", ""),
            chat_id=target_chat_id,
            reply_to_message_id=reply_id,
            message_thread_id=thread_id,
        )
        if result:
            msg_id = result.get("message_id")
            _update_reply_threading_state(target_chat_id, msg_id, sk)
            asyncio.create_task(_cache_and_describe_bot_media(
                args["document_path"], "document", "telegram", args.get("caption", "")
            ))
            return {"success": True, "message_id": msg_id}
        return {"success": False, "error": "Failed to send document"}
    return await _run_with_timeout(_do(), "telegram_send_document")

@tool("telegram_set_profile_photo", "Set the bot's Telegram profile photo", {
    "photo_path": str,
})
async def telegram_set_profile_photo(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.telegram import set_profile_photo
        from pathlib import Path
        photo_path = args["photo_path"]
        if not Path(photo_path).exists():
            return {"success": False, "error": f"File not found: {photo_path}"}
        result = await set_profile_photo(photo_path)
        if result:
            return {"success": True}
        return {"success": False, "error": "Pyrogram set_profile_photo returned False — check bot logs for details"}
    return await _run_with_timeout(_do(), "telegram_set_profile_photo")


@tool("telegram_update_profile", "Update the bot's Telegram display name", {
    "first_name": str, "last_name": str,
})
async def telegram_update_profile(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.telegram import update_profile
        result = await update_profile(args["first_name"], args.get("last_name", ""))
        if result:
            return {"success": True}
        return {"success": False, "error": "Pyrogram update_profile returned False — check bot logs"}
    return await _run_with_timeout(_do(), "telegram_update_profile")


@tool("telegram_send_video", "Send a video to a Telegram chat", {
    "video_path": str, "caption": str, "chat_id": int,
})
async def telegram_send_video(args: dict[str, Any]) -> dict:
    async def _do():
        sk = args.pop("_session_key", "")
        from integrations.telegram import send_video
        target_chat_id = args.get("chat_id")
        if not target_chat_id:
            return {"success": False, "error": "chat_id is required"}
        reply_id = _get_reply_threading_id(target_chat_id, sk)
        thread_id = _get_thread_id(sk)
        result = await send_video(
            args["video_path"], args.get("caption", ""),
            chat_id=target_chat_id,
            reply_to_message_id=reply_id,
            message_thread_id=thread_id,
        )
        if result:
            msg_id = result.get("message_id")
            _update_reply_threading_state(target_chat_id, msg_id, sk)
            return {"success": True, "message_id": msg_id}
        return {"success": False, "error": "Failed to send video"}
    return await _run_with_timeout(_do(), "telegram_send_video")


@tool("telegram_send_audio", "Send an audio file to a Telegram chat", {
    "audio_path": str, "caption": str, "chat_id": int,
})
async def telegram_send_audio(args: dict[str, Any]) -> dict:
    async def _do():
        sk = args.pop("_session_key", "")
        from integrations.telegram import send_audio
        target_chat_id = args.get("chat_id")
        if not target_chat_id:
            return {"success": False, "error": "chat_id is required"}
        reply_id = _get_reply_threading_id(target_chat_id, sk)
        thread_id = _get_thread_id(sk)
        result = await send_audio(
            args["audio_path"], args.get("caption", ""),
            chat_id=target_chat_id,
            reply_to_message_id=reply_id,
            message_thread_id=thread_id,
        )
        if result:
            msg_id = result.get("message_id")
            _update_reply_threading_state(target_chat_id, msg_id, sk)
            return {"success": True, "message_id": msg_id}
        return {"success": False, "error": "Failed to send audio"}
    return await _run_with_timeout(_do(), "telegram_send_audio")


@tool("telegram_send_voice", "Send a voice message to a Telegram chat", {
    "voice_path": str, "caption": str, "chat_id": int,
})
async def telegram_send_voice(args: dict[str, Any]) -> dict:
    async def _do():
        sk = args.pop("_session_key", "")
        from integrations.telegram import send_voice
        target_chat_id = args.get("chat_id")
        if not target_chat_id:
            return {"success": False, "error": "chat_id is required"}
        reply_id = _get_reply_threading_id(target_chat_id, sk)
        thread_id = _get_thread_id(sk)
        result = await send_voice(
            args["voice_path"], args.get("caption", ""),
            chat_id=target_chat_id,
            reply_to_message_id=reply_id,
            message_thread_id=thread_id,
        )
        if result:
            msg_id = result.get("message_id")
            _update_reply_threading_state(target_chat_id, msg_id, sk)
            return {"success": True, "message_id": msg_id}
        return {"success": False, "error": "Failed to send voice"}
    return await _run_with_timeout(_do(), "telegram_send_voice")


@tool("telegram_send_location", "Send a location pin to a Telegram chat", {
    "latitude": float, "longitude": float, "chat_id": int,
})
async def telegram_send_location(args: dict[str, Any]) -> dict:
    async def _do():
        sk = args.pop("_session_key", "")
        from integrations.telegram import send_location
        target_chat_id = args.get("chat_id")
        if not target_chat_id:
            return {"success": False, "error": "chat_id is required"}
        reply_id = _get_reply_threading_id(target_chat_id, sk)
        thread_id = _get_thread_id(sk)
        result = await send_location(
            args["latitude"], args["longitude"],
            chat_id=target_chat_id,
            reply_to_message_id=reply_id,
            message_thread_id=thread_id,
        )
        if result:
            msg_id = result.get("message_id")
            _update_reply_threading_state(target_chat_id, msg_id, sk)
            return {"success": True, "message_id": msg_id}
        return {"success": False, "error": "Failed to send location"}
    return await _run_with_timeout(_do(), "telegram_send_location")


@tool("telegram_forward_message", "Forward a message from one Telegram chat to another. Use message_thread_id to forward INTO a specific forum topic.", {
    "from_chat_id": int, "message_id": int, "to_chat_id": int,
    "message_thread_id": {"type": ["integer", "string"], "description": "Forum topic thread ID to forward into (optional)"},
})
async def telegram_forward_message(args: dict[str, Any]) -> dict:
    async def _do():
        sk = args.pop("_session_key", "")
        from integrations.telegram import forward_message
        to_chat_id = args.get("to_chat_id")
        if not to_chat_id:
            return {"success": False, "error": "to_chat_id is required"}
        raw_thread = args.get("message_thread_id")
        thread_id = _safe_int(raw_thread, None) if raw_thread is not None else _get_thread_id(sk)
        result = await forward_message(
            args["from_chat_id"], args["message_id"],
            to_chat_id=to_chat_id,
            message_thread_id=thread_id,
        )
        if result:
            return {"success": True, "message_id": result.get("message_id")}
        return {"success": False, "error": "Failed to forward message"}
    return await _run_with_timeout(_do(), "telegram_forward_message")


@tool("telegram_pin_message", "Pin a message in a Telegram chat", {
    "message_id": int, "chat_id": int, "disable_notification": bool,
})
async def telegram_pin_message(args: dict[str, Any]) -> dict:
    async def _do():
        sk = args.pop("_session_key", "")
        from integrations.telegram import pin_message
        target_chat_id = args.get("chat_id")
        if not target_chat_id:
            return {"success": False, "error": "chat_id is required"}
        thread_id = _get_thread_id(sk)
        result = await pin_message(
            args["message_id"],
            chat_id=target_chat_id,
            disable_notification=args.get("disable_notification", False),
            message_thread_id=thread_id,
        )
        return {"success": result}
    return await _run_with_timeout(_do(), "telegram_pin_message")


@tool("telegram_unpin_message", "Unpin a message in a Telegram chat", {
    "message_id": int, "chat_id": int,
})
async def telegram_unpin_message(args: dict[str, Any]) -> dict:
    async def _do():
        sk = args.pop("_session_key", "")
        from integrations.telegram import unpin_message
        target_chat_id = args.get("chat_id")
        if not target_chat_id:
            return {"success": False, "error": "chat_id is required"}
        thread_id = _get_thread_id(sk)
        result = await unpin_message(
            args["message_id"],
            chat_id=target_chat_id,
            message_thread_id=thread_id,
        )
        return {"success": result}
    return await _run_with_timeout(_do(), "telegram_unpin_message")


@tool("telegram_get_chat_members", "List members of a Telegram group/channel", {
    "chat_id": int, "limit": int,
})
async def telegram_get_chat_members(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.telegram import get_chat_members
        target_chat_id = args.get("chat_id")
        if not target_chat_id:
            return {"members": [], "count": 0, "error": "chat_id is required"}
        members = await get_chat_members(
            chat_id=target_chat_id,
            limit=args.get("limit", 200),
        )
        return {"members": members, "count": len(members)}
    return await _run_with_timeout(_do(), "telegram_get_chat_members")


@tool("telegram_get_chat_history", "Get recent messages from a Telegram chat", {
    "chat_id": int, "limit": int,
})
async def telegram_get_chat_history(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.telegram import get_chat_history
        target_chat_id = args.get("chat_id")
        if not target_chat_id:
            return {"messages": [], "count": 0, "error": "chat_id is required"}
        messages = await get_chat_history(
            chat_id=target_chat_id,
            limit=args.get("limit", 20),
        )
        return {"messages": messages, "count": len(messages)}
    return await _run_with_timeout(_do(), "telegram_get_chat_history")


@tool("telegram_set_chat_photo", "Set the photo of a Telegram group/channel", {
    "photo_path": str, "chat_id": int,
})
async def telegram_set_chat_photo(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.telegram import set_chat_photo
        from pathlib import Path
        photo_path = args["photo_path"]
        if not Path(photo_path).exists():
            return {"success": False, "error": f"File not found: {photo_path}"}
        target_chat_id = args.get("chat_id")
        if not target_chat_id:
            return {"success": False, "error": "chat_id is required"}
        result = await set_chat_photo(
            photo_path,
            chat_id=target_chat_id,
        )
        return result  # dict with success + optional error
    return await _run_with_timeout(_do(), "telegram_set_chat_photo")


@tool("telegram_set_chat_title", "Set the title of a Telegram group/channel", {
    "title": str, "chat_id": int,
})
async def telegram_set_chat_title(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.telegram import set_chat_title
        target_chat_id = args.get("chat_id")
        if not target_chat_id:
            return {"success": False, "error": "chat_id is required"}
        result = await set_chat_title(
            args["title"],
            chat_id=target_chat_id,
        )
        return {"success": result}
    return await _run_with_timeout(_do(), "telegram_set_chat_title")


_VALID_CHAT_MODES = {"active_plus", "active", "silent"}


@tool("telegram_create_group", "Create a new Telegram group chat. Registers as external chat and optionally assigns a harness.", {
    "title": {"type": "string", "description": "Group title (e.g. '🛠 Bot Coding')"},
    "mode": {"type": "string", "description": "External chat mode: active_plus (full UX), active (no streaming), silent (listen only). Default: active_plus"},
    "harness": {"type": "string", "description": "Harness label to assign (e.g. 'coding'). Optional."},
    "user_ids": {"type": "string", "description": "Comma-separated TG user IDs to add. Default: requesting user only (from session metadata)."},
})
async def telegram_create_group(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.telegram import create_group
        import bot.chat_registry as _cr
        title = args.get("title", "New Group")
        mode = args.get("mode", "active_plus")
        if mode not in _VALID_CHAT_MODES:
            return {"success": False, "error": f"Invalid mode '{mode}'. Must be one of: {', '.join(sorted(_VALID_CHAT_MODES))}"}
        harness_label = args.get("harness")

        # Add only specified users — model passes requesting user's ID
        raw_ids = args.get("user_ids", "").strip()
        if not raw_ids:
            return {"success": False, "error": "user_ids required (pass the requesting user's TG ID from session metadata)"}
        # M7 fix: AP-16 safe int — filter out non-numeric entries
        user_ids = [_safe_int(uid.strip(), 0) for uid in raw_ids.split(",") if uid.strip()]
        user_ids = [uid for uid in user_ids if uid != 0]
        result = await create_group(title, user_ids)
        if not result.get("success"):
            return result

        chat_id = result["chat_id"]

        # Register in unified chat registry
        chat_key = f"telegram:{chat_id}"
        if not _cr.is_registered(chat_key):
            _cr.register(chat_key, title=title, mode=mode)
        else:
            log.warning("create_group: chat %s already registered, skipping", chat_id)

        # Assign harness if specified
        if harness_label:
            try:
                from bot.harness import get_harness, set_chat_harness
                h = get_harness(harness_label)
                if h:
                    set_chat_harness(f"telegram:{chat_id}", harness_label)
                    result["harness"] = harness_label
                    if harness_label == "admin":
                        result["promoted"] = True
                else:
                    result["harness_error"] = f"Harness '{harness_label}' not found"
            except Exception as e:
                result["harness_error"] = str(e)

        result["mode"] = mode
        return result
    return await _run_with_timeout(_do(), "telegram_create_group")


@tool("telegram_leave_chat", "Leave a Telegram group chat.", {
    "chat_id": {"type": ["integer", "string"], "description": "Chat ID to leave"},
})
async def telegram_leave_chat(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.telegram import leave_chat
        raw_cid = args.get("chat_id")
        if not raw_cid:
            return {"success": False, "error": "chat_id required"}
        cid = _safe_int(raw_cid, 0)
        if cid == 0:
            return {"success": False, "error": f"Invalid chat_id: {raw_cid}"}
        return await leave_chat(cid)
    return await _run_with_timeout(_do(), "telegram_leave_chat")


# ============================================================
# FORUM TOPIC TOOLS
# ============================================================

@tool("telegram_create_topic", "Create a forum topic in a supergroup. Bot must be admin with can_manage_topics.", {
    "type": "object",
    "properties": {
        "chat_id": {"type": ["integer", "string"], "description": "Supergroup chat ID"},
        "name": {"type": "string", "description": "Topic name"},
        "icon_color": {"type": ["integer", "string"], "description": "Topic icon color as RGB integer (optional)"},
    },
    "required": ["chat_id", "name"],
})
async def telegram_create_topic(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.telegram_botapi import create_forum_topic
        chat_id = _safe_int(args.get("chat_id"), 0)
        if not chat_id:
            return {"success": False, "error": "chat_id is required"}
        name = args.get("name", "").strip()
        if not name:
            return {"success": False, "error": "name is required"}
        icon_color = None
        raw_color = args.get("icon_color")
        if raw_color is not None:
            icon_color = _safe_int(raw_color, None)
        result = await create_forum_topic(chat_id, name, icon_color=icon_color)
        if result:
            return {"success": True, **result}
        return {"success": False, "error": "Failed to create forum topic"}
    return await _run_with_timeout(_do(), "telegram_create_topic")


@tool("telegram_close_topic", "Close a forum topic (prevent new messages). Bot must be admin with can_manage_topics.", {
    "type": "object",
    "properties": {
        "chat_id": {"type": ["integer", "string"], "description": "Supergroup chat ID"},
        "message_thread_id": {"type": ["integer", "string"], "description": "Topic thread ID"},
    },
    "required": ["chat_id", "message_thread_id"],
})
async def telegram_close_topic(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.telegram_botapi import close_forum_topic
        chat_id = _safe_int(args.get("chat_id"), 0)
        thread_id = _safe_int(args.get("message_thread_id"), 0)
        if not chat_id:
            return {"success": False, "error": "chat_id is required"}
        if not thread_id:
            return {"success": False, "error": "message_thread_id is required"}
        result = await close_forum_topic(chat_id, thread_id)
        return {"success": result}
    return await _run_with_timeout(_do(), "telegram_close_topic")


@tool("telegram_reopen_topic", "Reopen a closed forum topic. Bot must be admin with can_manage_topics.", {
    "type": "object",
    "properties": {
        "chat_id": {"type": ["integer", "string"], "description": "Supergroup chat ID"},
        "message_thread_id": {"type": ["integer", "string"], "description": "Topic thread ID"},
    },
    "required": ["chat_id", "message_thread_id"],
})
async def telegram_reopen_topic(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.telegram_botapi import reopen_forum_topic
        chat_id = _safe_int(args.get("chat_id"), 0)
        thread_id = _safe_int(args.get("message_thread_id"), 0)
        if not chat_id:
            return {"success": False, "error": "chat_id is required"}
        if not thread_id:
            return {"success": False, "error": "message_thread_id is required"}
        result = await reopen_forum_topic(chat_id, thread_id)
        return {"success": result}
    return await _run_with_timeout(_do(), "telegram_reopen_topic")


@tool("telegram_delete_topic", "Delete a forum topic permanently (all messages in the topic will be deleted). Bot must be admin with can_manage_topics.", {
    "type": "object",
    "properties": {
        "chat_id": {"type": ["integer", "string"], "description": "Supergroup chat ID"},
        "message_thread_id": {"type": ["integer", "string"], "description": "Topic thread ID"},
    },
    "required": ["chat_id", "message_thread_id"],
})
async def telegram_delete_topic(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.telegram_botapi import delete_forum_topic
        chat_id = _safe_int(args.get("chat_id"), 0)
        thread_id = _safe_int(args.get("message_thread_id"), 0)
        if not chat_id:
            return {"success": False, "error": "chat_id is required"}
        if not thread_id:
            return {"success": False, "error": "message_thread_id is required"}
        result = await delete_forum_topic(chat_id, thread_id)
        return {"success": result}
    return await _run_with_timeout(_do(), "telegram_delete_topic")


# ============================================================
# Tool lists for server registration
# ============================================================

# Session-aware TG tools (need _session_key injection for reply threading)
SESSION_TG_TOOLS = [
    telegram_send_message, telegram_send_photo, telegram_send_document,
    telegram_send_video, telegram_send_audio, telegram_send_voice,
    telegram_send_location,
]

# Non-session TG tools (no reply threading needed)
NON_SESSION_TG_TOOLS = [
    telegram_edit_message, telegram_delete_message,
    telegram_forward_message, telegram_pin_message, telegram_unpin_message,
    telegram_get_chat_members, telegram_get_chat_history,
    telegram_set_profile_photo, telegram_update_profile,
    telegram_set_chat_photo, telegram_set_chat_title,
    telegram_create_group, telegram_leave_chat,
    # Forum topic tools
    telegram_create_topic, telegram_close_topic,
    telegram_reopen_topic, telegram_delete_topic,
]

__all__ = [
    # Helpers
    "_get_reply_threading_id", "_update_reply_threading_state",
    # Session-aware tools
    "telegram_send_message", "telegram_send_photo", "telegram_send_document",
    "telegram_send_video", "telegram_send_audio", "telegram_send_voice",
    "telegram_send_location",
    # Non-session tools
    "telegram_edit_message", "telegram_delete_message",
    "telegram_forward_message", "telegram_pin_message", "telegram_unpin_message",
    "telegram_get_chat_members", "telegram_get_chat_history",
    "telegram_set_profile_photo", "telegram_update_profile",
    "telegram_set_chat_photo", "telegram_set_chat_title",
    "telegram_create_group", "telegram_leave_chat",
    # Forum topic tools
    "telegram_create_topic", "telegram_close_topic",
    "telegram_reopen_topic", "telegram_delete_topic",
    # Tool lists
    "SESSION_TG_TOOLS", "NON_SESSION_TG_TOOLS",
]
