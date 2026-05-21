# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""PM retro & summary — post-sprint analysis and artifact collection.

Sprint retro: cost/finding/velocity analysis with estimation accuracy tracking.
Sprint summary: compact digest of completed work for demo/reporting.
"""

import json
import logging

from bot.pm.db import get_pm_db
from bot.pm.store import get_sprint, get_project, list_tickets
from bot.pm.tcd import deserialize_tcd

log = logging.getLogger(__name__)


async def generate_retro(sprint_id: int) -> dict:
    """Generate a retrospective analysis for a completed/aborted sprint.

    Returns structured retro data:
    - budget: allocated vs spent, per-ticket breakdown
    - velocity: tickets done/dropped/stuck, estimation accuracy
    - findings: severity distribution from reviewer
    - fix_loops: how many triggered, iterations used
    - agent_stats: per-role cost and duration averages
    - lessons: most expensive tickets, worst estimates, most findings
    """
    sprint = await get_sprint(sprint_id)
    if not sprint:
        return {"error": f"Sprint {sprint_id} not found"}

    # Warn (don't reject) for non-terminal sprints — partial retros are useful
    retro_warning = None
    if sprint["status"] not in ("completed", "aborted"):
        retro_warning = f"Sprint is still '{sprint['status']}' — retro data may be incomplete"

    project = await get_project(sprint["project_id"])
    tickets = await list_tickets(sprint_id)
    db = await get_pm_db()

    # ── Ticket-level analysis ──
    ticket_analysis = []
    total_estimated = 0.0
    total_actual = 0.0
    estimation_pairs = []  # (estimated, actual) for accuracy calc

    for t in tickets:
        estimated = t.get("estimated_cost_usd")
        if estimated is None:
            estimated = 0.0
        actual = t.get("spent_usd") or 0.0
        total_estimated += estimated
        total_actual += actual

        entry = {
            "id": t["id"],
            "title": t["title"],
            "status": t["status"],
            "tier": t["tier"],
            "priority": t.get("priority", 100),
            "estimated_cost_usd": estimated,
            "actual_cost_usd": actual,
            "accuracy_pct": None,
        }

        if estimated > 0:
            entry["accuracy_pct"] = round(actual / estimated * 100, 1)
            estimation_pairs.append((estimated, actual))

        # Extract findings from TCD
        if t.get("tcd"):
            try:
                tcd = deserialize_tcd(t["tcd"])
                findings = tcd.get("reviewer_findings", [])
                entry["findings_count"] = len(findings)
                entry["findings_by_severity"] = _count_by_severity(findings)
                entry["fix_loops"] = _count_fix_loops(tcd.get("decisions", []))
            except (json.JSONDecodeError, TypeError):
                entry["findings_count"] = 0
                entry["findings_by_severity"] = {}
                entry["fix_loops"] = 0
        else:
            entry["findings_count"] = 0
            entry["findings_by_severity"] = {}
            entry["fix_loops"] = 0

        ticket_analysis.append(entry)

    # ── Agent run stats (single JOIN query) ──
    runs = await (await db.execute(
        "SELECT r.agent_role, r.cost_usd, r.duration_s, r.status "
        "FROM pm_agent_runs r "
        "JOIN pm_tickets t ON r.ticket_id = t.id "
        "WHERE t.sprint_id = ?",
        (sprint_id,),
    )).fetchall()

    agent_stats: dict[str, dict] = {}
    for r in runs:
        role = r["agent_role"]
        if role not in agent_stats:
            agent_stats[role] = {"count": 0, "total_cost": 0.0, "total_duration": 0.0, "failures": 0}
        agent_stats[role]["count"] += 1
        agent_stats[role]["total_cost"] += r["cost_usd"] or 0
        agent_stats[role]["total_duration"] += r["duration_s"] or 0
        if r["status"] == "failed":
            agent_stats[role]["failures"] += 1

    for role, stats in agent_stats.items():
        stats["avg_cost"] = round(stats["total_cost"] / stats["count"], 3) if stats["count"] else 0
        stats["avg_duration_s"] = round(stats["total_duration"] / stats["count"], 1) if stats["count"] else 0

    # ── Velocity metrics ──
    done_count = sum(1 for t in tickets if t["status"] == "done")
    dropped_count = sum(1 for t in tickets if t["status"] == "dropped")
    stuck_count = sum(1 for t in tickets if t["status"] not in ("done", "dropped"))
    total_count = len(tickets)

    velocity = {
        "total_tickets": total_count,
        "done": done_count,
        "dropped": dropped_count,
        "stuck": stuck_count,
        "completion_rate_pct": round(done_count / total_count * 100, 1) if total_count else 0,
    }

    # ── Estimation accuracy ──
    estimation = {
        "total_estimated_usd": round(total_estimated, 2),
        "total_actual_usd": round(total_actual, 2),
        "overall_accuracy_pct": None,
        "tickets_with_estimates": len(estimation_pairs),
        "mean_accuracy_pct": None,
    }

    if total_estimated > 0:
        estimation["overall_accuracy_pct"] = round(total_actual / total_estimated * 100, 1)

    if estimation_pairs:
        accuracies = [actual / est * 100 for est, actual in estimation_pairs if est > 0]
        if accuracies:
            estimation["mean_accuracy_pct"] = round(sum(accuracies) / len(accuracies), 1)

    # ── Findings summary (across all tickets) ──
    all_findings = {}
    total_fix_loops = 0
    for ta in ticket_analysis:
        for sev, count in ta["findings_by_severity"].items():
            all_findings[sev] = all_findings.get(sev, 0) + count
        total_fix_loops += ta["fix_loops"]

    # ── Lessons (top offenders) ──
    sorted_by_cost = sorted(ticket_analysis, key=lambda t: t["actual_cost_usd"], reverse=True)
    most_expensive = sorted_by_cost[:3] if sorted_by_cost else []

    sorted_by_findings = sorted(ticket_analysis, key=lambda t: t["findings_count"], reverse=True)
    most_findings = [t for t in sorted_by_findings[:3] if t["findings_count"] > 0]

    worst_estimates = sorted(
        [t for t in ticket_analysis if t["accuracy_pct"] is not None],
        key=lambda t: abs(t["accuracy_pct"] - 100),
        reverse=True,
    )[:3]

    result = {
        "sprint_id": sprint_id,
        "sprint_name": sprint["name"],
        "sprint_status": sprint["status"],
        "project_name": project["name"] if project else "unknown",
        "budget": {
            "allocated": sprint.get("budget_usd"),
            "spent": sprint["spent_usd"],
            "remaining": (sprint.get("budget_usd") or 0) - sprint["spent_usd"],
        },
        "velocity": velocity,
        "estimation": estimation,
        "findings_summary": all_findings,
        "total_fix_loops": total_fix_loops,
        "agent_stats": agent_stats,
        "tickets": ticket_analysis,
        "lessons": {
            "most_expensive": [{"id": t["id"], "title": t["title"], "cost": t["actual_cost_usd"]} for t in most_expensive],
            "most_findings": [{"id": t["id"], "title": t["title"], "count": t["findings_count"]} for t in most_findings],
            "worst_estimates": [{"id": t["id"], "title": t["title"], "estimated": t["estimated_cost_usd"], "actual": t["actual_cost_usd"], "accuracy_pct": t["accuracy_pct"]} for t in worst_estimates],
        },
    }
    if retro_warning:
        result["warning"] = retro_warning
    return result


async def generate_summary(sprint_id: int) -> dict:
    """Generate a compact sprint summary for demo/reporting.

    Collects: goal, outcome, completed tickets, files changed,
    total cost, duration, key decisions.
    """
    sprint = await get_sprint(sprint_id)
    if not sprint:
        return {"error": f"Sprint {sprint_id} not found"}

    project = await get_project(sprint["project_id"])
    tickets = await list_tickets(sprint_id)
    db = await get_pm_db()

    # ── Completed tickets with summaries ──
    completed = []
    all_files = set()
    all_decisions = []

    for t in tickets:
        if t["status"] != "done":
            continue
        entry = {
            "id": t["id"],
            "title": t["title"],
            "tier": t["tier"],
            "cost_usd": t.get("spent_usd", 0),
        }
        if t.get("tcd"):
            try:
                tcd = deserialize_tcd(t["tcd"])
                entry["files"] = tcd.get("files_modified") or []
                entry["notes"] = tcd.get("implementation_notes", "")
                entry["verdict"] = tcd.get("current_state", "")
                all_files.update(entry["files"])
                # Collect key decisions (not skipped/budget entries)
                for d in tcd.get("decisions", []):
                    if "skip" not in d.get("decision", "").lower():
                        all_decisions.append(d)
            except (json.JSONDecodeError, TypeError):
                pass
        completed.append(entry)

    # ── Dropped tickets ──
    dropped = [
        {"id": t["id"], "title": t["title"], "tier": t["tier"]}
        for t in tickets if t["status"] == "dropped"
    ]

    # ── Total duration (first run start → last run end) ──
    duration_row = await (await db.execute(
        "SELECT MIN(r.created_at) as started, MAX(r.completed_at) as ended "
        "FROM pm_agent_runs r "
        "JOIN pm_tickets t ON r.ticket_id = t.id "
        "WHERE t.sprint_id = ?",
        (sprint_id,),
    )).fetchone()

    return {
        "sprint_id": sprint_id,
        "sprint_name": sprint["name"],
        "sprint_goal": sprint["goal"],
        "sprint_status": sprint["status"],
        "project_name": project["name"] if project else "unknown",
        "outcome": {
            "completed": len(completed),
            "dropped": len(dropped),
            "total": len(tickets),
            "total_cost_usd": round(sprint["spent_usd"], 2),
        },
        "started_at": duration_row["started"] if duration_row else None,
        "ended_at": duration_row["ended"] if duration_row else None,
        "completed_tickets": completed,
        "dropped_tickets": dropped,
        "all_files_changed": sorted(all_files),
        "key_decisions": all_decisions[-20:],  # cap to prevent bloat
    }


def _count_by_severity(findings: list[dict]) -> dict[str, int]:
    """Count findings by severity level."""
    counts: dict[str, int] = {}
    for f in findings:
        sev = f.get("severity", "UNKNOWN")
        counts[sev] = counts.get(sev, 0) + 1
    return counts


def _count_fix_loops(decisions: list[dict]) -> int:
    """Count fix loop iterations from decision log."""
    return sum(
        1 for d in decisions
        if d.get("agent") == "pm" and "fix iteration" in d.get("decision", "").lower()
    )
