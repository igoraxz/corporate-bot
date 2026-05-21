# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Onboarding data models — step definitions and state management."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ── Step definitions ──────────────────────────────────────────────────

ONBOARDING_STEPS = [
    {
        "id": "welcome",
        "name": "Welcome & Role Assignment",
        "description": "Welcome message and RBAC role assignment",
        "required": True,
        "auto_complete": True,  # completes automatically on detection
    },
    {
        "id": "claude_auth",
        "name": "Claude AI Authentication",
        "description": "Set up your Claude API access for personalized AI sessions",
        "required": False,
        "condition": "always",  # always shown
        "instructions": (
            "Run /claude-auth <label> in this DM to set up your personal Claude access.\n"
            "Example: /claude-auth main\n\n"
            "This gives you a personal AI session with your own Claude subscription. "
            "Skip this step if your organization uses a shared Claude account."
        ),
    },
    {
        "id": "m365_auth",
        "name": "Microsoft 365 Integration",
        "description": "Connect your Outlook mail and calendar",
        "required": False,
        "condition": "MS365_MCP_CLIENT_ID",  # only shown if M365 is configured
        "instructions": (
            "Run /m365-auth to connect your Microsoft 365 account.\n"
            "This enables:\n"
            "• Reading your Outlook emails\n"
            "• Managing your Outlook calendar\n"
            "• Cross-calendar conflict detection"
        ),
    },
    {
        "id": "google_auth",
        "name": "Google Workspace Integration",
        "description": "Connect your Gmail and Google Calendar",
        "required": False,
        "condition": "GOOGLE_OAUTH_CLIENT_ID",  # only if Google is configured
        "instructions": (
            "Connect your Google account for Gmail search and Google Calendar integration.\n\n"
            "Run: /google-auth\n\n"
            "This starts the Google OAuth flow. You'll sign in to Google and "
            "grant access to Gmail and Calendar.\n\n"
            "Skip if you don't use Google Workspace."
        ),
    },
    {
        "id": "github_auth",
        "name": "GitHub Integration",
        "description": "Connect your GitHub account for repository access",
        "required": False,
        "condition": "GITHUB_OAUTH_CLIENT_ID",
        "instructions": (
            "Connect your GitHub account for repository access, PRs, and code search.\n\n"
            "Run: /github-auth\n\n"
            "This uses GitHub's device code flow — you'll get a URL and code "
            "to enter on GitHub's login page. The bot polls for completion automatically.\n\n"
            "Skip if you don't use GitHub."
        ),
    },
    {
        "id": "schedule_tasks",
        "name": "Automated Routines",
        "description": "Set up daily briefings and email monitoring",
        "required": False,
        "condition": "always",
        "auto_setup": True,  # creates tasks automatically, user can opt out
        "instructions": (
            "I can set up automated routines for you:\n\n"
            "• MORNING BRIEFING (8am weekdays) — your calendar + email summary\n"
            "• EMAIL MONITOR (every 4h weekdays) — alerts on urgent items\n\n"
            "These use YOUR email/calendar accounts (set up in previous steps).\n"
            "Tasks run in this DM — only you see the results.\n\n"
            "Reply YES to enable both, or /skip to skip.\n"
            "You can always add/change routines later by asking me."
        ),
    },
]

# === ADMIN ONBOARDING STEPS ===
# Admins get a different onboarding path focused on connector setup.

ADMIN_ONBOARDING_STEPS = [
    {
        "id": "admin_welcome",
        "name": "Admin Setup",
        "description": "Welcome + connector setup checklist",
        "required": True,
        "auto_complete": True,
    },
    {
        "id": "admin_claude_oauth",
        "name": "Claude SDK Authentication",
        "description": "Set up Claude API access for bot inference",
        "required": True,
        "condition": "always",
        "instructions": (
            "The bot needs a Claude OAuth token to function.\n\n"
            "Run: /claude-auth main\n\n"
            "This opens Anthropic's login page. Sign in, copy the code, "
            "and paste it back here. The token is stored as the 'main' profile.\n\n"
            "Skip if already set up (the bot will detect existing credentials)."
        ),
    },
    {
        "id": "admin_google_oauth",
        "name": "Google Workspace Integration",
        "description": "Connect Gmail and Google Calendar for email/calendar tools",
        "required": False,
        "condition": "GOOGLE_OAUTH_CLIENT_ID",
        "instructions": (
            "Connect a Google account for Gmail search + Google Calendar.\n\n"
            "Run: /google-auth\n\n"
            "This starts the Google OAuth flow. You'll sign in to Google and "
            "grant access to Gmail (read) and Calendar (read/write).\n\n"
            "You can connect multiple accounts — run /google-auth again for each.\n\n"
            "Skip if Google Workspace is not used by your organization."
        ),
    },
    {
        "id": "admin_m365_oauth",
        "name": "Microsoft 365 Integration",
        "description": "Connect Outlook mail and calendar",
        "required": False,
        "condition": "MS365_MCP_CLIENT_ID",
        "instructions": (
            "Connect Microsoft 365 for Outlook email and calendar.\n\n"
            "Run: /m365-auth\n\n"
            "This uses the device code flow — you'll get a URL and code to enter "
            "on Microsoft's login page. The bot polls for completion automatically.\n\n"
            "Skip if M365 is not used by your organization."
        ),
    },
    {
        "id": "admin_verify",
        "name": "Verify Connectors",
        "description": "Check all configured integrations are working",
        "required": False,
        "condition": "always",
        "instructions": (
            "Let's verify your setup is working. I'll check:\n"
            "- Claude SDK authentication status\n"
            "- Google Workspace connectivity (if configured)\n"
            "- Microsoft 365 connectivity (if configured)\n\n"
            "Reply YES to run the check, or SKIP to finish setup."
        ),
    },
]

