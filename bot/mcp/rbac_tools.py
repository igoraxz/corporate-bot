# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""RBAC MCP tools — admin-only role, policy, and assignment management.

Provides the manage_rbac tool for administering the RBAC system:
roles, policies, assignments, access checks, and audit log queries.

Admin-gated: the tool itself is protected by RBAC — only subjects with
"admin" privilege on "tools" object "manage_rbac" can invoke it.
"""

import logging
from typing import Any

from claude_agent_sdk import tool

from bot.mcp import _safe_int, _run_with_timeout

log = logging.getLogger(__name__)


@tool("manage_rbac", "Manage RBAC roles, policies, and assignments. Admin only.", {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "description": (
                "list_roles | get_role | create_role | delete_role | "
                "add_policy | remove_policy | assign_role | revoke_role | "
                "check_access | audit"
            ),
        },
        "role_id": {"type": "string", "description": "Role ID (for role/policy operations)"},
        "subject_type": {"type": "string", "description": "user | team | chat (for assignments)"},
        "subject_id": {"type": "string", "description": "Subject ID (AAD ID, team ID, or chat ID)"},
        "privilege": {"type": "string", "description": "admin | read | write | use | execute"},
        "object_type": {"type": "string", "description": "knowledge_domain | credentials | tools | chat_data | filesystem | network"},
        "object_id": {"type": "string", "description": "Object ID or prefix pattern"},
        "policy_id": {"type": ["integer", "string"], "description": "Policy ID (for remove_policy)"},
        "effect": {"type": "string", "description": "allow | deny"},
        "name": {"type": "string", "description": "Display name (for create_role)"},
        "limit": {"type": ["integer", "string"], "description": "Max results for audit (default 20)"},
    },
    "required": ["action"],
})
async def manage_rbac(args: dict[str, Any]) -> dict:
    """Manage RBAC roles, policies, and assignments."""
    _sk = args.pop("_session_key", "")
    action = (args.get("action") or "").strip().lower()

    # Defense-in-depth: verify caller has admin privilege (security audit finding #5).
    # Do NOT rely solely on hook-layer RBAC — check here too.
    try:
        from bot.rbac import get_engine
        from bot.core.session_state import _get_or_create_state
        _state = _get_or_create_state(_sk) if _sk else {}
        _user_ctx = _state.get("current_user_ctx", {}) if isinstance(_state, dict) else {}
        # Flat permission model: DM = user subject, group = chat subject
        from bot.core.access import build_rbac_subject as _build_subj_rbac
        _caller_subject = _build_subj_rbac(
            source=_user_ctx.get("source", ""),
            user_id=_user_ctx.get("user_id", ""),
            chat_id=_state.get("origin_chat_id", "") if isinstance(_state, dict) else "",
        )
        _admin_check = await get_engine().check_access(_caller_subject, "admin", "tools", "manage_rbac")
        if not _admin_check.allowed:
            return {"error": "Permission denied: manage_rbac requires admin access"}
    except Exception as e:
        log.warning("manage_rbac self-auth check failed: %s — denying (fail-closed)", type(e).__name__)
        return {"error": "Permission denied: authorization check unavailable"}

    async def _dispatch():
        if action == "list_roles":
            return await _list_roles()
        elif action == "get_role":
            return await _get_role(args)
        elif action == "create_role":
            return await _create_role(args)
        elif action == "delete_role":
            return await _delete_role(args)
        elif action == "add_policy":
            return await _add_policy(args)
        elif action == "remove_policy":
            return await _remove_policy(args)
        elif action == "assign_role":
            return await _assign_role(args)
        elif action == "revoke_role":
            return await _revoke_role(args)
        elif action == "check_access":
            return await _check_access(args)
        elif action == "audit":
            return await _audit(args)
        else:
            return {
                "error": (
                    f"Unknown action: {action}. Use: list_roles, get_role, "
                    "create_role, delete_role, add_policy, remove_policy, "
                    "assign_role, revoke_role, check_access, audit"
                )
            }

    return await _run_with_timeout(_dispatch(), "manage_rbac")


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

async def _list_roles() -> dict:
    """List all roles with policy counts."""
    from bot.rbac.store import list_roles, get_policies_for_role

    roles = await list_roles()
    result = []
    for r in roles:
        policies = await get_policies_for_role(r["role_id"])
        result.append({
            "role_id": r["role_id"],
            "name": r["name"],
            "description": r["description"],
            "is_builtin": r["is_builtin"],
            "policy_count": len(policies),
        })
    return {"roles": result, "count": len(result)}


async def _get_role(args: dict) -> dict:
    """Show role details with all policies."""
    from bot.rbac.store import get_role, get_policies_for_role, get_all_assignments

    role_id = (args.get("role_id") or "").strip()
    if not role_id:
        return {"error": "role_id is required for get_role"}

    role = await get_role(role_id)
    if not role:
        return {"error": f"Role '{role_id}' not found"}

    policies = await get_policies_for_role(role_id)
    policy_dicts = [
        {
            "policy_id": p.policy_id,
            "privilege": p.privilege,
            "object_type": p.object_type,
            "object_id": p.object_id,
            "effect": p.effect,
        }
        for p in policies
    ]

    # Get assignments for this role
    all_assignments = await get_all_assignments()
    assignments = [
        {
            "subject_type": a["subject_type"],
            "subject_id": a["subject_id"],
            "granted_by": a["granted_by"],
            "granted_at": a["granted_at"],
            "expires_at": a["expires_at"],
        }
        for a in all_assignments
        if a["role_id"] == role_id
    ]

    return {
        **role,
        "policies": policy_dicts,
        "assignments": assignments,
    }


async def _create_role(args: dict) -> dict:
    """Create a new custom role."""
    from bot.rbac.store import create_role

    role_id = (args.get("role_id") or "").strip()
    name = (args.get("name") or "").strip()
    if not role_id:
        return {"error": "role_id is required for create_role"}
    if not name:
        return {"error": "name is required for create_role"}

    try:
        role = await create_role(role_id=role_id, name=name)
        # Invalidate engine cache so new role is visible immediately
        _invalidate_cache()
        return {"created": True, **role}
    except Exception as e:
        return {"error": str(e)}


async def _delete_role(args: dict) -> dict:
    """Delete a non-builtin role."""
    from bot.rbac.store import delete_role

    role_id = (args.get("role_id") or "").strip()
    if not role_id:
        return {"error": "role_id is required for delete_role"}

    try:
        deleted = await delete_role(role_id)
        if deleted:
            _invalidate_cache()
            return {"deleted": True, "role_id": role_id}
        return {"error": f"Role '{role_id}' not found"}
    except ValueError as e:
        return {"error": str(e)}


async def _add_policy(args: dict) -> dict:
    """Add a policy to a role."""
    from bot.rbac.store import add_policy
    from bot.rbac.models import PRIVILEGES, OBJECT_TYPES, EFFECTS

    role_id = (args.get("role_id") or "").strip()
    privilege = (args.get("privilege") or "").strip()
    object_type = (args.get("object_type") or "").strip()
    object_id = (args.get("object_id") or "*").strip()
    effect = (args.get("effect") or "allow").strip()

    if not role_id:
        return {"error": "role_id is required for add_policy"}
    if not privilege:
        return {"error": "privilege is required (admin | read | write | use | execute)"}
    if not object_type:
        return {"error": "object_type is required (knowledge_domain | credentials | tools | chat_data | filesystem | network)"}
    if privilege not in PRIVILEGES:
        return {"error": f"Invalid privilege '{privilege}'. Valid: {', '.join(sorted(PRIVILEGES))}"}
    if object_type not in OBJECT_TYPES:
        return {"error": f"Invalid object_type '{object_type}'. Valid: {', '.join(sorted(OBJECT_TYPES))}"}
    if effect not in EFFECTS:
        return {"error": f"Invalid effect '{effect}'. Valid: allow, deny"}

    try:
        policy_id = await add_policy(
            role_id=role_id,
            privilege=privilege,
            object_type=object_type,
            object_id=object_id,
            effect=effect,
        )
        _invalidate_cache()
        return {
            "added": True,
            "policy_id": policy_id,
            "role_id": role_id,
            "privilege": privilege,
            "object_type": object_type,
            "object_id": object_id,
            "effect": effect,
        }
    except Exception as e:
        return {"error": str(e)}


async def _remove_policy(args: dict) -> dict:
    """Remove a policy by ID."""
    from bot.rbac.store import remove_policy

    raw_id = args.get("policy_id") or args.get("object_id") or ""
    policy_id = _safe_int(raw_id, 0)
    if not policy_id:
        return {"error": "A numeric policy_id is required for remove_policy."}

    removed = await remove_policy(policy_id)
    if removed:
        _invalidate_cache()
        return {"removed": True, "policy_id": policy_id}
    return {"error": f"Policy {policy_id} not found"}


async def _assign_role(args: dict) -> dict:
    """Assign a role to a user/team/chat."""
    from bot.rbac.store import assign_role

    subject_type = (args.get("subject_type") or "").strip()
    subject_id = (args.get("subject_id") or "").strip()
    role_id = (args.get("role_id") or "").strip()

    if not subject_type:
        return {"error": "subject_type is required (user | team | chat)"}
    if subject_type not in ("user", "team", "chat"):
        return {"error": f"Invalid subject_type '{subject_type}'. Valid: user, team, chat"}
    if not subject_id:
        return {"error": "subject_id is required"}
    if not role_id:
        return {"error": "role_id is required"}

    try:
        assignment_id = await assign_role(
            subject_type=subject_type,
            subject_id=subject_id,
            role_id=role_id,
            granted_by="manage_rbac",
        )
        _invalidate_cache()
        return {
            "assigned": True,
            "assignment_id": assignment_id,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "role_id": role_id,
        }
    except Exception as e:
        return {"error": str(e)}


async def _revoke_role(args: dict) -> dict:
    """Remove a role assignment."""
    from bot.rbac.store import revoke_role

    subject_type = (args.get("subject_type") or "").strip()
    subject_id = (args.get("subject_id") or "").strip()
    role_id = (args.get("role_id") or "").strip()

    if not subject_type or not subject_id or not role_id:
        return {"error": "subject_type, subject_id, and role_id are all required for revoke_role"}

    revoked = await revoke_role(subject_type, subject_id, role_id)
    if revoked:
        _invalidate_cache()
        return {
            "revoked": True,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "role_id": role_id,
        }
    return {"error": "Assignment not found"}


async def _check_access(args: dict) -> dict:
    """Test if a subject has a privilege on an object."""
    from bot.rbac import get_engine
    from bot.rbac.models import Subject

    subject_id = (args.get("subject_id") or "").strip()
    subject_type = (args.get("subject_type") or "user").strip()
    privilege = (args.get("privilege") or "use").strip()
    object_type = (args.get("object_type") or "tools").strip()
    object_id = (args.get("object_id") or "*").strip()

    if not subject_id:
        return {"error": "subject_id is required for check_access"}

    # Build a Subject based on subject_type
    if subject_type == "user":
        subject = Subject(user_id=subject_id)
    elif subject_type == "team":
        subject = Subject(team_ids=(subject_id,))
    elif subject_type == "chat":
        subject = Subject(chat_id=subject_id)
    else:
        return {"error": f"Invalid subject_type '{subject_type}'. Valid: user, team, chat"}

    engine = get_engine()
    decision = await engine.check_access(
        subject=subject,
        privilege=privilege,
        object_type=object_type,
        object_id=object_id,
        log_decision=False,  # Don't pollute audit log with test queries
    )
    return {
        "allowed": decision.allowed,
        "reason": decision.reason,
        "policy_id": decision.policy_id,
        "role_id": decision.role_id,
        "query": {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "privilege": privilege,
            "object_type": object_type,
            "object_id": object_id,
        },
    }


async def _audit(args: dict) -> dict:
    """Show recent access decisions from rbac_access_log."""
    from bot.storage.db import open_db

    limit = _safe_int(args.get("limit"), 20)
    if limit < 1:
        limit = 20
    if limit > 200:
        limit = 200

    async with open_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM rbac_access_log ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )

    entries = []
    for r in rows:
        entries.append({
            "timestamp": r["timestamp"],
            "subject_id": r["subject_id"],
            "privilege": r["privilege"],
            "object_type": r["object_type"],
            "object_id": r["object_id"],
            "effect": r["effect"],
            "policy_id": r["policy_id"],
            "role_id": r["role_id"],
            "tool_name": r["tool_name"],
            "session_key": r["session_key"],
        })
    return {"entries": entries, "count": len(entries), "limit": limit}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _invalidate_cache() -> None:
    """Invalidate the RBAC engine cache so changes take effect immediately."""
    try:
        from bot.rbac import get_engine
        get_engine().invalidate_cache()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tool list for server registration
# ---------------------------------------------------------------------------

RBAC_TOOLS = [manage_rbac]

__all__ = ["manage_rbac", "RBAC_TOOLS"]
