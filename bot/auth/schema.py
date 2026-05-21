# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""OAuth token table schema — user and chat token storage.

Creates tables for:
- user_oauth_tokens: Per-user OAuth tokens (DM sessions)
- chat_oauth_tokens: Shared OAuth tokens (group sessions)

Called from main.py lifespan after RBAC init.
"""

import logging

from bot.storage.db import open_db

log = logging.getLogger(__name__)


async def init_auth_tables():
    """Create OAuth token tables."""
    async with open_db() as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS user_oauth_tokens (
                user_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                token TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, provider)
            );

            CREATE TABLE IF NOT EXISTS chat_oauth_tokens (
                chat_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                token TEXT NOT NULL,
                set_by TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, provider)
            );
        """)
        await db.commit()
    log.info("OAuth token tables initialized (user + chat)")
