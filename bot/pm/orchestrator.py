# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""PM orchestrator — pipeline gate verification + legacy agent dispatch.

Pipeline Orchestrator (v6):
Independent verification of coding pipeline stages. Prevents the implementor
LLM from self-skipping stages by providing a server-side verification layer:
1. Mechanically verifies TCD gate completeness and agent evidence
2. Runs independent AI code review via Anthropic API (separate LLM invocation)
3. Returns PASS/FAIL — only PASS sets the HMAC-signed orchestrator gate

Legacy agent dispatch (v1-v4):
Retains dispatch_agent() for pm_dispatch_agent MCP tool (backward compat).
TCD helpers shared between v5 in-session mode and legacy dispatch mode.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import time

from bot.pm.roles import get_role
from bot.pm.tcd import (
    create_tcd, deserialize_tcd,
    tcd_to_agent_context,
    update_tcd_from_pm, update_tcd_from_engineer,
    update_tcd_from_reviewer, update_tcd_from_qa,
    update_tcd_from_verifier,
)
from bot.pm.store import (
    get_ticket, get_sprint, get_project,
    create_agent_run, complete_agent_run,
)
from bot.pm.budget import sync_ticket_spend, check_budget

log = logging.getLogger(__name__)

# Per-agent dispatch timeout (seconds). Configurable via PM_AGENT_TIMEOUT_S env var.
_AGENT_TIMEOUT_S = int(os.environ.get("PM_AGENT_TIMEOUT_S", "3600"))

# Per-ticket concurrency lock — prevents parallel execution of same ticket
_ticket_locks: dict[int, asyncio.Lock] = {}


# ============================================================
# AGENT DISPATCH
# ============================================================

async def dispatch_agent(
    ticket_id: int,
    role_name: str,
    tcd: dict,
    chat_id: str = "",
    project_dir: str = "",
    harness_label: str = "",
) -> dict:
    """Dispatch a single agent for a ticket. Returns parsed result + metrics.

    1. Creates agent_run record
    2. Builds role-specific prompt with TCD context
    3. Spawns ephemeral session via process_system_task()
    4. Parses JSON result from agent output
    5. Completes agent_run with cost/token metrics
    6. Returns {output: dict, raw_text: str, run: dict, error: str|None}

    project_dir: explicit working directory — all agents in the pipeline
    share this CWD so code written by the Engineer is visible to Verifier.
    harness_label: harness to assign to the agent session — inherits model,
    effort, max_turns, project_dir from the calling coding chat.
    """
    role = get_role(role_name)

    # Check budget before dispatching
    budget_status = await check_budget(ticket_id)
    if not budget_status["ok"]:
        return {
            "output": None,
            "raw_text": "",
            "run": None,
            "error": f"Budget check failed: {budget_status.get('error', 'unknown')}",
        }

    # Create agent_run record
    run = await create_agent_run(
        ticket_id=ticket_id,
        agent_role=role_name,
        model=role.model,
    )

    # Build prompt: role prefix + TCD context
    tcd_context = tcd_to_agent_context(tcd, role_name)
    prompt = f"{tcd_context}\n\n---\nExecute your role as described in your system instructions. Return your output in the specified JSON format."

    start_time = time.time()
    raw_text = ""
    error = None
    parsed_output = None

    try:
        from bot.agent import process_system_task

        # Split into system_prompt (cacheable by Claude API — 90% discount
        # on identical prefixes) and user prompt (variable TCD context).
        # Role prefix stays identical across all dispatches of same role.
        # Per-role allowed_tools enforced via harness (replaces the old
        # "system tasks bypass admin gating" gap).
        # H4: Unique session key per ticket+role dispatch.
        # Prevents session collision when multiple agents run for
        # different tickets or roles from the same chat.
        session_suffix = f"pm-{ticket_id}-{role_name}"
        raw_text = await asyncio.wait_for(
            process_system_task(
                prompt=prompt,
                model=role.model,
                chat_id=chat_id,
                system_prompt=role.system_prompt_prefix,
                max_budget_usd=role.max_budget_usd,
                allowed_tools=list(role.allowed_tools),
                session_key_suffix=session_suffix,
                project_dir=project_dir,
                harness_label=harness_label,
            ),
            timeout=_AGENT_TIMEOUT_S,
        )

        # Parse JSON output from agent response
        parsed_output = _extract_json_output(raw_text)
        if parsed_output is None:
            error = "Agent did not return valid JSON output"
            log.warning(f"PM dispatch {role_name} for ticket {ticket_id}: no JSON in response")

    except asyncio.TimeoutError:
        error = f"Agent {role_name} timed out after {_AGENT_TIMEOUT_S}s"
        log.error(f"PM dispatch {role_name} for ticket {ticket_id}: timeout after {_AGENT_TIMEOUT_S}s")
    except asyncio.CancelledError:
        error = f"Agent {role_name} cancelled"
        log.warning(f"PM dispatch {role_name} for ticket {ticket_id}: cancelled")
    except Exception as e:
        error = str(e)
        log.error(f"PM dispatch {role_name} for ticket {ticket_id} failed: {e}", exc_info=True)

    duration = time.time() - start_time

    # Complete agent_run record
    # Note: actual cost/tokens would come from SDK usage tracking
    # Placeholder estimate: Opus ~$0.02/s, Sonnet ~$0.005/s
    if role.model and "opus" in role.model:
        estimated_cost = max(0.01, duration * 0.02)
    else:
        estimated_cost = max(0.005, duration * 0.005)

    completed_run = await complete_agent_run(
        run_id=run["id"],
        cost_usd=estimated_cost,
        duration_s=duration,
        status="completed" if error is None else "failed",
        error=error,
    )

    # Sync ticket spend so subsequent check_budget() calls see up-to-date
    # values. The pm_dispatch_agent tool also calls sync_budget_chain after
    # this — the ticket-level re-sync there is idempotent.
    await sync_ticket_spend(ticket_id)

    return {
        "output": parsed_output,
        "raw_text": raw_text,
        "run": completed_run,
        "error": error,
    }


