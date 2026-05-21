# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Relay transport abstraction — platform-agnostic message sending for relay channels.

Routes messages to Telegram or Teams based on the relay chat's source platform.
All relay UX code should use these functions instead of importing from
integrations.telegram directly.
"""

import logging
from typing import Any

log = logging.getLogger(__name__)


async def relay_send_message(
    chat_id: str | int,
    text: str,
    source: str = "telegram",
    parse_mode: str = "HTML",
    message_thread_id: int | None = None,
    **kwargs: Any,
) -> dict | int | None:
    """Send a text message to a relay chat (TG or Teams).

    Returns: message_id (TG int) or activity dict (Teams) or None on failure.
    """
    if source == "teams":
        try:
            from integrations.teams import get_client
            client = get_client()
            if client:
                result = await client.send_message(str(chat_id), text)
                return result
        except Exception as e:
            log.warning("Relay Teams send failed: %s", e)
            return None
    else:
        try:
            from integrations.telegram import send_message
            return await send_message(
                int(chat_id), text,
                parse_mode=parse_mode,
                message_thread_id=message_thread_id,
                **kwargs,
            )
        except Exception as e:
            log.warning("Relay TG send failed: %s", e)
            return None


async def relay_edit_message(
    chat_id: str | int,
    message_id: str | int,
    text: str,
    source: str = "telegram",
    parse_mode: str = "HTML",
    **kwargs: Any,
) -> bool:
    """Edit a message in a relay chat."""
    if source == "teams":
        try:
            from integrations.teams import get_client
            client = get_client()
            if client:
                await client.update_message(str(chat_id), str(message_id), text)
                return True
        except Exception as e:
            log.debug("Relay Teams edit failed: %s", e)
            return False
    else:
        try:
            from integrations.telegram import edit_message
            return await edit_message(
                int(chat_id), int(message_id), text,
                parse_mode=parse_mode,
                **kwargs,
            )
        except Exception as e:
            log.debug("Relay TG edit failed: %s", e)
            return False


async def relay_delete_message(
    chat_id: str | int,
    message_id: str | int,
    source: str = "telegram",
) -> bool:
    """Delete a message in a relay chat."""
    if source == "teams":
        try:
            from integrations.teams import get_client
            client = get_client()
            if client:
                await client.delete_activity(str(chat_id), str(message_id))
                return True
        except Exception as e:
            log.debug("Relay Teams delete failed: %s", e)
            return False
    else:
        try:
            from integrations.telegram import delete_message
            return await delete_message(int(chat_id), int(message_id))
        except Exception as e:
            log.debug("Relay TG delete failed: %s", e)
            return False


async def relay_send_document(
    chat_id: str | int,
    file_path: str,
    caption: str = "",
    source: str = "telegram",
    message_thread_id: int | None = None,
) -> dict | int | None:
    """Send a file to a relay chat."""
    if source == "teams":
        try:
            from integrations.teams import get_client
            client = get_client()
            if client:
                # Teams: send as text with file reference (full upload requires user token)
                from pathlib import Path
                fname = Path(file_path).name
                msg = f"\U0001f4ce **{fname}**"
                if caption:
                    msg += f"\n{caption}"
                return await client.send_message(str(chat_id), msg)
        except Exception as e:
            log.warning("Relay Teams document send failed: %s", e)
            return None
    else:
        try:
            from integrations.telegram import send_document
            return await send_document(
                int(chat_id), file_path,
                caption=caption,
                message_thread_id=message_thread_id,
            )
        except Exception as e:
            log.warning("Relay TG document send failed: %s", e)
            return None


def get_relay_source(chat_id: str | int) -> str:
    """Determine the platform source for a relay chat_id.

    Checks the relay registry for stored source, falls back to 'telegram'.
    Uses sync access (no lock needed — read-only).
    """
    try:
        from bot.relay.registry import _load_registry
        registry = _load_registry()
        entry = registry.get(str(chat_id))
        if entry and entry.get("source"):
            return entry["source"]
    except Exception:
        pass
    # Default: assume Telegram (backward compat)
    return "telegram"
