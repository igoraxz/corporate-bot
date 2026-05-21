# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Ticket Context Document (TCD) — checkpoint artifact for ticket execution.

v5 (in-session): The calling session IS the PM+Engineer. TCD serves as a
checkpoint for crash recovery and state tracking, NOT as a handoff between
separate agents. Review agents (security-reviewer, quality-reviewer) are
read-only and spawned from the same session via coding-principles stages.

Legacy (v1-v4): TCD was the ONLY context passed between agents in a star
topology. The `tcd_to_agent_context()` function with observation masking is
retained for backward compat with `pm_dispatch_agent` direct calls.

Structure:
{
  "ticket_id": 1,
  "title": "...",
  "description": "...",
  "tier": "significant",
  "objective": "...",
  "pm_plan": {...},              # set during planning phase
  "files_modified": [...],       # set during implementation
  "implementation_notes": "...", # set during implementation
  "file_contents": {...},        # captured after implementation (file path → content)
  "reviewer_findings": [...],    # set by review agents
  "test_results": {...},         # set during verification
  "decisions": [...],            # accumulated during execution
  "current_state": "...",        # last known state (for crash recovery)
  "pipeline_gates": {...},       # coding-principles gate tracking (gate → {status, commit, ts})
  "stage_costs": {...},          # per-stage cost tracking (v5)
  "budget": {"allocated": 5.0, "spent": 1.2, "remaining": 3.8}
}
"""

import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)


def create_tcd(
    ticket_id: int,
    title: str,
    description: str,
    tier: str,
    budget_usd: float | None = None,
    project_brief: str = "",
    sprint_goal: str = "",
) -> dict[str, Any]:
    """Create a new TCD from a ticket."""
    return {
        "ticket_id": ticket_id,
        "title": title,
        "description": description,
        "tier": tier,
        "project_brief": project_brief,
        "sprint_goal": sprint_goal,
        "objective": description,  # initial objective = description
        "pm_plan": None,
        "files_modified": None,  # set by Engineer (None = not yet run)
        "implementation_notes": "",
        "file_contents": None,  # captured after Engineer (path → content)
        "reviewer_findings": [],
        "test_results": None,
        "decisions": [],
        "current_state": "created",
        "pipeline_gates": {},  # coding-principles gate tracking: stage → {status, commit, timestamp}
        "stage_costs": {},  # v5: per-stage cost tracking (e.g. {"plan": 0.5, "implement": 2.1})
        "budget": {
            "allocated": budget_usd,
            "spent": 0.0,
            "remaining": budget_usd,
        },
    }


def update_tcd_from_pm(tcd: dict, pm_output: dict) -> dict:
    """Update TCD with PM's plan output."""
    plan = pm_output.get("plan", {})
    tcd["pm_plan"] = plan
    tcd["current_state"] = "planned"
    tcd["decisions"].append({
        "agent": "pm",
        "decision": f"Plan: {plan.get('summary', 'no summary')}",
    })
    return tcd


# ── Pipeline gate tracking (coding-principles enforcement) ────────────

# Required gates by tier (from coding-principles skill)
_REQUIRED_GATES: dict[str, list[str]] = {
    "small": ["T0", "SM", "stage_2", "stage_5", "stage_6", "stage_9", "stage_12", "stage_10",
              "orchestrator"],
    "medium": ["T0", "SM", "stage_2", "stage_5", "stage_6", "stage_9", "stage_12", "stage_10",
               "orchestrator"],
    "significant": ["T0", "SM", "stage_0", "stage_1", "stage_2", "stage_3", "stage_4",
                     "stage_5", "stage_6", "stage_7", "stage_8", "stage_9", "stage_12",
                     "stage_10", "stage_11", "orchestrator"],
}

# Optional gates that don't block deploy (skippable based on context)
_OPTIONAL_GATES = {"stage_0"}  # Product research — only for new features


def mark_gate(tcd: dict, gate: str, *, status: str = "passed",
              commit: str = "", notes: str = "",
              agent_id: str = "") -> dict:
    """Record a pipeline gate completion on the TCD.

    Args:
        tcd: The ticket context document
        gate: Gate name (T0, SM, stage_0 through stage_11)
        status: passed, failed, skipped, invalidated
        commit: Git commit hash the gate was verified against
        notes: Optional notes (e.g., "inline self-review" for small tier)
        agent_id: ID of the spawned review agent (proof of execution for 🔴 stages)
    """
    import time
    if "pipeline_gates" not in tcd:
        tcd["pipeline_gates"] = {}
    entry = {
        "status": status,
        "commit": commit,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "notes": notes,
    }
    if agent_id:
        entry["agent_id"] = agent_id
    tcd["pipeline_gates"][gate] = entry
    return tcd


