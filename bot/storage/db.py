# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Database singleton — shared aiosqlite connection with WAL mode.

Provides:
- get_db()  — returns the singleton connection (lazy-init)
- open_db() — async context manager yielding the singleton (does NOT close on exit)
- close_db() — closes the connection (call at shutdown)

WAL mode + busy_timeout are set once on first connection.
row_factory is set to aiosqlite.Row globally (supports both row["col"] and row[0]).
"""

import asyncio
import logging
from contextlib import asynccontextmanager

import aiosqlite

from config import DB_PATH, DB_BUSY_TIMEOUT

log = logging.getLogger(__name__)

_db: aiosqlite.Connection | None = None
_db_init_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    """Lazy-init the lock to avoid module-level event loop binding issues."""
    global _db_init_lock
    if _db_init_lock is None:
        _db_init_lock = asyncio.Lock()  # safe: asyncio is single-threaded, no preemption here
    return _db_init_lock


async def get_db() -> aiosqlite.Connection:
    """Return the shared singleton DB connection (lazy-init with WAL + busy_timeout)."""
    global _db
    if _db is not None:
        return _db
    async with _get_lock():
        if _db is not None:  # double-check after acquiring lock
            return _db
        conn = await aiosqlite.connect(str(DB_PATH))
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT}")
        await conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = aiosqlite.Row
        log.info(f"DB singleton opened: {DB_PATH} (WAL, busy_timeout={DB_BUSY_TIMEOUT}ms)")
        _db = conn
        return _db


@asynccontextmanager
async def open_db():
    """Yield the singleton DB connection. Does NOT close on exit.

    Drop-in replacement for ``async with aiosqlite.connect(...) as db:``.
    The connection lifecycle is managed by get_db/close_db, not by this context manager.
    """
    yield await get_db()


async def close_db():
    """Close the singleton connection. Call once at shutdown."""
    global _db
    async with _get_lock():
        if _db is not None:
            try:
                await _db.close()
            except Exception as e:
                log.warning(f"Error closing DB: {e}")
            _db = None
            log.info("DB singleton closed")
