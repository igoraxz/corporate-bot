# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Telegram message parsing — converts Pyrogram Messages to normalized dicts.

Extracted from integrations/telegram.py. Pure extraction, no behavior changes.

Note: _parse_pyrogram_message uses lazy imports from integrations.telegram
for module-level state (_my_user_id, TG_BOT_USER_ID, etc.) and from
bot.chat_registry for authorization checks.
"""

import logging

from pyrogram.types import Message

from integrations.tg_media import _extract_media_meta

log = logging.getLogger(__name__)

__all__ = ["_parse_pyrogram_message"]


def _parse_pyrogram_message(message: Message) -> dict | None:
    """Parse a Pyrogram Message into the normalized dict expected by main.py.

    Returns None if the message should be ignored.
    Returns dict with: source, user_id, user_name, text, message_id, chat_id,
                       has_media, location, contact, reply_to, forward, raw, etc.
    """
    # Lazy imports from telegram module for backward compat with test patching
    import integrations.telegram as _tg

    if not message:
        return None

    chat_id = message.chat.id if message.chat else 0
    user = message.from_user
    user_id = user.id if user else 0
    text = message.text or message.caption or ""

    # Skip own messages
    if user_id == _tg._my_user_id:
        return None

    # Determine chat type via unified chat_registry
    import bot.chat_registry as _cr
    _chat_key = f"telegram:{chat_id}"
    is_external_chat = False
    if not _cr.is_registered(_chat_key):
        # Not in registry — check if user is admin (admin DMs always authorized)
        from config import is_admin_user
        if not is_admin_user("telegram", str(user_id)):
            return None
    else:
        # Registered — check if it's a non-primary chat (external)
        _cr_entry = _cr.get(_chat_key)
        if _cr_entry and _cr_entry.get("mode") in ("active", "silent"):
            is_external_chat = True

    # Check for media
    has_media = bool(
        message.photo or message.document or message.sticker or
        message.voice or message.video_note or message.video or
        message.animation or message.audio
    )
    has_location = bool(message.location or message.venue)
    has_contact = bool(message.contact)

    # No content
    if not text and not has_media and not has_location and not has_contact:
        return None

    is_bot = user.is_bot if user else False

    # Bot messages — store for context but don't process
    if is_bot or user_id in (_tg.TG_BOT_USER_ID, _tg.TG_MCP_BOT_USER_ID):
        return {
            "source": "telegram",
            "is_bot_message": True,
            "is_external_chat": is_external_chat,
            "user_name": user.first_name if user else "Bot",
            "text": text or "[media]",
            "chat_id": chat_id,
            "date": int(message.date.timestamp()) if message.date else 0,
        }

    # In external chats: non-authorized users' messages are context, not commands.
    # USER-level check: is this specific user a registered member (DM registered or admin)?
    try:
        from config import is_admin_user as _is_admin_fn
        _is_admin = _is_admin_fn("telegram", str(user_id))
    except Exception:
        _is_admin = False
    _user_is_authorized = (
        _cr.is_registered(f"telegram:{user_id}")  # user's DM is registered
        or _is_admin  # root admin
    )
    if not _user_is_authorized:
        if is_external_chat:
            sender_name = user.first_name if user else "Unknown"
            return {
                "source": "telegram",
                "is_bot_message": False,
                "is_external_chat": True,
                "is_external_user": True,
                "user_id": user_id,
                "user_name": sender_name,
                "text": text,
                "message_id": message.id,
                "chat_id": chat_id,
                "has_media": has_media,
                "location": None,
                "contact": None,
                "reply_to": None,
                "forward": None,
                "raw": _extract_media_meta(message) if has_media else None,
                "date": int(message.date.timestamp()) if message.date else 0,
            }
        log.warning(f"Ignoring TG from unauthorized user {user_id}")
        return None

    # Extract reply-to context
    reply_context = None
    if message.reply_to_message:
        rt = message.reply_to_message
        rt_user = rt.from_user
        rt_text = rt.text or rt.caption or ""
        rt_has_media = bool(
            rt.photo or rt.document or rt.sticker or rt.voice or
            rt.video_note or rt.video or rt.animation or rt.audio
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
        from config import REPLY_QUOTE_MAX_CHARS as _RQMAX
        _rt_text_capped = rt_text[:_RQMAX]
        if len(rt_text) > _RQMAX:
            _rt_text_capped = _rt_text_capped + "…[truncated]"
        reply_context = {
            "message_id": rt.id,
            "user_id": rt_user.id if rt_user else 0,
            "is_bot": (rt_user.is_bot if rt_user else False) or
                      (rt_user.id == _tg._my_user_id if rt_user else False),
            "text": _rt_text_capped,
            "has_media": rt_has_media,
            "location": reply_location,
            "contact": reply_contact,
            "raw": _extract_media_meta(rt) if rt_has_media else None,
        }

    # Extract forward context
    # Use forward_origin if available (newer Pyrogram), fall back to forward_from (pyrotgfork 2.x)
    forward_context = None
    fwd = getattr(message, "forward_origin", None)
    if fwd:
        fwd_type = getattr(fwd, "type", "")
        if fwd_type == "user" and getattr(fwd, "sender_user", None):
            forward_context = {
                "type": "user",
                "sender_name": fwd.sender_user.first_name or "",
                "sender_id": fwd.sender_user.id,
            }
        elif fwd_type == "hidden_user":
            forward_context = {
                "type": "hidden_user",
                "sender_name": getattr(fwd, "sender_user_name", "") or "",
            }
        elif fwd_type in ("chat", "channel"):
            chat_obj = getattr(fwd, "chat", None) or getattr(fwd, "sender_chat", None)
            forward_context = {
                "type": "channel",
                "sender_name": (chat_obj.title if chat_obj else "") or "",
            }
    elif getattr(message, "forward_from", None):
        forward_context = {
            "type": "user",
            "sender_name": message.forward_from.first_name or "",
            "sender_id": message.forward_from.id,
        }
    elif getattr(message, "forward_sender_name", None):
        forward_context = {
            "type": "hidden_user",
            "sender_name": message.forward_sender_name,
        }
    elif getattr(message, "forward_from_chat", None):
        forward_context = {
            "type": "channel",
            "sender_name": message.forward_from_chat.title or "",
        }

    # Extract location/venue
    location_data = None
    if message.venue:
        loc = message.venue.location
        if loc:
            location_data = {
                "latitude": loc.latitude,
                "longitude": loc.longitude,
                "title": message.venue.title or "",
                "address": message.venue.address or "",
            }
    elif message.location:
        location_data = {
            "latitude": message.location.latitude,
            "longitude": message.location.longitude,
            "live_period": getattr(message.location, "live_period", None),
        }

    # Extract contact
    contact_data = None
    if message.contact:
        contact_data = {
            "phone_number": message.contact.phone_number or "",
            "first_name": message.contact.first_name or "",
            "last_name": message.contact.last_name or "",
            "vcard": message.contact.vcard or "",
        }

    return {
        "source": "telegram",
        "is_bot_message": False,
        "is_external_chat": is_external_chat,
        "user_id": user_id,
        "user_name": (user.first_name if user else "Unknown"),
        "text": text,
        "message_id": message.id,
        "chat_id": chat_id,
        "has_media": has_media,
        "location": location_data,
        "contact": contact_data,
        "reply_to": reply_context,
        "forward": forward_context,
        "raw": _extract_media_meta(message) if has_media else None,
        "date": int(message.date.timestamp()) if message.date else 0,
    }