def invalidate_post_impl_gates(tcd: dict) -> dict:
    """Invalidate post-implementation gates (7-12) after a pivot/redesign.

    Pre-implementation gates (T0, SM, 0-5) carry forward if problem didn't change.
    """
    post_impl = {"stage_7", "stage_8", "stage_9", "stage_10", "stage_11", "stage_12"}
    gates = tcd.get("pipeline_gates", {})
    for gate in post_impl:
        if gate in gates and gates[gate].get("status") == "passed":
            gates[gate]["status"] = "invalidated"
    return tcd


def check_gates_for_deploy(tcd: dict) -> tuple[bool, list[str]]:
    """Verify all required gates passed for the ticket's tier.

    Returns (all_passed, list_of_missing_or_failed_gates).
    """
    tier = tcd.get("tier", "significant")
    required = _REQUIRED_GATES.get(tier, _REQUIRED_GATES["significant"])
    gates = tcd.get("pipeline_gates", {})
    missing = []
    for gate in required:
        if gate in _OPTIONAL_GATES:
            continue  # optional gates don't block
        entry = gates.get(gate)
        if not entry:
            missing.append(f"{gate}: not recorded")
        elif entry.get("status") not in ("passed", "skipped"):
            missing.append(f"{gate}: {entry.get('status', 'unknown')}")
    return (len(missing) == 0, missing)


def format_gate_summary(tcd: dict) -> str:
    """Format pipeline gates as a compact summary string for ticket descriptions."""
    gates = tcd.get("pipeline_gates", {})
    if not gates:
        return "No pipeline gates recorded"
    lines = []
    for gate, info in sorted(gates.items()):
        status = info.get("status", "?")
        icon = {"passed": "✅", "failed": "❌", "skipped": "⏭️",
                "invalidated": "🔄"}.get(status, "❓")
        commit = info.get("commit", "")[:7]
        commit_tag = f" ({commit})" if commit else ""
        notes = info.get("notes", "")
        notes_tag = f" — {notes}" if notes else ""
        lines.append(f"{icon} {gate}: {status}{commit_tag}{notes_tag}")
    return "\n".join(lines)


def update_tcd_from_engineer(tcd: dict, engineer_output: dict) -> dict:
    """Update TCD with Engineer's implementation output."""
    impl = engineer_output.get("implementation", {})
    tcd["files_modified"] = impl.get("files_modified", []) + impl.get("files_created", [])
    tcd["implementation_notes"] = impl.get("notes", "")
    tcd["current_state"] = "implemented"

    deviations = engineer_output.get("deviations", [])
    if deviations:
        tcd["decisions"].append({
            "agent": "engineer",
            "decision": f"Deviations from plan: {len(deviations)} items",
            "details": deviations,
        })
    return tcd


def update_tcd_from_reviewer(tcd: dict, reviewer_output: dict) -> dict:
    """Update TCD with Reviewer's combined review output (architecture + security + quality)."""
    review = reviewer_output.get("review", {})
    tcd["reviewer_findings"] = review.get("findings", [])
    verdict = review.get("verdict", "unknown")
    tcd["current_state"] = f"reviewed ({verdict})"
    tcd["decisions"].append({
        "agent": "reviewer",
        "decision": f"Review verdict: {verdict} "
                    f"(score: {review.get('score', '?')}/10, "
                    f"findings: {len(review.get('findings', []))})",
    })
    return tcd


def update_tcd_from_verifier(tcd: dict, verifier_output: dict) -> dict:
    """Update TCD with Verifier's combined review + test output."""
    review = verifier_output.get("review", {})
    qa_report = verifier_output.get("qa_report", {})

    # Review side
    tcd["reviewer_findings"] = review.get("findings", [])
    verdict = review.get("verdict", "unknown")

    # QA side
    tcd["test_results"] = qa_report
    tests_run = qa_report.get("tests_run", [])
    passed = sum(1 for t in tests_run if t.get("result") == "pass")

    tcd["current_state"] = f"verified ({verdict}, {passed}/{len(tests_run)} tests)"
    tcd["decisions"].append({
        "agent": "verifier",
        "decision": (
            f"Verdict: {verdict} "
            f"(score: {review.get('score', '?')}/10, "
            f"findings: {len(review.get('findings', []))}, "
            f"tests: {passed}/{len(tests_run)})"
        ),
    })
    return tcd


