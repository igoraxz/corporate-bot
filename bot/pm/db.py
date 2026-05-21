# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""PM database singleton — own aiosqlite connection, separate from conversations.db.

Same pattern as bot/storage/db.py: lazy-init with asyncio.Lock, WAL mode, busy_timeout.
Stores all PM data (projects, sprints, tickets, transitions, agent_runs) in pm.db.
"""

import asyncio
import logging

import aiosqlite

from config import PM_DB_PATH, DB_BUSY_TIMEOUT

log = logging.getLogger(__name__)

_db: aiosqlite.Connection | None = None
_db_init_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    """Lazy-init the lock to avoid module-level event loop binding issues."""
    global _db_init_lock
    if _db_init_lock is None:
        _db_init_lock = asyncio.Lock()
    return _db_init_lock


async def get_pm_db() -> aiosqlite.Connection:
    """Return the shared singleton PM DB connection (lazy-init with WAL + busy_timeout)."""
    global _db
    if _db is not None:
        return _db
    async with _get_lock():
        if _db is not None:
            return _db
        conn = await aiosqlite.connect(str(PM_DB_PATH))
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT}")
        await conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = aiosqlite.Row
        log.info(f"PM DB singleton opened: {PM_DB_PATH} (WAL, busy_timeout={DB_BUSY_TIMEOUT}ms)")
        _db = conn
        # Initialize schema on first connection
        from bot.pm.schema import init_pm_db
        await init_pm_db(conn)
        return _db


async def close_pm_db():
    """Close the singleton PM connection. Call once at shutdown."""
    global _db
    async with _get_lock():
        if _db is not None:
            try:
                await _db.close()
            except Exception as e:
                log.warning(f"Error closing PM DB: {e}")
            _db = None
            log.info("PM DB singleton closed")
