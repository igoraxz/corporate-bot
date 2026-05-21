# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""PM store — async CRUD for projects, sprints, tickets, transitions, agent_runs, deploys.

All functions operate on the PM DB singleton (bot.pm.db.get_pm_db).
Ticket status transitions are validated against schema.TICKET_TRANSITIONS.
Deploy lifecycle: create_deploy() at trigger time, resolve_pending_deploys() at startup.

Concurrency: get_or_create_* functions use asyncio.Lock keyed by lookup args to
prevent duplicate creation when parallel sessions call them simultaneously.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone

import aiosqlite

from bot.pm.db import get_pm_db
from bot.pm.schema import (
    VALID_PROJECT_STATUSES,
    VALID_SPRINT_STATUSES,
    VALID_TICKET_STATUSES,
    TICKET_TRANSITIONS,
    SPRINT_TRANSITIONS,
)

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _row_to_dict(row: aiosqlite.Row | None) -> dict | None:
    if row is None:
        return None
    return dict(row)


# ============================================================
# JIRA-STYLE TICKET NAMING HELPERS
# ============================================================

def derive_project_key(name: str) -> str:
    """Derive a short UPPERCASE project key from a project name.

    'Corporate Bot' → 'CB', 'Bot Self-Upgrade' → 'BS', 'Testing' → 'TES'.
    Multi-word names use first letter of each word (up to 5 chars).
    Single-word names use the first 3 chars.
    Non-alpha characters are filtered from the fallback so the result
    always starts with a letter.
    """
    words = [w for w in name.split() if w and w[0].isalpha()]
    if not words:
        # Strip non-alpha chars and take up to 3 chars, fallback to "P"
        alpha = re.sub(r"[^A-Za-z]", "", name)[:3].upper()
        return alpha or "P"
    if len(words) == 1:
        return words[0][:3].upper()
    return "".join(w[0] for w in words)[:5].upper()


def format_ticket_ref(project_key: str, ticket_number: int | None) -> str:
    """Format a Jira-style ticket reference. Returns '' if data is missing."""
    if project_key and ticket_number is not None:
        return f"{project_key}-{ticket_number}"
    return ""


async def get_ticket_ref(ticket_id: int) -> str:
    """Get the Jira-style reference for a ticket (e.g. 'FB-3').

    Falls back to 'PM-{id}' if the ticket lacks a project key or ticket_number.
    """
    db = await get_pm_db()
    row = await (await db.execute(
        "SELECT t.ticket_number, p.key as project_key "
        "FROM pm_tickets t "
        "JOIN pm_sprints s ON t.sprint_id = s.id "
        "JOIN pm_projects p ON s.project_id = p.id "
        "WHERE t.id = ?",
        (ticket_id,),
    )).fetchone()
    if row and row["project_key"] and row["ticket_number"] is not None:
        return f"{row['project_key']}-{row['ticket_number']}"
    return f"PM-{ticket_id}"


async def resolve_ticket_ref(ref: str) -> int | None:
    """Resolve a ticket reference to an integer ticket ID.

    Accepts:
      - Integer strings: '4' → 4
      - Jira-style refs:  'FB-3' → looks up project key + ticket_number → id

    Returns None if not found.
    """
    s = str(ref).strip()
    if s.isdigit():
        return int(s)
    m = re.match(r"^([A-Za-z]+)-(\d+)$", s)
    if not m:
        return None
    key = m.group(1).upper()
    number = int(m.group(2))
    db = await get_pm_db()
    row = await (await db.execute(
        "SELECT t.id FROM pm_tickets t "
        "JOIN pm_sprints s ON t.sprint_id = s.id "
        "JOIN pm_projects p ON s.project_id = p.id "
        "WHERE p.key = ? AND t.ticket_number = ?",
        (key, number),
    )).fetchone()
    return row["id"] if row else None


# ============================================================
# PROJECTS
# ============================================================

