# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""PM schema — DDL statements, constants, and schema versioning.

Tables (Phase 0):
  pm_meta         — schema version tracking
  pm_projects     — top-level project containers
  pm_sprints      — completion-gated sprint batches within projects
  pm_tickets      — work items within sprints
  pm_transitions  — ticket state change audit log
  pm_agent_runs   — agent execution records (cost, tokens, duration)

Tables (Phase 5 — v3 migration):
  pm_deploys        — deploy event records (commit range, status)
  pm_deploy_tickets — many-to-many link between deploys and tickets
"""

import logging

import aiosqlite

log = logging.getLogger(__name__)

# Current schema version — bump on every migration
SCHEMA_VERSION = 5

# ============================================================
# STATUS CONSTANTS
# ============================================================

VALID_PROJECT_STATUSES = frozenset({"active", "completed", "archived"})
VALID_SPRINT_STATUSES = frozenset({"draft", "approved", "running", "completed", "aborted"})
VALID_TICKET_STATUSES = frozenset({"backlog", "ready", "in_progress", "review", "done", "dropped"})

# Allowed ticket status transitions (from_status -> set of valid to_statuses)
# H3: in_progress→ready enables recovery of stuck tickets (pipeline failure → reset)
# review→ready enables full re-review cycle (review→ready→in_progress→review)
TICKET_TRANSITIONS: dict[str, frozenset[str]] = {
    "backlog": frozenset({"ready", "dropped"}),
    "ready": frozenset({"in_progress", "backlog", "dropped"}),
    "in_progress": frozenset({"review", "ready", "dropped"}),
    "review": frozenset({"done", "in_progress", "ready", "dropped"}),
    "done": frozenset(),  # terminal
    "dropped": frozenset(),  # terminal
}

# Allowed sprint status transitions
SPRINT_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"approved", "aborted"}),
    "approved": frozenset({"running", "aborted"}),
    "running": frozenset({"completed", "aborted"}),
    "completed": frozenset(),  # terminal
    "aborted": frozenset(),  # terminal
}

# ============================================================
# DDL
# ============================================================

_DDL = """
-- Schema version tracking
CREATE TABLE IF NOT EXISTS pm_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Projects: top-level containers scoped to a coding chat
CREATE TABLE IF NOT EXISTS pm_projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_key TEXT NOT NULL,
    name TEXT NOT NULL,
    key TEXT NOT NULL DEFAULT '',
    brief TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    budget_usd REAL,
    spent_usd REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Sprints: completion-gated batches within a project
CREATE TABLE IF NOT EXISTS pm_sprints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES pm_projects(id),
    name TEXT NOT NULL,
    goal TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    budget_usd REAL,
    spent_usd REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Tickets: work items within a sprint
CREATE TABLE IF NOT EXISTS pm_tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_id INTEGER NOT NULL REFERENCES pm_sprints(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'backlog',
    tier TEXT NOT NULL DEFAULT 'significant',
    priority INTEGER NOT NULL DEFAULT 100,
    estimated_cost_usd REAL,
    assignee TEXT,
    budget_usd REAL,
    spent_usd REAL NOT NULL DEFAULT 0.0,
    tcd TEXT,
    ticket_number INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Transitions: audit log for ticket state changes
CREATE TABLE IF NOT EXISTS pm_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES pm_tickets(id),
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'pm',
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Agent runs: execution records for cost/token tracking
CREATE TABLE IF NOT EXISTS pm_agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES pm_tickets(id),
    agent_role TEXT NOT NULL,
    model TEXT,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    duration_s REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'running',
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at TEXT
);

-- Deploy event records (Phase 5, v3)
CREATE TABLE IF NOT EXISTS pm_deploys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES pm_projects(id),
    commit_from TEXT,
    commit_to TEXT,
    action TEXT NOT NULL DEFAULT 'rebuild',
    status TEXT NOT NULL DEFAULT 'triggered',
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at TEXT
);