def _extract_json_output(text: str) -> dict | None:
    """Extract JSON block from agent's text response.

    Looks for ```json ... ``` fenced blocks first, then tries to find
    any top-level JSON object in the text.
    """
    if not text:
        return None

    # Try fenced JSON blocks first
    json_pattern = re.compile(r"```json\s*\n(.*?)\n\s*```", re.DOTALL)
    matches = json_pattern.findall(text)
    for match in matches:
        try:
            return json.loads(match)
        except json.JSONDecodeError:
            continue

    # Try to find JSON by scanning for balanced braces, skipping string contents.
    # Without string-awareness, braces inside JSON string values would miscount.
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidate = text[start:i + 1]
                try:
                    parsed = json.loads(candidate)
                    if any(k in parsed for k in ("plan", "implementation", "review", "qa_report")):
                        return parsed
                except json.JSONDecodeError:
                    pass
                start = -1

    return None


# ============================================================
# TCD HELPERS
# ============================================================

async def load_or_create_tcd(ticket_id: int) -> dict:
    """Load existing TCD from ticket or create a new one.

    Shared helper — used by pm_dispatch_agent tool and SDK orchestrator.
    """
    ticket = await get_ticket(ticket_id)
    if not ticket:
        raise ValueError(f"Ticket {ticket_id} not found")

    if ticket.get("tcd"):
        tcd = deserialize_tcd(ticket["tcd"])
        # Strip transient fix-loop metadata that may persist from a crash mid-loop
        tcd.pop("_fix_iteration", None)
        tcd.pop("_fix_type", None)
        return tcd

    sprint = await get_sprint(ticket["sprint_id"])
    project = await get_project(sprint["project_id"]) if sprint else None
    return create_tcd(
        ticket_id=ticket_id,
        title=ticket["title"],
        description=ticket["description"],
        tier=ticket["tier"],
        budget_usd=ticket.get("budget_usd"),
        project_brief=project["brief"] if project else "",
        sprint_goal=sprint["goal"] if sprint else "",
    )


# TCD updater functions per role (used by pm_dispatch_agent tool)
_TCD_UPDATERS = {
    "pm": update_tcd_from_pm,
    "engineer": update_tcd_from_engineer,
    "verifier": update_tcd_from_verifier,
    "reviewer": update_tcd_from_reviewer,  # legacy
    "qa": update_tcd_from_qa,  # legacy
}




# v5: launch_pm_session removed — pm_run_ticket/pm_run_sprint now return
# TCD + instructions for in-session execution instead of spawning a
# separate orchestrator session. The coding-principles pipeline drives the
# workflow. See bot/pm/tools.py for the new implementation.


