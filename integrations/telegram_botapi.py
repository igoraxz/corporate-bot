# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Telegram Bot API integration using python-telegram-bot.

Corporate deployment: official Bot API (webhook-based) instead of MTProto userbot.
Provides same interface as telegram_userbot.py for transparent switching.

Setup: @BotFather -> create bot -> get token -> set TG_BOT_TOKEN in .env

Limitations vs userbot (Pyrogram MTProto):
- Cannot create groups (bots must be added to existing groups)
- Cannot read chat history (bots only see messages sent while they are members)
- Cannot read_chat_history (no blue ticks for bots)
- Cannot update own profile (first_name/bio — managed via @BotFather)
- Cannot set own profile photo (managed via @BotFather)
- get_chat_members returns admins only (Bot API restriction)
- No FloodWait retry (Bot API uses HTTP 429 with Retry-After header)
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

__all__ = [
    # Client lifecycle
    "get_client",
    "start",
    "stop",
    "is_connected",
    "get_my_names",
    "get_flood_wait_remaining",
    "reconnect",
    # User/chat management
    "get_users_info",
    "create_group",
    "leave_chat",
    "get_chat_members",
    "get_chat_history",
    "set_chat_photo",
    "set_chat_title",
    "update_profile",
    "set_profile_photo",
    # Sending
    "TG_MSG_LIMIT",
    "_split_message",
    "send_message",
    "edit_message",
    "send_message_draft",
    "delete_message",
    "send_chat_action",
    "send_photo",
    "send_document",
    "send_video",
    "send_audio",
    "send_voice",
    "send_location",
    "forward_message",
    "pin_message",
    "unpin_message",
    # Webhook
    "set_webhook",
    "delete_webhook",
    # Media download
    "_safe_filename",
    "download_media",
    "download_file_by_id",
    # Forum topics
    "create_forum_topic",
    "close_forum_topic",
    "reopen_forum_topic",
    "delete_forum_topic",
    # Inline keyboards
    "build_inline_keyboard",
    "answer_callback_query",
    # Bot menu commands
    "set_bot_commands",
    "delete_bot_commands",
    # Parsing
    "parse_update",
    "parse_callback_query",
]

# Module state
_bot = None  # telegram.Bot instance
_app = None  # telegram.ext.Application instance
_bot_info = None  # cached getMe result
_message_callback: Callable | None = None
_start_time: float = 0.0


async def get_client():
    """Return the active Bot instance. Raises if not started."""
    if _bot is None:
        raise RuntimeError("Bot API client not started -- call start() first")
    return _bot


async def start(message_callback: Callable | None = None) -> bool:
    """Initialize Bot API client. Does NOT start polling -- webhook is used.

    Args:
        message_callback: async callable(parsed_dict) -- queues incoming messages.
                          Typically triage_and_enqueue from main.py.

    Returns True if started successfully, False otherwise.
    """
    global _bot, _app, _bot_info, _message_callback, _start_time
    import time
    _message_callback = message_callback
    _start_time = time.time()

    from config import TG_BOT_TOKEN
    if not TG_BOT_TOKEN:
        log.warning("TG_BOT_TOKEN not set -- Telegram Bot API disabled")
        return False

    try:
        from telegram.ext import Application

        _app = Application.builder().token(TG_BOT_TOKEN).updater(None).build()
        _bot = _app.bot
        await _app.initialize()
        _bot_info = await _bot.get_me()
        log.info(
            "Telegram Bot API initialized: @%s (id=%s)",
            _bot_info.username, _bot_info.id,
        )
        return True
    except Exception as e:
        log.error("Failed to init Bot API: %s", e, exc_info=True)
        return False


async def stop():
    """Shut down the Bot API client."""
    global _bot, _app, _bot_info
    if _app:
        try:
            await _app.shutdown()
        except Exception as e:
            log.warning("Error shutting down Bot API: %s", e)
    _bot = None
    _app = None
    _bot_info = None


def is_connected() -> bool:
    """Check if the Bot API client is initialized."""
    return _bot is not None


def get_my_names() -> tuple[str, str]:
    """Return (first_name, username) -- the bot's TG display name and @handle."""
    if _bot_info:
        return (_bot_info.first_name or "", _bot_info.username or "")
    return ("", "")


def get_flood_wait_remaining() -> float:
    """Return seconds remaining on any rate limit, or 0.0.

    Bot API uses HTTP 429 + Retry-After, handled internally by
    python-telegram-bot. This stub keeps interface parity.
    """
    return 0.0


async def get_users_info(user_ids: list[int]) -> list[dict]:
    """Fetch basic info for given Telegram user IDs.

    Bot API cannot bulk-fetch users. Returns empty list.
    """
    # Bot API has no getUsers equivalent -- bots can only get user info
    # from messages they receive. Return empty for interface parity.
    return []