async def create_project(
    chat_key: str,
    name: str,
    brief: str = "",
    budget_usd: float | None = None,
    key: str | None = None,
    visibility: str = "private",
    owner_user_id: str = "",
) -> dict:
    """Create a new project scoped to a coding chat. Returns the created project.

    key: short UPPERCASE identifier (e.g. 'FB'). Auto-derived from name if omitted.
         Guaranteed unique per chat_key — a numeric suffix is appended if needed.
    visibility: 'private' (default, own chat only) or 'shared' (org-wide).
    owner_user_id: who created the project (source:user_id format).
    """
    if visibility not in ("private", "shared"):
        raise ValueError(f"Invalid visibility: {visibility!r} — must be 'private' or 'shared'")
    db = await get_pm_db()

    # Derive key if not provided; validate format ([A-Z][A-Z0-9]{0,9})
    import re as _re
    base_key = (key.strip().upper() if key else None) or derive_project_key(name)
    # Sanitize: keep only alphanumeric chars, ensure at least one letter
    base_key = _re.sub(r"[^A-Z0-9]", "", base_key)[:10] or "P"
    if not any(c.isalpha() for c in base_key):
        base_key = "P" + base_key

    # Ensure uniqueness within the chat (append counter if collision); cap at 20
    resolved_key = base_key
    for attempt in range(2, 22):
        existing = await (await db.execute(
            "SELECT id FROM pm_projects WHERE chat_key = ? AND key = ?",
            (chat_key, resolved_key),
        )).fetchone()
        if not existing:
            break
        resolved_key = f"{base_key}{attempt}"
    else:
        # All 20 suffix variants taken — highly unlikely, raise rather than silently break
        raise ValueError(
            f"Cannot assign a unique key for project '{name}' in chat '{chat_key}'. "
            "Too many projects with the same key prefix. Provide an explicit 'key' parameter."
        )

    import aiosqlite as _aiosl
    try:
        cursor = await db.execute(
            "INSERT INTO pm_projects (chat_key, name, key, brief, budget_usd, visibility, owner_user_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_key, name, resolved_key, brief, budget_usd, visibility, owner_user_id),
        )
    except _aiosl.IntegrityError:
        # Unique index race — append timestamp suffix as last resort
        import time as _time
        resolved_key = f"{base_key}{int(_time.time()) % 10000}"
        cursor = await db.execute(
            "INSERT INTO pm_projects (chat_key, name, key, brief, budget_usd, visibility, owner_user_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_key, name, resolved_key, brief, budget_usd, visibility, owner_user_id),
        )
    await db.commit()
    row = await (await db.execute("SELECT * FROM pm_projects WHERE id = ?", (cursor.lastrowid,))).fetchone()
    log.info("PM project created: id=%d name=%r key=%r chat=%s", cursor.lastrowid, name, resolved_key, chat_key)
    return _row_to_dict(row)


async def get_project(project_id: int) -> dict | None:
    """Get a project by ID."""
    db = await get_pm_db()
    row = await (await db.execute("SELECT * FROM pm_projects WHERE id = ?", (project_id,))).fetchone()
    return _row_to_dict(row)


async def list_projects(chat_key: str, status: str | None = None, limit: int = 100) -> list[dict]:
    """List projects for a chat, optionally filtered by status."""
    db = await get_pm_db()
    if status:
        if status not in VALID_PROJECT_STATUSES:
            raise ValueError(f"Invalid project status: {status}")
        cursor = await db.execute(
            "SELECT * FROM pm_projects WHERE chat_key = ? AND status = ? ORDER BY created_at DESC LIMIT ?",
            (chat_key, status, limit),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM pm_projects WHERE chat_key = ? ORDER BY created_at DESC LIMIT ?",
            (chat_key, limit),
        )
    return [_row_to_dict(r) for r in await cursor.fetchall()]


async def check_project_access(
    chat_key: str, project_id: int, privilege: str = "read"
) -> bool:
    """Check if a chat has access to a project.

    Checks in order:
    1. RBAC: explicit pm_project policy (admin wildcard covers this)
    2. Ownership: chat_key matches project's chat_key
    3. Visibility: shared projects are readable by all
    """
    # 1. RBAC check — flat permission model (CB-79)
    try:
        from bot.rbac import get_engine
        from bot.core.access import build_rbac_subject
        engine = get_engine()
        # Derive source + chat_id from chat_key (e.g. "telegram:123456789")
        _ck_parts = chat_key.split(":", 1)
        _ck_source = _ck_parts[0] if len(_ck_parts) >= 2 else ""
        _ck_id = _ck_parts[1] if len(_ck_parts) >= 2 else chat_key
        subject = build_rbac_subject(_ck_source, chat_key, _ck_id)
        # Check if caller is admin (wildcard * on *)
        _admin_decision = await engine.check_access(
            subject=subject,
            privilege="admin",
            object_type="*",
            object_id="*",
        )
        if _admin_decision.allowed:
            return True  # admin has full access to all projects
        # Check specific pm_project policy
        decision = await engine.check_access(
            subject=subject,
            privilege=privilege,
            object_type="pm_project",
            object_id=str(project_id),
        )
        if decision.allowed:
            return True
        if not decision.allowed:
            return False
    except Exception:
        pass  # RBAC unavailable → fall through

    # 2. Ownership + visibility check
    db = await get_pm_db()
    row = await (await db.execute(
        "SELECT chat_key, visibility FROM pm_projects WHERE id = ?", (project_id,)
    )).fetchone()
    if not row:
        return False
    if row["chat_key"] == chat_key:
        return True
    # Shared projects: readable by all
    if row["visibility"] == "shared":
        return True
    return False