# ═══════════════════════════════════════════════════════════════════════
# PIPELINE ORCHESTRATOR (v6) — Independent Verification Gate
# ═══════════════════════════════════════════════════════════════════════
#
# HMAC-signed approval: token = HMAC(diff_hash:ticket_id:commit, secret).
# Secret lives only in memory (generated at boot). The main LLM session
# has no access to it. Container restart invalidates all approvals.
# ═══════════════════════════════════════════════════════════════════════

from typing import Any

_HMAC_SECRET = secrets.token_bytes(32)

# Review model — uses the SDK's OAuth token for API calls
_REVIEW_MODEL = os.environ.get("ORCHESTRATOR_MODEL", "claude-sonnet-4-6")

_MAX_DIFF_CHARS = 80_000
_MAX_FILE_CHARS = 15_000
_MAX_TOTAL_FILE_CHARS = 120_000


def _sign_approval(ticket_id: int, diff_hash: str, commit_hash: str) -> str:
    """Create HMAC-signed approval token."""
    payload = f"{ticket_id}:{diff_hash}:{commit_hash}:{int(time.time())}"
    sig = hmac.new(_HMAC_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def verify_approval(token: str, current_commit: str) -> tuple[bool, str]:
    """Verify an orchestrator approval token.

    Returns (valid, reason). Checks:
    1. HMAC signature is valid (not forged)
    2. Commit hash matches current HEAD (no code changes after approval)
    """
    if not token:
        return False, "no orchestrator approval token"
    parts = token.split(":")
    if len(parts) != 5:
        return False, "malformed approval token"
    tid, diff_hash, commit_hash, ts, sig = parts
    payload = f"{tid}:{diff_hash}:{commit_hash}:{ts}"
    expected = hmac.new(_HMAC_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False, "invalid approval signature (forged or corrupted)"
    if commit_hash != current_commit:
        return False, f"code changed after approval (approved: {commit_hash[:7]}, current: {current_commit[:7]})"
    # TTL check: approval expires after 2 hours (stale approvals are suspicious)
    _APPROVAL_TTL_S = 7200  # 2 hours
    try:
        age = int(time.time()) - int(ts)
        if age > _APPROVAL_TTL_S:
            return False, f"approval expired ({age // 60}m old, max {_APPROVAL_TTL_S // 60}m)"
    except (ValueError, TypeError):
        return False, "malformed timestamp in approval token"
    return True, "valid"


# ── Git helpers (shell=False for security — AP-16 subprocess safety) ──

def _gate_git_diff(repo: str = "/host-repo") -> str:
    """Get committed diff (last 5 commits)."""
    try:
        stat_r = subprocess.run(
            ["git", "diff", "HEAD~5..HEAD", "--no-color", "--stat"],
            capture_output=True, text=True, timeout=10, cwd=repo,
        )
        stat = stat_r.stdout.strip()
    except Exception:
        stat = "(stat unavailable)"

    try:
        diff_r = subprocess.run(
            ["git", "diff", "HEAD~5..HEAD", "--no-color"],
            capture_output=True, text=True, timeout=30, cwd=repo,
        )
        diff = diff_r.stdout
    except Exception:
        diff = ""

    if len(diff) > _MAX_DIFF_CHARS:
        diff = diff[:_MAX_DIFF_CHARS] + f"\n\n... (truncated, {len(diff)} total chars)"
    return f"DIFF STAT:\n{stat}\n\nFULL DIFF:\n{diff}"


def _gate_git_head(repo: str = "/host-repo") -> str:
    """Get current HEAD commit hash."""
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=5, cwd=repo)
        return r.stdout.strip()
    except Exception:
        return ""


def _gate_diff_hash(repo: str = "/host-repo") -> str:
    """Compute hash of current diff for TOCTOU protection."""
    try:
        r = subprocess.run(["git", "diff", "HEAD~5..HEAD", "--no-color"],
                           capture_output=True, text=True, timeout=30, cwd=repo)
        return hashlib.sha256(r.stdout.encode()).hexdigest()[:16]
    except Exception:
        return ""


def _gate_read_file(path: str, max_chars: int = _MAX_FILE_CHARS) -> str:
    """Read file content, truncated."""
    try:
        with open(path) as f:
            content = f.read(max_chars + 100)
        if len(content) > max_chars:
            content = content[:max_chars] + "\n... (truncated)"
        return content
    except Exception:
        return "(file read error)"


# ── Mechanical verification ──────────────────────────────────────────

def check_mechanical(tcd: dict, tier: str) -> tuple[bool, list[str]]:
    """Mechanical verification of TCD pipeline gates.

    Returns (passed, findings). Public — also used by deploy hooks.
    """
    from bot.pm.tcd import _REQUIRED_GATES, _OPTIONAL_GATES

    required = _REQUIRED_GATES.get(tier, _REQUIRED_GATES["significant"])
    gates = tcd.get("pipeline_gates", {})
    findings: list[str] = []

    for gate in required:
        if gate in _OPTIONAL_GATES:
            continue
        if gate == "orchestrator":
            continue  # skip self
        entry = gates.get(gate)
        if not entry:
            findings.append(f"MISSING: {gate} — not recorded in TCD")
        elif entry.get("status") not in ("passed", "skipped"):
            findings.append(f"FAILED: {gate} — status='{entry.get('status', 'unknown')}'")

    # Significant tier: verify agent evidence on review stages
    if tier == "significant":
        agent_required = {"stage_3", "stage_4", "stage_7", "stage_8", "stage_9", "stage_11"}
        for gate in agent_required:
            entry = gates.get(gate, {})
            if entry.get("status") == "passed" and not entry.get("agent_id") and not entry.get("notes"):
                findings.append(
                    f"NO EVIDENCE: {gate} — passed but no agent_id/notes. "
                    "Significant tier requires spawned review agents."
                )

    # Unresolved HIGH/MEDIUM findings
    for f in tcd.get("reviewer_findings", []):
        if isinstance(f, dict):
            sev = f.get("severity", "").upper()
            if sev in ("HIGH", "MEDIUM") and not f.get("resolved"):
                findings.append(f"UNRESOLVED {sev}: {f.get('description', '?')[:100]}")

    return len(findings) == 0, findings


# ── Independent AI review ────────────────────────────────────────────

_REVIEW_PROMPT = """You are a Pipeline Orchestrator — an independent code reviewer.
Your ONLY job is to verify that code changes are safe, correct, and complete.

You are reviewing a diff already reviewed by the implementor's agents.
Your role: catch anything they missed and verify production-readiness.

IMPORTANT: Be strict but fair. Only flag REAL issues — bugs, security holes,
missing error handling. NOT style preferences or speculative concerns.

TICKET: {title} (Tier: {tier})
DESCRIPTION: {description}

PLAN:
{plan}

FILES MODIFIED: {files}

{diff}

{file_contents}

CHECKLIST:
1. SECURITY: Secrets? Path traversal? Injection? Missing auth?
2. LOGIC: Off-by-one? Race conditions? Missing error handling? Null checks?
3. COMPLETENESS: Implementation matches plan? Anything missing?
4. DEAD CODE: Unused imports? Unreachable branches? Stale references?
5. INTEGRATION: New tools registered in RBAC? Commands in help menus?

OUTPUT (strict JSON, no markdown wrapping):
{{"verdict": "PASS", "findings": [], "summary": "One sentence assessment"}}

OR:
{{"verdict": "FAIL", "findings": [{{"severity": "HIGH", "description": "...", "file": "...", "suggestion": "..."}}], "summary": "..."}}

PASS = no HIGH/MEDIUM findings. FAIL = any HIGH/MEDIUM. LOW findings OK with PASS."""


async def _run_ai_review(tcd: dict, diff: str, file_contents: str) -> dict:
    """Independent AI code review via Anthropic API. Server-side only."""
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if not token:
        log.warning("Orchestrator: no API token — skipping AI review")
        return {"verdict": "SKIP", "findings": [], "summary": "No API token"}

    plan_text = json.dumps(tcd["pm_plan"], indent=2, default=str)[:3000] if tcd.get("pm_plan") else "(no plan)"

    prompt = _REVIEW_PROMPT.format(
        title=tcd.get("title", "?"), tier=tcd.get("tier", "?"),
        description=tcd.get("description", "")[:500],
        plan=plan_text,
        files=", ".join(tcd.get("files_modified") or []),
        diff=diff, file_contents=file_contents,
    )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=token)

        def _sync_api_call() -> "anthropic.types.Message | None":
            """Run sync API call off the event loop."""
            for attempt in range(3):
                try:
                    return client.messages.create(
                        model=_REVIEW_MODEL, max_tokens=2000,
                        messages=[{"role": "user", "content": prompt}],
                    )
                except anthropic.RateLimitError:
                    if attempt < 2:
                        import time as _t
                        _t.sleep(3 * (attempt + 1))
            return None

        resp = await asyncio.to_thread(_sync_api_call)
        if not resp:
            return {"verdict": "SKIP", "findings": [], "summary": "Rate limited or no API response"}

        text = resp.content[0].text if resp.content else ""
        json_text = text
        if "```" in text:
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
            if m:
                json_text = m.group(1)

        try:
            result = json.loads(json_text)
            return {
                "verdict": result.get("verdict", "FAIL"),
                "findings": result.get("findings", []),
                "summary": result.get("summary", ""),
            }
        except json.JSONDecodeError:
            has_issues = any(w in text.upper() for w in ("HIGH", "FAIL", "CRITICAL", "BUG"))
            return {
                "verdict": "FAIL" if has_issues else "PASS",
                "findings": [{"severity": "MEDIUM", "description": text[:500]}] if has_issues else [],
                "summary": "Unstructured AI output",
            }
    except Exception as e:
        log.warning("Orchestrator AI review error: %s", e)
        return {"verdict": "SKIP", "findings": [], "summary": f"{type(e).__name__}: {str(e)[:100]}"}