async def create_group(title: str, user_ids: list[int]) -> dict:
    """Not available in Bot API mode.

    Bots cannot create groups. Groups must be created manually,
    then the bot is added as a member.
    """
    return {
        "success": False,
        "error": "create_group is not available in Bot API mode. "
                 "Groups must be created manually, then add the bot.",
    }


async def leave_chat(chat_id: int) -> dict:
    """Leave a Telegram group chat."""
    if not _bot:
        return {"success": False, "error": "Bot API client not initialized"}
    try:
        await _bot.leave_chat(chat_id=chat_id)
        log.info("Left TG chat via Bot API: %s", chat_id)
        return {"success": True, "chat_id": chat_id}
    except Exception as e:
        log.error("Bot API leave_chat failed: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


async def reconnect() -> bool:
    """Reconnect the Bot API client.

    Bot API is stateless HTTP -- no persistent connection to drop.
    Re-initializes the client as a health-check measure.
    """
    if _bot is None:
        log.warning("[Bot API watchdog] No client -- cannot reconnect")
        return False
    try:
        info = await _bot.get_me()
        if info:
            log.info("[Bot API watchdog] Connection OK: @%s", info.username)
            return True
        return False
    except Exception as e:
        log.error("[Bot API watchdog] Reconnect check failed: %s", e)
        return False


# === SENDING ===

TG_MSG_LIMIT = 4096


def _split_message(text: str, limit: int = TG_MSG_LIMIT) -> list[str]:
    """Split a long message into chunks that fit within Telegram's limit.

    Splits at paragraph boundaries (\\n\\n), then line boundaries (\\n),
    then sentence boundaries (. ! ?), then space boundaries,
    then hard-cuts as last resort.

    When TG_HTML_AWARE_SPLIT=1 (opt-in), also tries to keep <blockquote>
    boundaries intact.
    """
    if len(text) <= limit:
        return [text]

    _html_aware = os.environ.get("TG_HTML_AWARE_SPLIT", "0") == "1"

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        split_at = -1
        search_zone = remaining[:limit]

        # 1. Paragraph break
        idx = search_zone.rfind("\n\n")
        if idx > limit // 4:
            split_at = idx + 2

        # 2. Line break
        if split_at == -1:
            idx = search_zone.rfind("\n")
            if idx > limit // 4:
                split_at = idx + 1

        # 3. Sentence boundary (". " or "! " or "? ")
        if split_at == -1:
            for sep in [". ", "! ", "? "]:
                idx = search_zone.rfind(sep)
                if idx > limit // 4:
                    split_at = idx + len(sep)
                    break

        # 4. Space
        if split_at == -1:
            idx = search_zone.rfind(" ")
            if idx > limit // 4:
                split_at = idx + 1

        # 5. Hard cut (last resort)
        if split_at == -1:
            split_at = limit

        chunk = remaining[:split_at].rstrip()
        if _html_aware and chunk:
            opens = max(
                chunk.count("<blockquote>"),
                chunk.count("<blockquote expandable>"),
            )
            closes = chunk.count("</blockquote>")
            if opens > closes:
                chunk = chunk + "</blockquote>"
                remaining = (
                    "<blockquote expandable>\n"
                    + remaining[split_at:].lstrip("\n")
                )
                if chunk:
                    chunks.append(chunk)
                continue
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].lstrip("\n")

    return chunks if chunks else [text[:limit]]


def _to_parse_mode(parse_mode: str | None):
    """Convert string parse_mode to python-telegram-bot ParseMode constant."""
    if not parse_mode:
        return None
    from telegram.constants import ParseMode
    pm = parse_mode.upper()
    if pm == "HTML":
        return ParseMode.HTML
    if pm in ("MARKDOWN", "MARKDOWNV2"):
        return ParseMode.MARKDOWN_V2
    return None


async def _retry_on_429(coro_factory, max_retries: int = 2):
    """Execute a coroutine factory with retry on HTTP 429 (rate limit).

    python-telegram-bot handles Retry-After internally, but this provides
    an extra safety net for interface parity with _retry_on_flood.
    """
    from telegram.error import RetryAfter
    for attempt in range(max_retries + 1):
        try:
            return await coro_factory()
        except RetryAfter as e:
            if attempt < max_retries:
                wait = min(e.retry_after, 120)
                log.warning(
                    "Bot API RetryAfter %ss, waiting %ss (attempt %d)",
                    e.retry_after, wait, attempt + 1,
                )
                await asyncio.sleep(wait)
            else:
                log.error("Bot API RetryAfter exceeded retries: %ss", e.retry_after)
                raise
    return None