-- Many-to-many link: which tickets shipped in which deploy
CREATE TABLE IF NOT EXISTS pm_deploy_tickets (
    deploy_id INTEGER NOT NULL REFERENCES pm_deploys(id),
    ticket_id INTEGER NOT NULL REFERENCES pm_tickets(id),
    linked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (deploy_id, ticket_id)
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_pm_projects_chat ON pm_projects(chat_key);
CREATE INDEX IF NOT EXISTS idx_pm_sprints_project ON pm_sprints(project_id);
CREATE INDEX IF NOT EXISTS idx_pm_tickets_sprint ON pm_tickets(sprint_id);
CREATE INDEX IF NOT EXISTS idx_pm_transitions_ticket ON pm_transitions(ticket_id);
CREATE INDEX IF NOT EXISTS idx_pm_agent_runs_ticket ON pm_agent_runs(ticket_id);
CREATE INDEX IF NOT EXISTS idx_pm_deploys_project ON pm_deploys(project_id);
"""

# Index on priority is created by _migrate_v1_to_v2 (or init_pm_db post-DDL)
# to avoid failure when executescript runs against a v1 DB that lacks the column.
# Unique partial index on project key (only non-empty keys are unique per chat).
_POST_DDL_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_pm_tickets_priority ON pm_tickets(sprint_id, priority, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pm_projects_key ON pm_projects(chat_key, key) WHERE key != '';
"""


async def init_pm_db(conn: aiosqlite.Connection):
    """Initialize PM schema — idempotent (CREATE IF NOT EXISTS).

    Checks pm_meta for schema version. If missing or outdated, runs DDL.
    Future migrations go here as version-gated blocks.
    """
    await conn.executescript(_DDL)

    # Check/set schema version
    cursor = await conn.execute(
        "SELECT value FROM pm_meta WHERE key = 'schema_version'"
    )
    row = await cursor.fetchone()
    current_version = int(row["value"]) if row else 0

    if current_version < SCHEMA_VERSION:
        if current_version < 2:
            await _migrate_v1_to_v2(conn)
        if current_version < 3:
            await _migrate_v2_to_v3(conn)
        if current_version < 4:
            await _migrate_v3_to_v4(conn)
        if current_version < 5:
            await _migrate_v4_to_v5(conn)
        await conn.execute(
            "INSERT OR REPLACE INTO pm_meta (key, value, updated_at) "
            "VALUES ('schema_version', ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
            (str(SCHEMA_VERSION),),
        )
        await conn.commit()
        log.info(f"PM schema initialized at version {SCHEMA_VERSION}")
    else:
        log.debug(f"PM schema already at version {current_version}")

    # Post-DDL indexes that depend on columns added by migrations
    await conn.executescript(_POST_DDL_INDEXES)


async def _migrate_v2_to_v3(conn: aiosqlite.Connection):
    """Add ticket-commit-deploy linkage and parent tickets (Phase 5).

    New pm_tickets columns:
      commit_hash: git SHA(s) linked to this ticket (JSON array or single SHA)
      operator_request: original user message that triggered the work
      parent_ticket_id: FK for bug/subtask linking
      source_chat_id: originating chat for cross-chat visibility

    New tables (pm_deploys, pm_deploy_tickets) are created by DDL above
    (CREATE IF NOT EXISTS), so no explicit CREATE here.
    """
    log.info("PM schema: migrating v2 → v3 (ticket-commit-deploy linkage)")

    # Safe column adds — SQLite ALTER TABLE ADD COLUMN is always additive
    for col_ddl in (
        "ALTER TABLE pm_tickets ADD COLUMN commit_hash TEXT",
        "ALTER TABLE pm_tickets ADD COLUMN operator_request TEXT",
        "ALTER TABLE pm_tickets ADD COLUMN parent_ticket_id INTEGER REFERENCES pm_tickets(id)",
        "ALTER TABLE pm_tickets ADD COLUMN source_chat_id TEXT",
    ):
        try:
            await conn.execute(col_ddl)
        except Exception as e:
            # Column may already exist (partial migration recovery)
            if "duplicate column" in str(e).lower():
                log.debug("PM migration v3: column already exists, skipping: %s", col_ddl)
            else:
                raise

    # Index for parent ticket lookups
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pm_tickets_parent "
        "ON pm_tickets(parent_ticket_id)"
    )


