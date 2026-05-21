# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Telegram integration factory -- routes to userbot or Bot API based on TG_MODE.

TG_MODE=userbot: Pyrogram MTProto (family/personal deployment)
TG_MODE=bot:     Official Bot API via python-telegram-bot (corporate deployment)

Default: "bot" for corporate deployments.

Usage:
    from integrations.telegram import send_message, start, stop, is_connected
    # All functions are transparently routed to the active backend.
"""
import os

TG_MODE = os.environ.get("TG_MODE", "bot").lower().strip()
if TG_MODE not in ("bot", "userbot"):
    import logging as _log
    _log.getLogger(__name__).warning("Invalid TG_MODE='%s' — defaulting to 'bot'", TG_MODE)
    TG_MODE = "bot"

if TG_MODE == "userbot":
    from integrations.telegram_userbot import *  # noqa: F401,F403
else:
    from integrations.telegram_botapi import *  # noqa: F401,F403