async def send_message(
    text: str,
    chat_id: int = 0,
    parse_mode: str | None = "HTML",
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
    reply_markup=None,
) -> dict | None:
    """Send a text message. Auto-splits if >4096 chars.

    Args:
        reply_to_message_id: If set, sends as a reply to the specified message.
            Only applied to the FIRST part (subsequent split parts are standalone).
        message_thread_id: Forum topic thread ID. Routes message to a specific topic.
        reply_markup: Optional InlineKeyboardMarkup or other reply markup.
            Only applied to the LAST part (keyboard attaches to final message).

    Returns the LAST sent message dict (for message_id tracking), or None.
    """
    if not _bot:
        return None
    if not chat_id:
        from config import TG_CHAT_ID
        chat_id = TG_CHAT_ID

    parts = _split_message(text)
    last_result = None
    pm = _to_parse_mode(parse_mode)

    for i, part in enumerate(parts):
        reply_id = reply_to_message_id if i == 0 else None
        is_last = (i == len(parts) - 1)
        try:
            kwargs: dict = {
                "chat_id": chat_id,
                "text": part,
                "parse_mode": pm,
            }
            if reply_id:
                from telegram import ReplyParameters
                kwargs["reply_parameters"] = ReplyParameters(
                    message_id=reply_id,
                )
            if message_thread_id is not None:
                kwargs["message_thread_id"] = message_thread_id
            if reply_markup and is_last:
                kwargs["reply_markup"] = reply_markup
            msg = await _retry_on_429(
                lambda kw=kwargs: _bot.send_message(**kw),
            )
            if msg:
                last_result = {"message_id": msg.message_id, "chat_id": chat_id}
        except Exception as e:
            log.error("Bot API send_message failed: %s", e)
            # If HTML parse error, retry without parse_mode
            err_str = str(e).lower()
            if "parse" in err_str or "entities" in err_str:
                try:
                    fallback_kwargs: dict = {
                        "chat_id": chat_id, "text": part, "parse_mode": None,
                    }
                    if message_thread_id is not None:
                        fallback_kwargs["message_thread_id"] = message_thread_id
                    if reply_markup and is_last:
                        fallback_kwargs["reply_markup"] = reply_markup
                    msg = await _bot.send_message(**fallback_kwargs)
                    if msg:
                        last_result = {
                            "message_id": msg.message_id, "chat_id": chat_id,
                        }
                except Exception as e2:
                    log.error("Bot API send_message fallback also failed: %s", e2)

    return last_result


async def edit_message(
    message_id: int,
    text: str,
    chat_id: int = 0,
    parse_mode: str | None = "HTML",
    reply_markup=None,
    message_thread_id: int | None = None,
) -> dict | None:
    """Edit an existing message. Returns updated message dict or None.

    Args:
        reply_markup: Optional InlineKeyboardMarkup to attach/update on the message.
        message_thread_id: Forum topic thread ID (not required by TG API for edits,
            but included for consistency with the chat key pattern).
    """
    if not _bot:
        return None
    if not chat_id:
        from config import TG_CHAT_ID
        chat_id = TG_CHAT_ID
    pm = _to_parse_mode(parse_mode)
    try:
        kwargs: dict = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:4096],
            "parse_mode": pm,
        }
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        msg = await _retry_on_429(
            lambda kw=kwargs: _bot.edit_message_text(**kw),
        )
        if msg:
            return {"message_id": msg.message_id, "chat_id": chat_id}
    except Exception as e:
        if "Message is not modified" in str(e):
            return {"message_id": message_id, "chat_id": chat_id}
        if "message to edit not found" in str(e).lower():
            raise RuntimeError(f"editMessageText: {e}")
        log.error("Bot API edit_message failed: %s", e)
    return None


async def send_message_draft(
    text: str,
    chat_id: int = 0,
    parse_mode: str | None = None,
) -> dict | None:
    """Streaming draft not supported. Returns None to trigger fallback."""
    return None


async def delete_message(message_id: int, chat_id: int = 0,
                         message_thread_id: int | None = None) -> bool:
    """Delete a message.

    message_thread_id: accepted for API consistency but not used by TG deleteMessage.
    """
    if not _bot:
        return False
    if not chat_id:
        from config import TG_CHAT_ID
        chat_id = TG_CHAT_ID
    try:
        return await _bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        log.warning("Bot API delete_message failed: %s", e)
        return False


async def send_chat_action(
    action: str = "typing",
    chat_id: int = 0,
    message_thread_id: int | None = None,
):
    """Send typing indicator or other chat action."""
    if not _bot:
        return
    if not chat_id:
        from config import TG_CHAT_ID
        chat_id = TG_CHAT_ID
    try:
        kwargs: dict = {"chat_id": chat_id, "action": action}
        if message_thread_id is not None:
            kwargs["message_thread_id"] = message_thread_id
        await _bot.send_chat_action(**kwargs)
    except Exception as e:
        log.debug("Bot API send_chat_action failed: %s", e)


