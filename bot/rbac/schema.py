# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""RBAC SQLite DDL — tables, indexes, and initialization.

Tables:
- rbac_roles: role definitions (bot_admin, employee, custom)
- rbac_policies: privilege grants/denials per role
- rbac_assignments: subject-to-role bindings (user, team, chat)
- rbac_access_log: audit trail of access decisions
"""

import logging

from bot.storage.db import open_db

log = logging.getLogger(__name__)

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS rbac_roles (
    role_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    is_builtin BOOLEAN DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rbac_policies (
    policy_id INTEGER PRIMARY KEY AUTOINCREMENT,
    role_id TEXT NOT NULL,
    privilege TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT DEFAULT '*',
    effect TEXT DEFAULT 'allow',
    conditions TEXT DEFAULT '{}',
    FOREIGN KEY (role_id) REFERENCES rbac_roles(role_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rbac_assignments (
    assignment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    role_id TEXT NOT NULL,
    granted_by TEXT DEFAULT '',
    granted_at TEXT NOT NULL,
    expires_at TEXT,
    FOREIGN KEY (role_id) REFERENCES rbac_roles(role_id) ON DELETE CASCADE,
    UNIQUE(subject_type, subject_id, role_id)
);

CREATE TABLE IF NOT EXISTS rbac_access_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    privilege TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    effect TEXT NOT NULL,
    policy_id INTEGER,
    role_id TEXT DEFAULT '',
    session_key TEXT DEFAULT '',
    tool_name TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_rbac_assignments_subject
    ON rbac_assignments(subject_type, subject_id);
CREATE INDEX IF NOT EXISTS idx_rbac_policies_role
    ON rbac_policies(role_id);
CREATE INDEX IF NOT EXISTS idx_rbac_access_log_ts
    ON rbac_access_log(timestamp);
"""


async def init_rbac_db() -> None:
    """Create RBAC tables and indexes if they don't exist.

    Safe to call multiple times — uses CREATE IF NOT EXISTS.
    """
    async with open_db() as db:
        await db.executescript(_SCHEMA_DDL)
        await db.commit()
        log.info("RBAC schema initialized")
