# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""PM budget tracking — roll-up from agent_runs → tickets → sprints → projects.

Budget is the primary constraint for sprint execution.
All amounts in USD. Tracks actual spend vs allocated budget.

Budget sync uses single-transaction atomicity — all three levels
(ticket, sprint, project) are updated in one commit to prevent
inconsistent intermediate states.
"""

import logging

from bot.pm.db import get_pm_db

log = logging.getLogger(__name__)


async def sync_ticket_spend(ticket_id: int) -> float:
    """Recalculate ticket spent_usd from its agent_runs. Returns new total."""
    db = await get_pm_db()
    cursor = await db.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) as total FROM pm_agent_runs "
        "WHERE ticket_id = ? AND status IN ('completed', 'failed')",
        (ticket_id,),
    )
    row = await cursor.fetchone()
    total = float(row["total"])
    await db.execute(
        "UPDATE pm_tickets SET spent_usd = ? WHERE id = ?",
        (total, ticket_id),
    )
    await db.commit()
    return total


async def sync_budget_chain(ticket_id: int) -> dict:
    """Atomically sync budget from ticket → sprint → project in one transaction.

    Replaces separate sync_ticket/sprint/project calls to prevent inconsistent
    intermediate states. Returns {ticket_spent, sprint_spent, project_spent}.
    """
    db = await get_pm_db()
    await db.execute("BEGIN IMMEDIATE")
    try:
        # 1. Ticket spend from agent_runs
        cursor = await db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) as total FROM pm_agent_runs "
            "WHERE ticket_id = ? AND status IN ('completed', 'failed')",
            (ticket_id,),
        )
        ticket_spent = float((await cursor.fetchone())["total"])
        await db.execute(
            "UPDATE pm_tickets SET spent_usd = ? WHERE id = ?",
            (ticket_spent, ticket_id),
        )

        # 2. Get sprint_id for this ticket
        cursor = await db.execute(
            "SELECT sprint_id FROM pm_tickets WHERE id = ?", (ticket_id,),
        )
        ticket_row = await cursor.fetchone()
        if not ticket_row:
            await db.execute("COMMIT")
            return {"ticket_spent": ticket_spent, "sprint_spent": 0, "project_spent": 0}
        sprint_id = ticket_row["sprint_id"]

        # 3. Sprint spend from tickets
        cursor = await db.execute(
            "SELECT COALESCE(SUM(spent_usd), 0) as total FROM pm_tickets WHERE sprint_id = ?",
            (sprint_id,),
        )
        sprint_spent = float((await cursor.fetchone())["total"])
        await db.execute(
            "UPDATE pm_sprints SET spent_usd = ? WHERE id = ?",
            (sprint_spent, sprint_id),
        )

        # 4. Get project_id for this sprint
        cursor = await db.execute(
            "SELECT project_id FROM pm_sprints WHERE id = ?", (sprint_id,),
        )
        sprint_row = await cursor.fetchone()
        if not sprint_row:
            await db.execute("COMMIT")
            return {"ticket_spent": ticket_spent, "sprint_spent": sprint_spent, "project_spent": 0}
        project_id = sprint_row["project_id"]

        # 5. Project spend from sprints
        cursor = await db.execute(
            "SELECT COALESCE(SUM(spent_usd), 0) as total FROM pm_sprints WHERE project_id = ?",
            (project_id,),
        )
        project_spent = float((await cursor.fetchone())["total"])
        await db.execute(
            "UPDATE pm_projects SET spent_usd = ? WHERE id = ?",
            (project_spent, project_id),
        )

        await db.execute("COMMIT")
        return {"ticket_spent": ticket_spent, "sprint_spent": sprint_spent, "project_spent": project_spent}
    except Exception:
        try:
            await db.execute("ROLLBACK")
        except Exception:
            pass
        raise


async def sync_sprint_spend(sprint_id: int) -> float:
    """Recalculate sprint spent_usd from its tickets. Returns new total."""
    db = await get_pm_db()
    cursor = await db.execute(
        "SELECT COALESCE(SUM(spent_usd), 0) as total FROM pm_tickets WHERE sprint_id = ?",
        (sprint_id,),
    )
    row = await cursor.fetchone()
    total = float(row["total"])
    await db.execute(
        "UPDATE pm_sprints SET spent_usd = ? WHERE id = ?",
        (total, sprint_id),
    )
    await db.commit()
    return total


async def sync_project_spend(project_id: int) -> float:
    """Recalculate project spent_usd from its sprints. Returns new total."""
    db = await get_pm_db()
    cursor = await db.execute(
        "SELECT COALESCE(SUM(spent_usd), 0) as total FROM pm_sprints WHERE project_id = ?",
        (project_id,),
    )
    row = await cursor.fetchone()
    total = float(row["total"])
    await db.execute(
        "UPDATE pm_projects SET spent_usd = ? WHERE id = ?",
        (total, project_id),
    )
    await db.commit()
    return total


async def check_budget(ticket_id: int) -> dict:
    """Check budget availability for a ticket. Returns budget status dict.

    Checks ticket, sprint, and project budgets in cascade.
    Returns: {ok: bool, ticket_remaining, sprint_remaining, project_remaining, warnings: []}
    """
    db = await get_pm_db()

    # Get ticket
    ticket = await (await db.execute(
        "SELECT * FROM pm_tickets WHERE id = ?", (ticket_id,)
    )).fetchone()
    if not ticket:
        return {"ok": False, "error": f"Ticket {ticket_id} not found"}

    # Get sprint
    sprint = await (await db.execute(
        "SELECT * FROM pm_sprints WHERE id = ?", (ticket["sprint_id"],)
    )).fetchone()
    if not sprint:
        return {"ok": False, "error": f"Sprint {ticket['sprint_id']} not found for ticket {ticket_id}"}

    # Get project
    project = await (await db.execute(
        "SELECT * FROM pm_projects WHERE id = ?", (sprint["project_id"],)
    )).fetchone()
    if not project:
        return {"ok": False, "error": f"Project {sprint['project_id']} not found for sprint {sprint['id']}"}

    warnings = []
    ticket_remaining = None
    sprint_remaining = None
    project_remaining = None

    if ticket["budget_usd"]:
        ticket_remaining = ticket["budget_usd"] - ticket["spent_usd"]
        if ticket_remaining <= 0:
            return {
                "ok": False,
                "error": f"Ticket budget exhausted: ${ticket['spent_usd']:.2f} / ${ticket['budget_usd']:.2f}",
                "ticket_remaining": ticket_remaining,
            }
        if ticket_remaining < ticket["budget_usd"] * 0.2:
            warnings.append(f"Ticket budget low: ${ticket_remaining:.2f} remaining")

    if sprint["budget_usd"]:
        sprint_remaining = sprint["budget_usd"] - sprint["spent_usd"]
        if sprint_remaining <= 0:
            return {
                "ok": False,
                "error": f"Sprint budget exhausted: ${sprint['spent_usd']:.2f} / ${sprint['budget_usd']:.2f}",
                "sprint_remaining": sprint_remaining,
            }
        if sprint_remaining < sprint["budget_usd"] * 0.2:
            warnings.append(f"Sprint budget low: ${sprint_remaining:.2f} remaining")

    if project["budget_usd"]:
        project_remaining = project["budget_usd"] - project["spent_usd"]
        if project_remaining <= 0:
            return {
                "ok": False,
                "error": f"Project budget exhausted: ${project['spent_usd']:.2f} / ${project['budget_usd']:.2f}",
                "project_remaining": project_remaining,
            }

    # Warn about uncapped spending — all budgets are NULL means no guardrails
    if ticket_remaining is None and sprint_remaining is None and project_remaining is None:
        warnings.append("No budget caps set at any level — spending is uncapped")

    return {
        "ok": True,
        "ticket_remaining": ticket_remaining,
        "sprint_remaining": sprint_remaining,
        "project_remaining": project_remaining,
        "warnings": warnings,
    }


async def get_sprint_budget_summary(sprint_id: int) -> dict:
    """Get a detailed budget breakdown for a sprint.

    Uses a JOIN to fetch tickets + runs in one query (fixes N+1).
    """
    db = await get_pm_db()

    sprint = await (await db.execute(
        "SELECT * FROM pm_sprints WHERE id = ?", (sprint_id,)
    )).fetchone()
    if not sprint:
        return {"error": f"Sprint {sprint_id} not found"}

    # Single JOIN query — no N+1
    rows = await (await db.execute(
        "SELECT t.id as ticket_id, t.title, t.status as ticket_status, "
        "t.budget_usd as ticket_budget, t.spent_usd as ticket_spent, "
        "r.agent_role, r.cost_usd as run_cost, r.status as run_status, "
        "r.duration_s "
        "FROM pm_tickets t "
        "LEFT JOIN pm_agent_runs r ON r.ticket_id = t.id "
        "WHERE t.sprint_id = ? "
        "ORDER BY t.id, r.created_at",
        (sprint_id,),
    )).fetchall()

    # Group runs by ticket
    tickets_map: dict[int, dict] = {}
    for row in rows:
        tid = row["ticket_id"]
        if tid not in tickets_map:
            tickets_map[tid] = {
                "id": tid,
                "title": row["title"],
                "status": row["ticket_status"],
                "budget_usd": row["ticket_budget"],
                "spent_usd": row["ticket_spent"],
                "runs": [],
            }
        if row["agent_role"]:  # LEFT JOIN may produce NULL runs for tickets with no runs
            tickets_map[tid]["runs"].append({
                "agent_role": row["agent_role"],
                "cost_usd": row["run_cost"],
                "status": row["run_status"],
                "duration_s": row["duration_s"],
            })

    return {
        "sprint_id": sprint_id,
        "sprint_name": sprint["name"],
        "sprint_budget": sprint["budget_usd"],
        "sprint_spent": sprint["spent_usd"],
        "sprint_remaining": (sprint["budget_usd"] or 0) - sprint["spent_usd"],
        "tickets": list(tickets_map.values()),
    }