async def send_photo(
    photo_path: str,
    caption: str = "",
    chat_id: int = 0,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
) -> dict | None:
    """Send a photo file."""
    if not _bot:
        return None
    if not chat_id:
        from config import TG_CHAT_ID
        chat_id = TG_CHAT_ID
    try:
        base_kwargs: dict = {
            "chat_id": chat_id,
            "caption": caption[:1024] if caption else None,
            "parse_mode": "HTML" if caption else None,
        }
        if reply_to_message_id:
            from telegram import ReplyParameters
            base_kwargs["reply_parameters"] = ReplyParameters(
                message_id=reply_to_message_id,
            )
        if message_thread_id is not None:
            base_kwargs["message_thread_id"] = message_thread_id

        async def _do():
            with open(photo_path, "rb") as f:
                return await _bot.send_photo(**base_kwargs, photo=f)

        msg = await _retry_on_429(_do)
        if msg:
            return {"message_id": msg.message_id, "chat_id": chat_id}
    except Exception as e:
        log.error("Bot API send_photo failed: %s", e)
    return None


async def send_document(
    doc_path: str,
    caption: str = "",
    chat_id: int = 0,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
) -> dict | None:
    """Send a document file."""
    if not _bot:
        return None
    if not chat_id:
        from config import TG_CHAT_ID
        chat_id = TG_CHAT_ID
    try:
        base_kwargs: dict = {
            "chat_id": chat_id,
            "caption": caption[:1024] if caption else None,
            "parse_mode": "HTML" if caption else None,
        }
        if reply_to_message_id:
            from telegram import ReplyParameters
            base_kwargs["reply_parameters"] = ReplyParameters(
                message_id=reply_to_message_id,
            )
        if message_thread_id is not None:
            base_kwargs["message_thread_id"] = message_thread_id

        async def _do():
            with open(doc_path, "rb") as f:
                return await _bot.send_document(**base_kwargs, document=f)

        msg = await _retry_on_429(_do)
        if msg:
            return {"message_id": msg.message_id, "chat_id": chat_id}
    except Exception as e:
        log.error("Bot API send_document failed: %s", e)
    return None


async def send_video(
    video_path: str,
    caption: str = "",
    chat_id: int = 0,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
) -> dict | None:
    """Send a video file."""
    if not _bot:
        return None
    if not chat_id:
        from config import TG_CHAT_ID
        chat_id = TG_CHAT_ID
    try:
        base_kwargs: dict = {
            "chat_id": chat_id,
            "caption": caption[:1024] if caption else None,
            "parse_mode": "HTML" if caption else None,
        }
        if reply_to_message_id:
            from telegram import ReplyParameters
            base_kwargs["reply_parameters"] = ReplyParameters(
                message_id=reply_to_message_id,
            )
        if message_thread_id is not None:
            base_kwargs["message_thread_id"] = message_thread_id

        async def _do():
            with open(video_path, "rb") as f:
                return await _bot.send_video(**base_kwargs, video=f)

        msg = await _retry_on_429(_do)
        if msg:
            return {"message_id": msg.message_id, "chat_id": chat_id}
    except Exception as e:
        log.error("Bot API send_video failed: %s", e)
    return None


async def send_audio(
    audio_path: str,
    caption: str = "",
    chat_id: int = 0,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
) -> dict | None:
    """Send an audio file."""
    if not _bot:
        return None
    if not chat_id:
        from config import TG_CHAT_ID
        chat_id = TG_CHAT_ID
    try:
        base_kwargs: dict = {
            "chat_id": chat_id,
            "caption": caption[:1024] if caption else None,
            "parse_mode": "HTML" if caption else None,
        }
        if reply_to_message_id:
            from telegram import ReplyParameters
            base_kwargs["reply_parameters"] = ReplyParameters(
                message_id=reply_to_message_id,
            )
        if message_thread_id is not None:
            base_kwargs["message_thread_id"] = message_thread_id

        async def _do():
            with open(audio_path, "rb") as f:
                return await _bot.send_audio(**base_kwargs, audio=f)

        msg = await _retry_on_429(_do)
        if msg:
            return {"message_id": msg.message_id, "chat_id": chat_id}
    except Exception as e:
        log.error("Bot API send_audio failed: %s", e)
    return None


