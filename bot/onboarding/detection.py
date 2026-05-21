# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""New user detection — triggers onboarding for first-time users.

Called from the message dispatch pipeline. Checks if the user has an
onboarding record; if not, creates one and triggers the welcome step.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def check_and_start_onboarding(
    source: str,
    user_id: str,
    display_name: str = "",
    chat_id: str = "",
) -> bool:
    """Check if user needs onboarding. Returns True if onboarding was started.

    This should be called on every DM message from a non-admin user.
    It's idempotent — returns False if the user already has an onboarding record.

    Args:
        source: Platform ("telegram" or "teams")
        user_id: User identifier (TG user ID or AAD object ID)
        display_name: User's display name (for welcome message)
        chat_id: Chat ID where the message arrived (for sending welcome)
    """
    from bot.onboarding.store import get_onboarding, create_onboarding, update_onboarding
    from bot.onboarding.engine import run_step

    subject_id = f"{source}:{user_id}"

    # Already has onboarding record — skip
    existing = await get_onboarding(subject_id)
    if existing:
        return False

    # Check if user is in the org config (allowed member)
    if not _is_allowed_member(source, user_id):
        log.info("Onboarding skipped for %s — not in org config", subject_id)
        return False

    # Detect admin vs employee for different onboarding paths
    _is_admin = False
    try:
        from bot.rbac import get_engine
        from bot.rbac.models import Subject
        engine = get_engine()
        role_ids = engine._get_role_ids(Subject(user_id=subject_id))
        _is_admin = "bot_admin" in role_ids
    except Exception:
        from config import ADMIN_USERS
        _is_admin = (source, str(user_id)) in ADMIN_USERS

    # Create onboarding record
    state = await create_onboarding(subject_id, source, display_name)
    if _is_admin:
        state.step_data["is_admin"] = True
        await update_onboarding(state)  # persist is_admin before run_step (crash safety)

    # Auto-assign RBAC role (employee only — admins already have bot_admin)
    if not _is_admin:
        try:
            from bot.rbac.store import assign_role, get_assignments_for_subject
            existing_roles = await get_assignments_for_subject("user", subject_id)
            if not existing_roles:
                await assign_role(
                    subject_type="user",
                    subject_id=subject_id,
                    role_id="employee",
                    granted_by="system:onboarding",
                )
                log.info("RBAC: auto-assigned employee to %s via onboarding", subject_id)
        except Exception as e:
            log.warning("Failed to assign employee role during onboarding: %s", e)

    # Run the welcome step (different for admin vs employee)
    step = "admin_welcome" if _is_admin else "welcome"
    await run_step(state, step, source=source, chat_id=chat_id)
    return True


def _is_allowed_member(source: str, user_id: str) -> bool:
    """Check if user is listed in org config, RBAC, or chat_registry."""
    # Admin users are allowed members but get a different onboarding path.
    # Return True so they're not skipped — the onboarding engine detects admin
    # and routes them to admin-specific steps.
    try:
        from bot.rbac import get_engine
        from bot.rbac.models import Subject
        engine = get_engine()
        role_ids = engine._get_role_ids(Subject(user_id=f"{source}:{str(user_id)}"))
        if "bot_admin" in role_ids:
            return True  # admins get admin onboarding path
    except Exception:
        from config import ADMIN_USERS
        if (source, str(user_id)) in ADMIN_USERS:
            return True

    # Check chat_registry — user's DM registered means they're allowed
    try:
        import bot.chat_registry as _cr
        if _cr.is_registered(f"{source}:{user_id}"):
            return True
    except Exception as _exc:
        log.debug("Suppressed: %s", _exc)
    try:
        from config import ORG_CONFIG
        members = ORG_CONFIG.get("members", {})
        # Check all member lists (admins, team_members, parents, children, other)
        for key in ("admins", "team_members", "parents", "children", "other"):
            member_list = members.get(key, [])
            if isinstance(member_list, list):
                for m in member_list:
                    if source == "telegram" and str(m.get("telegram_id", "")) == str(user_id):
                        return True
                    if source == "teams" and str(m.get("teams_aad_id", "")) == str(user_id):
                        return True
    except Exception as _exc:
        log.debug("Suppressed: %s", _exc)
    return False


async def check_onboarding_resume(
    source: str,
    user_id: str,
    chat_id: str = "",
    message_text: str = "",
) -> bool:
    """Check if user has an active onboarding and handle wizard commands.

    Returns True if the message was consumed by the wizard (don't process further).
    Returns False if the message should proceed to normal processing.
    """
    from bot.onboarding.store import get_onboarding
    from bot.onboarding.engine import handle_wizard_input

    subject_id = f"{source}:{user_id}"
    state = await get_onboarding(subject_id)

    if not state or state.status == "complete":
        return False

    # Check for wizard commands
    text_lower = message_text.strip().lower()
    if text_lower in ("/onboarding", "/setup"):
        # Show current progress
        from bot.onboarding.engine import show_progress
        await show_progress(state, source=source, chat_id=chat_id)
        return True

    if text_lower in ("skip", "/skip") and state.current_step:
        from bot.onboarding.engine import skip_current_step
        await skip_current_step(state, source=source, chat_id=chat_id)
        return True

    # Delegate to wizard engine for step-specific input (yes/no for schedule_tasks, etc.)
    if state.current_step:
        consumed = await handle_wizard_input(state, message_text, source=source, chat_id=chat_id)
        if consumed:
            return True

    # Block SDK inference while claude_auth is pending — no OAuth = no SDK session.
    # UNLESS admin already assigned a shared OAuth profile to this chat.
    if state.current_step == "claude_auth" or (
        state.current_step == "welcome" and "claude_auth" not in state.completed_steps
        and "claude_auth" not in state.skipped_steps
    ):
        # Check if admin EXPLICITLY assigned a shared OAuth profile to this chat
        # (get_oauth_for_chat always returns "default" — can't use it)
        _has_shared_oauth = False
        try:
            from config_overrides import get_chat_oauth_overrides
            _ck = f"{source}:{chat_id}" if source and chat_id else ""
            if _ck and _ck in get_chat_oauth_overrides():
                _has_shared_oauth = True
        except Exception as _exc:
            log.debug("Suppressed: %s", _exc)
        if not _has_shared_oauth:
            from bot.onboarding.engine import _send
            await _send(source, chat_id,
                "\u2699\ufe0f Please complete <code>/claude-auth &lt;label&gt;</code> first "
                "(or <code>/skip</code> to use shared access).\n"
                "I can't process messages until Claude authentication is set up.")
            return True

    # Past auth steps — let messages through to SDK for normal processing
    return False