async def list_accessible_projects(
    chat_key: str,
    status: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """List projects accessible to a chat: own private + all shared.

    Returns projects where:
    - chat_key matches (own projects) OR
    - visibility = 'shared' (org-wide projects readable by all)
    """
    db = await get_pm_db()
    status_clause = "AND p.status = ?" if status else ""
    params: list = [chat_key, chat_key]
    if status:
        if status not in VALID_PROJECT_STATUSES:
            raise ValueError(f"Invalid project status: {status}")
        params.append(status)
    cursor = await db.execute(
        f"SELECT * FROM pm_projects p "
        f"WHERE (p.chat_key = ? OR (p.visibility = 'shared' AND p.chat_key != ?)) "
        f"{status_clause} "
        f"ORDER BY p.created_at DESC LIMIT ?",
        (*params, limit),
    )
    return [_row_to_dict(r) for r in await cursor.fetchall()]


async def update_project(project_id: int, **fields) -> dict | None:
    """Update project fields. Validates status if provided."""
    if "status" in fields and fields["status"] not in VALID_PROJECT_STATUSES:
        raise ValueError(f"Invalid project status: {fields['status']}")

    db = await get_pm_db()
    sets = []
    vals = []
    for k, v in fields.items():
        if k in ("name", "brief", "status", "budget_usd", "spent_usd"):
            # NOTE: 'key' is intentionally excluded — key changes require uniqueness
            # validation that update_project does not perform. Use create_project instead.
            sets.append(f"{k} = ?")
            vals.append(v)
    if not sets:
        return await get_project(project_id)
    sets.append("updated_at = ?")
    vals.append(_now_iso())
    vals.append(project_id)
    await db.execute(f"UPDATE pm_projects SET {', '.join(sets)} WHERE id = ?", vals)
    await db.commit()
    return await get_project(project_id)


# ============================================================
# SPRINTS
# ============================================================

async def create_sprint(
    project_id: int,
    name: str,
    goal: str = "",
    budget_usd: float | None = None,
) -> dict:
    """Create a new sprint within a project."""
    db = await get_pm_db()
    cursor = await db.execute(
        "INSERT INTO pm_sprints (project_id, name, goal, budget_usd) VALUES (?, ?, ?, ?)",
        (project_id, name, goal, budget_usd),
    )
    await db.commit()
    row = await (await db.execute("SELECT * FROM pm_sprints WHERE id = ?", (cursor.lastrowid,))).fetchone()
    log.info(f"PM sprint created: id={cursor.lastrowid} name={name!r} project={project_id}")
    return _row_to_dict(row)


async def get_sprint(sprint_id: int) -> dict | None:
    """Get a sprint by ID."""
    db = await get_pm_db()
    row = await (await db.execute("SELECT * FROM pm_sprints WHERE id = ?", (sprint_id,))).fetchone()
    return _row_to_dict(row)


async def list_sprints(project_id: int, status: str | None = None, limit: int = 100) -> list[dict]:
    """List sprints for a project, optionally filtered by status."""
    db = await get_pm_db()
    if status:
        if status not in VALID_SPRINT_STATUSES:
            raise ValueError(f"Invalid sprint status: {status}")
        cursor = await db.execute(
            "SELECT * FROM pm_sprints WHERE project_id = ? AND status = ? ORDER BY created_at LIMIT ?",
            (project_id, status, limit),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM pm_sprints WHERE project_id = ? ORDER BY created_at LIMIT ?",
            (project_id, limit),
        )
    return [_row_to_dict(r) for r in await cursor.fetchall()]


async def transition_sprint(sprint_id: int, to_status: str, actor: str = "pm") -> dict:
    """Transition a sprint to a new status. Validates transition is allowed.

    Uses BEGIN IMMEDIATE for atomicity — prevents concurrent transitions
    from reading the same from_status.
    """
    db = await get_pm_db()
    await db.execute("BEGIN IMMEDIATE")
    try:
        row = await (await db.execute(
            "SELECT * FROM pm_sprints WHERE id = ?", (sprint_id,)
        )).fetchone()
        if not row:
            raise ValueError(f"Sprint {sprint_id} not found")
        from_status = row["status"]
        allowed = SPRINT_TRANSITIONS.get(from_status, frozenset())
        if to_status not in allowed:
            raise ValueError(
                f"Invalid sprint transition: {from_status} -> {to_status}. "
                f"Allowed: {', '.join(sorted(allowed)) or 'none (terminal)'}"
            )
        # Guard: completing a sprint requires all tickets to be terminal
        if to_status == "completed":
            ticket_cursor = await db.execute(
                "SELECT COUNT(*) as cnt FROM pm_tickets "
                "WHERE sprint_id = ? AND status NOT IN ('done', 'dropped')",
                (sprint_id,),
            )
            ticket_row = await ticket_cursor.fetchone()
            if ticket_row["cnt"] > 0:
                raise ValueError(
                    f"Cannot complete sprint {sprint_id}: "
                    f"{ticket_row['cnt']} ticket(s) still active. "
                    f"Move all tickets to done/dropped first, or abort the sprint."
                )

        await db.execute(
            "UPDATE pm_sprints SET status = ?, updated_at = ? WHERE id = ?",
            (to_status, _now_iso(), sprint_id),
        )
        await db.execute("COMMIT")
    except Exception:
        try:
            await db.execute("ROLLBACK")
        except Exception:
            pass
        raise
    log.info(f"PM sprint {sprint_id}: {from_status} -> {to_status} by {actor}")
    return await get_sprint(sprint_id)


# ============================================================
# TICKETS
# ============================================================

MAX_TICKETS_PER_SPRINT = 50  # safety cap to prevent unbounded sprints


async def create_ticket(
    sprint_id: int,
    title: str,
    description: str = "",
    tier: str = "significant",
    budget_usd: float | None = None,
    priority: int = 100,
    estimated_cost_usd: float | None = None,
    operator_request: str | None = None,
    parent_ticket_id: int | None = None,
    source_chat_id: str | None = None,
) -> dict:
    """Create a new ticket within a sprint.

    Args:
        priority: execution order (lower = higher priority, default 100).
        estimated_cost_usd: pre-execution cost estimate for velocity tracking.
        operator_request: original user message that triggered this work.
        parent_ticket_id: FK to parent ticket (for bugs/subtasks).
        source_chat_id: originating chat ID.
    """
    if tier not in ("trivial", "small", "medium", "significant"):
        raise ValueError(f"Invalid tier: {tier}. Must be trivial/small/medium/significant (trivial accepted for backward compat)")
    db = await get_pm_db()
    # Safety cap: prevent unbounded ticket creation
    count = await (await db.execute(
        "SELECT COUNT(*) as cnt FROM pm_tickets WHERE sprint_id = ?", (sprint_id,)
    )).fetchone()
    if count["cnt"] >= MAX_TICKETS_PER_SPRINT:
        raise ValueError(
            f"Sprint {sprint_id} already has {count['cnt']} tickets "
            f"(max {MAX_TICKETS_PER_SPRINT}). Create a new sprint for more work."
        )
    # Auto-assign ticket_number (per project, sequential). The subquery is atomic
    # within SQLite's serialized writer — no separate transaction needed.
    cursor = await db.execute(
        "INSERT INTO pm_tickets (sprint_id, title, description, tier, budget_usd, "
        "priority, estimated_cost_usd, operator_request, parent_ticket_id, source_chat_id, "
        "ticket_number) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "  (SELECT COALESCE(MAX(t2.ticket_number), 0) + 1 "
        "   FROM pm_tickets t2 "
        "   JOIN pm_sprints s2 ON t2.sprint_id = s2.id "
        "   WHERE s2.project_id = (SELECT project_id FROM pm_sprints WHERE id = ?)))",
        (sprint_id, title, description, tier, budget_usd, priority,
         estimated_cost_usd, operator_request, parent_ticket_id, source_chat_id,
         sprint_id),
    )
    await db.commit()
    row = await (await db.execute("SELECT * FROM pm_tickets WHERE id = ?", (cursor.lastrowid,))).fetchone()
    log.info("PM ticket created: id=%d ticket_number=%s title=%r sprint=%d",
             cursor.lastrowid, (dict(row) if row else {}).get("ticket_number"), title, sprint_id)
    return _row_to_dict(row)


async def get_ticket(ticket_id: int) -> dict | None:
    """Get a ticket by ID."""
    db = await get_pm_db()
    row = await (await db.execute("SELECT * FROM pm_tickets WHERE id = ?", (ticket_id,))).fetchone()
    return _row_to_dict(row)


async def list_tickets(
    sprint_id: int | None = None,
    status: str | None = None,
    limit: int = 200,
    project_id: int | None = None,
    parent_ticket_id: int | None = None,
) -> list[dict]:
    """List tickets with flexible filtering.

    Filters (all optional, combined with AND):
      sprint_id: tickets in a specific sprint
      project_id: all tickets across all sprints in a project
      parent_ticket_id: child tickets of a parent (for follow-up counting)
      status: filter by ticket status

    At least one filter is required.
    """
    if status and status not in VALID_TICKET_STATUSES:
        raise ValueError(f"Invalid ticket status: {status}")

    db = await get_pm_db()
    conditions: list[str] = []
    params: list = []

    if sprint_id is not None:
        conditions.append("t.sprint_id = ?")
        params.append(sprint_id)

    if project_id is not None:
        conditions.append("s.project_id = ?")
        params.append(project_id)

    if parent_ticket_id is not None:
        conditions.append("t.parent_ticket_id = ?")
        params.append(parent_ticket_id)

    if status:
        conditions.append("t.status = ?")
        params.append(status)

    if not conditions:
        raise ValueError("At least one filter (sprint_id, project_id, or parent_ticket_id) is required")

    # Use JOIN to pm_sprints only when project_id filter is needed
    needs_join = project_id is not None
    if needs_join:
        query = (
            f"SELECT t.* FROM pm_tickets t "
            f"JOIN pm_sprints s ON t.sprint_id = s.id "
            f"WHERE {' AND '.join(conditions)} "
            f"ORDER BY t.priority ASC, t.id LIMIT ?"
        )
    else:
        query = (
            f"SELECT t.* FROM pm_tickets t "
            f"WHERE {' AND '.join(conditions)} "
            f"ORDER BY t.priority ASC, t.id LIMIT ?"
        )
    params.append(limit)

    cursor = await db.execute(query, params)
    return [_row_to_dict(r) for r in await cursor.fetchall()]


async def update_ticket(ticket_id: int, **fields) -> dict | None:
    """Update ticket fields.

    For status changes, use move_ticket() which validates transitions.
    """
    db = await get_pm_db()
    allowed_fields = (
        "title", "description", "tier", "assignee", "budget_usd", "spent_usd",
        "tcd", "priority", "estimated_cost_usd",
        "commit_hash", "operator_request", "parent_ticket_id", "source_chat_id",
    )
    sets = []
    vals = []
    for k, v in fields.items():
        if k in allowed_fields:
            sets.append(f"{k} = ?")
            vals.append(v)
    if not sets:
        return await get_ticket(ticket_id)
    sets.append("updated_at = ?")
    vals.append(_now_iso())
    vals.append(ticket_id)
    await db.execute(f"UPDATE pm_tickets SET {', '.join(sets)} WHERE id = ?", vals)
    await db.commit()
    return await get_ticket(ticket_id)


async def move_ticket(
    ticket_id: int,
    to_status: str,
    actor: str = "pm",
    reason: str | None = None,
) -> dict:
    """Move a ticket to a new status. Validates transition and logs to pm_transitions.

    Uses BEGIN IMMEDIATE for atomicity — prevents concurrent transitions
    from reading the same from_status.
    """
    if to_status not in VALID_TICKET_STATUSES:
        raise ValueError(f"Invalid ticket status: {to_status}")

    db = await get_pm_db()
    await db.execute("BEGIN IMMEDIATE")
    try:
        row = await (await db.execute(
            "SELECT * FROM pm_tickets WHERE id = ?", (ticket_id,)
        )).fetchone()
        if not row:
            raise ValueError(f"Ticket {ticket_id} not found")
        from_status = row["status"]
        allowed = TICKET_TRANSITIONS.get(from_status, frozenset())
        if to_status not in allowed:
            raise ValueError(
                f"Invalid transition: {from_status} -> {to_status}. "
                f"Allowed: {', '.join(sorted(allowed)) or 'none (terminal)'}"
            )
        now = _now_iso()
        await db.execute(
            "UPDATE pm_tickets SET status = ?, updated_at = ? WHERE id = ?",
            (to_status, now, ticket_id),
        )
        await db.execute(
            "INSERT INTO pm_transitions (ticket_id, from_status, to_status, actor, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (ticket_id, from_status, to_status, actor, reason),
        )
        await db.execute("COMMIT")
    except Exception:
        try:
            await db.execute("ROLLBACK")
        except Exception:
            pass
        raise
    log.info(f"PM ticket {ticket_id}: {from_status} -> {to_status} by {actor}")
    return await get_ticket(ticket_id)


# ============================================================
# AGENT RUNS
# ============================================================

async def create_agent_run(
    ticket_id: int,
    agent_role: str,
    model: str | None = None,
) -> dict:
    """Record start of an agent execution run."""
    db = await get_pm_db()
    cursor = await db.execute(
        "INSERT INTO pm_agent_runs (ticket_id, agent_role, model) VALUES (?, ?, ?)",
        (ticket_id, agent_role, model),
    )
    await db.commit()
    row = await (await db.execute("SELECT * FROM pm_agent_runs WHERE id = ?", (cursor.lastrowid,))).fetchone()
    return _row_to_dict(row)


async def complete_agent_run(
    run_id: int,
    cost_usd: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    duration_s: float = 0.0,
    status: str = "completed",
    error: str | None = None,
) -> dict | None:
    """Complete an agent run with cost/token metrics."""
    db = await get_pm_db()
    await db.execute(
        "UPDATE pm_agent_runs SET cost_usd = ?, input_tokens = ?, output_tokens = ?, "
        "duration_s = ?, status = ?, error = ?, completed_at = ? WHERE id = ?",
        (cost_usd, input_tokens, output_tokens, duration_s, status, error, _now_iso(), run_id),
    )
    await db.commit()
    row = await (await db.execute("SELECT * FROM pm_agent_runs WHERE id = ?", (run_id,))).fetchone()
    return _row_to_dict(row)


async def get_ticket_runs(ticket_id: int) -> list[dict]:
    """Get all agent runs for a ticket."""
    db = await get_pm_db()
    cursor = await db.execute(
        "SELECT * FROM pm_agent_runs WHERE ticket_id = ? ORDER BY created_at",
        (ticket_id,),
    )
    return [_row_to_dict(r) for r in await cursor.fetchall()]


async def cleanup_stale_runs(ticket_id: int) -> int:
    """Mark stale 'running' agent_runs as 'failed' for a ticket.

    Called before re-executing a ticket to clean up runs from previous
    crashed executions that were never completed.
    Returns number of runs cleaned up.
    """
    db = await get_pm_db()
    cursor = await db.execute(
        "UPDATE pm_agent_runs SET status = 'failed', "
        "error = 'Cleaned up: stale running state from prior execution', "
        "completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
        "WHERE ticket_id = ? AND status = 'running'",
        (ticket_id,),
    )
    await db.commit()
    count = cursor.rowcount
    if count:
        log.info(f"PM cleanup: marked {count} stale agent_runs as failed for ticket {ticket_id}")
    return count


# ============================================================
# BOARD VIEW
# ============================================================

async def get_board(project_id: int, sprint_id: int | None = None) -> dict:
    """Get a board view of tickets grouped by status.

    If sprint_id is provided, shows that sprint only.
    Otherwise shows the latest running/approved sprint, or latest draft.
    """
    _db = await get_pm_db()
    project = await get_project(project_id)
    if not project:
        raise ValueError(f"Project {project_id} not found")

    if sprint_id:
        sprint = await get_sprint(sprint_id)
        if not sprint or sprint["project_id"] != project_id:
            raise ValueError(f"Sprint {sprint_id} not found in project {project_id}")
    else:
        # Find the most relevant sprint: running > approved > draft
        for pref_status in ("running", "approved", "draft"):
            sprints = await list_sprints(project_id, status=pref_status)
            if sprints:
                sprint = sprints[0]
                break
        else:
            return {
                "project": project,
                "sprint": None,
                "columns": {},
                "summary": "No sprints found",
            }

    tickets = await list_tickets(sprint["id"])
    columns: dict[str, list[dict]] = {}
    for status in ("backlog", "ready", "in_progress", "review", "done", "dropped"):
        columns[status] = [t for t in tickets if t["status"] == status]

    # Budget summary
    total_budget = sprint.get("budget_usd") or 0
    total_spent = sum(t.get("spent_usd", 0) for t in tickets)
    total_tickets = len(tickets)
    done_count = len(columns["done"])
    dropped_count = len(columns["dropped"])

    return {
        "project": project,
        "sprint": sprint,
        "columns": columns,
        "summary": {
            "total_tickets": total_tickets,
            "done": done_count,
            "dropped": dropped_count,
            "in_flight": total_tickets - done_count - dropped_count,
            "budget_usd": total_budget,
            "spent_usd": total_spent,
            "remaining_usd": total_budget - total_spent if total_budget else None,
        },
    }


# ============================================================
# ROLLING SPRINTS (with concurrency guards)
# ============================================================

# Locks for get_or_create_* to prevent duplicate creation when
# parallel coding sessions call pm_auto_ticket simultaneously.
_project_locks: dict[str, asyncio.Lock] = {}
_sprint_locks: dict[int, asyncio.Lock] = {}


_backlog_locks: dict[int, asyncio.Lock] = {}


async def get_or_create_backlog_sprint(project_id: int) -> dict:
    """Get or create the permanent Backlog sprint for a project.

    The Backlog sprint is a permanent container for unassigned tickets.
    It never transitions to completed — it stays running forever.
    Tickets are moved OUT of Backlog into real sprints via reassign_ticket_sprint().
    Thread-safe: uses per-project asyncio.Lock.
    """
    async with _backlog_locks.setdefault(project_id, asyncio.Lock()):
        db = await get_pm_db()
        cursor = await db.execute(
            "SELECT * FROM pm_sprints WHERE project_id = ? AND name = 'Backlog' "
            "AND status NOT IN ('completed', 'aborted')",
            (project_id,),
        )
        row = await cursor.fetchone()
        if row:
            return _row_to_dict(row)

        sprint = await create_sprint(
            project_id=project_id,
            name="Backlog",
            goal="Unassigned project tickets — not in any sprint",
        )
        try:
            sprint = await transition_sprint(sprint["id"], "approved", actor="system")
            sprint = await transition_sprint(sprint["id"], "running", actor="system")
        except ValueError as e:
            log.warning("PM backlog sprint %d: auto-transition failed: %s", sprint["id"], e)
        log.info("PM backlog sprint created: id=%d project=%d", sprint["id"], project_id)
        return sprint


async def reassign_ticket_sprint(ticket_id: int, new_sprint_id: int) -> dict:
    """Move a ticket from one sprint to another, then resync budget on both sprints.

    Updates sprint_id on the ticket atomically, then resyncs spent_usd on both
    the old and new sprints (and their project) in separate commits.
    Budget resync is eventually consistent — derived from agent_runs on each sync.
    """
    from bot.pm.budget import sync_sprint_spend, sync_project_spend

    db = await get_pm_db()
    # Get current ticket
    ticket = await get_ticket(ticket_id)
    if not ticket:
        raise ValueError(f"Ticket {ticket_id} not found")
    old_sprint_id = ticket["sprint_id"]
    if old_sprint_id == new_sprint_id:
        return ticket  # no-op

    # Verify new sprint exists and is in the same project
    new_sprint = await get_sprint(new_sprint_id)
    if not new_sprint:
        raise ValueError(f"Sprint {new_sprint_id} not found")
    old_sprint = await get_sprint(old_sprint_id)
    if not old_sprint:
        raise ValueError(f"Old sprint {old_sprint_id} not found")
    if old_sprint["project_id"] != new_sprint["project_id"]:
        raise ValueError(
            f"Cannot move ticket across projects: sprint {old_sprint_id} "
            f"(project {old_sprint['project_id']}) → sprint {new_sprint_id} "
            f"(project {new_sprint['project_id']})"
        )

    # Safety cap: prevent unbounded tickets in destination sprint
    count = await (await db.execute(
        "SELECT COUNT(*) as cnt FROM pm_tickets WHERE sprint_id = ?", (new_sprint_id,)
    )).fetchone()
    if count["cnt"] >= MAX_TICKETS_PER_SPRINT:
        raise ValueError(
            f"Target sprint {new_sprint_id} already has {count['cnt']} tickets "
            f"(max {MAX_TICKETS_PER_SPRINT}). Cannot reassign more tickets to it."
        )

    # Atomic sprint_id update
    await db.execute("BEGIN IMMEDIATE")
    try:
        await db.execute(
            "UPDATE pm_tickets SET sprint_id = ?, updated_at = ? WHERE id = ?",
            (new_sprint_id, _now_iso(), ticket_id),
        )
        await db.execute("COMMIT")
    except Exception:
        try:
            await db.execute("ROLLBACK")
        except Exception:
            pass
        raise

    # Resync budgets on both sprints (and project) — eventually consistent
    await sync_sprint_spend(old_sprint_id)
    await sync_sprint_spend(new_sprint_id)
    project_id = old_sprint["project_id"]
    await sync_project_spend(project_id)

    log.info("PM ticket %d: reassigned sprint %d → %d", ticket_id, old_sprint_id, new_sprint_id)
    return await get_ticket(ticket_id)


async def get_or_create_rolling_sprint(project_id: int) -> dict:
    """Get or create the current month's rolling sprint for a project.

    Rolling sprints are always-open monthly containers for ad-hoc tickets.
    Named "YYYY-MM Rolling". Auto-created if none exists for the current month.
    Thread-safe: uses per-project asyncio.Lock to prevent duplicate sprints
    when parallel sessions call this simultaneously.
    """
    # Serialize per project_id to prevent duplicate creation
    async with _sprint_locks.setdefault(project_id, asyncio.Lock()):
        now = datetime.now(timezone.utc)
        rolling_name = f"{now.strftime('%Y-%m')} Rolling"

        db = await get_pm_db()
        # Look for existing rolling sprint this month
        cursor = await db.execute(
            "SELECT * FROM pm_sprints WHERE project_id = ? AND name = ? "
            "AND status NOT IN ('completed', 'aborted')",
            (project_id, rolling_name),
        )
        row = await cursor.fetchone()
        if row:
            return _row_to_dict(row)

        # Create new rolling sprint — auto-approved (ready to accept tickets)
        sprint = await create_sprint(
            project_id=project_id,
            name=rolling_name,
            goal=f"Rolling work for {now.strftime('%B %Y')}",
        )
        # Auto-transition to approved → running for immediate use
        try:
            sprint = await transition_sprint(sprint["id"], "approved", actor="system")
            sprint = await transition_sprint(sprint["id"], "running", actor="system")
        except ValueError as e:
            log.warning("PM rolling sprint %d: auto-transition failed (usable in current status): %s",
                        sprint["id"], e)
        log.info(f"PM rolling sprint created: id={sprint['id']} name={rolling_name!r} project={project_id}")
        return sprint


async def get_or_create_project(
    chat_key: str,
    name: str,
    brief: str = "",
    key: str | None = None,
    visibility: str = "private",
    owner_user_id: str = "",
) -> dict:
    """Get existing active project by name for a chat, or create one.

    Used by auto-ticket flow to ensure a project exists without manual setup.
    Thread-safe: uses per-chat_key+name asyncio.Lock to prevent duplicate
    projects when parallel sessions call this simultaneously.
    key: optional project key override (auto-derived if not provided).
    visibility: 'private' or 'shared' (only used on creation, not updates).
    """
    lock_key = f"{chat_key}:{name}"
    async with _project_locks.setdefault(lock_key, asyncio.Lock()):
        db = await get_pm_db()
        cursor = await db.execute(
            "SELECT * FROM pm_projects WHERE chat_key = ? AND name = ? AND status = 'active'",
            (chat_key, name),
        )
        row = await cursor.fetchone()
        if row:
            return _row_to_dict(row)
        return await create_project(
            chat_key=chat_key, name=name, brief=brief, key=key,
            visibility=visibility, owner_user_id=owner_user_id,
        )


# ============================================================
# DEPLOY RECORDS
# ============================================================

async def create_deploy(
    project_id: int | None,
    commit_from: str | None,
    commit_to: str | None,
    action: str = "rebuild",
    reason: str = "",
) -> dict:
    """Record a deploy event. Returns the created deploy."""
    db = await get_pm_db()
    cursor = await db.execute(
        "INSERT INTO pm_deploys (project_id, commit_from, commit_to, action, reason) "
        "VALUES (?, ?, ?, ?, ?)",
        (project_id, commit_from, commit_to, action, reason),
    )
    await db.commit()
    row = await (await db.execute(
        "SELECT * FROM pm_deploys WHERE id = ?", (cursor.lastrowid,)
    )).fetchone()
    log.info(f"PM deploy created: id={cursor.lastrowid} {commit_from}..{commit_to}")
    return _row_to_dict(row)


async def complete_deploy(deploy_id: int, status: str = "success") -> dict | None:
    """Mark a deploy as completed with final status.

    Called automatically by resolve_pending_deploys() on startup — reads the
    host watcher's deploy_result.json and updates the PM record accordingly.
    """
    db = await get_pm_db()
    await db.execute(
        "UPDATE pm_deploys SET status = ?, completed_at = ? WHERE id = ?",
        (status, _now_iso(), deploy_id),
    )
    await db.commit()
    row = await (await db.execute(
        "SELECT * FROM pm_deploys WHERE id = ?", (deploy_id,)
    )).fetchone()
    return _row_to_dict(row)


async def link_tickets_to_deploy(deploy_id: int, ticket_ids: list[int]) -> int:
    """Link tickets to a deploy record. Returns count of links created."""
    if not ticket_ids:
        return 0
    db = await get_pm_db()
    count = 0
    for tid in ticket_ids:
        try:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO pm_deploy_tickets (deploy_id, ticket_id) VALUES (?, ?)",
                (deploy_id, tid),
            )
            if cursor.rowcount > 0:
                count += 1
        except Exception as e:
            log.debug("Failed to link ticket %d to deploy %d: %s", tid, deploy_id, e)
    await db.commit()
    log.info("PM deploy %d: linked %d/%d tickets", deploy_id, count, len(ticket_ids))
    return count


async def get_deploy(deploy_id: int) -> dict | None:
    """Get a deploy by ID with linked ticket IDs.

    Called by resolve_pending_deploys() and available for deploy detail lookup.
    """
    db = await get_pm_db()
    row = await (await db.execute(
        "SELECT * FROM pm_deploys WHERE id = ?", (deploy_id,)
    )).fetchone()
    if not row:
        return None
    deploy = _row_to_dict(row)
    # Attach linked ticket IDs
    tickets = await (await db.execute(
        "SELECT ticket_id FROM pm_deploy_tickets WHERE deploy_id = ?", (deploy_id,)
    )).fetchall()
    deploy["ticket_ids"] = [t["ticket_id"] for t in tickets]
    return deploy


async def find_tickets_by_commits(commit_shas: list[str]) -> list[dict]:
    """Find tickets that reference any of the given commit SHAs.

    Searches the commit_hash field (may contain single SHA or JSON array).
    Available for commit-based ticket lookup (e.g. from CI or post-deploy analysis).
    Note: _create_pm_deploy_record uses git trailer parsing instead.
    """
    if not commit_shas:
        return []
    db = await get_pm_db()
    results = []
    for sha in commit_shas:
        # Match exact SHA or SHA within JSON array (escape LIKE wildcards for safety)
        safe = sha[:12].replace("%", "\\%").replace("_", "\\_")
        cursor = await db.execute(
            "SELECT * FROM pm_tickets WHERE commit_hash LIKE ? ESCAPE '\\'",
            (f"%{safe}%",),
        )
        for row in await cursor.fetchall():
            ticket = _row_to_dict(row)
            if ticket not in results:
                results.append(ticket)
    return results


async def get_child_tickets(parent_ticket_id: int) -> list[dict]:
    """Get all child tickets of a parent ticket.

    Convenience wrapper around list_tickets(parent_ticket_id=...).
    """
    return await list_tickets(parent_ticket_id=parent_ticket_id)


async def get_orphan_tickets(hours: int = 24) -> list[dict]:
    """Find in_progress tickets that haven't been updated in N hours.

    Available for morning briefings and scheduled tasks to flag potentially
    abandoned work.
    """
    hours = max(1, min(int(hours), 8760))  # 1h to 365d
    db = await get_pm_db()
    cursor = await db.execute(
        "SELECT * FROM pm_tickets WHERE status = 'in_progress' "
        "AND updated_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
        (f"-{hours} hours",),
    )
    return [_row_to_dict(r) for r in await cursor.fetchall()]


# ============================================================
# DEPLOY LIFECYCLE — STARTUP RESOLUTION
# ============================================================

async def resolve_pending_deploys() -> dict:
    """Resolve any pm_deploys stuck in 'triggered' status on startup.

    Reads /deploy-triggers/deploy_result.json (written by host watcher)
    and completes the most recent 'triggered' deploy with the actual outcome.
    Called from main.py lifespan after DB is ready.

    Returns a summary dict for logging.
    """
    import json
    from pathlib import Path

    result_path = Path("/deploy-triggers/deploy_result.json")

    db = await get_pm_db()

    # Find the most recent triggered (incomplete) deploy
    row = await (await db.execute(
        "SELECT * FROM pm_deploys WHERE status = 'triggered' "
        "ORDER BY created_at DESC LIMIT 1"
    )).fetchone()

    if not row:
        return {"resolved": False, "reason": "no pending deploys"}

    deploy = _row_to_dict(row)
    deploy_id = deploy["id"]

    # Read deploy result from host watcher
    if not result_path.exists():
        log.info("PM deploy %d still triggered — no deploy_result.json yet", deploy_id)
        return {"resolved": False, "reason": "no deploy_result.json", "deploy_id": deploy_id}

    try:
        result_data = json.loads(result_path.read_text())
    except Exception as e:
        log.warning("PM deploy %d — failed to read deploy_result.json: %s", deploy_id, e)
        return {"resolved": False, "reason": f"parse error: {e}", "deploy_id": deploy_id}

    watcher_status = result_data.get("status", "unknown")

    # Map host watcher statuses to PM deploy statuses
    status_map = {
        "success": "success",
        "rolled_back": "rolled_back",
        "rollback_build_failed": "failed",
        "rollback_unhealthy": "failed",
        "unhealthy": "failed",
        "build_failed": "failed",
        "in_progress": None,  # still running — don't resolve
    }

    pm_status = status_map.get(watcher_status)

    if pm_status is None:
        if watcher_status == "in_progress":
            log.info("PM deploy %d — deploy still in_progress", deploy_id)
            return {"resolved": False, "reason": "deploy still in_progress", "deploy_id": deploy_id}
        # Unknown status — mark as failed to avoid stale 'triggered' records
        pm_status = "failed"
        log.warning("PM deploy %d — unknown watcher status '%s', marking as failed", deploy_id, watcher_status)

    _completed = await complete_deploy(deploy_id, status=pm_status)
    log.info("PM deploy %d resolved: %s (watcher: %s)", deploy_id, pm_status, watcher_status)

    return {
        "resolved": True,
        "deploy_id": deploy_id,
        "pm_status": pm_status,
        "watcher_status": watcher_status,
        "commit_range": f"{deploy.get('commit_from', '?')[:8]}..{deploy.get('commit_to', '?')[:8]}",
    }