async def _migrate_v3_to_v4(conn: aiosqlite.Connection):
    """Add Jira-style ticket naming: project key + per-project ticket number.

    New pm_projects column:
      key: short UPPERCASE identifier (e.g. 'CB' for 'Corporate Bot').
           Unique per chat_key (enforced via partial unique index).

    New pm_tickets column:
      ticket_number: sequential counter per project (restarts at 1 for each project).
                     Display format: '{key}-{ticket_number}' (e.g. 'CB-3').

    Backfill:
      - Derives key from existing project names (initials of words, uppercased).
      - Assigns ticket_number = rank within each project ordered by ticket.id.
    """
    log.info("PM schema: migrating v3 → v4 (Jira-style ticket naming)")

    def _derive_key(name: str) -> str:
        """Derive project key from name. 'Corporate Bot' → 'CB', 'Testing' → 'TES'."""
        words = [w for w in name.split() if w and w[0].isalpha()]
        if not words:
            return (name[:3] or "P").upper()
        if len(words) == 1:
            return words[0][:3].upper()
        return "".join(w[0] for w in words)[:5].upper()

    # Add key column to pm_projects (safe — additive only)
    for col_ddl in (
        "ALTER TABLE pm_projects ADD COLUMN key TEXT NOT NULL DEFAULT ''",
    ):
        try:
            await conn.execute(col_ddl)
        except Exception as e:
            if "duplicate column" in str(e).lower():
                log.debug("PM migration v4: column already exists, skipping: %s", col_ddl)
            else:
                raise

    # Add ticket_number column to pm_tickets (safe — additive only)
    for col_ddl in (
        "ALTER TABLE pm_tickets ADD COLUMN ticket_number INTEGER",
    ):
        try:
            await conn.execute(col_ddl)
        except Exception as e:
            if "duplicate column" in str(e).lower():
                log.debug("PM migration v4: column already exists, skipping: %s", col_ddl)
            else:
                raise

    # Backfill: derive keys for existing projects (key == '' means not yet set)
    cursor = await conn.execute(
        "SELECT id, chat_key, name FROM pm_projects WHERE key = '' ORDER BY id"
    )
    projects = await cursor.fetchall()

    # Track used keys per chat_key to ensure uniqueness
    used: dict[str, set] = {}
    for p in projects:
        chat_key = p["chat_key"]
        base_key = _derive_key(p["name"])
        if chat_key not in used:
            # Load already-assigned keys for this chat to avoid conflicts
            ex_cursor = await conn.execute(
                "SELECT key FROM pm_projects WHERE chat_key = ? AND key != ''",
                (chat_key,),
            )
            used[chat_key] = {r["key"] for r in await ex_cursor.fetchall()}
        key = base_key
        attempt = 1
        while key in used[chat_key] and attempt < 100:
            attempt += 1
            key = f"{base_key}{attempt}"
        # If still colliding after 100 tries (extreme edge case), use project ID
        if key in used[chat_key]:
            key = f"{base_key}{p['id']}"
        used[chat_key].add(key)
        await conn.execute(
            "UPDATE pm_projects SET key = ? WHERE id = ?", (key, p["id"])
        )
        log.info("PM migration v4: project %d '%s' → key '%s'", p["id"], p["name"], key)

    # Backfill: assign ticket_number per project (order by ticket.id)
    # Fetch all tickets with their project_id, ordered for stable numbering
    cursor = await conn.execute("""
        SELECT t.id, s.project_id
        FROM pm_tickets t
        JOIN pm_sprints s ON t.sprint_id = s.id
        WHERE t.ticket_number IS NULL
        ORDER BY s.project_id, t.id
    """)
    rows = await cursor.fetchall()

    # Group by project_id, assign sequential numbers
    counters: dict[int, int] = {}
    for row in rows:
        pid = row["project_id"]
        if pid not in counters:
            # Find the max existing ticket_number for this project (partial migration safety)
            mc = await conn.execute("""
                SELECT COALESCE(MAX(t2.ticket_number), 0) as max_n
                FROM pm_tickets t2
                JOIN pm_sprints s2 ON t2.sprint_id = s2.id
                WHERE s2.project_id = ?
            """, (pid,))
            mr = await mc.fetchone()
            counters[pid] = (mr["max_n"] or 0)
        counters[pid] += 1
        await conn.execute(
            "UPDATE pm_tickets SET ticket_number = ? WHERE id = ?",
            (counters[pid], row["id"]),
        )
        log.info("PM migration v4: ticket %d → %d-#%d", row["id"], pid, counters[pid])

    log.info("PM schema: v3→v4 migration complete (%d projects, %d tickets updated)",
             len(projects), len(rows))


async def _migrate_v4_to_v5(conn: aiosqlite.Connection):
    """Add visibility + owner_user_id to pm_projects for chat-scoped private projects.

    visibility: 'private' (default, owner chat only) or 'shared' (org-wide, admin only).
    owner_user_id: who created the project (source:user_id format).

    Existing projects default to 'shared' (backward compat — they were created by admin).
    """
    log.info("PM schema: migrating v4 → v5 (chat-scoped project visibility)")

    for col_ddl in (
        "ALTER TABLE pm_projects ADD COLUMN visibility TEXT NOT NULL DEFAULT 'shared'",
        "ALTER TABLE pm_projects ADD COLUMN owner_user_id TEXT NOT NULL DEFAULT ''",
    ):
        try:
            await conn.execute(col_ddl)
        except Exception as e:
            if "duplicate column" in str(e).lower():
                log.debug("PM migration v5: column already exists, skipping: %s", col_ddl)
            else:
                raise

    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pm_projects_visibility "
        "ON pm_projects(visibility)"
    )
    log.info("PM schema: v4→v5 migration complete (visibility + owner_user_id added)")


async def _migrate_v1_to_v2(conn: aiosqlite.Connection):
    """Add priority and estimated_cost_usd columns to pm_tickets.

    priority: execution order within a sprint (lower = higher priority, default 100).
    estimated_cost_usd: pre-execution cost estimate for velocity tracking.
    """
    log.info("PM schema: migrating v1 → v2 (ticket priority + estimation)")
    # M25 fix: add duplicate-column guard (matching v2→v3, v3→v4 pattern)
    for col_sql in [
        "ALTER TABLE pm_tickets ADD COLUMN priority INTEGER NOT NULL DEFAULT 100",
        "ALTER TABLE pm_tickets ADD COLUMN estimated_cost_usd REAL",
    ]:
        try:
            await conn.execute(col_sql)
        except Exception as e:
            if "duplicate column" in str(e).lower():
                log.debug("PM v1→v2: column already exists, skipping: %s", e)
            else:
                raise
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pm_tickets_priority "
        "ON pm_tickets(sprint_id, priority, id)"
    )
