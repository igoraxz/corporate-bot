# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""RBAC CRUD operations — roles, policies, assignments, audit log.

All functions are async and use the shared DB singleton via open_db().
"""

import json
import logging
from datetime import datetime, timezone

from bot.rbac.models import Policy
from bot.storage.db import open_db

log = logging.getLogger(__name__)


def _now_iso() -> str:
    """Current UTC time in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

async def create_role(
    role_id: str,
    name: str,
    description: str = "",
    is_builtin: bool = False,
) -> dict:
    """Create a new role. Returns the role dict."""
    now = _now_iso()
    async with open_db() as db:
        await db.execute(
            "INSERT INTO rbac_roles (role_id, name, description, is_builtin, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (role_id, name, description, int(is_builtin), now, now),
        )
        await db.commit()
    log.info(f"RBAC role created: {role_id} ({name})")
    return {"role_id": role_id, "name": name, "description": description,
            "is_builtin": is_builtin, "created_at": now, "updated_at": now}


async def get_role(role_id: str) -> dict | None:
    """Get a single role by ID. Returns None if not found."""
    async with open_db() as db:
        row = await db.execute_fetchall(
            "SELECT * FROM rbac_roles WHERE role_id = ?", (role_id,)
        )
        if not row:
            return None
        r = row[0]
        return {
            "role_id": r["role_id"], "name": r["name"],
            "description": r["description"], "is_builtin": bool(r["is_builtin"]),
            "created_at": r["created_at"], "updated_at": r["updated_at"],
        }


async def list_roles() -> list[dict]:
    """List all roles."""
    async with open_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM rbac_roles ORDER BY role_id")
    return [
        {
            "role_id": r["role_id"], "name": r["name"],
            "description": r["description"], "is_builtin": bool(r["is_builtin"]),
            "created_at": r["created_at"], "updated_at": r["updated_at"],
        }
        for r in rows
    ]


async def delete_role(role_id: str) -> bool:
    """Delete a role. Blocked for builtin roles. Returns True if deleted."""
    role = await get_role(role_id)
    if role is None:
        return False
    if role["is_builtin"]:
        raise ValueError(f"Cannot delete builtin role: {role_id}")
    async with open_db() as db:
        await db.execute("DELETE FROM rbac_roles WHERE role_id = ?", (role_id,))
        await db.commit()
    log.info(f"RBAC role deleted: {role_id}")
    return True


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------

async def add_policy(
    role_id: str,
    privilege: str,
    object_type: str,
    object_id: str = "*",
    effect: str = "allow",
    conditions: dict | None = None,
) -> int:
    """Add a policy to a role. Returns the policy_id."""
    cond_json = json.dumps(conditions or {})
    async with open_db() as db:
        cursor = await db.execute(
            "INSERT INTO rbac_policies (role_id, privilege, object_type, object_id, effect, conditions)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (role_id, privilege, object_type, object_id, effect, cond_json),
        )
        await db.commit()
        policy_id = cursor.lastrowid
    log.info(f"RBAC policy added: id={policy_id} role={role_id} {effect} {privilege} on {object_type}:{object_id}")
    return policy_id


async def remove_policy(policy_id: int) -> bool:
    """Remove a policy by ID. Returns True if deleted."""
    async with open_db() as db:
        cursor = await db.execute(
            "DELETE FROM rbac_policies WHERE policy_id = ?", (policy_id,)
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_policies_for_role(role_id: str) -> list[Policy]:
    """Get all policies for a role. Returns list of Policy objects."""
    async with open_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM rbac_policies WHERE role_id = ? ORDER BY policy_id",
            (role_id,),
        )
    result = []
    for r in rows:
        try:
            cond = json.loads(r["conditions"]) if r["conditions"] else {}
        except (json.JSONDecodeError, TypeError):
            cond = {}
        result.append(Policy(
            policy_id=r["policy_id"],
            role_id=r["role_id"],
            privilege=r["privilege"],
            object_type=r["object_type"],
            object_id=r["object_id"],
            effect=r["effect"],
            conditions=cond,
        ))
    return result


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------

async def assign_role(
    subject_type: str,
    subject_id: str,
    role_id: str,
    granted_by: str = "",
    expires_at: str | None = None,
) -> int:
    """Assign a role to a subject. Returns the assignment_id.

    subject_type: "user" | "team" | "chat"
    """
    now = _now_iso()
    async with open_db() as db:
        cursor = await db.execute(
            "INSERT OR REPLACE INTO rbac_assignments"
            " (subject_type, subject_id, role_id, granted_by, granted_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (subject_type, subject_id, role_id, granted_by, now, expires_at),
        )
        await db.commit()
        assignment_id = cursor.lastrowid
    log.info(f"RBAC assignment: {subject_type}:{subject_id} -> {role_id} (by {granted_by})")
    return assignment_id


async def revoke_role(subject_type: str, subject_id: str, role_id: str) -> bool:
    """Revoke a role from a subject. Returns True if revoked."""
    async with open_db() as db:
        cursor = await db.execute(
            "DELETE FROM rbac_assignments"
            " WHERE subject_type = ? AND subject_id = ? AND role_id = ?",
            (subject_type, subject_id, role_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_assignments_for_subject(
    subject_type: str,
    subject_id: str,
) -> list[dict]:
    """Get all role assignments for a subject."""
    async with open_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM rbac_assignments"
            " WHERE subject_type = ? AND subject_id = ?"
            " ORDER BY granted_at",
            (subject_type, subject_id),
        )
    return [
        {
            "assignment_id": r["assignment_id"],
            "subject_type": r["subject_type"],
            "subject_id": r["subject_id"],
            "role_id": r["role_id"],
            "granted_by": r["granted_by"],
            "granted_at": r["granted_at"],
            "expires_at": r["expires_at"],
        }
        for r in rows
    ]


async def get_all_assignments() -> list[dict]:
    """Get all role assignments (used by PolicyEngine cache refresh)."""
    async with open_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM rbac_assignments ORDER BY granted_at"
        )
    return [
        {
            "assignment_id": r["assignment_id"],
            "subject_type": r["subject_type"],
            "subject_id": r["subject_id"],
            "role_id": r["role_id"],
            "granted_by": r["granted_by"],
            "granted_at": r["granted_at"],
            "expires_at": r["expires_at"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

async def log_access(
    subject_id: str,
    privilege: str,
    object_type: str,
    object_id: str,
    effect: str,
    policy_id: int | None = None,
    role_id: str = "",
    session_key: str = "",
    tool_name: str = "",
) -> None:
    """Write an access decision to the audit log."""
    now = _now_iso()
    try:
        async with open_db() as db:
            await db.execute(
                "INSERT INTO rbac_access_log"
                " (timestamp, subject_id, privilege, object_type, object_id,"
                "  effect, policy_id, role_id, session_key, tool_name)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now, subject_id, privilege, object_type, object_id,
                 effect, policy_id, role_id, session_key, tool_name),
            )
            await db.commit()
    except Exception as e:
        # Audit log failures must never block access decisions
        log.warning(f"RBAC audit log write failed: {e}")