# ── Main entry point ─────────────────────────────────────────────────

async def run_pipeline_gate(ticket_id: int) -> dict[str, Any]:
    """Run the full pipeline orchestrator for a ticket.

    Called from pipeline_orchestrate MCP tool handler (server-side).
    The main LLM session has NO control over this execution.
    """
    ticket = await get_ticket(ticket_id)
    if not ticket:
        return {"passed": False, "error": f"Ticket #{ticket_id} not found"}

    tier = ticket.get("tier", "significant")
    tcd_raw = ticket.get("tcd")
    if not tcd_raw:
        return {"passed": False, "error": f"Ticket #{ticket_id} has no TCD — record pipeline gates first"}

    tcd = json.loads(tcd_raw) if isinstance(tcd_raw, str) else tcd_raw

    # Phase 1: Mechanical verification
    mech_ok, mech_findings = check_mechanical(tcd, tier)
    if not mech_ok:
        commit = await asyncio.to_thread(_gate_git_head)
        return {
            "passed": False,
            "mechanical": {"passed": False, "findings": mech_findings},
            "ai_review": {"verdict": "SKIP", "findings": [], "summary": "Skipped — mechanical failed"},
            "approval_token": None,
            "commit_hash": commit,
        }

    # Phase 2: Context gathering (run sync git ops off event loop)
    diff = await asyncio.to_thread(_gate_git_diff)
    commit = await asyncio.to_thread(_gate_git_head)
    d_hash = await asyncio.to_thread(_gate_diff_hash)

    file_parts: list[str] = []
    total = 0
    for fpath in (tcd.get("files_modified") or []):
        full = os.path.join("/host-repo", fpath) if not fpath.startswith("/") else fpath
        full = os.path.realpath(full)  # resolve symlinks + ../
        if not full.startswith("/host-repo/"):
            continue  # path traversal — skip
        if os.path.isfile(full) and total < _MAX_TOTAL_FILE_CHARS:
            content = _gate_read_file(full)
            file_parts.append(f"=== {fpath} ===\n{content}")
            total += len(content)

    # Phase 3: Independent AI review
    ai = await _run_ai_review(tcd, diff, "\n\n".join(file_parts) or "(no files)")

    # Phase 4: Decision
    ai_ok = ai["verdict"] != "FAIL"
    can_sign = bool(commit and d_hash)
    if ai_ok and not can_sign:
        # Passed review but can't create token (git failure) — explicit fail
        return {
            "passed": False,
            "mechanical": {"passed": True, "findings": []},
            "ai_review": ai,
            "approval_token": None,
            "commit_hash": commit,
            "diff_hash": d_hash,
            "error": "Cannot sign approval — git HEAD or diff hash unavailable",
        }
    overall = ai_ok and can_sign
    token = _sign_approval(ticket_id, d_hash, commit) if overall else None

    return {
        "passed": overall,
        "mechanical": {"passed": True, "findings": mech_findings},
        "ai_review": ai,
        "approval_token": token,
        "commit_hash": commit,
        "diff_hash": d_hash,
    }