ADMIN_STEP_IDS = [s["id"] for s in ADMIN_ONBOARDING_STEPS]

STEP_IDS = [s["id"] for s in ONBOARDING_STEPS]
ALL_STEP_IDS = STEP_IDS + ADMIN_STEP_IDS


def get_step_def(step_id: str) -> dict | None:
    """Get step definition by ID (searches both employee and admin steps)."""
    for s in ONBOARDING_STEPS:
        if s["id"] == step_id:
            return s
    for s in ADMIN_ONBOARDING_STEPS:
        if s["id"] == step_id:
            return s
    return None


def _check_condition(condition: str) -> bool:
    """Check if a deployment condition is met.

    Uses config module attrs (not os.environ) because config.py strips
    sensitive vars from env after reading them. Boolean values like
    TEAMS_ENABLED must be checked as actual booleans, not truthy strings.
    """
    if condition == "always":
        return True
    # Map condition names to config module attributes
    import os
    try:
        import config as _cfg
        # Boolean flags (TEAMS_ENABLED, etc.) — check truthiness properly
        val = getattr(_cfg, condition, None)
        if val is not None:
            # Handle string "false"/"0" as falsy
            if isinstance(val, str) and val.lower() in ("false", "0", "no", ""):
                return False
            return bool(val)
    except Exception:
        pass
    # Fallback: check env (for vars not in config module)
    val = os.environ.get(condition, "")
    if val.lower() in ("false", "0", "no", ""):
        return False
    return bool(val)


def get_applicable_steps() -> list[dict]:
    """Get employee steps applicable to the current deployment (checks conditions)."""
    return [s for s in ONBOARDING_STEPS if _check_condition(s.get("condition", "always"))]


def get_applicable_admin_steps() -> list[dict]:
    """Get admin steps applicable to the current deployment (checks conditions)."""
    return [s for s in ADMIN_ONBOARDING_STEPS if _check_condition(s.get("condition", "always"))]


# ── Onboarding state ──────────────────────────────────────────────────

@dataclass
class OnboardingState:
    """Per-user onboarding progress."""
    user_id: str
    source: str = ""  # CB-132: no platform assumption, caller must provide
    display_name: str = ""
    status: str = "pending"       # pending, in_progress, complete, skipped
    current_step: str = ""
    completed_steps: list[str] = field(default_factory=list)
    skipped_steps: list[str] = field(default_factory=list)
    step_data: dict = field(default_factory=dict)  # per-step state
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None

    def is_step_done(self, step_id: str) -> bool:
        return step_id in self.completed_steps

    def is_step_skipped(self, step_id: str) -> bool:
        return step_id in self.skipped_steps

    @property
    def is_admin(self) -> bool:
        return self.step_data.get("is_admin", False)

    def next_incomplete_step(self) -> str | None:
        """Find the next step that isn't completed or skipped."""
        applicable = get_applicable_admin_steps() if self.is_admin else get_applicable_steps()
        for step in applicable:
            sid = step["id"]
            if sid not in self.completed_steps and sid not in self.skipped_steps:
                return sid
        return None

    def progress_summary(self) -> str:
        """Generate a text checklist of all steps with status."""
        applicable = get_applicable_admin_steps() if self.is_admin else get_applicable_steps()
        lines = []
        for step in applicable:
            sid = step["id"]
            if sid in self.completed_steps:
                icon = "\u2705"  # ✅
            elif sid in self.skipped_steps:
                icon = "\u23ed\ufe0f"  # ⏭️
            elif sid == self.current_step:
                icon = "\u25b6\ufe0f"  # ▶️
            else:
                icon = "\u2b1c"  # ⬜
            lines.append(f"{icon} {step['name']}")
        done = len(self.completed_steps)
        total = len(applicable)
        lines.append(f"\nProgress: {done}/{total} steps completed")
        return "\n".join(lines)

    @classmethod
    def from_row(cls, row: dict) -> "OnboardingState":
        """Create from SQLite row dict."""
        return cls(
            user_id=row["user_id"],
            source=row.get("source", "telegram"),
            display_name=row.get("display_name", ""),
            status=row.get("status", "pending"),
            current_step=row.get("current_step", ""),
            completed_steps=json.loads(row.get("completed_steps", "[]")),
            skipped_steps=json.loads(row.get("skipped_steps", "[]")),
            step_data=json.loads(row.get("step_data", "{}")),
            created_at=row.get("created_at", ""),
            updated_at=row.get("updated_at", ""),
            completed_at=row.get("completed_at"),
        )

    def to_row(self) -> dict:
        """Serialize to dict for DB storage."""
        now = datetime.now(timezone.utc).isoformat()
        return {
            "user_id": self.user_id,
            "source": self.source,
            "display_name": self.display_name,
            "status": self.status,
            "current_step": self.current_step,
            "completed_steps": json.dumps(self.completed_steps),
            "skipped_steps": json.dumps(self.skipped_steps),
            "step_data": json.dumps(self.step_data),
            "created_at": self.created_at or now,
            "updated_at": now,
            "completed_at": self.completed_at,
        }
