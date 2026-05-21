# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Onboarding database schema — DDL for user_onboarding and domain_access_requests."""

import logging

log = logging.getLogger(__name__)

ONBOARDING_DDL = """
CREATE TABLE IF NOT EXISTS user_onboarding (
    user_id TEXT PRIMARY KEY,                        -- "telegram:123456789" or "teams:aad-object-id"
    source TEXT NOT NULL DEFAULT '',                    -- platform: telegram, teams (CB-132: no default assumption)
    display_name TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',           -- pending, in_progress, complete, skipped
    current_step TEXT DEFAULT '',                     -- current wizard step ID
    completed_steps TEXT DEFAULT '[]',                -- JSON array of completed step IDs
    skipped_steps TEXT DEFAULT '[]',                  -- JSON array of skipped step IDs
    step_data TEXT DEFAULT '{}',                      -- JSON dict of per-step state (e.g. auth tokens, preferences)
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT                                 -- NULL until status=complete
);

CREATE TABLE IF NOT EXISTS domain_access_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,                            -- requesting user
    domain_id TEXT NOT NULL,                          -- knowledge domain ID
    status TEXT NOT NULL DEFAULT 'pending',           -- pending, approved, denied
    requested_at TEXT NOT NULL,
    reviewed_by TEXT DEFAULT '',                      -- admin who approved/denied
    reviewed_at TEXT,
    note TEXT DEFAULT ''                              -- admin note on approval/denial
);
"""


async def init_onboarding_db() -> None:
    """Create onboarding tables if they don't exist. Idempotent."""
    from bot.storage.db import open_db
    try:
        async with open_db() as db:
            await db.executescript(ONBOARDING_DDL)
            await db.commit()
        log.info("Onboarding DB tables initialized")
    except Exception as e:
        log.error("Failed to initialize onboarding tables: %s", e, exc_info=True)
