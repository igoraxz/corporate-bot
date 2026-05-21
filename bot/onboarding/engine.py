# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Onboarding wizard engine — state machine for step-by-step setup.

Manages the flow: welcome → auth (claude/m365/google/github) → schedule_tasks → complete.
Each step can be completed, skipped, or deferred. The engine tracks state
and renders progress to the user.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from bot.onboarding.models import (
    OnboardingState,
    get_step_def,
    get_applicable_steps,
)
from bot.onboarding.store import update_onboarding

log = logging.getLogger(__name__)


async def run_step(
    state: OnboardingState,
    step_id: str,
    source: str = "",
    chat_id: str = "",
) -> None:
    """Execute a wizard step — show instructions and update state.

    source: If empty, inherits from state.source (CB-132).
    """
    source = source or state.source
    step_def = get_step_def(step_id)
    if not step_def:
        log.warning("Unknown onboarding step: %s", step_id)
        return

    state.current_step = step_id
    state.status = "in_progress"
    await update_onboarding(state)

    if step_id == "welcome":
        await _send_welcome(state, source, chat_id)
        await complete_step(state, step_id, source=source, chat_id=chat_id)
    elif step_id == "admin_welcome":
        await _send_admin_welcome(state, source, chat_id)
        await complete_step(state, step_id, source=source, chat_id=chat_id)
    else:
        # Auto-detect if auth steps are already configured FOR THIS USER.
        # Both admin and employee use per-user credential checks. Admin's creds
        # are personal by default — they can share them to other chats/roles
        # via manage_harness, but onboarding checks the admin's own creds.
        # System-wide file checks are a backward-compat fallback only.
        _auto_done = False
        _provider_map = {
            "claude_auth": "claude", "admin_claude_oauth": "claude",
            "google_auth": "google-workspace", "admin_google_oauth": "google-workspace",
            "m365_auth": "ms365", "admin_m365_oauth": "ms365",
            "github_auth": "github",
        }
        _cred_type = _provider_map.get(step_id)

        if _cred_type:
            try:
                # Primary: per-user credential store (works for all roles)
                if _cred_type == "claude":
                    # Claude: check per-user token OR explicit shared OAuth assigned to chat
                    from bot.auth.oauth_flow import get_user_oauth_token
                    if await get_user_oauth_token(state.user_id, "claude"):
                        _auto_done = True
                    if not _auto_done:
                        # Check if admin EXPLICITLY assigned an OAuth profile to this chat
                        # (not the default fallback — get_oauth_for_chat always returns "default")
                        from config_overrides import get_chat_oauth_overrides
                        _chat_key = f"{source}:{chat_id}" if source and chat_id else ""
                        if _chat_key and _chat_key in get_chat_oauth_overrides():
                            _auto_done = True
                            log.info("claude_auth: explicit OAuth profile assigned to %s", _chat_key)
                elif _cred_type == "github":
                    from bot.auth.oauth_flow import get_user_oauth_token
                    if await get_user_oauth_token(state.user_id, _cred_type):
                        _auto_done = True
                else:
                    # File-based (google/m365): check credential_store by owner
                    from bot.auth.credentials import list_credentials
                    if await list_credentials(owner=state.user_id, cred_type=_cred_type):
                        _auto_done = True

                # Fallback for admin: check system-wide files (pre-CB-52 setups)
                if not _auto_done and step_id.startswith("admin_"):
                    from config import DATA_DIR, list_oauth_profiles
                    if _cred_type == "claude":
                        if any(p.get("exists") for p in list_oauth_profiles()):
                            _auto_done = True
                    elif _cred_type == "google-workspace":
                        _gdir = DATA_DIR / "google-workspace-creds"
                        if _gdir.exists() and any(_gdir.glob("*.json")):
                            _auto_done = True
                    elif _cred_type == "ms365":
                        _mdir = DATA_DIR / "ms365-mcp"
                        if _mdir.exists() and any(_mdir.iterdir()):
                            _auto_done = True

                if _auto_done:
                    await complete_step(state, step_id, source=source, chat_id=chat_id)
                    log.info("Onboarding step '%s' auto-detected as complete for %s", step_id, state.user_id)
            except Exception as e:
                log.debug("%s auto-detect failed: %s", step_id, e)
        if _auto_done:
            return

        # Show step instructions
        instructions = step_def.get("instructions", "")
        step_name = step_def["name"]
        _progress = state.progress_summary()

        msg = (
            f"\U0001f4cb <b>Setup: {_esc(step_name)}</b>\n\n"
            f"{_esc(instructions)}\n\n"
            f"Type <code>/skip</code> to skip this step.\n"
            f"Type <code>/onboarding</code> to see your progress."
        )
        await _send(source, chat_id, msg)