async def send_voice(
    voice_path: str,
    caption: str = "",
    chat_id: int = 0,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
) -> dict | None:
    """Send a voice message."""
    if not _bot:
        return None
    if not chat_id:
        from config import TG_CHAT_ID
        chat_id = TG_CHAT_ID
    try:
        base_kwargs: dict = {
            "chat_id": chat_id,
            "caption": caption[:1024] if caption else None,
            "parse_mode": "HTML" if caption else None,
        }
        if reply_to_message_id:
            from telegram import ReplyParameters
            base_kwargs["reply_parameters"] = ReplyParameters(
                message_id=reply_to_message_id,
            )
        if message_thread_id is not None:
            base_kwargs["message_thread_id"] = message_thread_id

        async def _do():
            with open(voice_path, "rb") as f:
                return await _bot.send_voice(**base_kwargs, voice=f)

        msg = await _retry_on_429(_do)
        if msg:
            return {"message_id": msg.message_id, "chat_id": chat_id}
    except Exception as e:
        log.error("Bot API send_voice failed: %s", e)
    return None


async def send_location(
    latitude: float,
    longitude: float,
    chat_id: int = 0,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
) -> dict | None:
    """Send a location pin."""
    if not _bot:
        return None
    if not chat_id:
        from config import TG_CHAT_ID
        chat_id = TG_CHAT_ID
    try:
        kwargs: dict = {
            "chat_id": chat_id,
            "latitude": latitude,
            "longitude": longitude,
        }
        if reply_to_message_id:
            from telegram import ReplyParameters
            kwargs["reply_parameters"] = ReplyParameters(
                message_id=reply_to_message_id,
            )
        if message_thread_id is not None:
            kwargs["message_thread_id"] = message_thread_id
        msg = await _retry_on_429(
            lambda kw=kwargs: _bot.send_location(**kw),
        )
        if msg:
            return {"message_id": msg.message_id, "chat_id": chat_id}
    except Exception as e:
        log.error("Bot API send_location failed: %s", e)
    return None


async def forward_message(
    from_chat_id: int,
    message_id: int,
    to_chat_id: int = 0,
    message_thread_id: int | None = None,
) -> dict | None:
    """Forward a message from one chat to another."""
    if not _bot:
        return None
    if not to_chat_id:
        from config import TG_CHAT_ID
        to_chat_id = TG_CHAT_ID
    try:
        kwargs: dict = {
            "chat_id": to_chat_id,
            "from_chat_id": from_chat_id,
            "message_id": message_id,
        }
        if message_thread_id is not None:
            kwargs["message_thread_id"] = message_thread_id
        msg = await _retry_on_429(
            lambda kw=kwargs: _bot.forward_message(**kw),
        )
        if msg:
            return {"message_id": msg.message_id, "chat_id": to_chat_id}
    except Exception as e:
        log.error("Bot API forward_message failed: %s", e)
    return None


async def pin_message(
    message_id: int,
    chat_id: int = 0,
    both_sides: bool = True,
    disable_notification: bool = False,
    message_thread_id: int | None = None,
) -> bool:
    """Pin a message in a chat."""
    if not _bot:
        return False
    if not chat_id:
        from config import TG_CHAT_ID
        chat_id = TG_CHAT_ID
    try:
        await _retry_on_429(
            lambda: _bot.pin_chat_message(
                chat_id=chat_id,
                message_id=message_id,
                disable_notification=disable_notification,
            ),
        )
        return True
    except Exception as e:
        log.error("Bot API pin_message failed: %s", e)
        return False


async def unpin_message(message_id: int, chat_id: int = 0,
                        message_thread_id: int | None = None) -> bool:
    """Unpin a message in a chat."""
    if not _bot:
        return False
    if not chat_id:
        from config import TG_CHAT_ID
        chat_id = TG_CHAT_ID
    try:
        await _retry_on_429(
            lambda: _bot.unpin_chat_message(
                chat_id=chat_id,
                message_id=message_id,
            ),
        )
        return True
    except Exception as e:
        log.error("Bot API unpin_message failed: %s", e)
        return False


async def get_chat_members(chat_id: int = 0, limit: int = 200) -> list[dict]:
    """Get administrators of a group/channel.

    Bot API only provides getChatAdministrators -- cannot enumerate
    all members. Returns admins only.
    """
    if not _bot:
        return []
    if not chat_id:
        from config import TG_CHAT_ID
        chat_id = TG_CHAT_ID
    try:
        admins = await _bot.get_chat_administrators(chat_id=chat_id)
        members = []
        for admin in admins:
            user = admin.user
            members.append({
                "user_id": user.id,
                "first_name": user.first_name or "",
                "last_name": user.last_name or "",
                "username": user.username or "",
                "status": admin.status if hasattr(admin, "status") else "administrator",
            })
        return members
    except Exception as e:
        log.error("Bot API get_chat_members failed: %s", e)
        return []