def update_tcd_from_qa(tcd: dict, qa_output: dict) -> dict:
    """Update TCD with QA's test results."""
    qa_report = qa_output.get("qa_report", {})
    tcd["test_results"] = qa_report
    verdict = qa_report.get("verdict", "unknown")
    tests_run = qa_report.get("tests_run", [])
    passed = sum(1 for t in tests_run if t.get("result") == "pass")
    tcd["current_state"] = f"tested ({verdict}: {passed}/{len(tests_run)} passed)"
    tcd["decisions"].append({
        "agent": "qa",
        "decision": f"QA verdict: {verdict} ({passed}/{len(tests_run)} tests passed)",
    })
    return tcd


def update_tcd_budget(tcd: dict, cost_usd: float) -> dict:
    """Update TCD budget after an agent run."""
    tcd["budget"]["spent"] = tcd["budget"].get("spent", 0) + cost_usd
    if tcd["budget"]["allocated"]:
        tcd["budget"]["remaining"] = tcd["budget"]["allocated"] - tcd["budget"]["spent"]
    return tcd


_FILE_CONTENT_MAX_LINES = 200  # per file
_FILE_CONTENT_MAX_FILES = 10  # total files captured
_FILE_CONTENT_EXTENSIONS = {".py", ".go", ".js", ".ts", ".sh", ".yml", ".yaml", ".json", ".md", ".txt", ".toml"}


_FILE_CONTENT_MAX_BYTES = 500_000  # total bytes budget across all files


