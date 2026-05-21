# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Onboarding step implementations — individual step logic.

Each step checks preconditions, shows instructions, and handles completion.
Steps delegate to existing auth flows (/claude-auth, /m365-auth) rather
than reimplementing them.
"""

from __future__ import annotations

import logging

from bot.onboarding.models import OnboardingState
from bot.onboarding.store import update_onboarding

log = logging.getLogger(__name__)


# NOTE: Legacy step checkers (check_claude_auth_complete, check_m365_auth_complete,
# check_google_auth_complete) removed in CB-52 review. Auto-detection is now in
# engine.py using per-user credential checks (get_user_oauth_token, list_credentials).


# ── Content Sanitization (used by import flows + hooks) ──────────────

# Rate limit: max imports per user per hour
_import_counts: dict[str, list[float]] = {}  # user_id -> [timestamps]
_MAX_IMPORTS_PER_HOUR = 10

# Patterns that could be interpreted as bot control instructions
_BLOCKED_PATTERNS = [
    "FACTS_UPDATE", "SYSTEM:", "[SYSTEM", "<%", "%>",
    "<tool_use>", "</tool_use>", "<function_calls>",
]


async def import_text_knowledge(
    user_id: str,
    text: str,
    source_label: str = "pasted context",
) -> dict:
    """Import text as facts into the user's knowledge store.

    Sanitizes content, rate-limits per user, and stores as a scoped fact.
    Returns summary of what was imported.
    """
    import time
    from bot.storage.facts import save_message_fact
    from datetime import datetime, timezone

    # Rate limiting
    now_ts = time.time()
    user_times = _import_counts.get(user_id, [])
    user_times = [t for t in user_times if now_ts - t < 3600]  # last hour
    if len(user_times) >= _MAX_IMPORTS_PER_HOUR:
        return {"error": f"Rate limit: max {_MAX_IMPORTS_PER_HOUR} imports per hour"}
    user_times.append(now_ts)
    _import_counts[user_id] = user_times

    # Content sanitization — strip patterns that could be interpreted as bot instructions
    sanitized = text
    for pattern in _BLOCKED_PATTERNS:
        sanitized = sanitized.replace(pattern, f"[blocked:{pattern[:8]}]")

    # Store as a user-scoped fact (subject=user_id ensures isolation)
    now = datetime.now(timezone.utc).isoformat()
    key = f"{user_id}_imported_{now[:10]}_{source_label.replace(' ', '_')[:30]}"
    await save_message_fact(
        key=key,
        value=sanitized[:5000],  # cap at 5K chars per fact
        subject=user_id,
        category="imported_knowledge",
    )
    return {
        "key": key,
        "chars_stored": min(len(sanitized), 5000),
        "truncated": len(text) > 5000,
        "source": source_label,
    }



# NOTE: save_profile and domain_access steps removed in CB-51.
# _BLOCKED_PATTERNS and _MAX_IMPORTS_PER_HOUR retained — used by
# hooks.py content sanitization and test_onboarding.py tests.


# ── Step: Scheduled Tasks Setup ────────────────────────────────────────

async def create_employee_tasks(
    state: OnboardingState,
    chat_id: str,
    opt_in: bool = True,
) -> dict:
    """Create employee-scoped scheduled tasks during onboarding.

    Tasks are scoped to the employee's DM chat_id, so they:
    - Use the employee's email/calendar accounts (via harness allowed_emails)
    - Run with the employee's AccessContext (own-chat data only)
    - Send output to the employee's DM (not admin group)
    """
    if not opt_in:
        log.info("Employee %s opted out of scheduled tasks", state.user_id)
        return {"created": 0, "skipped": True}

    from bot.scheduler import add_task, EMPLOYEE_TASK_TEMPLATES

    created = 0
    for tmpl in EMPLOYEE_TASK_TEMPLATES:
        if not tmpl.get("default_opt_in", True):
            continue
        task_id = f"emp_{state.user_id}_{tmpl['id_suffix']}"
        # Check if task already exists (idempotent on re-onboarding)
        from bot.scheduler import get_task
        if get_task(task_id):
            log.info("Employee task %s already exists, skipping", task_id)
            created += 1
            continue
        task_name = f"{state.display_name or state.user_id}'s {tmpl['name']}"
        try:
            add_task(
                name=task_name,
                hour=tmpl["hour"],
                minute=tmpl["minute"],
                prompt=tmpl["prompt"],
                days=tmpl["days"],
                platform=tmpl["platform"],
                enabled=tmpl["enabled"],
                chat_id=str(chat_id),
                interval_hours=tmpl.get("interval_hours"),
                task_id=task_id,
            )
            created += 1
            log.info("Created employee task %s for user %s (chat=%s)",
                     task_id, state.user_id, chat_id)
        except Exception as e:
            log.warning("Failed to create employee task %s: %s", task_id, e)

    state.step_data["scheduled_tasks_created"] = created

    # Set allowed_credentials on the employee's DM harness override.
    # This ensures Layer 1b3 only allows tools to use this employee's credentials.
    try:
        from bot.auth.credentials import list_credentials
        # Find all credentials owned by this user
        # Owner format matches register_credential_metadata: "telegram:<user_id>"
        _owner_key = state.user_id
        if ":" not in _owner_key:
            _owner_key = f"telegram:{_owner_key}"
        user_creds = await list_credentials(scope="user", owner=_owner_key)
        if user_creds:
            cred_ids = [c["id"] for c in user_creds]
            from bot.harness import set_chat_override
            chat_key = f"telegram:{chat_id}"
            set_chat_override(chat_key, "allowed_credentials", cred_ids)
            log.info("Set allowed_credentials for %s: %s", chat_key, cred_ids)
            state.step_data["allowed_credentials"] = cred_ids
    except Exception as e:
        log.warning("Failed to set allowed_credentials for employee %s: %s", state.user_id, e)

    await update_onboarding(state)
    return {"created": created, "skipped": False}


# ── Admin: Onboarding status dashboard ───────────────────────────────

async def format_admin_dashboard(status_filter: str | None = None) -> str:
    """Format the admin onboarding dashboard as HTML."""
    from bot.onboarding.store import list_onboarding

    states = await list_onboarding(status=status_filter)
    if not states:
        filter_msg = f" (filter: {status_filter})" if status_filter else ""
        return f"No onboarding records found{filter_msg}."

    lines = [f"<b>\U0001f4cb Onboarding Dashboard</b> ({len(states)} users)\n"]
    for s in states:
        if s.status == "complete":
            icon = "\u2705"
        elif s.status == "in_progress":
            icon = "\U0001f7e1"
        elif s.status == "skipped":
            icon = "\u23ed\ufe0f"
        else:
            icon = "\u2b1c"

        name = s.display_name or s.user_id
        done = len(s.completed_steps)
        skipped = len(s.skipped_steps)
        step = s.current_step or "—"

        lines.append(
            f"{icon} <b>{_esc(name)}</b> — {s.status}\n"
            f"   Completed: {done} | Skipped: {skipped} | Current: {step}"
        )

    return "\n".join(lines)


def _esc(text: str) -> str:
    import html
    return html.escape(text)