async def get_chat_history(chat_id: int = 0, limit: int = 20) -> list[dict]:
    """Not available in Bot API mode.

    Bots cannot read chat history -- they only see messages addressed
    to them or in groups where they receive updates.
    """
    return []


async def set_chat_photo(photo_path: str, chat_id: int = 0) -> dict:
    """Set a group/channel photo."""
    if not _bot:
        return {"success": False, "error": "Bot API client not initialized"}
    if not chat_id:
        from config import TG_CHAT_ID
        chat_id = TG_CHAT_ID
    try:
        async def _do():
            with open(photo_path, "rb") as f:
                await _bot.set_chat_photo(chat_id=chat_id, photo=f)

        await _retry_on_429(_do)
        return {"success": True}
    except Exception as e:
        log.error("Bot API set_chat_photo failed: %s", e)
        return {"success": False, "error": str(e)}


async def set_chat_title(title: str, chat_id: int = 0) -> bool:
    """Set a group/channel title."""
    if not _bot:
        return False
    if not chat_id:
        from config import TG_CHAT_ID
        chat_id = TG_CHAT_ID
    try:
        await _retry_on_429(
            lambda: _bot.set_chat_title(chat_id=chat_id, title=title),
        )
        return True
    except Exception as e:
        log.error("Bot API set_chat_title failed: %s", e)
        return False


async def update_profile(
    first_name: str,
    last_name: str = "",
    bio: str | None = None,
) -> bool:
    """Not available in Bot API mode.

    Bot profile is managed via @BotFather, not the API.
    """
    log.info(
        "update_profile called but not available in Bot API mode "
        "(use @BotFather to update bot profile)",
    )
    return False


async def set_profile_photo(photo_path: str) -> bool:
    """Not available in Bot API mode.

    Bot profile photo is managed via @BotFather, not the API.
    """
    log.info(
        "set_profile_photo called but not available in Bot API mode "
        "(use @BotFather to update bot photo)",
    )
    return False


# === WEBHOOK MANAGEMENT ===

async def set_webhook(url: str) -> bool:
    """Set the webhook URL for receiving updates."""
    if not _bot:
        return False
    try:
        from config import TG_WEBHOOK_SECRET
        kwargs: dict = {"url": url}
        if TG_WEBHOOK_SECRET:
            kwargs["secret_token"] = TG_WEBHOOK_SECRET
        kwargs["allowed_updates"] = [
            "message", "callback_query", "my_chat_member",
        ]
        result = await _bot.set_webhook(**kwargs)
        log.info("Bot API webhook set: %s (result=%s)", url, result)
        return bool(result)
    except Exception as e:
        log.error("Bot API set_webhook failed: %s", e)
        return False


async def delete_webhook() -> bool:
    """Remove the webhook (switch to getUpdates mode or just disable)."""
    if not _bot:
        return False
    try:
        result = await _bot.delete_webhook()
        log.info("Bot API webhook deleted (result=%s)", result)
        return bool(result)
    except Exception as e:
        log.error("Bot API delete_webhook failed: %s", e)
        return False


# === MEDIA DOWNLOAD ===

def _safe_filename(name: str, fallback: str = "file") -> str:
    """Sanitize a filename to prevent path traversal."""
    safe = os.path.basename(name)
    safe = "".join(c for c in safe if c.isprintable() and c != "\x00")
    return safe.strip() or fallback


async def download_media(message) -> dict | None:
    """Download media from a Bot API message or metadata dict.

    Accepts:
    - dict with file_id and media_type (from parse_bot_update or metadata)
    - str (broken serialization fallback -- logs warning, returns None)

    Returns dict with: local_path, media_type, filename, file_size, file_id.
    """
    if isinstance(message, str):
        log.warning("download_media: got str instead of dict, skipping")
        return None
    if not isinstance(message, dict):
        log.warning("download_media: unexpected type %s, skipping", type(message))
        return None

    file_id = message.get("file_id")
    media_type = message.get("media_type")
    if not file_id:
        return None

    msg_id = message.get("message_id", 0)
    media_type = media_type or "document"
    filename = message.get("file_name") or message.get("filename")

    if not filename:
        ext_map = {
            "photo": "jpg", "document": "bin", "animation": "mp4",
            "video": "mp4", "sticker": "webp", "voice": "ogg",
            "video_note": "mp4", "audio": "mp3",
        }
        ext = ext_map.get(media_type, "bin")
        filename = f"{msg_id}_{media_type}.{ext}"

    filename = _safe_filename(filename, f"{msg_id}_{media_type}")

    return await download_file_by_id(file_id, filename, media_type=media_type)


