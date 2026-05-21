# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Parse Bot API Update objects into normalized message dicts.

Converts python-telegram-bot Update/Message objects into the same
normalized dict format as tg_parser._parse_pyrogram_message, enabling
transparent backend switching.

Key differences from Pyrogram parser:
- Uses telegram.Update (python-telegram-bot) instead of pyrogram.types.Message
- Bot API provides forward_origin (MessageOrigin) natively
- Bot API photo is a list of PhotoSize objects (largest = last)
- Bot API stickers have is_animated/is_video fields
- Forum topics supported via message_thread_id
"""

import logging

log = logging.getLogger(__name__)

__all__ = ["parse_bot_update", "parse_callback_query"]


def _extract_media_meta(msg) -> dict | None:
    """Extract JSON-serializable media metadata from a Bot API Message.

    Returns a dict with file_id, media_type, and type-specific fields,
    or None if no media. Compatible with the userbot _extract_media_meta format.
    """
    msg_id = msg.message_id

    if msg.photo:
        # Bot API photo is a list of PhotoSize — largest is last
        best = msg.photo[-1]
        return {
            "message_id": msg_id,
            "media_type": "photo",
            "file_id": best.file_id,
            "width": best.width,
            "height": best.height,
        }
    if msg.document:
        return {
            "message_id": msg_id,
            "media_type": "document",
            "file_id": msg.document.file_id,
            "file_name": msg.document.file_name or f"{msg_id}_document",
            "mime_type": msg.document.mime_type or "",
            "file_size": msg.document.file_size or 0,
        }
    if msg.animation:
        return {
            "message_id": msg_id,
            "media_type": "animation",
            "file_id": msg.animation.file_id,
        }
    if msg.video:
        return {
            "message_id": msg_id,
            "media_type": "video",
            "file_id": msg.video.file_id,
            "file_size": msg.video.file_size or 0,
        }
    if msg.sticker:
        return {
            "message_id": msg_id,
            "media_type": "sticker",
            "file_id": msg.sticker.file_id,
            "emoji": msg.sticker.emoji or "",
        }
    if msg.voice:
        return {
            "message_id": msg_id,
            "media_type": "voice",
            "file_id": msg.voice.file_id,
            "duration": msg.voice.duration or 0,
        }
    if msg.video_note:
        return {
            "message_id": msg_id,
            "media_type": "video_note",
            "file_id": msg.video_note.file_id,
        }
    if msg.audio:
        return {
            "message_id": msg_id,
            "media_type": "audio",
            "file_id": msg.audio.file_id,
            "file_name": msg.audio.file_name or f"{msg_id}_audio.mp3",
            "duration": msg.audio.duration or 0,
            "title": msg.audio.title or "",
            "performer": msg.audio.performer or "",
            "mime_type": msg.audio.mime_type or "",
            "file_size": msg.audio.file_size or 0,
        }
    return None


def parse_bot_update(update) -> dict | None:
    """Parse a telegram.Update into the same format as _parse_pyrogram_message.

    Returns dict with: text, user_id, user_name, chat_id, message_id, source,
    has_media, location, contact, reply_to, forward, raw, date, etc.

    Returns None if the update should be ignored (no message, no content).
    """
    if not update:
        return None

    # Support both message and edited_message
    msg = update.message or update.edited_message
    if not msg:
        return None

    user = msg.from_user
    if not user:
        return None

    text = msg.text or msg.caption or ""

    # Check for media
    has_media = bool(
        msg.photo or msg.document or msg.sticker
        or msg.voice or msg.video_note or msg.video
        or msg.animation or msg.audio
    )
    has_location = bool(msg.location or msg.venue)
    has_contact = bool(msg.contact)

    # No content at all
    if not text and not has_media and not has_location and not has_contact:
        return None

    # Build user name (first + last)
    user_name = user.first_name or ""
    if user.last_name:
        user_name = user_name + " " + user.last_name

    chat_id = msg.chat_id

    # Detect bot messages
    is_bot = user.is_bot

    # Extract reply-to context
    reply_context = None
    if msg.reply_to_message:
        rt = msg.reply_to_message
        rt_user = rt.from_user
        rt_text = rt.text or rt.caption or ""
        rt_has_media = bool(
            rt.photo or rt.document or rt.sticker or rt.voice
            or rt.video_note or rt.video or rt.animation or rt.audio
        )
        # Extract contact from reply-to
        reply_contact = None
        if rt.contact:
            reply_contact = {
                "phone_number": rt.contact.phone_number or "",
                "first_name": rt.contact.first_name or "",
                "last_name": rt.contact.last_name or "",
            }
        # Extract location/venue from reply-to
        reply_location = None
        if rt.venue:
            reply_location = {
                "latitude": rt.venue.location.latitude if rt.venue.location else None,
                "longitude": rt.venue.location.longitude if rt.venue.location else None,
                "title": rt.venue.title or "",
                "address": rt.venue.address or "",
            }
        elif rt.location:
            reply_location = {
                "latitude": rt.location.latitude,
                "longitude": rt.location.longitude,
                "live_period": getattr(rt.location, "live_period", None),
            }

        from config import REPLY_QUOTE_MAX_CHARS
        _rt_text_capped = rt_text[:REPLY_QUOTE_MAX_CHARS]
        if len(rt_text) > REPLY_QUOTE_MAX_CHARS:
            _rt_text_capped = _rt_text_capped + "\u2026[truncated]"

        reply_context = {
            "message_id": rt.message_id,
            "user_id": rt_user.id if rt_user else 0,
            "is_bot": (rt_user.is_bot if rt_user else False),
            "text": _rt_text_capped,
            "has_media": rt_has_media,
            "location": reply_location,
            "contact": reply_contact,
            "raw": _extract_media_meta(rt) if rt_has_media else None,
        }

    # Extract forward context
    forward_context = None
    fwd = getattr(msg, "forward_origin", None)
    if fwd:
        from telegram import MessageOriginUser, MessageOriginHiddenUser
        from telegram import MessageOriginChat, MessageOriginChannel
        if isinstance(fwd, MessageOriginUser):
            forward_context = {
                "type": "user",
                "sender_name": fwd.sender_user.first_name or "",
                "sender_id": fwd.sender_user.id,
            }
        elif isinstance(fwd, MessageOriginHiddenUser):
            forward_context = {
                "type": "hidden_user",
                "sender_name": fwd.sender_user_name or "",
            }
        elif isinstance(fwd, (MessageOriginChat, MessageOriginChannel)):
            chat_obj = getattr(fwd, "sender_chat", None) or getattr(fwd, "chat", None)
            forward_context = {
                "type": "channel",
                "sender_name": (chat_obj.title if chat_obj else "") or "",
            }
    # Legacy fallback fields (older Bot API versions)
    elif getattr(msg, "forward_from", None):
        forward_context = {
            "type": "user",
            "sender_name": msg.forward_from.first_name or "",
            "sender_id": msg.forward_from.id,
        }
    elif getattr(msg, "forward_sender_name", None):
        forward_context = {
            "type": "hidden_user",
            "sender_name": msg.forward_sender_name,
        }
    elif getattr(msg, "forward_from_chat", None):
        forward_context = {
            "type": "channel",
            "sender_name": msg.forward_from_chat.title or "",
        }

    # Extract location/venue
    location_data = None
    if msg.venue:
        loc = msg.venue.location
        if loc:
            location_data = {
                "latitude": loc.latitude,
                "longitude": loc.longitude,
                "title": msg.venue.title or "",
                "address": msg.venue.address or "",
            }
    elif msg.location:
        location_data = {
            "latitude": msg.location.latitude,
            "longitude": msg.location.longitude,
            "live_period": getattr(msg.location, "live_period", None),
        }

    # Extract contact
    contact_data = None
    if msg.contact:
        contact_data = {
            "phone_number": msg.contact.phone_number or "",
            "first_name": msg.contact.first_name or "",
            "last_name": msg.contact.last_name or "",
            "vcard": msg.contact.vcard or "",
        }

    # Determine external chat status from unified registry.
    # active_plus = team-level (not external), active/silent = external (own-chat data only).
    _is_external = False
    try:
        from bot.chat_registry import get_mode as _get_chat_mode
        _mode = _get_chat_mode(f"telegram:{chat_id}")
        if _mode and _mode != "active_plus":
            _is_external = True
    except Exception:
        _is_external = True  # Fail-closed: treat as external until registry confirms

    return {
        "source": "telegram",
        "is_bot_message": is_bot,
        "is_external_chat": _is_external,
        "user_id": user.id,
        "user_name": user_name,
        "text": text,
        "raw_text": text,  # Original text before any metadata wrapping (dispatch uses this)
        "message_id": msg.message_id,
        "chat_id": chat_id,
        "has_media": has_media,
        "location": location_data,
        "contact": contact_data,
        "reply_to": reply_context,
        "forward": forward_context,
        "raw": _extract_media_meta(msg) if has_media else None,
        "date": int(msg.date.timestamp()) if msg.date else 0,
        "message_thread_id": msg.message_thread_id,  # Forum topic support
    }


def parse_callback_query(update) -> dict | None:
    """Parse a callback query (inline button press) into a normalized dict.

    Callback queries fire when a user presses an InlineKeyboardButton with
    callback_data. The returned dict is compatible with triage_and_enqueue.

    Returns None if the update has no callback_query.
    """
    if not update:
        return None

    cq = update.callback_query
    if not cq:
        return None

    user = cq.from_user
    if not user:
        return None

    # Build user name (first + last)
    user_name = user.first_name or ""
    if user.last_name:
        user_name = user_name + " " + user.last_name

    # Extract chat context from the message the button was attached to
    chat_id = 0
    message_id = 0
    message_thread_id = None
    if cq.message:
        chat_id = cq.message.chat_id
        message_id = cq.message.message_id
        message_thread_id = getattr(cq.message, "message_thread_id", None)

    return {
        "source": "telegram",
        "type": "callback_query",
        "msg_type": "callback_query",
        "callback_query_id": cq.id,
        "text": cq.data or "",
        "data": cq.data or "",
        "user_id": user.id,
        "user_name": user_name,
        "chat_id": chat_id,
        "message_id": message_id,
        "message_thread_id": message_thread_id,
        "is_bot_message": False,
        "is_external_chat": False,  # Set by dispatch layer
        "has_media": False,
        "date": 0,
    }