async def complete_step(
    state: OnboardingState,
    step_id: str,
    source: str = "",
    chat_id: str = "",
) -> None:
    """Mark a step as completed and advance to the next one."""
    if step_id not in state.completed_steps:
        state.completed_steps.append(step_id)

    next_step = state.next_incomplete_step()
    if next_step:
        state.current_step = next_step
        await update_onboarding(state)
        await run_step(state, next_step, source=source, chat_id=chat_id)
    else:
        # All steps done
        state.status = "complete"
        state.current_step = ""
        state.completed_at = datetime.now(timezone.utc).isoformat()
        await update_onboarding(state)
        await _send_completion(state, source, chat_id)


async def skip_current_step(
    state: OnboardingState,
    source: str = "",
    chat_id: str = "",
) -> None:
    """Skip the current step and advance."""
    step_id = state.current_step
    if not step_id:
        return

    step_def = get_step_def(step_id)
    if step_def and step_def.get("required"):
        await _send(source, chat_id,
                     f"\u26a0\ufe0f This step ({_esc(step_def['name'])}) is required and cannot be skipped.")
        return

    if step_id not in state.skipped_steps:
        state.skipped_steps.append(step_id)

    next_step = state.next_incomplete_step()
    if next_step:
        state.current_step = next_step
        await update_onboarding(state)
        await _send(source, chat_id,
                     f"\u23ed\ufe0f Skipped: {_esc(step_def['name'] if step_def else step_id)}")
        await run_step(state, next_step, source=source, chat_id=chat_id)
    else:
        state.status = "complete"
        state.current_step = ""
        state.completed_at = datetime.now(timezone.utc).isoformat()
        await update_onboarding(state)
        await _send_completion(state, source, chat_id)


async def show_progress(
    state: OnboardingState,
    source: str = "",
    chat_id: str = "",
) -> None:
    """Show the onboarding progress checklist."""
    summary = state.progress_summary()
    msg = f"\U0001f4cb <b>Onboarding Progress</b>\n\n{_esc(summary)}"
    if state.current_step:
        step_def = get_step_def(state.current_step)
        if step_def:
            msg += f"\n\n\u25b6\ufe0f Current: <b>{_esc(step_def['name'])}</b>"
    await _send(source, chat_id, msg)


async def handle_wizard_input(
    state: OnboardingState,
    message_text: str,
    source: str = "",
    chat_id: str = "",
) -> bool:
    """Handle user input during active onboarding step.

    Returns True if the input was consumed by the wizard.
    """
    step_id = state.current_step
    if not step_id or state.status == "complete":
        return False

    # Step-specific input handling
    lower = message_text.strip().lower()

    if step_id == "admin_verify":
        if lower in ("yes", "y", "check", "verify"):
            status = await check_admin_connector_status()
            status_text = format_connector_status(status)
            await _send(source, chat_id,
                        f"\U0001f50d <b>Connector Status</b>\n\n{status_text}")
            await complete_step(state, step_id, source, chat_id)
            return True
        elif lower in ("skip", "no", "n"):
            await skip_current_step(state, source, chat_id)
            return True

    if step_id == "schedule_tasks":
        if lower in ("yes", "y", "enable", "ok", "sure", "go"):
            from bot.onboarding.steps import create_employee_tasks
            result = await create_employee_tasks(state, chat_id=chat_id, opt_in=True)
            created = result.get("created", 0)
            await _send(source, chat_id,
                        f"\u2705 Created {created} automated routine(s) for you. "
                        "They'll run in this DM using your accounts.")
            await complete_step(state, step_id, source, chat_id)
            return True
        elif lower in ("skip", "no", "n", "later"):
            await skip_current_step(state, source, chat_id)
            return True

    # For other steps: most complete via external flows (/claude-auth, /m365-auth)
    # and the wizard just tracks their completion
    return False


# ── Welcome message ───────────────────────────────────────────────────

async def _send_welcome(
    state: OnboardingState,
    source: str,
    chat_id: str,
) -> None:
    """Send the welcome message to a new user."""
    name = (state.display_name or "there")[:64]
    applicable = get_applicable_steps()
    total = len(applicable) - 1  # exclude welcome step itself

    msg = (
        f"\U0001f44b <b>Welcome, {_esc(name)}!</b>\n\n"
        f"I'm your organization's AI assistant. I can help with:\n"
        f"\u2022 Answering questions about your team's knowledge domains\n"
        f"\u2022 Managing calendars and email\n"
        f"\u2022 Research, document analysis, and task tracking\n\n"
        f"To get the most out of me, let's set up a few things. "
        f"There are <b>{total} optional setup steps</b> — you can complete "
        f"them now or come back anytime by typing <code>/onboarding</code>.\n\n"
        f"You can <code>/skip</code> any step. Let's start!"
    )
    await _send(source, chat_id, msg)
    log.info("Onboarding welcome sent to %s (%s)", state.user_id, source)