async def download_file_by_id(
    file_id: str,
    filename: str = "refetched",
    media_type: str = "document",
) -> dict | None:
    """Download a file from Telegram by file_id using Bot API.

    Bot API two-step process:
    1. bot.get_file(file_id) -> File object with file_path
    2. file.download_to_drive(local_path)

    Returns dict with local_path, media_type, filename, file_size, file_id.
    """
    if not _bot:
        return None
    from config import TMP_DIR

    filename = _safe_filename(filename, "refetched")
    local_path = str(TMP_DIR / filename)

    # Verify path doesn't escape TMP_DIR
    if not Path(local_path).resolve().is_relative_to(TMP_DIR.resolve()):
        log.error("Path traversal blocked: %s", local_path)
        return None

    try:
        tg_file = await _bot.get_file(file_id)
        await tg_file.download_to_drive(local_path)

        lp = Path(local_path)
        if not lp.exists():
            log.warning("Bot API download completed but file not found: %s", local_path)
            return None
        file_size = lp.stat().st_size
        log.info("Bot API downloaded %s: %s (%d bytes)", media_type, filename, file_size)
        return {
            "local_path": local_path,
            "media_type": media_type,
            "filename": filename,
            "file_size": file_size,
            "file_id": file_id,
        }
    except Exception as e:
        log.error("Bot API download failed: %s", e)
        return None


# === FORUM TOPIC MANAGEMENT ===

async def create_forum_topic(
    chat_id: int,
    name: str,
    icon_color: int | None = None,
) -> dict | None:
    """Create a forum topic in a supergroup.

    Bot must be admin with can_manage_topics permission.
    Returns {"message_thread_id": int, "name": str, ...} or None on failure.
    """
    if not _bot:
        return None
    try:
        kwargs: dict = {"chat_id": chat_id, "name": name}
        if icon_color is not None:
            kwargs["icon_color"] = icon_color
        result = await _retry_on_429(
            lambda kw=kwargs: _bot.create_forum_topic(**kw),
        )
        if result:
            return {
                "message_thread_id": result.message_thread_id,
                "name": result.name,
                "icon_color": result.icon_color,
            }
    except Exception as e:
        log.error("Bot API create_forum_topic failed: %s", e, exc_info=True)
    return None


async def close_forum_topic(chat_id: int, message_thread_id: int) -> bool:
    """Close a forum topic (prevent new messages).

    Bot must be admin with can_manage_topics permission.
    """
    if not _bot:
        return False
    try:
        await _retry_on_429(
            lambda: _bot.close_forum_topic(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
            ),
        )
        return True
    except Exception as e:
        log.error("Bot API close_forum_topic failed: %s", e, exc_info=True)
        return False


async def reopen_forum_topic(chat_id: int, message_thread_id: int) -> bool:
    """Reopen a closed forum topic.

    Bot must be admin with can_manage_topics permission.
    """
    if not _bot:
        return False
    try:
        await _retry_on_429(
            lambda: _bot.reopen_forum_topic(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
            ),
        )
        return True
    except Exception as e:
        log.error("Bot API reopen_forum_topic failed: %s", e, exc_info=True)
        return False


async def delete_forum_topic(chat_id: int, message_thread_id: int) -> bool:
    """Delete a forum topic permanently.

    Bot must be admin with can_manage_topics permission.
    All messages in the topic will be deleted.
    """
    if not _bot:
        return False
    try:
        await _retry_on_429(
            lambda: _bot.delete_forum_topic(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
            ),
        )
        return True
    except Exception as e:
        log.error("Bot API delete_forum_topic failed: %s", e, exc_info=True)
        return False


# === INLINE KEYBOARD HELPERS ===

