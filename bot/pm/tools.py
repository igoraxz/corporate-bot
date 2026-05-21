# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""PM MCP tools — 20 tools for project/sprint/ticket management and orchestration.

Phase 0: 11 data model tools (CRUD for projects, sprints, tickets).
Phase 1-2: 5 orchestration tools (agent dispatch, pipeline execution, budget).
Phase 3: 2 convenience tools (plan, auto_ticket).
Phase 4: 2 retro/summary tools (sprint retrospective, sprint demo summary).

All tools are harness-gated (enforced by hooks.py Layer 1b2 via _EXPLICIT_HARNESS_TOOLS).
Non-admin coding chats can use PM tools if their harness includes mcp__bot-pm__ in allowed_tools.
Discovered chats are blocked via Layer 1c prefix matching.
"""

import logging
import os
from typing import Any

from claude_agent_sdk import tool

from bot.mcp import _text_response

log = logging.getLogger(__name__)


# ============================================================
# HELPERS
# ============================================================

def _derive_chat_key(args: dict) -> str:
    """Derive chat_key from args for per-chat project scoping.

    Priority: _session_key (injected by session-aware wrapper) → explicit
    chat_key → chat_id → 'default'. Session key ensures proper per-chat
    isolation even when model doesn't provide chat_key explicitly.
    """
    session_key = args.pop("_session_key", "")
    key = session_key or args.get("chat_key") or args.get("chat_id") or "default"
    if key == "default":
        log.debug("PM: using 'default' chat_key — projects won't be isolated per chat")
    return key


async def _resolve_ticket_id(val) -> int:
    """Resolve ticket_id argument to an integer DB id.

    Accepts:
      - int or int-coercible string: '4' → 4
      - Jira-style refs: 'FB-3' → looks up project key + ticket_number → id
    Raises ValueError if the reference is not found.
    """
    from bot.pm.store import resolve_ticket_ref
    result = await resolve_ticket_ref(str(val))
    if result is None:
        raise ValueError(f"Ticket not found: {val!r}")
    return result


async def _check_project_access(
    args: dict, project_id: int, privilege: str = "write"
) -> str | None:
    """Check if the calling session has access to a project.

    Returns None if access is granted, or an error message if denied.
    Uses RBAC (pm_project object type) + chat_key ownership as fallback.
    """
    from bot.pm.store import check_project_access
    session_key = args.get("_session_key", "")
    chat_key = session_key or args.get("chat_key") or args.get("chat_id") or "default"
    if not await check_project_access(chat_key, project_id, privilege=privilege):
        return f"Access denied: project {project_id} is not accessible from this chat"
    return None


async def _check_sprint_access(
    args: dict, sprint_id: int, privilege: str = "write"
) -> str | None:
    """Check sprint access by resolving its parent project."""
    from bot.pm.store import get_sprint
    sprint = await get_sprint(sprint_id)
    if not sprint:
        return f"Sprint {sprint_id} not found"
    return await _check_project_access(args, sprint["project_id"], privilege=privilege)


def _resolve_harness_config(args: dict) -> tuple[str, str]:
    """Resolve the calling session's harness label + project_dir.

    Uses _session_key (injected by session-aware PM server wrapper) to find
    the harness assigned to the calling chat. Returns (harness_label, project_dir)
    so dispatched agents inherit the full harness config (model, effort, max_turns,
    project_dir) from the calling coding chat.

    Returns ("", "") if harness can't be resolved (agents use defaults).
    """
    # Pop _session_key to match messaging/memory tool pattern
    session_key = args.pop("_session_key", "")
    if not session_key:
        return ("", "")
    try:
        from bot.harness import get_harness_for_chat, resolve_field, get_harness_cwd
        harness = get_harness_for_chat(session_key)
        label = harness.label or "default"
        # Resolve project_dir: per-chat override first, then harness-level
        pd = resolve_field(harness, "project_dir", session_key)
        if pd:
            # Defense-in-depth: validate path is safe
            if ".." in pd or not pd.startswith("/app/data/"):
                log.warning("PM: project_dir rejected (unsafe path): %s", pd)
                pd = ""
        elif label != "default":
            pd = get_harness_cwd(label, chat_key=session_key)
        return (label, pd)
    except Exception as e:
        log.warning("PM: failed to resolve harness config: %s", e)
        return ("", "")


# ============================================================
# PROJECT TOOLS
# ============================================================

@tool("pm_create_project", "Create a new PM project in the current coding chat. Returns the created project.", {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Project name"},
        "key": {"type": "string", "description": "Short UPPERCASE project key (e.g. 'FB'). Auto-derived from name if omitted."},
        "brief": {"type": "string", "description": "Project brief / objective"},
        "budget_usd": {"type": "number", "description": "Total budget cap in USD (optional)"},
        "chat_key": {"type": "string", "description": "Chat key for scoping (auto-derived if omitted)"},
    },
    "required": ["name"],
})
async def pm_create_project(args: dict[str, Any]) -> dict:
    from bot.pm.store import create_project
    try:
        budget = args.get("budget_usd")
        if budget is not None and budget < 0:
            return _text_response({"error": "budget_usd must be >= 0"})
        session_key = args.get("_session_key", "")
        chat_key = _derive_chat_key(args)
        result = await create_project(
            chat_key=chat_key,
            name=args["name"],
            key=args.get("key"),
            brief=args.get("brief", ""),
            budget_usd=budget,
            visibility="private",
            owner_user_id=session_key,
        )
        return _text_response({"success": True, "project": result})
    except Exception as e:
        log.error(f"pm_create_project failed: {e}", exc_info=True)
        return _text_response({"error": str(e)})


@tool("pm_list_projects", "List PM projects for the current coding chat.", {
    "type": "object",
    "properties": {
        "chat_key": {"type": "string", "description": "Chat key for scoping (auto-derived if omitted)"},
        "status": {"type": "string", "description": "Filter by status: active, completed, archived"},
    },
    "required": [],
})
async def pm_list_projects(args: dict[str, Any]) -> dict:
    from bot.pm.store import list_accessible_projects
    try:
        chat_key = _derive_chat_key(args)
        result = await list_accessible_projects(chat_key, status=args.get("status"))
        return _text_response({"projects": result, "count": len(result)})
    except Exception as e:
        log.error(f"pm_list_projects failed: {e}", exc_info=True)
        return _text_response({"error": str(e)})


# ============================================================
# SPRINT TOOLS
# ============================================================

@tool("pm_create_sprint", "Create a new sprint within a PM project.", {
    "type": "object",
    "properties": {
        "project_id": {"type": "integer", "description": "Parent project ID"},
        "name": {"type": "string", "description": "Sprint name"},
        "goal": {"type": "string", "description": "Sprint goal / objective"},
        "budget_usd": {"type": "number", "description": "Sprint budget cap in USD"},
    },
    "required": ["project_id", "name"],
})
async def pm_create_sprint(args: dict[str, Any]) -> dict:
    from bot.pm.store import create_sprint
    try:
        budget = args.get("budget_usd")
        if budget is not None and budget < 0:
            return _text_response({"error": "budget_usd must be >= 0"})
        _pid = int(args["project_id"])
        _err = await _check_project_access(args, _pid)
        if _err:
            return _text_response({"error": _err})
        result = await create_sprint(
            project_id=_pid,
            name=args["name"],
            goal=args.get("goal", ""),
            budget_usd=budget,
        )
        return _text_response({"success": True, "sprint": result})
    except Exception as e:
        log.error(f"pm_create_sprint failed: {e}", exc_info=True)
        return _text_response({"error": str(e)})


@tool("pm_transition_sprint", "Transition a sprint status (draft->approved->running->completed/aborted). Completing requires all tickets to be done/dropped.", {
    "type": "object",
    "properties": {
        "sprint_id": {"type": "integer", "description": "Sprint ID"},
        "action": {"type": "string", "description": "Target status: approved, running, completed, aborted"},
    },
    "required": ["sprint_id", "action"],
})
async def pm_transition_sprint(args: dict[str, Any]) -> dict:
    from bot.pm.store import transition_sprint
    try:
        _sid = int(args["sprint_id"])
        _err = await _check_sprint_access(args, _sid)
        if _err:
            return _text_response({"error": _err})
        result = await transition_sprint(
            sprint_id=_sid,
            to_status=args["action"],
        )
        return _text_response({"success": True, "sprint": result})
    except Exception as e:
        log.error(f"pm_transition_sprint failed: {e}", exc_info=True)
        return _text_response({"error": str(e)})


@tool("pm_cancel_sprint", "Cancel a running sprint — aborts the sprint and drops all non-terminal tickets.", {
    "type": "object",
    "properties": {
        "sprint_id": {"type": "integer", "description": "Sprint ID"},
        "reason": {"type": "string", "description": "Reason for cancellation"},
    },
    "required": ["sprint_id"],
})
async def pm_cancel_sprint(args: dict[str, Any]) -> dict:
    from bot.pm.store import transition_sprint, list_tickets, move_ticket
    try:
        sprint_id = int(args["sprint_id"])
        _err = await _check_sprint_access(args, sprint_id)
        if _err:
            return _text_response({"error": _err})
        reason = args.get("reason", "Sprint cancelled")

        # Drop all non-terminal tickets first
        tickets = await list_tickets(sprint_id)
        dropped = 0
        for t in tickets:
            if t["status"] not in ("done", "dropped"):
                try:
                    await move_ticket(t["id"], "dropped", actor="pm", reason=reason)
                    dropped += 1
                except ValueError:
                    pass  # skip if transition not allowed

        # Abort the sprint
        result = await transition_sprint(sprint_id, "aborted", actor="pm")

        return _text_response({
            "success": True,
            "sprint": result,
            "tickets_dropped": dropped,
            "reason": reason,
        })
    except Exception as e:
        log.error(f"pm_cancel_sprint failed: {e}", exc_info=True)
        return _text_response({"error": str(e)})


# ============================================================
# TICKET TOOLS
# ============================================================

@tool("pm_create_ticket", "Create a new ticket within a sprint.", {
    "type": "object",
    "properties": {
        "sprint_id": {"type": "integer", "description": "Parent sprint ID"},
        "title": {"type": "string", "description": "Ticket title"},
        "description": {"type": "string", "description": "Detailed ticket description"},
        "tier": {"type": "string", "description": "Complexity tier: small, medium, significant (default: significant)"},
        "budget_usd": {"type": "number", "description": "Ticket budget cap in USD"},
        "priority": {"type": "integer", "description": "Execution priority (lower = higher priority, default: 100)"},
        "estimated_cost_usd": {"type": "number", "description": "Pre-execution cost estimate for velocity tracking"},
        "operator_request": {"type": "string", "description": "Original user message that triggered this work"},
        "parent_ticket_id": {"type": "integer", "description": "Parent ticket ID (for bugs/subtasks found during work)"},
        "source_chat_id": {"type": "string", "description": "Originating chat ID"},
    },
    "required": ["sprint_id", "title"],
})
async def pm_create_ticket(args: dict[str, Any]) -> dict:
    from bot.pm.store import create_ticket, get_ticket_ref
    try:
        _sid = int(args["sprint_id"])
        _err = await _check_sprint_access(args, _sid)
        if _err:
            return _text_response({"error": _err})
        budget = args.get("budget_usd")
        if budget is not None and budget < 0:
            return _text_response({"error": "budget_usd must be >= 0"})
        estimated = args.get("estimated_cost_usd")
        if estimated is not None and estimated < 0:
            return _text_response({"error": "estimated_cost_usd must be >= 0"})
        try:
            priority = int(args.get("priority", 100))
        except (ValueError, TypeError):
            priority = 100
        if priority < 0 or priority > 10000:
            return _text_response({"error": "priority must be 0-10000"})
        parent_id = args.get("parent_ticket_id")
        if parent_id is not None:
            try:
                parent_id = int(parent_id)
            except (ValueError, TypeError):
                return _text_response({"error": "parent_ticket_id must be an integer"})
        result = await create_ticket(
            sprint_id=int(args["sprint_id"]),
            title=args["title"],
            description=args.get("description", ""),
            tier=args.get("tier", "significant"),
            budget_usd=budget,
            priority=priority,
            estimated_cost_usd=estimated,
            operator_request=args.get("operator_request"),
            parent_ticket_id=parent_id,
            source_chat_id=args.get("source_chat_id"),
        )
        ref = await get_ticket_ref(result["id"])
        return _text_response({"success": True, "ref": ref, "ticket": result})
    except Exception as e:
        log.error(f"pm_create_ticket failed: {e}", exc_info=True)
        return _text_response({"error": str(e)})


@tool("pm_update_ticket", "Update ticket fields. Use sprint_id to reassign ticket between sprints (with budget resync). Does NOT change status — use pm_move_ticket for that.", {
    "type": "object",
    "properties": {
        "ticket_id": {"type": "integer", "description": "Ticket ID"},
        "title": {"type": "string", "description": "New title"},
        "description": {"type": "string", "description": "New description"},
        "tier": {"type": "string", "description": "New tier: small, medium, significant"},
        "sprint_id": {"type": "integer", "description": "Move ticket to a different sprint (same project). Triggers budget resync on both sprints."},
        "assignee": {"type": "string", "description": "Agent role assigned to this ticket"},
        "budget_usd": {"type": "number", "description": "Updated budget cap"},
        "priority": {"type": "integer", "description": "Execution priority (lower = higher priority)"},
        "estimated_cost_usd": {"type": "number", "description": "Pre-execution cost estimate"},
        "tcd": {"type": "string", "description": "Ticket Context Document (JSON string)"},
        "commit_hash": {"type": "string", "description": "Git commit SHA(s) linked to this ticket"},
        "operator_request": {"type": "string", "description": "Original user message"},
        "parent_ticket_id": {"type": "integer", "description": "Parent ticket ID"},
        "source_chat_id": {"type": "string", "description": "Originating chat ID"},
    },
    "required": ["ticket_id"],
})
async def pm_update_ticket(args: dict[str, Any]) -> dict:
    from bot.pm.store import update_ticket, get_ticket, get_ticket_ref, reassign_ticket_sprint
    try:
        ticket_id = await _resolve_ticket_id(args["ticket_id"])
        # Validate ALL inputs before any mutations
        new_sprint_id = None
        if "sprint_id" in args:
            try:
                new_sprint_id = int(args["sprint_id"])
            except (ValueError, TypeError):
                return _text_response({"error": "sprint_id must be an integer"})
        for nonneg in ("budget_usd", "estimated_cost_usd"):
            if nonneg in args and args[nonneg] is not None and args[nonneg] < 0:
                return _text_response({"error": f"{nonneg} must be >= 0"})
        if "priority" in args:
            try:
                p = int(args["priority"])
            except (ValueError, TypeError):
                return _text_response({"error": "priority must be an integer"})
            if p < 0 or p > 10000:
                return _text_response({"error": "priority must be 0-10000"})
            args["priority"] = p
        # Execute sprint reassignment first (validated above)
        if new_sprint_id is not None:
            await reassign_ticket_sprint(ticket_id, new_sprint_id)
        # Explicit allowlist — never pass unknown keys to store
        update_fields = {}
        for key in ("title", "description", "tier", "assignee", "budget_usd", "priority",
                     "estimated_cost_usd", "tcd", "commit_hash", "operator_request",
                     "parent_ticket_id", "source_chat_id"):
            if key in args:
                update_fields[key] = args[key]
        if update_fields:
            result = await update_ticket(ticket_id, **update_fields)
        else:
            result = await get_ticket(ticket_id)
        if result is None:
            return _text_response({"error": f"Ticket {ticket_id} not found"})
        ref = await get_ticket_ref(ticket_id)
        return _text_response({"success": True, "ref": ref, "ticket": result})
    except Exception as e:
        log.error(f"pm_update_ticket failed: {e}", exc_info=True)
        return _text_response({"error": str(e)})


@tool("pm_move_ticket", "Move a ticket to a new status. Validates transition rules and logs the change.", {
    "type": "object",
    "properties": {
        "ticket_id": {"type": "integer", "description": "Ticket ID"},
        "to_status": {"type": "string", "description": "Target status: backlog, ready, in_progress, review, done, dropped"},
        "reason": {"type": "string", "description": "Reason for the transition"},
    },
    "required": ["ticket_id", "to_status"],
})
async def pm_move_ticket(args: dict[str, Any]) -> dict:
    from bot.pm.store import move_ticket, get_ticket_ref
    try:
        ticket_id = await _resolve_ticket_id(args["ticket_id"])
        result = await move_ticket(
            ticket_id=ticket_id,
            to_status=args["to_status"],
            actor=args.get("actor", "pm"),
            reason=args.get("reason"),
        )
        ref = await get_ticket_ref(ticket_id)
        # Clear active_ticket_id when ticket is completed or dropped
        if args["to_status"] in ("done", "dropped"):
            try:
                _mv_sk = args.get("_session_key", "")
                if _mv_sk:
                    from bot.core.session_state import _get_or_create_state
                    _st = _get_or_create_state(_mv_sk)
                    if _st.get("active_ticket_id") == ticket_id:
                        _st["active_ticket_id"] = None
            except Exception:
                pass
        return _text_response({"success": True, "ref": ref, "ticket": result})
    except Exception as e:
        log.error(f"pm_move_ticket failed: {e}", exc_info=True)
        return _text_response({"error": str(e)})


@tool("pm_reset_ticket", "Reset a stuck ticket back to ready status — clears TCD and marks stale runs as failed. Use when a ticket is stuck in_progress due to pipeline failure.", {
    "type": "object",
    "properties": {
        "ticket_id": {"type": "integer", "description": "Ticket ID"},
        "reason": {"type": "string", "description": "Reason for reset"},
        "clear_tcd": {"type": "boolean", "description": "Clear TCD (default: true — fresh start)"},
    },
    "required": ["ticket_id"],
})
async def pm_reset_ticket(args: dict[str, Any]) -> dict:
    from bot.pm.store import move_ticket, update_ticket, cleanup_stale_runs, get_ticket_ref
    try:
        ticket_id = await _resolve_ticket_id(args["ticket_id"])
        reason = args.get("reason", "Manual reset — stuck ticket recovery")
        clear_tcd = args.get("clear_tcd", True)

        # Clean up stale runs
        cleaned = await cleanup_stale_runs(ticket_id)

        # Clear TCD if requested
        if clear_tcd:
            await update_ticket(ticket_id, tcd=None)

        # Move back to ready
        result = await move_ticket(ticket_id, "ready", actor="pm", reason=reason)
        ref = await get_ticket_ref(ticket_id)

        return _text_response({
            "success": True,
            "ref": ref,
            "ticket": result,
            "stale_runs_cleaned": cleaned,
            "tcd_cleared": clear_tcd,
        })
    except Exception as e:
        log.error(f"pm_reset_ticket failed: {e}", exc_info=True)
        return _text_response({"error": str(e)})


@tool("pm_list_tickets", "List tickets with flexible filtering. At least one of sprint_id, project_id, or parent_ticket_id is required.", {
    "type": "object",
    "properties": {
        "sprint_id": {"type": "integer", "description": "Filter by sprint ID"},
        "project_id": {"type": "integer", "description": "List all tickets across all sprints in a project"},
        "parent_ticket_id": {"type": "integer", "description": "List child tickets of a parent (for follow-up counting/escalation)"},
        "status": {"type": "string", "description": "Filter by status: backlog, ready, in_progress, review, done, dropped"},
    },
    "required": [],
})
async def pm_list_tickets(args: dict[str, Any]) -> dict:
    from bot.pm.store import list_tickets
    try:
        sprint_id = int(args["sprint_id"]) if args.get("sprint_id") else None
        project_id = int(args["project_id"]) if args.get("project_id") else None
        parent_id = int(args["parent_ticket_id"]) if args.get("parent_ticket_id") else None
        if not sprint_id and not project_id and not parent_id:
            return _text_response({"error": "At least one of sprint_id, project_id, or parent_ticket_id is required"})
        result = await list_tickets(
            sprint_id=sprint_id,
            project_id=project_id,
            parent_ticket_id=parent_id,
            status=args.get("status"),
        )
        return _text_response({"tickets": result, "count": len(result)})
    except Exception as e:
        log.error(f"pm_list_tickets failed: {e}", exc_info=True)
        return _text_response({"error": str(e)})


# ============================================================
# BOARD VIEW
# ============================================================

@tool("pm_board", "Get a kanban-style board view of the current sprint. Shows tickets grouped by status with budget summary.", {
    "type": "object",
    "properties": {
        "project_id": {"type": "integer", "description": "Project ID"},
        "sprint_id": {"type": "integer", "description": "Sprint ID (optional — defaults to current active sprint)"},
    },
    "required": ["project_id"],
})
async def pm_board(args: dict[str, Any]) -> dict:
    from bot.pm.store import get_board
    try:
        result = await get_board(
            project_id=int(args["project_id"]),
            sprint_id=int(args["sprint_id"]) if args.get("sprint_id") else None,
        )
        return _text_response(result)
    except Exception as e:
        log.error(f"pm_board failed: {e}", exc_info=True)
        return _text_response({"error": str(e)})


# ============================================================
# ORCHESTRATION TOOLS (Phase 1+2)
# ============================================================

@tool("pm_dispatch_agent", "Dispatch a single agent (pm/engineer/verifier, or legacy reviewer/qa) to work on a ticket. Returns the agent's output and execution metrics.", {
    "type": "object",
    "properties": {
        "ticket_id": {"type": "integer", "description": "Ticket ID"},
        "role": {"type": "string", "description": "Agent role: pm, engineer, verifier (or legacy: reviewer, qa)"},
        "chat_id": {"type": "string", "description": "Chat ID for access scoping (auto-derived if omitted)"},
    },
    "required": ["ticket_id", "role"],
})
async def pm_dispatch_agent(args: dict[str, Any]) -> dict:
    import asyncio
    from bot.pm.orchestrator import dispatch_agent, load_or_create_tcd, _ticket_locks
    from bot.pm.roles import ROLES
    from bot.pm.store import update_ticket
    from bot.pm.tcd import serialize_tcd
    from bot.pm.budget import sync_budget_chain
    try:
        ticket_id = await _resolve_ticket_id(args["ticket_id"])
        role = args["role"]
        chat_id = args.get("chat_id", "")
        harness_label, project_dir = _resolve_harness_config(args)

        # H2: Validate role name before any work
        if role not in ROLES:
            return _text_response({"error": f"Invalid role: {role}. Valid: {list(ROLES.keys())}"})

        # Acquire per-ticket lock
        if ticket_id not in _ticket_locks:
            _ticket_locks[ticket_id] = asyncio.Lock()
        lock = _ticket_locks[ticket_id]

        if lock.locked():
            return _text_response({"error": f"Ticket {ticket_id} is already being executed"})

        async with lock:
            # Load or create TCD
            tcd = await load_or_create_tcd(ticket_id)

            result = await dispatch_agent(
                ticket_id=ticket_id,
                role_name=role,
                tcd=tcd,
                chat_id=chat_id,
                project_dir=project_dir,
                harness_label=harness_label,
            )

            # Save updated TCD
            if result.get("output"):
                from bot.pm.orchestrator import _TCD_UPDATERS
                from bot.pm.tcd import update_tcd_budget, capture_file_contents
                if role in _TCD_UPDATERS:
                    tcd = _TCD_UPDATERS[role](tcd, result["output"])
                # After Engineer: capture modified file contents for Verifier
                if role == "engineer" and tcd.get("files_modified") and project_dir:
                    tcd = capture_file_contents(tcd, project_dir)
                if result.get("run") and result["run"].get("cost_usd"):
                    tcd = update_tcd_budget(tcd, result["run"]["cost_usd"])
                await update_ticket(ticket_id, tcd=serialize_tcd(tcd))

            # Sync budget chain (ticket → sprint → project).
            # Note: dispatch_agent already calls sync_ticket_spend for inter-step
            # budget accuracy; sync_budget_chain re-syncs ticket atomically with
            # sprint+project — the ticket-level re-sync is idempotent and cheap.
            await sync_budget_chain(ticket_id)

        # M3: Clean up lock OUTSIDE the async-with block (lock is released now)
        if ticket_id in _ticket_locks and not _ticket_locks[ticket_id].locked():
            _ticket_locks.pop(ticket_id, None)

        return _text_response({
            "success": result["error"] is None,
            "role": role,
            "ticket_id": ticket_id,
            "output": result["output"],
            "error": result["error"],
            "run": result["run"],
        })
    except Exception as e:
        log.error(f"pm_dispatch_agent failed: {e}", exc_info=True)
        return _text_response({"error": str(e)})


@tool("pm_run_ticket", "Load a ticket's TCD and return it for in-session execution. The calling session plans, implements, and reviews the ticket itself (v5 in-session mode). Follow the coding-principles pipeline after receiving the response.", {
    "type": "object",
    "properties": {
        "ticket_id": {"type": "integer", "description": "Ticket ID"},
        "chat_id": {"type": "string", "description": "Chat ID for access scoping"},
    },
    "required": ["ticket_id"],
})
async def pm_run_ticket(args: dict[str, Any]) -> dict:
    from bot.pm.orchestrator import load_or_create_tcd
    from bot.pm.tcd import tcd_to_checkpoint, serialize_tcd
    from bot.pm.store import get_ticket, get_sprint, get_project, move_ticket, update_ticket, get_ticket_ref
    from bot.pm.budget import check_budget
    try:
        ticket_id = await _resolve_ticket_id(args["ticket_id"])
        _resolve_harness_config(args)  # pops _session_key

        ticket = await get_ticket(ticket_id)
        if not ticket:
            return _text_response({"error": f"Ticket {ticket_id} not found"})
        ref = await get_ticket_ref(ticket_id)

        sprint = await get_sprint(ticket["sprint_id"])
        project = await get_project(sprint["project_id"]) if sprint else None

        # Warn if already in_progress (concurrent execution guard)
        concurrent_warning = None
        if ticket["status"] == "in_progress":
            concurrent_warning = f"⚠️ Ticket {ticket_id} is already in_progress — another session may be working on it. Check TCD current_state before proceeding."
            log.warning(f"pm_run_ticket: ticket {ticket_id} already in_progress — possible concurrent execution")

        # Load or create TCD
        tcd = await load_or_create_tcd(ticket_id)
        checkpoint = tcd_to_checkpoint(tcd)

        # Check budget
        budget_status = await check_budget(ticket_id)

        # Move to in_progress if currently ready/backlog
        if ticket["status"] in ("backlog", "ready", "created"):
            try:
                await move_ticket(ticket_id, "in_progress", actor="pm", reason="v5 in-session execution started")
            except ValueError:
                log.warning(f"pm_run_ticket: could not transition ticket {ticket_id} from '{ticket['status']}' to in_progress")

        # Save TCD (ensure it exists in DB for crash recovery)
        await update_ticket(ticket_id, tcd=serialize_tcd(tcd))

        return _text_response({
            "mode": "in_session",
            "ticket_id": ticket_id,
            "ref": ref,
            "title": ticket["title"],
            "tier": ticket.get("tier", "significant"),
            "project": project["name"] if project else None,
            "project_key": project.get("key", "") if project else "",
            "sprint": sprint["name"] if sprint else None,
            "sprint_id": ticket["sprint_id"],
            "budget_ok": budget_status.get("ok", False),
            "budget_warning": None if budget_status.get("ok", False) else "⚠️ Budget limit reached — proceed with caution or stop.",
            "concurrent_warning": concurrent_warning,
            "checkpoint": checkpoint,
            "instructions": (
                f"Execute ticket {ref} IN-SESSION using the coding-principles pipeline.\n"
                "You ARE the PM+Engineer — plan, implement, and verify in this session.\n"
                "Do NOT spawn pm-engineer or pm-verifier agents.\n"
                "Use coding-principles review stages (security-reviewer + quality-reviewer) for review.\n"
                f"Update TCD via pm_update_ticket(ticket_id={ticket_id}, tcd=...) after each phase.\n"
                "Check current_state for crash recovery — skip completed phases.\n"
                f"Move ticket to done via pm_move_ticket(ticket_id={ticket_id}, to_status='done') when complete.\n"
                f"Reference this ticket as {ref} in commits: 'Ticket: {ref}'"
            ),
        })
    except Exception as e:
        log.error(f"pm_run_ticket failed: {e}", exc_info=True)
        return _text_response({"error": str(e)})


@tool("pm_run_sprint", "Load all active tickets in a sprint and return them for in-session sequential execution (v5). Follow the coding-principles pipeline.", {
    "type": "object",
    "properties": {
        "sprint_id": {"type": "integer", "description": "Sprint ID"},
        "chat_id": {"type": "string", "description": "Chat ID for access scoping"},
    },
    "required": ["sprint_id"],
})
async def pm_run_sprint(args: dict[str, Any]) -> dict:
    from bot.pm.store import get_sprint, get_project, list_tickets, transition_sprint
    try:
        sprint_id = int(args["sprint_id"])
        _resolve_harness_config(args)  # pops _session_key

        sprint = await get_sprint(sprint_id)
        if not sprint:
            return _text_response({"error": f"Sprint {sprint_id} not found"})

        project = await get_project(sprint["project_id"])

        # Check sprint is in a runnable state
        if sprint["status"] in ("completed", "aborted"):
            return _text_response({"error": f"Sprint {sprint_id} is {sprint['status']} — cannot run."})

        # Auto-transition to running if approved
        if sprint["status"] == "approved":
            try:
                sprint = await transition_sprint(sprint_id, "running", actor="pm")
            except ValueError:
                log.warning(f"pm_run_sprint: could not transition sprint {sprint_id} from '{sprint['status']}' to running")

        tickets = await list_tickets(sprint_id)
        active = [t for t in tickets if t["status"] not in ("done", "dropped")]
        active.sort(key=lambda t: (t.get("priority", 100), t["id"]))

        from bot.pm.store import get_ticket_ref as _gtr
        ticket_list = []
        for t in active:
            ref = await _gtr(t["id"])
            ticket_list.append({
                "id": t["id"], "ref": ref, "title": t["title"],
                "tier": t.get("tier", "significant"),
                "priority": t.get("priority", 100), "status": t["status"],
            })

        return _text_response({
            "mode": "in_session",
            "sprint_id": sprint_id,
            "sprint_name": sprint["name"],
            "sprint_goal": sprint.get("goal", ""),
            "project": project["name"] if project else None,
            "budget_usd": sprint.get("budget_usd"),
            "active_tickets": ticket_list,
            "total_active": len(active),
            "instructions": (
                "Execute this sprint IN-SESSION. Process tickets in priority order.\n"
                "For each ticket: call pm_run_ticket(ticket_id=...) then follow the\n"
                "returned instructions using the coding-principles pipeline.\n"
                "If a ticket fails or needs escalation, continue to the next.\n"
                "After all tickets: report summary with completed/failed/escalated counts.\n"
                "Complete the sprint via pm_transition_sprint(sprint_id, action='completed')."
            ),
        })
    except Exception as e:
        log.error(f"pm_run_sprint failed: {e}", exc_info=True)
        return _text_response({"error": str(e)})


@tool("pm_budget_report", "Get detailed budget breakdown for a sprint — shows spend per ticket and agent run.", {
    "type": "object",
    "properties": {
        "sprint_id": {"type": "integer", "description": "Sprint ID"},
    },
    "required": ["sprint_id"],
})
async def pm_budget_report(args: dict[str, Any]) -> dict:
    from bot.pm.budget import get_sprint_budget_summary
    try:
        result = await get_sprint_budget_summary(int(args["sprint_id"]))
        return _text_response(result)
    except Exception as e:
        log.error(f"pm_budget_report failed: {e}", exc_info=True)
        return _text_response({"error": str(e)})


@tool("pm_get_tcd", "Get the Ticket Context Document (TCD) for a ticket — shows checkpoint state and execution data.", {
    "type": "object",
    "properties": {
        "ticket_id": {"type": "integer", "description": "Ticket ID"},
    },
    "required": ["ticket_id"],
})
async def pm_get_tcd(args: dict[str, Any]) -> dict:
    from bot.pm.store import get_ticket, get_ticket_ref
    from bot.pm.tcd import deserialize_tcd
    try:
        ticket_id = await _resolve_ticket_id(args["ticket_id"])
        ticket = await get_ticket(ticket_id)
        if not ticket:
            return _text_response({"error": f"Ticket {args['ticket_id']} not found"})
        ref = await get_ticket_ref(ticket_id)
        if not ticket.get("tcd"):
            return _text_response({"ref": ref, "tcd": None, "message": "No TCD yet — ticket has not been processed"})
        tcd = deserialize_tcd(ticket["tcd"])
        return _text_response({"ref": ref, "tcd": tcd})
    except Exception as e:
        log.error(f"pm_get_tcd failed: {e}", exc_info=True)
        return _text_response({"error": str(e)})


# ============================================================
# CONVENIENCE TOOLS (Phase 3)
# ============================================================

@tool("pm_plan", "Convenience: create a project from a brief, plan a sprint with tickets. Given a natural-language brief, creates a project + sprint + tickets based on the brief's scope. Returns the full plan for review before execution.", {
    "type": "object",
    "properties": {
        "brief": {"type": "string", "description": "Natural-language project brief describing what to build"},
        "name": {"type": "string", "description": "Project name (auto-derived from brief if omitted)"},
        "budget_usd": {"type": "number", "description": "Total project budget cap in USD"},
        "chat_key": {"type": "string", "description": "Chat key for scoping (auto-derived if omitted)"},
        "tickets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "tier": {"type": "string"},
                    "budget_usd": {"type": "number"},
                    "priority": {"type": "integer"},
                    "estimated_cost_usd": {"type": "number"},
                },
                "required": ["title"],
            },
            "description": "Pre-decomposed tickets (if PM has already broken down the work)",
        },
    },
    "required": ["brief"],
})
async def pm_plan(args: dict[str, Any]) -> dict:
    from bot.pm.store import create_project, create_sprint, create_ticket
    try:
        chat_key = _derive_chat_key(args)
        brief = args["brief"]
        name = args.get("name") or brief[:60].strip()
        budget_usd = args.get("budget_usd")
        tickets_spec = args.get("tickets", [])

        # Create project
        project = await create_project(
            chat_key=chat_key,
            name=name,
            brief=brief,
            budget_usd=budget_usd,
        )

        # Create sprint — budget defaults to project budget (a project may
        # have multiple sprints, so this is the initial allocation)
        sprint = await create_sprint(
            project_id=project["id"],
            name="Sprint 1",
            goal=brief[:200],
            budget_usd=budget_usd,
        )

        # Create tickets from spec (if provided)
        created_tickets = []
        for t_spec in tickets_spec:
            ticket = await create_ticket(
                sprint_id=sprint["id"],
                title=t_spec["title"],
                description=t_spec.get("description", ""),
                tier=t_spec.get("tier", "significant"),
                budget_usd=t_spec.get("budget_usd"),
                priority=int(t_spec.get("priority", 100)),
                estimated_cost_usd=t_spec.get("estimated_cost_usd"),
            )
            created_tickets.append(ticket)

        return _text_response({
            "success": True,
            "project": project,
            "sprint": sprint,
            "tickets": created_tickets,
            "next_steps": (
                "Review the plan above. To proceed:\n"
                "1. Add tickets if needed: pm_create_ticket(sprint_id=...)\n"
                "2. Approve sprint: pm_transition_sprint(sprint_id=..., action='approved')\n"
                "3. Execute: pm_run_sprint(sprint_id=...)"
            ),
        })
    except Exception as e:
        log.error(f"pm_plan failed: {e}", exc_info=True)
        return _text_response({"error": str(e)})


# ============================================================
# AUTO-TICKET (Phase 5 — PM-first workflow)
# ============================================================

@tool("pm_auto_ticket", "Auto-create a ticket for the current coding task. Ensures project + rolling sprint exist, creates ticket with operator context. Returns ticket for session tracking. Use at the START of every coding task.", {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Short ticket title (extracted from operator request)"},
        "operator_request": {"type": "string", "description": "Full original user message that triggered this work"},
        "tier": {"type": "string", "description": "Coding tier: small, medium, significant"},
        "project_name": {"type": "string", "description": "Project name (default: derived from harness label or 'Corporate Bot')"},
        "parent_ticket_id": {"type": "integer", "description": "Parent ticket ID if this is a bug/subtask"},
        "chat_key": {"type": "string", "description": "Chat key for scoping (auto-derived if omitted)"},
    },
    "required": ["title", "tier"],
})
async def pm_auto_ticket(args: dict[str, Any]) -> dict:
    """Auto-create ticket with project + rolling sprint auto-setup.

    This is the entry point for PM-first coding workflow. Call once at
    the start of every coding task. The ticket ID should be stored in
    session state and referenced in commit trailers.
    """
    from bot.pm.store import (
        get_or_create_project, get_or_create_rolling_sprint, create_ticket, get_ticket_ref,
    )
    try:
        # Save _session_key before _derive_chat_key pops it (both helpers pop it)
        session_key = args.get("_session_key", "")
        chat_key = _derive_chat_key(args)
        # Re-inject for _resolve_harness_config since _derive_chat_key popped it
        if session_key:
            args["_session_key"] = session_key
        harness_label, harness_project_dir = _resolve_harness_config(args)
        title = args["title"]
        tier = args.get("tier", "significant")
        operator_request = args.get("operator_request", "")

        # Derive project name: explicit > harness project_dir label > "Corporate Bot"
        # Harnesses without project_dir (admin, coding, default) are all
        # working on the bot itself → use "Corporate Bot" as canonical project.
        # Only harnesses with their own project_dir get a separate project name.
        project_name = args.get("project_name")
        if not project_name:
            if harness_project_dir and harness_label not in ("default", "admin", "coding"):
                project_name = harness_label.replace("_", " ").title()
            else:
                project_name = "Corporate Bot"

        parent_id = args.get("parent_ticket_id")
        if parent_id is not None:
            try:
                parent_id = int(parent_id)
            except (ValueError, TypeError):
                parent_id = None

        # Determine visibility: admin/coding harness → shared (global), employee → private
        _is_global = harness_label in ("admin", "coding", "default")
        _visibility = "shared" if _is_global else "private"
        _owner = session_key  # source:chat_id

        # CB-5: Check harness pm_project_id first — harness-driven project routing.
        # Admin can override per-chat via harness field overrides.
        project = None
        try:
            from bot.harness import get_harness_for_chat, resolve_field
            _pm_harness = get_harness_for_chat(chat_key)
            _pm_project_id = resolve_field(_pm_harness, "pm_project_id", chat_key)
            if _pm_project_id:
                from bot.pm.store import get_project
                project = await get_project(int(_pm_project_id))
                if project:
                    log.info("pm_auto_ticket: using harness pm_project_id=%s for '%s'",
                             _pm_project_id, chat_key)
        except Exception as _pm_err:
            log.debug("pm_auto_ticket: harness pm_project_id lookup failed: %s", _pm_err)

        # Fallback: check project registry — versioned mapping of canonical projects.
        # This prevents duplicate projects when different chats work on the same codebase.
        # Registry lives in /host-repo/data/ (bind-mounted git repo).
        # Can't use __file__-relative path — that resolves to /app/ (container image)
        # but /app/data/ is a volume mount that doesn't contain build-time files.
        _registry_path = "/host-repo/data/pm_project_registry.json"
        if not os.path.isfile(_registry_path):
            # Fallback: check relative to module (works if file is in image data/)
            _registry_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "data", "pm_project_registry.json",
            )
        try:
            if os.path.isfile(_registry_path):
                import json as _json_reg
                with open(_registry_path) as _f_reg:
                    _reg = _json_reg.loads(_f_reg.read())
                _entry = _reg.get(project_name)
                if _entry and _entry.get("project_id"):
                    from bot.pm.store import get_project
                    project = await get_project(int(_entry["project_id"]))
                    if project:
                        log.info("pm_auto_ticket: using registered project %s (id=%s) for '%s'",
                                 _entry.get("key", "?"), _entry["project_id"], project_name)
        except Exception as _reg_err:
            log.warning("pm_auto_ticket: registry lookup failed: %s", _reg_err)

        if not project:
            # Fallback: auto-create (flags a warning if the name looks canonical)
            if project_name == "Corporate Bot":
                log.warning("pm_auto_ticket: 'Corporate Bot' not found in registry — "
                            "creating new project (check data/pm_project_registry.json)")
            project = await get_or_create_project(
                chat_key=chat_key,
                name=project_name,
                brief=f"Auto-created for {chat_key}",
                visibility=_visibility,
                owner_user_id=_owner,
            )

        # Get or create rolling sprint
        sprint = await get_or_create_rolling_sprint(project["id"])

        # Create ticket
        ticket = await create_ticket(
            sprint_id=sprint["id"],
            title=title,
            description=operator_request[:500] if operator_request else "",
            tier=tier,
            operator_request=operator_request,
            parent_ticket_id=parent_id,
            source_chat_id=chat_key,
        )

        ref = await get_ticket_ref(ticket["id"])
        # Store active ticket ID in session state for deploy gate enforcement.
        # Use `session_key` saved at line 876 — args["_session_key"] was already
        # popped by _derive_chat_key + _resolve_harness_config.
        try:
            if session_key:
                from bot.core.session_state import _get_or_create_state
                _get_or_create_state(session_key)["active_ticket_id"] = ticket["id"]
        except Exception:
            pass
        return _text_response({
            "success": True,
            "ref": ref,
            "ticket": ticket,
            "project": {"id": project["id"], "name": project["name"], "key": project.get("key", "")},
            "sprint": {"id": sprint["id"], "name": sprint["name"]},
            "instructions": (
                f"Ticket {ref} created. Track this in your session.\n"
                f"After committing, update with: pm_update_ticket(ticket_id={ticket['id']}, commit_hash='<sha>')\n"
                f"Include in commit trailer: Ticket: {ref}\n"
                f"When done: pm_move_ticket(ticket_id={ticket['id']}, to_status='done')"
            ),
        })
    except Exception as e:
        log.error(f"pm_auto_ticket failed: {e}", exc_info=True)
        return _text_response({"error": str(e)})


# ============================================================
# RETRO & SUMMARY TOOLS (Phase 4)
# ============================================================

@tool("pm_retro", "Generate a sprint retrospective — cost/velocity/findings analysis with estimation accuracy tracking. Works on completed or aborted sprints.", {
    "type": "object",
    "properties": {
        "sprint_id": {"type": "integer", "description": "Sprint ID"},
    },
    "required": ["sprint_id"],
})
async def pm_retro(args: dict[str, Any]) -> dict:
    from bot.pm.retro import generate_retro
    try:
        result = await generate_retro(int(args["sprint_id"]))
        return _text_response(result)
    except Exception as e:
        log.error(f"pm_retro failed: {e}", exc_info=True)
        return _text_response({"error": str(e)})


@tool("pm_summary", "Generate a compact sprint summary for demo/reporting — completed work, files changed, cost, key decisions.", {
    "type": "object",
    "properties": {
        "sprint_id": {"type": "integer", "description": "Sprint ID"},
    },
    "required": ["sprint_id"],
})
async def pm_summary(args: dict[str, Any]) -> dict:
    from bot.pm.retro import generate_summary
    try:
        result = await generate_summary(int(args["sprint_id"]))
        return _text_response(result)
    except Exception as e:
        log.error(f"pm_summary failed: {e}", exc_info=True)
        return _text_response({"error": str(e)})


# ============================================================
# TOOL REGISTRY
# ============================================================

PM_TOOLS = [
    # Phase 0 — data model
    pm_create_project,
    pm_list_projects,
    pm_create_sprint,
    pm_transition_sprint,
    pm_cancel_sprint,
    pm_create_ticket,
    pm_update_ticket,
    pm_move_ticket,
    pm_reset_ticket,
    pm_list_tickets,
    pm_board,
    # Phase 1+2 — orchestration
    pm_dispatch_agent,
    pm_run_ticket,
    pm_run_sprint,
    pm_budget_report,
    pm_get_tcd,
    # Phase 3 — convenience
    pm_plan,
    pm_auto_ticket,
    # Phase 4 — retro & summary
    pm_retro,
    pm_summary,
]