async def _send_completion(
    state: OnboardingState,
    source: str,
    chat_id: str,
) -> None:
    """Send the completion message."""
    summary = state.progress_summary()
    msg = (
        f"\U0001f389 <b>You're all set!</b>\n\n"
        f"{_esc(summary)}\n\n"
        f"You can now use me for anything — just send a message.\n"
        f"Type <code>/help</code> to see available commands.\n\n"
        f"Any setup steps you skipped can be completed later via <code>/onboarding</code>."
    )
    await _send(source, chat_id, msg)
    log.info("Onboarding completed for %s", state.user_id)


# ── Admin welcome ────────────────────────────────────────────────────

async def _send_admin_welcome(
    state: OnboardingState,
    source: str,
    chat_id: str,
) -> None:
    """Send the admin setup welcome message."""
    name = (state.display_name or "Admin")[:64]
    msg = (
        f"\U0001f6e0 <b>Welcome, {_esc(name)}! Let's set up your bot.</b>\n\n"
        "As admin, you need to configure connectors before the bot is fully functional:\n\n"
        "1. <b>Claude SDK</b> — <code>/claude-auth main</code> (required for AI inference)\n"
        "2. <b>Google Workspace</b> — <code>/google-auth</code> (Gmail + Calendar)\n"
        "3. <b>Microsoft 365</b> — <code>/m365-auth</code> (Outlook + Teams calendar)\n\n"
        "Run each command when ready. I'll track your progress.\n"
        "Type <code>/onboarding</code> anytime to see what's done."
    )
    await _send(source, chat_id, msg)
    log.info("Admin onboarding welcome sent to %s (%s)", state.user_id, source)


async def check_admin_connector_status() -> dict:
    """Check which admin connectors are configured. Returns status dict."""
    status: dict[str, str] = {}

    # Claude OAuth — check both SDK credential path and profiles dir
    from pathlib import Path
    cred_path = Path.home() / ".claude" / ".credentials.json"
    try:
        from config import CREDENTIALS_DIR
        profiles_dir = CREDENTIALS_DIR / "oauth_profiles"
    except Exception:
        profiles_dir = Path.home() / ".claude" / ".oauth_profiles"
    if cred_path.exists() or (profiles_dir.exists() and any(profiles_dir.glob("*.json"))):
        status["claude_oauth"] = "ready"
    else:
        status["claude_oauth"] = "missing"

    # Google Workspace
    try:
        from config import DATA_DIR
        gws_dir = DATA_DIR / "google-workspace-creds"
        gws_files = list(gws_dir.glob("*.json")) if gws_dir.exists() else []
        if gws_files:
            status["google_workspace"] = f"ready ({len(gws_files)} account(s))"
        else:
            status["google_workspace"] = "not configured"
    except Exception:
        status["google_workspace"] = "unknown"

    # M365
    try:
        from config import DATA_DIR
        m365_paths = [
            DATA_DIR / "credentials" / "ms365-mcp" / "msal_cache.json",
            DATA_DIR / "ms365-mcp" / "msal_cache.json",
        ]
        if any(p.exists() for p in m365_paths):
            status["m365"] = "ready"
        else:
            import os
            if os.environ.get("MS365_MCP_CLIENT_ID"):
                status["m365"] = "configured but not authenticated"
            else:
                status["m365"] = "not configured (env vars missing)"
    except Exception:
        status["m365"] = "unknown"

    return status


def format_connector_status(status: dict) -> str:
    """Format connector status for display."""
    lines = []
    icons = {"ready": "\u2705", "missing": "\u274c", "not configured": "\u2796",
             "unknown": "\u2753"}
    for key, val in status.items():
        icon = icons.get(val.split(" ")[0], "\u2753")
        label = key.replace("_", " ").title()
        lines.append(f"  {icon} <b>{label}</b> — {val}")
    return "\n".join(lines)


# ── Helpers ───────────────────────────────────────────────────────────

def _esc(text: str) -> str:
    """HTML-escape for Telegram."""
    import html
    return html.escape(text)


async def _send(source: str, chat_id: str, text: str,
                message_thread_id: int | None = None) -> None:
    """Send a message to the user on their platform."""
    if not chat_id:
        log.warning("Cannot send onboarding message — no chat_id")
        return

    if source == "telegram":
        try:
            from integrations.telegram import send_message
            try:
                tg_cid = int(chat_id)
            except (ValueError, TypeError):
                log.error("Invalid TG chat_id for onboarding: %r", chat_id)
                return
            _ob_kwargs: dict = {"chat_id": tg_cid, "text": text, "parse_mode": "HTML"}
            if message_thread_id is not None:
                _ob_kwargs["message_thread_id"] = message_thread_id
            await send_message(**_ob_kwargs)
        except Exception as e:
            log.error("Failed to send onboarding TG message: %s", e)
    elif source == "teams":
        try:
            from integrations.teams import get_client
            tc = get_client()
            if tc:
                await tc.send_message(conversation_id=chat_id, text=text)
            else:
                log.warning("Teams client not initialized — onboarding msg dropped")
        except Exception as e:
            log.error("Failed to send onboarding Teams message: %s", e)