def build_inline_keyboard(buttons: list[list[dict]]):
    """Build an inline keyboard from a list of button rows.

    Args:
        buttons: Nested list of button dicts. Each inner list is a row.
            Button dict keys: "text" (required), "callback_data" (optional,
            defaults to text), "url" (optional, for link buttons).

    Example:
        build_inline_keyboard([
            [{"text": "Yes", "callback_data": "confirm_yes"},
             {"text": "No", "callback_data": "confirm_no"}],
            [{"text": "Help", "url": "https://example.com/help"}],
        ])
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = []
    for row in buttons:
        kb_row = []
        for b in row:
            btn_kwargs: dict = {"text": b["text"]}
            if "url" in b:
                btn_kwargs["url"] = b["url"]
            elif "callback_data" in b:
                btn_kwargs["callback_data"] = b["callback_data"]
            else:
                btn_kwargs["callback_data"] = b["text"]
            kb_row.append(InlineKeyboardButton(**btn_kwargs))
        keyboard.append(kb_row)
    return InlineKeyboardMarkup(keyboard)


async def answer_callback_query(
    callback_query_id: str,
    text: str | None = None,
    show_alert: bool = False,
) -> bool:
    """Answer a callback query (inline button press).

    Must be called to remove the loading indicator on the button.
    Optionally shows a notification or alert to the user.
    """
    if not _bot:
        return False
    try:
        kwargs: dict = {"callback_query_id": callback_query_id}
        if text:
            kwargs["text"] = text
        if show_alert:
            kwargs["show_alert"] = True
        await _bot.answer_callback_query(**kwargs)
        return True
    except Exception as e:
        log.error("Bot API answer_callback_query failed: %s", e, exc_info=True)
        return False


# === BOT MENU COMMANDS ===

async def set_bot_commands(
    commands: list[dict],
    scope: str = "default",
    chat_id: int | None = None,
) -> bool:
    """Register bot menu commands visible in the Telegram UI.

    Args:
        commands: List of {"command": "name", "description": "Help text"} dicts.
        scope: One of "default", "all_private_chats", "all_group_chats", "chat".
            "chat" requires chat_id parameter.
        chat_id: Required when scope="chat". Sets commands for a specific chat.

    Returns True on success, False on failure.
    """
    if not _bot:
        return False
    try:
        from telegram import (
            BotCommand,
            BotCommandScopeDefault,
            BotCommandScopeAllPrivateChats,
            BotCommandScopeAllGroupChats,
            BotCommandScopeChat,
        )
        bot_commands = [
            BotCommand(c["command"], c["description"]) for c in commands
        ]
        if scope == "all_private_chats":
            scope_obj = BotCommandScopeAllPrivateChats()
        elif scope == "all_group_chats":
            scope_obj = BotCommandScopeAllGroupChats()
        elif scope == "chat" and chat_id:
            scope_obj = BotCommandScopeChat(chat_id=chat_id)
        else:
            scope_obj = BotCommandScopeDefault()

        await _bot.set_my_commands(bot_commands, scope=scope_obj)
        log.info("Bot menu commands set (scope=%s, count=%d)", scope, len(commands))
        return True
    except Exception as e:
        log.error("Bot API set_bot_commands failed: %s", e, exc_info=True)
        return False


async def delete_bot_commands(scope: str = "default", chat_id: int | None = None) -> bool:
    """Remove all bot menu commands for a given scope."""
    if not _bot:
        return False
    try:
        from telegram import (
            BotCommandScopeDefault,
            BotCommandScopeAllPrivateChats,
            BotCommandScopeAllGroupChats,
            BotCommandScopeChat,
        )
        if scope == "all_private_chats":
            scope_obj = BotCommandScopeAllPrivateChats()
        elif scope == "all_group_chats":
            scope_obj = BotCommandScopeAllGroupChats()
        elif scope == "chat" and chat_id:
            scope_obj = BotCommandScopeChat(chat_id=chat_id)
        else:
            scope_obj = BotCommandScopeDefault()

        await _bot.delete_my_commands(scope=scope_obj)
        log.info("Bot menu commands deleted (scope=%s)", scope)
        return True
    except Exception as e:
        log.error("Bot API delete_bot_commands failed: %s", e, exc_info=True)
        return False


# === PARSING STUBS (webhook parsing happens in tg_parser_botapi.py) ===

def parse_update(update: dict) -> dict | None:
    """Parse a raw webhook update dict.

    Delegates to tg_parser_botapi.parse_bot_update for actual parsing.
    This stub exists for import compatibility.
    """
    from integrations.tg_parser_botapi import parse_bot_update
    from telegram import Update
    update_obj = Update.de_json(update, _bot) if _bot else None
    if update_obj:
        return parse_bot_update(update_obj)
    return None


def parse_callback_query(update: dict) -> dict | None:
    """Parse a callback query from an inline keyboard button press.

    Bot API supports inline keyboards (unlike userbot mode).
    """
    if not update:
        return None
    cq = update.get("callback_query")
    if not cq:
        return None
    user = cq.get("from", {})
    return {
        "type": "callback_query",
        "callback_query_id": cq.get("id"),
        "data": cq.get("data", ""),
        "user_id": user.get("id"),
        "user_name": user.get("first_name", ""),
        "chat_id": cq.get("message", {}).get("chat", {}).get("id"),
        "message_id": cq.get("message", {}).get("message_id"),
    }


# === RE-EXPORTS for backward compatibility ===
# tg_parser and tg_media functions are NOT available in Bot API mode.
# Callers that import from integrations.telegram will get these from
# the factory module which re-exports from the active backend.
# Media extraction uses Bot API-specific parsing in tg_parser_botapi.py.

# Valid media types (same as userbot for interface parity)
_VALID_MEDIA_TYPES = {
    "photo", "document", "animation", "video",
    "sticker", "voice", "video_note", "audio",
}