def capture_file_contents(tcd: dict, project_dir: str) -> dict:
    """Read modified files into TCD so Verifier gets pre-loaded content.

    Eliminates 3-8 Read tool calls per Verifier dispatch. Only captures
    text source files with known extensions, capped at 200 lines / 10 files
    / 500KB total. All paths are resolved and validated against project_dir
    to prevent path traversal.
    """
    files = tcd.get("files_modified") or []
    if not files or not project_dir:
        return tcd

    real_project = os.path.realpath(project_dir)
    contents: dict[str, str] = {}
    total_bytes = 0
    for fpath_raw in files[:_FILE_CONTENT_MAX_FILES]:
        # Engineer may output dicts {"path": "...", "description": "..."} or plain strings
        fpath = fpath_raw["path"] if isinstance(fpath_raw, dict) else str(fpath_raw)
        ext = os.path.splitext(fpath)[1].lower()
        if ext not in _FILE_CONTENT_EXTENSIONS:
            continue
        # Resolve to absolute path then validate containment
        full = fpath if os.path.isabs(fpath) else os.path.join(project_dir, fpath)
        real_full = os.path.realpath(full)
        if not real_full.startswith(real_project + os.sep) and real_full != real_project:
            log.warning("capture_file_contents: path %s resolves outside project_dir %s, skipping",
                        fpath, project_dir)
            continue
        try:
            with open(real_full, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            if len(lines) > _FILE_CONTENT_MAX_LINES:
                text = "".join(lines[:_FILE_CONTENT_MAX_LINES])
                text += f"\n... [{len(lines) - _FILE_CONTENT_MAX_LINES} more lines truncated]"
            else:
                text = "".join(lines)
            total_bytes += len(text.encode("utf-8", errors="replace"))
            if total_bytes > _FILE_CONTENT_MAX_BYTES:
                contents[fpath] = "[Skipped: total bytes budget exceeded]"
                break
            contents[fpath] = text
        except OSError:
            contents[fpath] = "[Error reading file]"

    tcd["file_contents"] = contents if contents else None
    return tcd


def serialize_tcd(tcd: dict) -> str:
    """Serialize TCD to JSON string for storage/handoff."""
    return json.dumps(tcd, ensure_ascii=False, default=str)


def deserialize_tcd(tcd_json: str) -> dict:
    """Deserialize TCD from JSON string."""
    return json.loads(tcd_json)


def tcd_to_checkpoint(tcd: dict) -> str:
    """Format TCD as a full-view checkpoint for in-session execution (v5).

    No observation masking — the calling session sees everything. Used by
    pm_run_ticket to return TCD state for crash recovery and context.
    """
    sections = [
        f"# Ticket #{tcd['ticket_id']}: {tcd['title']}",
        f"\n## State: {tcd['current_state']}",
        f"\n## Objective\n{tcd['objective']}",
    ]

    if tcd.get("project_brief"):
        sections.append(f"\n## Project Brief\n{tcd['project_brief']}")

    if tcd.get("sprint_goal"):
        sections.append(f"\n## Sprint Goal\n{tcd['sprint_goal']}")

    sections.append(f"\n## Description\n{tcd['description']}")

    plan = tcd.get("pm_plan") or tcd.get("technical_plan")
    if plan:
        sections.append(f"\n## Plan\n{json.dumps(plan, indent=2)}")

    if tcd.get("files_modified"):
        sections.append(f"\n## Files Modified\n{json.dumps(tcd['files_modified'], indent=2)}")

    if tcd.get("implementation_notes"):
        sections.append(f"\n## Implementation Notes\n{tcd['implementation_notes']}")

    file_contents = tcd.get("file_contents")
    if file_contents:
        sections.append("\n## File Contents (pre-loaded)")
        for fp, content in file_contents.items():
            sections.append(f"\n### {fp}\n```\n{content}\n```")

    reviewer_findings = tcd.get("reviewer_findings") or tcd.get("review_findings", [])
    if reviewer_findings:
        sections.append(f"\n## Review Findings\n{json.dumps(reviewer_findings, indent=2)}")

    test_results = tcd.get("test_results")
    if test_results:
        sections.append(f"\n## Test Results\n{json.dumps(test_results, indent=2)}")

    # Budget info
    budget = tcd.get("budget", {})
    if budget.get("allocated"):
        sections.append(
            f"\n## Budget\n"
            f"Allocated: ${budget['allocated']:.2f} | "
            f"Spent: ${budget.get('spent', 0):.2f} | "
            f"Remaining: ${budget.get('remaining', budget['allocated']):.2f}"
        )

    # Stage costs (v5)
    stage_costs = tcd.get("stage_costs", {})
    if stage_costs:
        cost_lines = [f"  {stage}: ${float(cost):.2f}" for stage, cost in stage_costs.items()]
        sections.append("\n## Stage Costs\n" + "\n".join(cost_lines))

    # Decision log (capped)
    decisions = tcd.get("decisions", [])
    if decisions:
        if len(decisions) > 20:
            decisions = decisions[-20:]
            sections.append(f"\n## Decision Log (last 20 of {len(tcd['decisions'])})")
        else:
            sections.append("\n## Decision Log")
        decision_lines = [f"- [{d.get('agent', '?')}] {d.get('decision', '')}" for d in decisions]
        sections[-1] += "\n" + "\n".join(decision_lines)

    return "\n".join(sections)


def tcd_to_agent_context(tcd: dict, role: str) -> str:
    """Format TCD as context string for an agent prompt (LEGACY v1-v4).

    Provides observation masking — each role sees only what's relevant.
    Retained for backward compat with pm_dispatch_agent direct calls.
    For v5 in-session execution, use tcd_to_checkpoint() instead.
    """
    sections = [
        f"# Ticket #{tcd['ticket_id']}: {tcd['title']}",
        f"\n## Objective\n{tcd['objective']}",
    ]

    if tcd.get("project_brief"):
        sections.append(f"\n## Project Brief\n{tcd['project_brief']}")

    if tcd.get("sprint_goal"):
        sections.append(f"\n## Sprint Goal\n{tcd['sprint_goal']}")

    # Backward compat: old TCDs may use "technical_plan" instead of "pm_plan",
    # and "review_findings"/"security_findings" instead of "reviewer_findings"
    plan = tcd.get("pm_plan") or tcd.get("technical_plan")
    reviewer_findings = tcd.get("reviewer_findings") or tcd.get("review_findings", [])
    if not reviewer_findings and tcd.get("security_findings"):
        reviewer_findings = tcd.get("security_findings", [])

    # Role-specific context (observation masking)
    if role == "pm":
        sections.append(f"\n## Description\n{tcd['description']}")

    elif role == "engineer":
        sections.append(f"\n## Description\n{tcd['description']}")
        if plan:
            sections.append(f"\n## PM Plan (implement this)\n{json.dumps(plan, indent=2)}")

        # Fix loop context — show what needs fixing and iteration info
        fix_iter = tcd.get("_fix_iteration")
        fix_type = tcd.get("_fix_type", "")
        if fix_iter:
            sections.append(
                f"\n## ⚠️ FIX ITERATION {fix_iter} ({fix_type} fix loop)\n"
                f"You are in fix iteration {fix_iter}. ALL HIGH and MEDIUM "
                f"findings below MUST be resolved. Each iteration costs budget. "
                f"Fix everything in one pass."
            )

        if reviewer_findings:
            sections.append(f"\n## Reviewer Findings (FIX THESE — ALL HIGH+MEDIUM)\n{json.dumps(reviewer_findings, indent=2)}")

        # QA test failures visible to engineer during QA fix loop
        test_results = tcd.get("test_results")
        if test_results and fix_type == "qa":
            failed_tests = [t for t in test_results.get("tests_run", [])
                           if t.get("result") != "pass"]
            if failed_tests:
                sections.append(f"\n## QA Test Failures (FIX THESE)\n{json.dumps(failed_tests, indent=2)}")
            if test_results.get("notes"):
                sections.append(f"\nQA Notes: {test_results['notes']}")

    elif role in ("reviewer", "verifier"):
        # Verifier/Reviewer: plan + files + implementation + pre-loaded content
        if plan:
            sections.append(f"\n## PM Plan (verify implementation against this)\n{json.dumps(plan, indent=2)}")
        if tcd.get("files_modified"):
            sections.append(f"\n## Files Modified\n{json.dumps(tcd['files_modified'], indent=2)}")
        if tcd.get("implementation_notes"):
            sections.append(f"\n## Implementation Notes\n{tcd['implementation_notes']}")
        # Pre-loaded file contents (saves 3-8 Read tool calls)
        file_contents = tcd.get("file_contents")
        if file_contents:
            sections.append("\n## File Contents (pre-loaded)")
            for fp, content in file_contents.items():
                sections.append(f"\n### {fp}\n```\n{content}\n```")
        # Previous findings for fix iterations
        if reviewer_findings:
            sections.append(f"\n## Previous Findings (FIX THESE)\n{json.dumps(reviewer_findings, indent=2)}")

    elif role == "qa":
        # QA sees everything for comprehensive testing
        if plan:
            sections.append(f"\n## PM Plan (test against acceptance criteria)\n{json.dumps(plan, indent=2)}")
        if tcd.get("files_modified"):
            sections.append(f"\n## Files Modified\n{json.dumps(tcd['files_modified'], indent=2)}")
        if tcd.get("implementation_notes"):
            sections.append(f"\n## Implementation Notes\n{tcd['implementation_notes']}")
        file_contents = tcd.get("file_contents")
        if file_contents:
            sections.append("\n## File Contents (pre-loaded)")
            for fp, content in file_contents.items():
                sections.append(f"\n### {fp}\n```\n{content}\n```")
        if reviewer_findings:
            sections.append(f"\n## Reviewer Findings\n{json.dumps(reviewer_findings, indent=2)}")

    # Budget info for all roles (cost consciousness)
    budget = tcd.get("budget", {})
    if budget.get("allocated"):
        sections.append(
            f"\n## Budget\n"
            f"Allocated: ${budget['allocated']:.2f} | "
            f"Spent: ${budget.get('spent', 0):.2f} | "
            f"Remaining: ${budget.get('remaining', budget['allocated']):.2f}\n"
            f"Each fix iteration costs $3-5. Optimize for one-shot completion."
        )

    # Decisions history (compact, capped at last 20 to prevent context bloat)
    decisions = tcd.get("decisions", [])
    if decisions:
        if len(decisions) > 20:
            decisions = decisions[-20:]
            sections.append(f"\n## Decision Log (last 20 of {len(tcd['decisions'])})")
        else:
            sections.append("\n## Decision Log")
        decision_lines = [f"- [{d['agent']}] {d['decision']}" for d in decisions]
        sections[-1] += "\n" + "\n".join(decision_lines)

    return "\n".join(sections)
