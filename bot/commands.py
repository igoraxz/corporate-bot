# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Slash command handlers and regex patterns extracted from main.py.

All bot.*, config.*, integrations.* imports are LAZY (inside function bodies)
to avoid circular import issues.
"""

import asyncio
import html as _html
import json
import logging
import os
import re
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Request-scoped forum topic thread_id — set by dispatch.py triage_and_enqueue()
# before calling any lightweight command. Used as fallback by _send_to_platform_simple()
# when message_thread_id is not explicitly passed. Safe: ContextVar is per-asyncio-task,
# not process-global. asyncio.create_task() copies the value to the child task.
_request_thread_id: ContextVar[int | None] = ContextVar("_request_thread_id", default=None)


# ---------------------------------------------------------------------------
# RBAC admin check helper — replaces legacy is_admin_user() calls
# ---------------------------------------------------------------------------

async def _is_admin_rbac(source: str, user_id: str) -> bool:
    """Check if user has bot_admin RBAC role.

    Falls back to static ADMIN_USERS set when RBAC engine is unavailable
    (bootstrap, DB corruption). This ensures admin commands work even
    before the RBAC engine has loaded.
    """
    try:
        from bot.rbac import get_engine
        from bot.rbac.models import Subject
        engine = get_engine()
        subject = Subject(user_id=f"{source}:{user_id}")
        role_ids = engine._get_role_ids(subject)
        return "bot_admin" in role_ids
    except Exception:
        # Fallback to static set during bootstrap
        from config import is_admin_user
        return is_admin_user(source, user_id)


async def _is_employee_own_dm(source: str, chat_id: str, user_id: str) -> bool:
    """Check if user is an employee running a command in their own TG DM.

    Validates: Telegram source, positive chat_id (DM), chat_id matches user_id,
    and user has at least the employee RBAC role.
    """
    if source != "telegram" or not chat_id:
        return False
    try:
        cid = int(chat_id)
    except (ValueError, TypeError):
        return False
    if cid <= 0 or str(chat_id) != str(user_id):
        return False
    # Verify user has employee role (not just any random DM sender)
    try:
        from bot.rbac import get_engine
        from bot.rbac.models import Subject
        engine = get_engine()
        subject = Subject(user_id=f"{source}:{user_id}")
        role_ids = engine._get_role_ids(subject)
        return bool(role_ids)  # any role = authorized (employee, bot_admin, etc.)
    except Exception:
        return False


async def _check_command_access(source: str, chat_id: str, user_id: str,
                                command_id: str) -> bool:
    """Check if user is allowed to use a lightweight command via RBAC.

    Uses RBAC engine: check_access(subject, "use", "commands", command_id).
    Falls back to legacy scope checks when RBAC engine unavailable.
    """
    try:
        from bot.rbac import get_engine
        from bot.core.access import build_rbac_subject
        engine = get_engine()
        # Flat permission model (CB-79): DM = user subject, group = chat subject.
        subject = build_rbac_subject(
            source=source,
            user_id=f"{source}:{user_id}",
            chat_id=chat_id,
        )
        decision = await engine.check_access(
            subject=subject,
            privilege="use",
            object_type="commands",
            object_id=command_id,
            log_decision=False,
        )
        if not decision.allowed:
            return False
        # Enforce own_dm context: non-admin employees can only use own_dm commands in their DM
        from bot.command_registry import COMMANDS as _cmd_reg
        _cdef = _cmd_reg.get(command_id, {})
        if _cdef.get("scope") == "own_dm":
            _is_adm = "bot_admin" in engine._get_role_ids(subject)
            if not _is_adm and not await _is_employee_own_dm(source, chat_id, user_id):
                return False
        return True
    except Exception:
        # Fallback to legacy checks
        from bot.command_registry import COMMANDS
        cdef = COMMANDS.get(command_id, {})
        scope = cdef.get("scope", "admin")
        if scope == "anyone":
            return True
        if scope == "admin":
            return await _is_admin_rbac(source, user_id)
        if scope in ("own_dm", "registered"):
            return await _is_admin_rbac(source, user_id) or await _is_employee_own_dm(source, chat_id, user_id)
        return False


async def _has_rbac_capability(source: str, user_id: str,
                               object_type: str, object_id: str,
                               chat_id: str = "") -> bool:
    """Check if current context has a specific RBAC capability (for display branching).

    Flat permission model (CB-79): DM = user subject, group = chat subject.
    Unlike _check_command_access (gate), this is for deciding WHAT to show,
    not WHETHER to show. E.g. "can this chat/user manage all OAuth profiles?"
    """
    try:
        from bot.rbac import get_engine
        from bot.core.access import build_rbac_subject
        engine = get_engine()
        subject = build_rbac_subject(source, f"{source}:{user_id}", chat_id)
        decision = await engine.check_access(
            subject=subject, privilege="use",
            object_type=object_type, object_id=object_id,
            log_decision=False,
        )
        return decision.allowed
    except Exception:
        return await _is_admin_rbac(source, user_id)  # fallback


async def _deny_command(source: str, chat_id: str, command_id: str,
                        message_thread_id: int | None = None):
    """Send a standardized access denied message for a command."""
    from bot.command_registry import COMMANDS
    cdef = COMMANDS.get(command_id, {})
    scope = cdef.get("scope", "admin")
    if scope == "own_dm":
        msg = "\u26d4 Run this in your DM with the bot."
    elif scope == "admin":
        msg = "\u26d4 Admin only."
    else:
        msg = "\u26d4 Access denied."
    await _send_to_platform_simple(source, chat_id, msg, message_thread_id=message_thread_id)


async def _mark_onboarding_step_done(source: str, chat_id: str, user_id: str, step_id: str):
    """Auto-detect and mark an onboarding step as done when auth succeeds.

    Called from auth command handlers (/claude-auth, /google-auth, /m365-auth).
    Safe to call even if onboarding isn't active — silently no-ops.
    Handles admin step ID mapping (employee: claude_auth → admin: admin_claude_oauth).
    """
    try:
        from bot.onboarding.store import get_onboarding
        from bot.onboarding.engine import complete_step
        subject_id = f"{source}:{user_id}"
        state = await get_onboarding(subject_id)
        if not state or state.status == "complete":
            return
        # Admin onboarding uses different step IDs
        _admin_step_map = {
            "claude_auth": "admin_claude_oauth",
            "google_auth": "admin_google_oauth",
            "m365_auth": "admin_m365_oauth",
        }
        effective_step = _admin_step_map.get(step_id, step_id) if state.is_admin else step_id
        if effective_step not in state.completed_steps:
            await complete_step(state, effective_step, source=source, chat_id=chat_id)
            log.info("Onboarding step '%s' auto-completed for %s", effective_step, subject_id)
    except Exception as e:
        log.debug("Onboarding step mark failed (non-blocking): %s", e)


async def _auto_reset_session_after_auth(source: str, chat_id: str, auth_type: str):
    """Delegate to bot.auth.oauth_flow.auto_reset_session_after_auth."""
    from bot.auth.oauth_flow import auto_reset_session_after_auth
    await auto_reset_session_after_auth(source, chat_id, auth_type)


# ---------------------------------------------------------------------------
# Locale-configurable words — imported at module load time from config
# ---------------------------------------------------------------------------
from config import (
    LOCALE_CANCEL_WORDS, LOCALE_CANCEL_PREFIX,
    LOCALE_DIAGNOSE_WORDS, LOCALE_DROP_WORDS,
    LOCALE_CONTINUE_WORDS,
    TASK_STALE_TIMEOUT_S,
)


# ===================================================================
# DISPLAY HELPERS
# ===================================================================


def _format_model_short(model: str) -> str:
    """Format model string for concise display: 'opus-4-6 200K', 'sonnet-4-6 1M', etc."""
    ctx = "1M" if "[1m]" in model else "200K"
    base = model.replace("[1m]", "").split("/")[-1]
    # Extract variant name: claude-opus-4-6 → opus, claude-sonnet-4-6 → sonnet
    short = base.replace("claude-", "")
    return f"{short} {ctx}"


# ===================================================================
# REGEX PATTERNS — slash command matchers
# ===================================================================

# === SELF-DIAGNOSE ===
_diagnose_words = "diagnose|self-diagnose|check health|check bot|bot status|what'?s wrong"
if LOCALE_DIAGNOSE_WORDS:
    _diagnose_words += "|" + LOCALE_DIAGNOSE_WORDS
_DIAGNOSE_RE = re.compile(
    rf"^/(?:{_diagnose_words})[\s?!.]*$",
    re.IGNORECASE,
)
# /knowledge — show knowledge domains + access
_KNOWLEDGE_RE = re.compile(r"^/knowledge[\s?!.]*$", re.IGNORECASE)

# Lightweight status command — bypasses SDK, instant response
_STATUS_RE = re.compile(r"^/(?:status|бот|bot)[\s?!.]*$", re.IGNORECASE)

# Admin session reset command — bypasses SDK, force-resets session for any chat
# Format: "/reset" (current chat) or "/reset <session_key_or_chat_id>" (specific chat)
_RESET_RE = re.compile(r"^/reset(?:\s+session)?(?:\s+(.+))?$", re.IGNORECASE)

# Employee workstation TOTP auth — /auth XXXXXX (admin only)
_AUTH_RE = re.compile(r"^/auth\s*(\d{6})\s*$", re.IGNORECASE)

# Employee workstation commands — /mac-setup (own DM), /mac, /lock (admin only)
_MAC_SETUP_RE = re.compile(r"^/mac[-_]?setup(?:\s+(.+))?[\s?!.]*$", re.IGNORECASE)
_MAC_STATUS_RE = re.compile(r"^/mac(?:\s+status)?[\s?!.]*$", re.IGNORECASE)
# /mac-reauth [label] — admin-only, triggers remote `claude setup-token` flow on Mac.
# Optional <label> arg: after token is captured, auto-promote to ~/.relay/oauth_token.<label>
# for multi-account support. Label regex must match bot.desktop_relay._ACCOUNT_LABEL_RE
# and guard.sh's LABEL_RE (source of truth: bot.desktop_relay.validate_account_label).
_MAC_REAUTH_RE = re.compile(
    r"^/mac[-_]?reauth(?:\s+([a-z0-9_-]{1,32}))?[\s?!.]*$", re.IGNORECASE
)
# /mac-accounts — list OAuth account labels present on Mac (no token values)
_MAC_ACCOUNTS_RE = re.compile(r"^/mac[-_]?accounts[\s?!.]*$", re.IGNORECASE)
# /mac-set-account <label> — bind current relay chat to a labeled OAuth account
_MAC_SET_ACCOUNT_RE = re.compile(
    r"^/mac[-_]?set[-_]?account(?:\s+([a-z0-9_-]{1,32}))?[\s?!.]*$", re.IGNORECASE
)
# v7 setup-token: user pastes a bare OAuth code (not a URL). Anthropic PKCE
# codes are alphanumeric with underscores / hyphens and often include a single
# `#` delimiter (e.g. `sessionKey#uuid`). Require min length to avoid matching
# short/accidental tokens. Gated by _pending_mac_reauth so it only triggers
# during active reauth flow.
_MAC_OAUTH_CODE_RE = re.compile(r"^\s*([A-Za-z0-9_#\-]{20,512})\s*$")

# In-flight Mac setup-token sessions awaiting code paste-back.
# Keyed by (source, chat_id, user_id) → {"started": ts}
_pending_mac_reauth: dict[tuple[str, str, str], dict] = {}
_PENDING_MAC_REAUTH_TTL_S = 300  # 5min — matches Mac helper timeout

# In-flight M365 device code flows (one per chat max).
# Keyed by (source, chat_id) → {"task": asyncio.Task, "started": ts}
_pending_m365_auth: dict[tuple[str, str], dict] = {}
_M365_AUTH_POLL_INTERVAL_S = 5
_M365_AUTH_TIMEOUT_S = 900  # 15 min

_MAC_LOCK_RE = re.compile(r"^/(?:mac\s+)?lock[\s?!.]*$", re.IGNORECASE)

# /chats — unified view of all chats with modes, sessions, models, OAuth
_CHATS_RE = re.compile(r"^/chats[\s?!.]*$", re.IGNORECASE)
# /usage [hours] — multi-timeframe usage report (no arg = all timeframes)
_USAGE_RE = re.compile(r"^/usage(?:\s+(\d+))?[\s?!.]*$", re.IGNORECASE)
# /claude-accounts — list OAuth profiles
_CLAUDE_ACCOUNTS_RE = re.compile(r"^/claude[-_]?accounts[\s?!.]*$", re.IGNORECASE)
_ACCOUNTS_RE = re.compile(r"^/accounts[\s?!.]*$", re.IGNORECASE)

# Claude relay — admin commands (accept both /relays and legacy /daemons)
_DAEMONS_RE = re.compile(r"^/(?:relays|daemons)[\s?!.]*$", re.IGNORECASE)
_DAEMON_REAP_RE = re.compile(r"^/(?:relay|daemon)[-_]reap[\s?!.]*$", re.IGNORECASE)
# /relay-reset [N] — restart relay daemon. N from /relays, or no arg = current relay chat
_DAEMON_KILL_RE = re.compile(r"^/(?:relay[-_]?(?:reset|respawn|kill)|daemon[-_]kill)(?:\s+(-?\d+))?[\s?!.]*$", re.IGNORECASE)
# /create-relay N — create relay for Mac session #N from /mac-sessions
_CREATE_RELAY_RE = re.compile(r"^/create[-_]?relay\s+(\d+)[\s?!.]*$", re.IGNORECASE)
# /delete-relay N — delete relay #N from /relays
_DELETE_RELAY_RE = re.compile(r"^/delete[-_]?relay\s+(\d+)[\s?!.]*$", re.IGNORECASE)
# /delete-chat N [confirm] — remove external chat #N from /chats
_DELETE_CHAT_RE = re.compile(r"^/delete[-_]?chat\s+(\d+)(?:\s+(confirm))?[\s?!.]*$", re.IGNORECASE)
# /claude-auth [label] — in-container OAuth token acquisition
_CLAUDE_AUTH_RE = re.compile(r"^/claude[-_]?auth(?:\s+(\S+))?[\s?!.]*$", re.IGNORECASE)
# /m365-auth — in-container M365 device code reauth
_M365_AUTH_RE = re.compile(r"^/m365[-_]?auth(?:\s+(cancel))?[\s?!.]*$", re.IGNORECASE)
# /google-auth — in-container Google OAuth code-paste flow
_GOOGLE_AUTH_RE = re.compile(r"^/google[-_]?auth(?:\s+(cancel))?[\s?!.]*$", re.IGNORECASE)
# /github-auth — GitHub device code flow
_GITHUB_AUTH_RE = re.compile(r"^/github[-_]?auth(?:\s+(cancel))?[\s?!.]*$", re.IGNORECASE)
# /revoke <type> [label] — revoke credentials (own DM or admin)
# Examples: /revoke claude main, /revoke google user@company.com, /revoke m365
_REVOKE_RE = re.compile(
    r"^/revoke(?:\s+(claude|google|m365|github)(?:\s+(\S+))?)?[\s?!.]*$",
    re.IGNORECASE,
)
# /permissions [user_id|chat_id] — admin-only, show full permission profile
_PERMISSIONS_RE = re.compile(r"^/permissions?(?:\s+(\S+))?[\s?!.]*$", re.IGNORECASE)
# /rollback [confirm] — admin-only, revert to last healthy deploy
_ROLLBACK_RE = re.compile(r"^/rollback(?:\s+(confirm))?[\s?!.]*$", re.IGNORECASE)
# /harnesses — list all harness definitions
_HARNESSES_RE = re.compile(r"^/harnesses?(?:\s+(\S+))?[\s?!.]*$", re.IGNORECASE)
# /set-harness <label> — assign harness to current chat (arg optional for menu tap)
_SET_HARNESS_RE = re.compile(r"^/set[-_]?harness(?:\s+(\S+))?[\s?!.]*$", re.IGNORECASE)
# /claude-set-oauth <label> [all] — switch OAuth for this chat or all known chats (no inference)
# Examples:
#   /claude-set-oauth personal          — this chat only
#   /claude-set-oauth fiducia all       — every chat in oauth_overrides
#   /claude-set-oauth default           — revert this chat to default
# Also accepts legacy /switch-oauth for backward compat
_SWITCH_OAUTH_RE = re.compile(
    r"^/(?:claude[-_]?set[-_]?oauth|switch[-_]?oauth)(?:\s+(\S+)(?:\s+(\S+))?)?\s*[\s?!.]*$",
    re.IGNORECASE,
)

# Help / commands menu.
# Accepts: "/help", "/menu", "/commands", "/cmd", "/?" (always, with
#   optional @botname suffix and leading/trailing whitespace)
# OR: "help?", "menu!", "help." — slashless word + terminal punctuation
# Intentionally REJECTS bare "help" / "menu" / "?" so single-word natural
# language messages still flow to the SDK. (AP-12: structurally decisive —
# a slash or punctuation is required; ambient speech doesn't match.)
_HELP_RE = re.compile(
    r"^\s*(?:/(?:help|commands|menu|cmd|\?)(?:@\w+)?|(?:help|commands|menu|cmd)[?!.]+)[\s?!.]*$",
    re.IGNORECASE,
)

# /onboarding — show onboarding progress (employees) or dashboard (admins)
_ONBOARDING_RE = re.compile(r"^/(?:onboarding|setup)(?:\s+(.*))?$", re.IGNORECASE)

# /session picker — relay-chat-only. Subcommands: (none)/info, list, pin <N|uuid>, all
_SESSION_RE = re.compile(r"^/session(?:\s+(.*))?$", re.IGNORECASE)
# Model override validation — mirrors guard.sh regex for defense-in-depth
_MODEL_OVERRIDE_RE = re.compile(r"^[a-zA-Z0-9._\[\]-]{1,64}$")
# /macsessions or /sessions — admin DM cross-project JSONL browse.
# /sessions is also accepted in relay chats as muscle-memory alias for `/session list`.
_MAC_SESSIONS_RE = re.compile(r"^/(?:mac[-_]?sessions|macsessions|sessions)\b", re.IGNORECASE)


# ===================================================================
# CANCEL / CONTINUE / DROP patterns (used by triage_and_enqueue)
# ===================================================================

_cancel_words = "cancel|stop|abort"
if LOCALE_CANCEL_WORDS:
    _cancel_words += "|" + LOCALE_CANCEL_WORDS
_CANCEL_RE = re.compile(
    rf"/(?:{_cancel_words})",
    re.IGNORECASE,
)

_CANCEL_STRICT_RE = re.compile(
    rf"^/(?:{_cancel_words})[\s!.]*$",
    re.IGNORECASE,
)

_drop_words = r"drop|hang\s*up|end\s*call|drop\s*call"
if LOCALE_DROP_WORDS:
    _drop_words += "|" + LOCALE_DROP_WORDS
_DROP_CALL_RE = re.compile(
    rf"^/(?:{_drop_words})[\s!.]*$",
    re.IGNORECASE,
)

# "Continue" — no-op when tasks are active (task is already running)
_continue_words = r"continue|go on|keep going"
if LOCALE_CONTINUE_WORDS:
    _continue_words += "|" + LOCALE_CONTINUE_WORDS
_CONTINUE_RE = re.compile(
    rf"^/(?:{_continue_words})[\s!.]*$",
    re.IGNORECASE,
)

# English cancel prefixes + locale-configurable extras
_cancel_prefixes = ["cancel this", "stop this", "cancel the task", "stop the task"]
if LOCALE_CANCEL_PREFIX:
    _cancel_prefixes.extend(LOCALE_CANCEL_PREFIX.split("|"))
_cancel_prefixes_tuple = tuple(_cancel_prefixes)


# ===================================================================
# HELPER FUNCTIONS
# ===================================================================

def _pty_drain_read(fd: int) -> bytes:
    """Non-blocking PTY drain read with select timeout.

    Uses select(0.5s) so the executor thread returns quickly when stopped,
    preventing orphaned threads from stealing token data after drain stops.
    """
    import select as _sel
    try:
        r, _, _ = _sel.select([fd], [], [], 0.5)
        if r:
            return os.read(fd, 4096)
    except (OSError, ValueError):
        return b""
    return b""


async def _pty_drain_loop(master_fd: int, stop_event: asyncio.Event) -> None:
    """Background drain: read and discard PTY output to prevent buffer fill.

    Without this, Ink's cursor/status writes fill the ~4KB Linux PTY buffer
    while waiting for user to complete OAuth. Once full, Ink blocks on write
    and can't read stdin → code paste-back never processed.
    Matches Mac helper Phase 2 drain (claude_login_helper.py lines 261-268).

    IMPORTANT: Uses select(0.5s) not raw os.read — ensures executor thread
    returns within 0.5s so await-on-stop actually waits for thread completion.
    Without this, orphaned os.read thread steals token data from the PTY.
    """
    loop = asyncio.get_event_loop()
    while not stop_event.is_set():
        try:
            data = await loop.run_in_executor(None, _pty_drain_read, master_fd)
            if not data:
                # select timeout or fd closed — check stop event
                continue
        except Exception:
            break


def _is_daemon_auth_error(raw: str) -> bool:
    """Return True if the daemon error text signals a TOTP auth failure.

    Single source of truth — used by both _friendly_daemon_error and the
    relay_ux preflight / cache-invalidation paths.
    """
    r = raw.lower()
    # New path: pool detects guard.sh AUTH_REQUIRED before daemon starts →
    # raises "auth_expired:AUTH_REQUIRED:session_expired:..." directly.
    if r.startswith("auth_expired:"):
        return True
    # Legacy path: daemon started but emitted non-JSON auth rejection.
    return "non_json_output" in r and (
        "blocked" in r or "auth" in r or "expired" in r or "no_session" in r
    )


def _friendly_daemon_error(raw: str) -> str:
    """Map a raw daemon_error string to a user-friendly TG message.

    Preserves the raw code at the end for diagnostics.
    """
    # Empty / unknown: passthrough.
    if not raw:
        return "\u26a0\ufe0f Daemon error: (empty)"
    r = raw.lower()
    # Common Mac-offline signatures from SSH spawn failure.
    if ("no route to host" in r or "connection refused" in r
            or "operation timed out" in r or "host is down" in r
            or raw.startswith("no_output_rc255")):
        return (
            "\U0001f534 Mac unreachable \u2014 check Tailscale on both ends. "
            f"<code>{_html.escape(raw[:200])}</code>"
        )
    # Overall read timeout.
    if raw.startswith("timeout_") or raw.startswith("read_timeout_"):
        return (
            "\u23f1 Mac daemon hung \u2014 claude --print didn't respond in time. "
            "Try again, or restart Claude.app on the Mac. "
            f"<code>{_html.escape(raw[:200])}</code>"
        )
    # Hybrid query timeouts (wall-clock or idle).
    if raw.startswith("query_timeout"):
        if "idle" in raw:
            return (
                "\u23f1 Mac Claude went silent for 45 min \u2014 task may have "
                "stalled. Use <code>/relay-reset</code> to respawn and resync."
            )
        # Extract timeout from error string if present (e.g. "query_timeout_3h")
        return (
            f"\u23f1 Query exceeded daemon time limit ({raw.split('_', 2)[-1] if '_' in raw else '3h'}). "
            "Use <code>/relay-reset</code> to respawn and resync missed turns."
        )
    # guard.sh rejection (pre-daemon).
    if _is_daemon_auth_error(raw):
        return (
            "\U0001f6ab <b>Mac TOTP session expired</b> \u2014 relay needs a "
            "fresh 6-digit code.\n\n"
            "Send: <code>/auth 123456</code> (your current TOTP) in any admin "
            "chat. Then retry the message.\n\n"
            f"<code>{_html.escape(raw[:200])}</code>"
        )
    # SSH binary missing on the bot container.
    if raw == "SSH_NOT_FOUND":
        return "\u274c SSH binary missing on bot container \u2014 broken install."
    # Feature flag off.
    if raw == "DESKTOP_RELAY_DISABLED":
        return "\U0001f515 Employee workstation control is disabled (DESKTOP_ENABLED=false)."
    # Bad input (shouldn't happen in normal flow).
    if raw.startswith("invalid_uuid_format") or raw.startswith("invalid_cwd") \
            or raw.startswith("query_too_large"):
        return (
            f"\u274c Invalid daemon input: <code>{_html.escape(raw[:200])}</code>"
        )
    # Stream-level error (partial output + crash).
    if raw.startswith("stream_error") or raw.startswith("spawn_failed"):
        return (
            "\u26a0\ufe0f Daemon subprocess error \u2014 likely stale auth or Mac hiccup. "
            f"<code>{_html.escape(raw[:200])}</code>"
        )
    # Fallback: show the raw code.
    return f"\u26a0\ufe0f Daemon error: <code>{_html.escape(raw[:500])}</code>"


async def _send_to_platform_simple(source: str, chat_id: str, msg: str,
                                   parse_mode: str | None = None,
                                   message_thread_id: int | None = None):
    """Send a message on a platform by source/chat_id.

    message_thread_id: forum topic thread ID for TG forum supergroups.
    Falls back to _request_thread_id ContextVar (set by dispatch.py) when not
    explicitly passed — ensures ALL lightweight command sends route to the
    correct forum topic without requiring message_thread_id on every call site.
    """
    # ContextVar fallback: if caller didn't pass explicit thread_id, use the
    # request-scoped value set by triage_and_enqueue in dispatch.py.
    if message_thread_id is None:
        message_thread_id = _request_thread_id.get(None)
    # In staging, buffer the response for the QA test framework BEFORE attempting
    # real network I/O (which will fail — no TG/WA connection in staging).
    try:
        from config import IS_STAGING
        if IS_STAGING:
            from bot.pipeline.staging import staging_buffer_response
            _tool_map = {"teams": "teams_send_message"}
            tool_name = _tool_map.get(source, "telegram_send_message")
            staging_buffer_response(str(chat_id), tool_name, {"text": msg, "chat_id": chat_id})
            return
    except Exception as _exc:
        log.debug("Suppressed: %s", _exc)
    try:
        if source == "teams":
            from integrations.teams import get_client
            client = get_client()
            if client:
                await client.send_message(chat_id, msg, text_format="plain")
            else:
                log.warning("Teams: _send_to_platform_simple called but client not initialized")
        else:
            from integrations.telegram import send_message
            kwargs: dict = {"chat_id": int(chat_id)}
            if parse_mode:
                kwargs["parse_mode"] = parse_mode
            if message_thread_id is not None:
                kwargs["message_thread_id"] = message_thread_id
            await send_message(msg, **kwargs)
    except Exception as e:
        log.debug(f"Cleanup failed: {e}")


async def _send_to_platform_from_task(task: Any, msg: str):
    """Send a message on the task's platform, replying to the user's original message."""
    try:
        if task.source == "teams":
            from integrations.teams import get_client
            client = get_client()
            if client:
                await client.send_message(task.chat_id, msg, text_format="plain")
        else:
            from integrations.telegram import send_message
            _kwargs: dict = {"chat_id": int(task.chat_id)}
            if task.tg_user_message_id:
                _kwargs["reply_to_message_id"] = task.tg_user_message_id
            if getattr(task, "tg_message_thread_id", None) is not None:
                _kwargs["message_thread_id"] = task.tg_message_thread_id
            await send_message(msg, **_kwargs)
        # Mark chat activity so parallel tasks detect checkpoint interleaving
        from bot.hooks import mark_chat_activity
        mark_chat_activity(task.chat_id, task.task_id)
    except Exception as e:
        log.debug(f"Checkpoint send failed: {e}")


def _is_cancel_message(text: str) -> bool:
    """Detect cancel intent — strict match or 'cancel this task and ...' patterns."""
    if _CANCEL_STRICT_RE.match(text):
        return True
    text_lower = text.lower().strip()
    if text_lower.startswith(_cancel_prefixes_tuple):
        return True
    return False


# ===================================================================
# CHECKPOINT FUNCTIONS
# ===================================================================

def _get_task_timeout(task: Any) -> int:
    """Get query_timeout_s from the task's chat harness (default: TASK_STALE_TIMEOUT_S).

    Uses build_chat_key_from_task() to include thread_id for forum topics,
    so the topic's harness is resolved — not the parent group's harness.
    """
    from bot.chat_key import build_chat_key_from_task
    chat_key = build_chat_key_from_task(task)
    return _get_harness_timeout_by_key(chat_key)


def _get_harness_timeout_by_key(chat_key: str) -> int:
    """Look up query_timeout_s for a chat_key. Returns 0 for 'no timeout'."""
    try:
        from bot.harness import get_harness_for_chat, resolve_field
        h = get_harness_for_chat(chat_key)
        val = resolve_field(h, "query_timeout_s", chat_key)
        if val is not None:
            return max(0, int(val))
    except Exception as e:
        log.debug("Harness timeout lookup failed for %s: %s", chat_key, e)
    return TASK_STALE_TIMEOUT_S


async def _task_checkpoint_loop():
    """Check active tasks every 30s; send one-shot checkpoints based on harness query_timeout_s."""
    # Lazy import to avoid circular dependency
    from main import task_manager
    while True:
        await asyncio.sleep(30)
        for task in task_manager.get_active_tasks():
            if task.phase in ("done", "failed"):
                continue
            elapsed = task.elapsed_seconds()
            timeout = _get_task_timeout(task)
            if timeout <= 0:
                continue  # 0 = no checkpoints (e.g. coding harness)
            # First alert at 50% of query_timeout_s (sent exactly once)
            if elapsed >= timeout // 2 and not task.checkpoint_15m_sent:
                task.checkpoint_15m_sent = True
                msg = (
                    f"\u23f3 This has been running for {task.elapsed()} \u2014 "
                    f"tools used: {', '.join(task.tools_used[-5:]) or 'none yet'}.\n"
                    f"Send \"cancel\" to stop, or \"diagnose\" to check what's wrong."
                )
                await _send_to_platform_from_task(task, msg)
            # Second alert at 75% of query_timeout_s (sent exactly once)
            elif elapsed >= timeout * 3 // 4 and not task.checkpoint_20m_sent:
                task.checkpoint_20m_sent = True
                msg = (
                    f"\u26a0\ufe0f Task has been running for {task.elapsed()}. "
                    f"This may indicate a problem. Send \"cancel\" to stop, "
                    f"or \"diagnose\" to run self-diagnostics."
                )
                await _send_to_platform_from_task(task, msg)


async def _suggest_diagnose_on_failure(task: Any, error: str):
    """After a task failure, suggest running self-diagnose."""
    msg = (
        "\U0001f4a1 Task failed. Send \"diagnose\" to run self-diagnostics, "
        "or describe what you'd like me to fix."
    )
    await _send_to_platform_from_task(task, msg)


# ===================================================================
# LIGHTWEIGHT COMMAND HANDLERS
# ===================================================================

async def _lightweight_auth(source: str, chat_id: str, user_id: str, totp_code: str,
                           *, message_thread_id: int | None = None):
    """TOTP auth to Mac desktop — no LLM, instant."""
    if not await _check_command_access(source, chat_id, user_id, "mac_auth"):
        await _deny_command(source, chat_id, "mac_auth", message_thread_id=message_thread_id)
        return
    from bot import desktop_relay
    if not desktop_relay.is_enabled():
        await _send_to_platform_simple(source, chat_id, "\u274c Employee workstation control disabled.", message_thread_id=message_thread_id)
        return
    # Check Mac online status first — give clear feedback if offline
    mac_online = await desktop_relay.is_mac_online(max_age_s=10.0)
    if not mac_online:
        await _send_to_platform_simple(source, chat_id,
            "\u274c Auth failed \u2014 Mac is <b>offline</b>. Start Tailscale on Mac first.",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return
    result = await desktop_relay.authenticate(totp_code)
    if result["authenticated"]:
        ttl = result.get("ttl_seconds", 7200)
        mins = ttl // 60
        await _send_to_platform_simple(source, chat_id,
            f"\U0001f513 <b>Authenticated</b> \u2014 session valid for {mins} min.",
            parse_mode="HTML", message_thread_id=message_thread_id)
    else:
        err = result.get("error", "unknown")
        await _send_to_platform_simple(source, chat_id,
            f"\u274c Auth failed: <code>{err}</code>\n<i>Mac is online but guard.sh rejected the code.</i>",
            parse_mode="HTML", message_thread_id=message_thread_id)


async def _lightweight_mac_setup(source: str, chat_id: str, user_id: str,
                                 args: str = "",
                                 *, message_thread_id: int | None = None) -> None:
    """CB-140: Per-employee workstation setup — register personal Mac for relay control.

    Usage: /mac-setup <hostname> <username>
    Example: /mac-setup alice-macbook alice
    """
    if not await _check_command_access(source, chat_id, user_id, "mac_setup"):
        await _deny_command(source, chat_id, "mac_setup", message_thread_id=message_thread_id)
        return

    from bot.relay.workstation_config import (
        get_workstation, set_workstation, WorkstationConfig,
    )

    user_key = f"{source}:{user_id}"

    # Show current config if no args
    if not args or not args.strip():
        ws = get_workstation(user_key)
        if ws:
            import time as _time_check
            _auth_status = "authenticated" if ws.authenticated_until > _time_check.time() else "not authenticated"
            await _send_to_platform_simple(source, chat_id,
                f"<b>\U0001f4bb Your Workstation</b>\n\n"
                f"Host: <code>{_html.escape(ws.mac_host)}</code>\n"
                f"User: <code>{_html.escape(ws.mac_user)}</code>\n"
                f"Status: {_auth_status}\n"
                f"Enabled: {'yes' if ws.enabled else 'no'}\n\n"
                f"<i>To update: <code>/mac-setup &lt;hostname&gt; &lt;username&gt;</code></i>\n"
                f"<i>To remove: <code>/mac-setup remove</code></i>",
                parse_mode="HTML", message_thread_id=message_thread_id)
        else:
            await _send_to_platform_simple(source, chat_id,
                "<b>\U0001f4bb Workstation Setup</b>\n\n"
                "No workstation configured. To set up:\n"
                "<code>/mac-setup &lt;hostname&gt; &lt;username&gt;</code>\n\n"
                "Example: <code>/mac-setup my-macbook alice</code>\n\n"
                "The hostname is your Mac's network name or Tailscale hostname.\n"
                "The username is your Mac OS login.",
                parse_mode="HTML", message_thread_id=message_thread_id)
        return

    parts = args.strip().split()

    # Handle remove
    if parts[0].lower() in ("remove", "delete", "clear"):
        from bot.relay.workstation_config import delete_workstation
        if delete_workstation(user_key):
            await _send_to_platform_simple(source, chat_id,
                "\u2705 Workstation config removed.",
                message_thread_id=message_thread_id)
        else:
            await _send_to_platform_simple(source, chat_id,
                "No workstation config to remove.",
                message_thread_id=message_thread_id)
        return

    if len(parts) < 2:
        await _send_to_platform_simple(source, chat_id,
            "\u274c Usage: <code>/mac-setup &lt;hostname&gt; &lt;username&gt;</code>",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return

    hostname, username = parts[0], parts[1]

    # Validate hostname and username (CB-140 security: prevent SSH argument injection)
    from bot.relay.workstation_config import _SAFE_HOSTNAME, _SAFE_USERNAME
    if not _SAFE_HOSTNAME.fullmatch(hostname):
        await _send_to_platform_simple(source, chat_id,
            f"\u274c Invalid hostname: <code>{_html.escape(hostname[:60])}</code>",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return
    if not _SAFE_USERNAME.fullmatch(username):
        await _send_to_platform_simple(source, chat_id,
            f"\u274c Invalid username: <code>{_html.escape(username[:40])}</code>\n"
            "Must start with letter/underscore, max 32 chars, alphanumeric/._-",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return

    import time as _time_setup
    config = WorkstationConfig(
        mac_host=hostname,
        mac_user=username,
        enabled=True,
        session_timeout=7200,
        created_at=_time_setup.strftime("%Y-%m-%dT%H:%M:%SZ", _time_setup.gmtime()),
        display_name=user_id,
    )
    set_workstation(user_key, config)

    await _send_to_platform_simple(source, chat_id,
        f"<b>\u2705 Workstation configured</b>\n\n"
        f"Host: <code>{_html.escape(hostname)}</code>\n"
        f"User: <code>{_html.escape(username)}</code>\n\n"
        f"Next: authenticate with <code>/auth &lt;TOTP-code&gt;</code> when you want to start a relay session.",
        parse_mode="HTML", message_thread_id=message_thread_id)


async def _lightweight_mac_status(source: str, chat_id: str, user_id: str,
                                  *, message_thread_id: int | None = None):
    """Employee workstation status — no LLM, instant."""
    if not await _check_command_access(source, chat_id, user_id, "mac_status"):
        await _deny_command(source, chat_id, "mac_status", message_thread_id=message_thread_id)
        return
    from bot import desktop_relay
    if not desktop_relay.is_enabled():
        await _send_to_platform_simple(source, chat_id, "\u274c Employee workstation control disabled (DESKTOP_ENABLED=false).", message_thread_id=message_thread_id)
        return
    result = await desktop_relay.status()
    online = "\U0001f7e2 ONLINE" if result["online"] else "\U0001f534 OFFLINE"
    latency = f"{result['latency_ms']}ms" if result.get("latency_ms") else "-"
    auth = "\U0001f513 Authenticated" if result.get("authenticated") else "\U0001f512 Not authenticated"
    remaining = ""
    if result.get("session_remaining", 0) > 0:
        remaining = f" ({result['session_remaining'] // 60}m remaining)"
    lines = [
        f"\U0001f5a5 Mac: {online} ({latency})",
        f"\U0001f510 Auth: {auth}{remaining}",
    ]
    if result.get("error") and not result["online"]:
        lines.append(f"\u26a0\ufe0f {result['error']}")
    await _send_to_platform_simple(source, chat_id, "\n".join(lines), parse_mode="HTML", message_thread_id=message_thread_id)


async def _lightweight_mac_reauth(
    source: str, chat_id: str, user_id: str, label: str | None = None,
    *, message_thread_id: int | None = None,
):
    """Remote Mac Claude CLI re-auth via `claude setup-token` paste-back (v7).

    Step 1 of 2: triggers the Mac helper, captures OAuth URL (no port —
    setup-token uses Anthropic's hosted callback). Stores pending state for
    the follow-up CODE paste-back (handled inline in triage_and_enqueue).

    If `label` is provided, after TOKEN_SAVED the default slot
    (~/.relay/oauth_token) is auto-promoted to ~/.relay/oauth_token.<label>
    for multi-account support. Label validated on bot-side AND guard.sh-side.
    """
    if not await _is_admin_rbac(source, user_id):
        await _send_to_platform_simple(source, chat_id, "\u26d4 Admin only.", message_thread_id=message_thread_id)
        return
    from bot import desktop_relay
    if not desktop_relay.is_enabled():
        await _send_to_platform_simple(source, chat_id, "\u274c Employee workstation control disabled.", message_thread_id=message_thread_id)
        return

    # Validate label early (bot-side) if supplied — guard.sh will re-validate.
    if label is not None and not desktop_relay.validate_account_label(label):
        await _send_to_platform_simple(
            source, chat_id,
            f"\u274c Invalid label <code>{_html.escape(label)}</code> \u2014 "
            f"must match <code>[a-z0-9_-]{{1,32}}</code>.",
            parse_mode="HTML", message_thread_id=message_thread_id,
        )
        return

    label_suffix = f" (\u2192 oauth_token.<b>{_html.escape(label)}</b>)" if label else ""
    await _send_to_platform_simple(
        source, chat_id,
        "\U0001f510 Starting remote Mac <code>claude setup-token</code>" + label_suffix
        + "... (can take 20-60s while helper spawns)",
        parse_mode="HTML", message_thread_id=message_thread_id,
    )

    ok, url_or_err = await desktop_relay.start_mac_oauth_flow()
    if not ok:
        err = url_or_err
        if err == "TOTP_EXPIRED":
            await _send_to_platform_simple(
                source, chat_id,
                "\U0001f6ab <b>TOTP session expired</b> \u2014 run "
                "<code>/auth 123456</code> first, then retry <code>/mac-reauth</code>.",
                parse_mode="HTML", message_thread_id=message_thread_id,
            )
        else:
            await _send_to_platform_simple(
                source, chat_id,
                f"\u274c Failed to start Mac setup-token: <code>{_html.escape(err[:200])}</code>",
                parse_mode="HTML", message_thread_id=message_thread_id,
            )
        return

    # Record pending state so code paste-back can find the session.
    # Store label so _handle_mac_reauth_code can auto-promote after TOKEN_SAVED.
    key = (source, chat_id, user_id)
    _pending_mac_reauth[key] = {"started": time.time(), "label": label}

    # Post URL + instructions. v7 PKCE flow: user signs in, Anthropic's
    # callback page shows a one-time code, user pastes that code here.
    safe_url = _html.escape(url_or_err)
    promote_note = (
        f"\n\n\U0001f3f7\ufe0f After capture, token will be promoted to "
        f"<code>oauth_token.{_html.escape(label)}</code> slot automatically."
        if label else ""
    )
    msg = (
        "\U0001f517 <b>Step 2/2 \u2014 complete OAuth in your browser</b>\n\n"
        f"1. Open this URL on any device:\n<a href=\"{safe_url}\">{safe_url}</a>\n\n"
        "2. Sign in on the <b>Mac's Anthropic account</b> and click Allow.\n\n"
        "3. Anthropic will show a one-time <b>code</b> on the callback page.\n\n"
        "4. Copy that code and paste it here (just the code \u2014 no URL, no other text).\n\n"
        "\u23f1 I\u2019ll hold the Mac helper alive for 5min. After paste, Mac will store "
        "a 1-year token locally \u2014 no more re-auth until April 2027."
        + promote_note
    )
    await _send_to_platform_simple(source, chat_id, msg, parse_mode="HTML", message_thread_id=message_thread_id)


async def _handle_mac_reauth_code(
    source: str, chat_id: str, user_id: str, code: str,
):
    """Step 2 of /mac-reauth (v7): user pasted the OAuth code. Pipe it to
    the Mac helper, wait for token capture, then probe daemon auth.
    """
    key = (source, chat_id, user_id)
    pending = _pending_mac_reauth.get(key)
    if not pending:
        return False  # not in a reauth flow — let message fall through

    # TTL check
    if time.time() - pending["started"] > _PENDING_MAC_REAUTH_TTL_S:
        # Kill any lingering subprocess from /claude-auth
        # Stop drain + cleanup stale auth flow
        _drain_stop_s = pending.get("drain_stop")
        _drain_task_s = pending.get("drain_task")
        if _drain_stop_s:
            _drain_stop_s.set()
        if _drain_task_s:
            try:
                await asyncio.wait_for(_drain_task_s, timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                _drain_task_s.cancel()
        _stale_proc = pending.get("claude_auth_proc")
        if _stale_proc and _stale_proc.returncode is None:
            try:
                _stale_proc.kill()
            except Exception as _exc:
                log.debug("Suppressed: %s", _exc)
        _stale_fd = pending.get("pty_master_fd")
        if _stale_fd is not None:
            try:
                os.close(_stale_fd)
            except OSError:
                pass
        _pending_mac_reauth.pop(key, None)
        await _send_to_platform_simple(
            source, chat_id,
            "\u23f1 Auth flow expired. Re-run <code>/claude-auth</code>.",
            parse_mode="HTML",
        )
        return True

    # Clear pending BEFORE submit to avoid double-processing on re-paste
    _pending_mac_reauth.pop(key, None)

    # Stop background PTY drain — we need exclusive fd access for code write
    _drain_stop = pending.get("drain_stop")
    _drain_task = pending.get("drain_task")
    if _drain_stop:
        _drain_stop.set()
        log.info("/claude-auth: drain stop signalled")
    if _drain_task:
        try:
            await asyncio.wait_for(_drain_task, timeout=2.0)
            log.info("/claude-auth: drain task completed cleanly")
        except (asyncio.TimeoutError, Exception) as _de:
            _drain_task.cancel()
            log.warning("/claude-auth: drain task force-cancelled: %s", _de)

    # In-container /claude-auth flow: pipe code to local process
    local_proc = pending.get("claude_auth_proc")
    if local_proc:
        await _send_to_platform_simple(source, chat_id,
            "\U0001f500 Delivering code to setup-token...", parse_mode="HTML")
        try:
            # Write code via PTY master fd (or proc.stdin as fallback)
            # Dual detection: PTY stream for sk-ant-* AND credentials file change
            _master_fd = pending.get("pty_master_fd")
            output = ""
            extracted_token = None

            if _master_fd is not None:
                # Write code then \r to submit. Ink's TextInput treats \r as Enter.
                # IMPORTANT: \r\n breaks — the \n triggers "retry" in Ink and eats the response.
                # \r alone is correct (verified via pty_submit_test.py 2026-04-22).
                log.info("/claude-auth: writing code (len=%d) to PTY fd=%d, proc.rc=%s",
                         len(code), _master_fd, local_proc.returncode)
                os.write(_master_fd, code.encode("utf-8"))
                await asyncio.sleep(0.3)
                os.write(_master_fd, b"\r")
                log.info("/claude-auth: code written, starting 60s token read loop")
                # Read PTY output WHILE process runs — Ink UI may never exit
                _ansi_pat = re.compile(r'\x1b[\[\]()#;?]*[0-9;]*[a-zA-Z\x07]|\x1b\[[0-9;?]*[hlm]')
                raw_resp = b""
                pty_token_found = False
                try:
                    deadline = asyncio.get_event_loop().time() + 60.0
                    while asyncio.get_event_loop().time() < deadline:
                        remaining = max(0.1, deadline - asyncio.get_event_loop().time())
                        try:
                            chunk = await asyncio.wait_for(
                                asyncio.get_event_loop().run_in_executor(
                                    None, os.read, _master_fd, 4096),
                                timeout=min(remaining, 2.0))
                            raw_resp += chunk
                            text_so_far = _ansi_pat.sub("", raw_resp.decode("utf-8", errors="replace"))
                            if re.search(r'sk-ant-[a-zA-Z0-9_-]+', text_so_far):
                                # Token appeared in stream — grab trailing bytes
                                await asyncio.sleep(0.3)
                                try:
                                    extra = await asyncio.wait_for(
                                        asyncio.get_event_loop().run_in_executor(
                                            None, os.read, _master_fd, 4096),
                                        timeout=0.5)
                                    raw_resp += extra
                                except (asyncio.TimeoutError, OSError):
                                    pass
                                pty_token_found = True
                                break
                        except asyncio.TimeoutError:
                            # Check if process exited (no more output coming)
                            if local_proc.returncode is not None:
                                break
                            continue
                        except OSError:
                            break
                except Exception as _exc:
                    log.debug("Suppressed: %s", _exc)
                if local_proc.returncode is None:
                    try:
                        local_proc.kill()
                        await local_proc.wait()
                    except Exception as _exc:
                        log.debug("Suppressed: %s", _exc)
                try:
                    os.close(_master_fd)
                except OSError:
                    pass
                output = _ansi_pat.sub("", raw_resp.decode("utf-8", errors="replace"))

                # Extract token from PTY stream
                log.info("/claude-auth: PTY read done. token_found=%s, output_len=%d, proc.rc=%s",
                         pty_token_found, len(raw_resp), local_proc.returncode)
                if pty_token_found:
                    tok_match = re.search(r'(sk-ant-[a-zA-Z0-9_-]+)', output)
                    if tok_match:
                        extracted_token = tok_match.group(1)
                        log.info("/claude-auth: token extracted (len=%d)", len(extracted_token))
            elif local_proc.stdin:
                # Stdin fallback (non-PTY): \n for line-buffered subprocess
                local_proc.stdin.write(code.encode("utf-8"))
                await local_proc.stdin.drain()
                await asyncio.sleep(0.3)
                local_proc.stdin.write(b"\r")
                await local_proc.stdin.drain()
                local_proc.stdin.close()
                stdout, stderr = await asyncio.wait_for(local_proc.communicate(), timeout=60.0)
                output = stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace")
                tok_match = re.search(r'(sk-ant-[a-zA-Z0-9_-]+)', output)
                if tok_match:
                    extracted_token = tok_match.group(1)

            # --- Result handling ---
            if extracted_token:
                label = pending.get("label", "")
                result_msg = "\u2705 <b>Auth captured!</b>"
                if label:
                    from config import add_oauth_profile, get_oauth_profile_path
                    from bot.relay.core import DESKTOP_ENABLED
                    _can_manage_oauth = await _has_rbac_capability(source, user_id, "tools", "manage_oauth", chat_id=chat_id)
                    # Users without manage_oauth: namespace label to prevent overwrites
                    if not _can_manage_oauth:
                        # Check if label already exists as someone else's profile
                        _existing_path = get_oauth_profile_path(label)
                        if _existing_path.exists():
                            # Reject — use namespaced label instead
                            label = f"user-{user_id}-{label}"
                            result_msg += f"\n\u26a0\ufe0f Label taken — using <code>{_html.escape(label)}</code> instead"
                    add_oauth_profile(label, extracted_token)
                    result_msg += f"\n\U0001f511 Saved as profile <code>{_html.escape(label)}</code>"
                    # Auto-assign to user's DM (shared profiles stay unassigned)
                    if not _can_manage_oauth:
                        try:
                            from config import set_oauth_for_chat
                            _emp_chat_key = f"{source}:{chat_id}"
                            set_oauth_for_chat(_emp_chat_key, label)
                            result_msg += "\n\U0001f464 Auto-assigned to your DM session"
                            # Add to allowed_credentials so they can actually use it
                            from bot.harness import get_harness_for_chat, set_chat_override, resolve_field, get_chat_overrides, is_credential_unrestricted
                            _emp_h = get_harness_for_chat(_emp_chat_key)
                            # Read existing override (not harness default) to preserve prior credentials
                            _existing_ov = get_chat_overrides(_emp_chat_key).get("allowed_credentials")
                            _emp_ac = _existing_ov if _existing_ov is not None else resolve_field(_emp_h, "allowed_credentials", _emp_chat_key) or []
                            _cred_id = f"claude:{label}"
                            if is_credential_unrestricted(_emp_ac):
                                pass  # wildcard — no need to add individual creds
                            elif _cred_id not in _emp_ac:
                                _new_ac = list(_emp_ac) + [_cred_id]
                                set_chat_override(_emp_chat_key, "allowed_credentials", _new_ac)
                                result_msg += "\n\u2705 Credential access granted"
                        except Exception as _e:
                            log.warning("Failed to auto-assign OAuth to employee DM: %s", _e)
                    if DESKTOP_ENABLED:
                        from bot.desktop_relay import push_oauth_to_mac
                        mac_sync = await push_oauth_to_mac(label, extracted_token)
                        result_msg += f"\n\U0001f4bb Mac sync: {'OK' if mac_sync.get('success') else 'failed'}"
                else:
                    result_msg += "\n<i>Set as <code>CLAUDE_CODE_OAUTH_TOKEN</code> env var to use.</i>"
                await _send_to_platform_simple(source, chat_id, result_msg, parse_mode="HTML")
                await _mark_onboarding_step_done(source, chat_id, user_id, "claude_auth")
                await _auto_reset_session_after_auth(source, chat_id, "claude")
            else:
                # Token not found in PTY stream
                rc = local_proc.returncode
                _safe_output = re.sub(r'sk-ant-[a-zA-Z0-9_-]{10,}', '[REDACTED]', output[:500])
                if rc is not None and rc < 0:
                    sig = abs(rc)
                    err_msg = (f"\u274c setup-token was killed (signal {sig}). "
                               f"60s timeout expired — no sk-ant-* appeared in PTY output.\n"
                               f"<pre>{_html.escape(_safe_output)}</pre>")
                else:
                    err_msg = (f"\u26a0\ufe0f setup-token exited (rc={rc}) but no auth data found.\n"
                               f"<pre>{_html.escape(_safe_output)}</pre>")
                await _send_to_platform_simple(source, chat_id, err_msg, parse_mode="HTML")
        except asyncio.TimeoutError:
            await _send_to_platform_simple(source, chat_id,
                "\u23f1 setup-token timed out after code submission.")
        except Exception as e:
            await _send_to_platform_simple(source, chat_id,
                f"\u274c Error: <code>{_html.escape(str(e)[:200])}</code>",
                parse_mode="HTML")
        return True

    await _send_to_platform_simple(
        source, chat_id,
        "\U0001f500 Delivering code to Mac helper...",
        parse_mode="HTML",
    )

    from bot import desktop_relay
    ok, msg = await desktop_relay.submit_mac_oauth_code(code)
    if not ok:
        await _send_to_platform_simple(
            source, chat_id,
            f"\u274c Code submission failed: <code>{_html.escape(msg[:200])}</code>\n\n"
            "Re-run <code>/mac-reauth</code>.",
            parse_mode="HTML",
        )
        return True

    # Wait for setup-token to finish capturing the long-lived token
    await _send_to_platform_simple(
        source, chat_id,
        "\U0001f9ed Code accepted. Waiting for setup-token to finish and save the "
        "long-lived token on Mac...",
        parse_mode="HTML",
    )
    ok2, msg2 = await desktop_relay.poll_mac_oauth_status()
    if not ok2:
        await _send_to_platform_simple(
            source, chat_id,
            f"\u274c Setup-token did not complete: <code>{_html.escape(msg2[:200])}</code>\n\n"
            "Re-run <code>/mac-reauth</code>.",
            parse_mode="HTML",
        )
        return True

    # If a label was attached to this reauth flow, auto-promote the freshly
    # written default token into the labeled slot. On success, the default
    # slot is emptied and the label slot is filled — ready to be bound to
    # specific relay chats via /mac-set-account.
    label = pending.get("label")
    if label:
        await _send_to_platform_simple(
            source, chat_id,
            f"\U0001f3f7\ufe0f Promoting token \u2192 "
            f"<code>oauth_token.{_html.escape(label)}</code>...",
            parse_mode="HTML",
        )
        promo = await desktop_relay.promote_oauth_token(label)
        if not promo.get("success"):
            err = promo.get("error", "unknown")
            await _send_to_platform_simple(
                source, chat_id,
                "\u2705 Token captured, but promote failed: "
                f"<code>{_html.escape(str(err)[:200])}</code>\n\n"
                "Token is still at <code>~/.relay/oauth_token</code> "
                "(default slot). You can retry with "
                f"<code>desktop_relay action=promote_account_token "
                f"account_label={_html.escape(label)}</code>.",
                parse_mode="HTML",
            )
            return True
        await _send_to_platform_simple(
            source, chat_id,
            "\u2705 <b>Mac Claude re-auth + label promote complete.</b>\n"
            f"Token now at <code>~/.relay/oauth_token.{_html.escape(label)}</code>.\n\n"
            "Next step: bind a relay chat to this account with "
            f"<code>/mac-set-account {_html.escape(label)}</code> "
            "(run inside the target relay chat), or use the MCP tool "
            "<code>desktop_relay action=set_chat_account "
            f"chat_id=&lt;id&gt; account_label={_html.escape(label)}</code>.",
            parse_mode="HTML",
        )
        return True

    # No label — default single-account flow (backward compat)
    await _send_to_platform_simple(
        source, chat_id,
        "\u2705 <b>Mac Claude re-auth complete.</b>\n"
        "Token stored on Mac (<code>~/.relay/oauth_token</code>).\n\n"
        "Next persistent-daemon query in any relay chat will pick up the new "
        "token automatically. Send a test message (e.g. \"say ok\") in a relay "
        "chat to confirm — or <code>/relay-kill</code> &lt;chat_id&gt; first to "
        "force a fresh daemon spawn.",
        parse_mode="HTML",
    )
    return True


async def _lightweight_mac_accounts(source: str, chat_id: str, user_id: str,
                                    *, message_thread_id: int | None = None):
    """List OAuth account labels present on Mac. Admin only, no LLM.

    Also annotates which label (if any) the CURRENT chat is bound to, read
    from the relay session registry. No token values are ever returned.
    """
    if not await _is_admin_rbac(source, user_id):
        await _send_to_platform_simple(source, chat_id, "\u26d4 Admin only.", message_thread_id=message_thread_id)
        return
    from bot import desktop_relay
    if not desktop_relay.is_enabled():
        await _send_to_platform_simple(source, chat_id, "\u274c Employee workstation control disabled.", message_thread_id=message_thread_id)
        return

    result = await desktop_relay.list_mac_accounts()
    if not result.get("success"):
        await _send_to_platform_simple(
            source, chat_id,
            f"\u274c Failed to list accounts: "
            f"<code>{_html.escape(str(result.get('error', 'unknown'))[:200])}</code>",
            parse_mode="HTML", message_thread_id=message_thread_id,
        )
        return

    # Read current chat's binding (if any) for annotation
    bound_label: str | None = None
    try:
        entry = await desktop_relay.get_linked_session(str(chat_id))
        if entry:
            bound_label = entry.get("account_label")
    except Exception as e:  # defensive — never block listing on registry read
        log.warning("_lightweight_mac_accounts: registry read failed: %s", e)

    labels = result.get("labels", [])
    lines = ["<b>\U0001f3f7\ufe0f Mac OAuth account labels</b>", ""]
    if not labels:
        lines.append("<i>No labeled slots yet.</i> Run "
                     "<code>/mac-reauth</code> &lt;label&gt; to create one.")
    else:
        for lbl in labels:
            marker = " \u2190 <b>this chat</b>" if lbl == bound_label else ""
            lines.append(f"  \u2022 <code>{_html.escape(lbl)}</code>{marker}")
    if bound_label and bound_label not in labels:
        lines.append("")
        lines.append(
            f"\u26a0\ufe0f This chat is bound to <code>{_html.escape(bound_label)}</code> "
            "but that label is missing on Mac \u2014 reauth it with "
            f"<code>/mac-reauth {_html.escape(bound_label)}</code>."
        )
    lines.append("")
    lines.append(
        "<i>Bind a chat with <code>/mac-set-account</code> &lt;label&gt; "
        "(run inside the relay chat).</i>"
    )
    await _send_to_platform_simple(
        source, chat_id, "\n".join(lines), parse_mode="HTML",
        message_thread_id=message_thread_id,
    )


async def _lightweight_mac_set_account(
    source: str, chat_id: str, user_id: str, label: str | None,
    *, message_thread_id: int | None = None,
):
    """Bind the current relay chat to a named OAuth account.

    Must be invoked from WITHIN a relay chat (the one you want to bind).
    Fails gracefully if label is missing, invalid, or the chat isn't
    registered as a relay session.
    """
    if not await _is_admin_rbac(source, user_id):
        await _send_to_platform_simple(source, chat_id, "\u26d4 Admin only.", message_thread_id=message_thread_id)
        return
    from bot import desktop_relay
    if not desktop_relay.is_enabled():
        await _send_to_platform_simple(source, chat_id, "\u274c Employee workstation control disabled.", message_thread_id=message_thread_id)
        return

    if not label:
        await _send_to_platform_simple(
            source, chat_id,
            "Usage: /mac-set-account &lt;label&gt;\n\n"
            "List available labels with /mac-accounts.",
            parse_mode="HTML", message_thread_id=message_thread_id,
        )
        return

    if not desktop_relay.validate_account_label(label):
        await _send_to_platform_simple(
            source, chat_id,
            f"\u274c Invalid label <code>{_html.escape(label)}</code> \u2014 "
            f"must match <code>[a-z0-9_-]{{1,32}}</code>.",
            parse_mode="HTML", message_thread_id=message_thread_id,
        )
        return

    result = await desktop_relay.set_chat_account(str(chat_id), label)
    if not result.get("success"):
        err = result.get("error", "unknown")
        if err == "no_linked_session":
            await _send_to_platform_simple(
                source, chat_id,
                "\u274c This chat isn\u2019t linked to a Mac relay session. "
                "Use <code>desktop_relay action=create_relay_group</code> "
                "(or <code>link</code>) first, then retry.",
                parse_mode="HTML", message_thread_id=message_thread_id,
            )
        else:
            await _send_to_platform_simple(
                source, chat_id,
                f"\u274c Set-account failed: <code>{_html.escape(str(err)[:200])}</code>",
                parse_mode="HTML", message_thread_id=message_thread_id,
            )
        return

    # Cycle the daemon: kill Mac-side + release bot-side pool, then IMMEDIATELY
    # eager-respawn. Relay-chat daemons must stay alive to serve the realtime
    # Desktop→TG tail — waiting for the next user message creates a gap where
    # Desktop-typed turns go un-forwarded.
    killed_note = ""
    try:
        kill_res = await desktop_relay.kill_daemon(str(chat_id))
        if kill_res.get("ok") and kill_res.get("killed_pid"):
            killed_note = " (active daemon killed)"
    except Exception as e:
        log.warning("_lightweight_mac_set_account: kill_daemon failed: %s", e)

    try:
        from bot.relay.pool import get_pool
        await get_pool().release(str(chat_id))
    except Exception as e:
        log.warning("_lightweight_mac_set_account: pool release failed: %s", e)

    # Eager respawn — fire-and-forget, bounded by internal timeouts.
    # Mac-offline case falls through harmlessly (task logs warning, user
    # sees normal offline UX on their next message).
    respawn_note = ""
    try:
        respawn = await desktop_relay.eager_respawn_daemon(str(chat_id))
        if respawn.get("ok"):
            respawn_note = " + respawning now"
        elif respawn.get("reason") == "missing_uuid_or_cwd":
            respawn_note = (
                " (registry missing uuid/cwd \u2014 daemon will lazy-spawn "
                "on next message)"
            )
    except Exception as e:
        log.warning("_lightweight_mac_set_account: eager_respawn failed: %s", e)

    await _send_to_platform_simple(
        source, chat_id,
        f"\u2705 Chat bound to account <code>{_html.escape(label)}</code>"
        f"{killed_note}{respawn_note}.\n\n"
        f"Fresh daemon uses <code>oauth_token.{_html.escape(label)}</code>. "
        f"Realtime Desktop\u2192TG tail resumes within ~5-15s.",
        parse_mode="HTML", message_thread_id=message_thread_id,
    )


async def _lightweight_mac_lock(source: str, chat_id: str, user_id: str,
                                *, message_thread_id: int | None = None):
    """Lock Mac desktop session — no LLM, instant."""
    if not await _check_command_access(source, chat_id, user_id, "mac_lock"):
        await _deny_command(source, chat_id, "mac_lock", message_thread_id=message_thread_id)
        return
    from bot import desktop_relay
    if not desktop_relay.is_enabled():
        await _send_to_platform_simple(source, chat_id, "\u274c Employee workstation control disabled.", message_thread_id=message_thread_id)
        return
    result = await desktop_relay.lock()
    if result["locked"]:
        await _send_to_platform_simple(source, chat_id, "\U0001f512 Mac session locked.", message_thread_id=message_thread_id)
    else:
        await _send_to_platform_simple(source, chat_id,
            f"\u26a0\ufe0f Lock result: {result.get('message', 'unknown')}", message_thread_id=message_thread_id)


def _daemon_settings_tag(md: dict) -> str:
    """Build [effort=val] tag from daemon state dict for /chats and /relays."""
    effort = md.get("effort")
    if effort:
        return f" <code>[effort={_html.escape(effort)}]</code>"
    return ""


_GENERIC_TITLE_RE = re.compile(r"^(DM|Chat)\s+[-\d]+$")


async def _resolve_tg_title(chat_id_int: int) -> str | None:
    """Resolve actual TG chat title via Pyrogram API. Returns None on failure."""
    try:
        from integrations.telegram import get_client
        client = await get_client()
        chat = await client.get_chat(chat_id_int)
        raw = chat.title or chat.first_name or None
        return raw[:60].replace("\n", " ").replace("\r", "") if raw else None
    except Exception as e:
        log.debug(f"Could not resolve TG title for {chat_id_int}: {e}")
        return None


async def _refresh_external_titles() -> None:
    """Batch-resolve stale generic titles ('DM {id}', 'Chat {id}') in chat_registry.

    Results are cached in the registry.
    """
    import bot.chat_registry as _cr
    all_entries = _cr.all_entries()

    # Find entries with generic titles (telegram chats only)
    stale: list[tuple[str, dict]] = []
    for key, info in all_entries.items():
        if not key.startswith("telegram:"):
            continue
        parts = key.split(":")
        if len(parts) < 2:
            continue
        title = info.get("title", "")
        if _GENERIC_TITLE_RE.match(title):
            try:
                _cid = int(parts[1])
                stale.append((key, info))
            except ValueError:
                pass
    if stale:
        results = await asyncio.gather(
            *[_resolve_tg_title(int(k.split(":")[1])) for k, _ in stale],
            return_exceptions=True,
        )
        for (key, info), resolved in zip(stale, results):
            if isinstance(resolved, str) and resolved:
                entry = dict(info)
                entry["title"] = resolved
                _cr.register(key, **entry)


async def _get_ec_display_list() -> tuple[list[tuple[int, dict]], set[str]]:
    """Build filtered list of registered chats for /chats display and /delete-chat numbering.

    Filters out relay chats (shown in section 4).
    Returns (filtered_list, relay_cids_set).
    """
    import bot.chat_registry as _cr

    # Relay chat IDs
    _relay_cids: set[str] = set()
    try:
        from bot import desktop_relay
        if desktop_relay.is_enabled():
            _relay_reg = await desktop_relay.get_all_links()
            _relay_cids = set(_relay_reg.keys()) if _relay_reg else set()
    except Exception as _exc:
        log.debug("Suppressed: %s", _exc)
    all_entries = _cr.all_entries()
    dc_list: list[tuple[int, dict]] = []
    for key, info in all_entries.items():
        if not key.startswith("telegram:"):
            continue
        parts = key.split(":")
        if len(parts) != 2:
            continue
        try:
            cid = int(parts[1])
        except ValueError:
            continue
        if str(cid) in _relay_cids:
            continue
        dc_list.append((cid, info))
    return dc_list, _relay_cids


async def _lightweight_chats(source: str, chat_id: str, user_id: str,
                             *, message_thread_id: int | None = None):
    """Unified view of all chats: mode, model, OAuth, relay status."""
    from config import (
        get_model_for_chat, get_oauth_for_chat,
        get_chat_model_overrides, get_chat_oauth_overrides,
        MODEL_PRIMARY,
        get_default_oauth_label,
    )
    if not await _check_command_access(source, chat_id, user_id, "chats"):
        await _deny_command(source, chat_id, "chats", message_thread_id=message_thread_id)
        return

    model_ov = get_chat_model_overrides()
    oauth_ov = get_chat_oauth_overrides()
    from bot.harness import get_chat_harness_label, get_chat_overrides, get_harness

    def _harness_tags(tg_key: str) -> str:
        """Build harness + override tags for a chat."""
        lbl = get_chat_harness_label(tg_key)
        ov = get_chat_overrides(tg_key)
        tag = f" \u2699\ufe0f<code>{_html.escape(lbl)}</code>" if lbl else ""
        if ov:
            ov_str = ", ".join(f"{k}={v}" for k, v in ov.items())
            preview = ov_str[:60] + ("…" if len(ov_str) > 60 else "")
            tag += f" \u270f\ufe0f<i>[{_html.escape(preview)}]</i>"
        return tag

    def _sandbox_tag(tg_key: str) -> str:
        """Show sandbox status for a chat. Read-only — no side effects."""
        from bot.harness import resolve_field, _projects_base
        lbl = get_chat_harness_label(tg_key)
        h = get_harness(lbl)
        if not h:
            return ""
        sandbox_flag = resolve_field(h, "sandbox", tg_key)
        if not sandbox_flag:
            return ""
        project_dir = resolve_field(h, "project_dir", tg_key)
        if not project_dir and h.label:
            # Read-only check: does the auto-derived dir exist?
            auto_dir = _projects_base() / h.label
            if auto_dir.is_dir():
                project_dir = str(auto_dir)
        if project_dir:
            short = project_dir.replace("/app/data/harness-projects/", "")
            return f" \U0001f9ea<code>{_html.escape(short)}</code>"
        return ""

    def _model_oauth_tags(chat_key: str) -> tuple[str, str]:
        """Build model and OAuth display tags for a chat key."""
        model = get_model_for_chat(chat_key)
        oauth = get_oauth_for_chat(chat_key)
        m_tag = f" <i>({_format_model_short(model)})</i>" if model != MODEL_PRIMARY else ""
        o_tag = f" \U0001f511<code>{_html.escape(oauth)}</code>" if oauth else ""
        return m_tag, o_tag

    lines = ["<b>\U0001f4ac Registered Chats</b>", ""]

    # Unified registry view — scoped by chat visibility.
    # Admin DMs: see all chats. Everything else: only this chat's entry.
    from bot.chat_registry import all_entries
    _full_registry = all_entries()
    _is_admin_dm = await _has_rbac_capability(source, user_id, "commands", "chats", chat_id=chat_id)
    try:
        _is_private = int(chat_id) > 0
    except (ValueError, TypeError):
        _is_private = False
    _global_scope = _is_admin_dm and _is_private
    if _global_scope:
        registry = _full_registry
    else:
        # Local scope: only show this chat + its topics
        _own_key = f"{source}:{chat_id}"
        _own_topic_key = f"{source}:{chat_id}:" if message_thread_id else None
        registry = {
            k: v for k, v in _full_registry.items()
            if k == _own_key or k.startswith(f"{_own_key}:")
        }
    _mode_emoji = {
        "active_plus": "\u2b50",    # ⭐
        "active": "\U0001f4ac",     # 💬
        "silent": "\U0001f507",     # 🔇
    }

    # Separate topics from top-level chats for nested display
    top_level: list[tuple[str, dict]] = []
    topics: dict[str, list[tuple[str, dict]]] = {}  # parent_key → [(key, entry)]
    for key, entry in sorted(registry.items()):
        parts = key.split(":")
        if len(parts) == 3:
            parent = entry.get("parent") or f"{parts[0]}:{parts[1]}"
            topics.setdefault(parent, []).append((key, entry))
        else:
            top_level.append((key, entry))

    # Also include legacy display sources (relay chats handled separately below)
    dc_idx = 1
    for key, entry in top_level:
        mode = entry.get("mode", "active_plus")
        title = entry.get("title") or key
        emoji = _mode_emoji.get(mode, "\u2753")
        model_tag, oauth_tag = _model_oauth_tags(key)
        harness_tag = _harness_tags(key)
        sb_tag = _sandbox_tag(key)
        lines.append(f" {dc_idx}. {emoji} <b>{_html.escape(str(title)[:30])}</b> [{mode}]{model_tag}{oauth_tag}{harness_tag}{sb_tag}")
        # Show nested topics under this chat
        child_topics = topics.get(key, [])
        for tkey, tentry in child_topics:
            t_mode = tentry.get("mode", mode)  # inherit parent mode if not set
            t_title = tentry.get("title") or tkey.split(":")[-1]
            t_emoji = _mode_emoji.get(t_mode, "\u2753")
            t_model, t_oauth = _model_oauth_tags(tkey)
            t_harness = _harness_tags(tkey)
            lines.append(f"    \u2514\u2500 {t_emoji} <b>{_html.escape(str(t_title)[:25])}</b> [{t_mode}]{t_model}{t_oauth}{t_harness}")
        dc_idx += 1

    if dc_idx == 1:
        lines.append("<i>No registered chats.</i>")

    # CB-130: Show tracked (unregistered) topic names for forum groups
    if _global_scope:
        try:
            from bot.chat_registry import list_topic_names
            _unregistered_topics = []
            for key, _ in top_level:
                parts = key.split(":")
                if len(parts) == 2:
                    _src, _cid = parts
                    _known = list_topic_names(_src, _cid)
                    _registered_tids = {tkey.split(":")[-1] for tkey, _ in topics.get(key, [])}
                    for tid, name in sorted(_known.items()):
                        if tid not in _registered_tids:
                            _unregistered_topics.append((key, tid, name))
            if _unregistered_topics:
                lines.append("")
                lines.append("<b>Unregistered topics</b> (tracked, not active):")
                for _pkey, _tid, _tname in _unregistered_topics:
                    lines.append(f"  \U0001f4ad {_html.escape(_tname)} (thread {_tid} in {_html.escape(_pkey[-15:])})")
        except Exception as _exc:
            log.debug("Suppressed: %s", _exc)
    lines.append("")

    # 4. Relay chats (Mac-side config — no bot harness, show daemon model instead)
    try:
        from bot import desktop_relay
        if desktop_relay.is_enabled():
            registry = await desktop_relay.get_all_links()
            # Fetch daemon info for Mac-side model
            mac_daemons: dict[str, dict] = {}
            try:
                from bot.relay.sessions import list_daemons
                dm_result = await list_daemons()
                for dm in dm_result.get("daemons", []):
                    mac_daemons[dm["chat_id"]] = dm
            except Exception as _exc:
                log.debug("Suppressed: %s", _exc)
            if registry:
                lines.append(f"<b>\U0001f5a5 Relay Chats</b> ({len(registry)})")
                for cid, entry in sorted(registry.items()):
                    sname = entry.get("session_name", "")
                    if sname.startswith("relay_"):
                        sname = sname[6:]
                    name = sname or cid
                    acct = entry.get("account_label", get_default_oauth_label())
                    # Mac-side model from daemon state (not bot harness)
                    md = mac_daemons.get(str(cid), {})
                    mac_model = md.get("model")
                    if not mac_model:
                        # Fallback: show registry model override if daemon hasn't reported yet
                        mac_model = entry.get("model_override")
                    if mac_model:
                        model_tag = f" <i>({_html.escape(_format_model_short(mac_model))})</i>"
                    else:
                        model_tag = ""
                    settings_tag = _daemon_settings_tag(md)
                    oauth_tag = f" \U0001f511<code>{_html.escape(acct)}</code>" if acct else ""
                    proj = entry.get("project_path", "")
                    proj_short = proj.rsplit("/", 1)[-1] if proj else ""
                    lines.append(f"  \U0001f5a5 <b>{_html.escape(name)}</b> \U0001f4c1<code>{_html.escape(proj_short)}</code>{model_tag}{settings_tag}{oauth_tag}")
            else:
                lines.append("<i>No relay chats.</i>")
    except Exception as e:
        lines.append(f"<i>Relay error: {_html.escape(str(e)[:80])}</i>")

    # 5. Summary
    lines.extend([
        "",
        f"<i>Default model: {_format_model_short(MODEL_PRIMARY)}</i>",
        f"<i>Model overrides: {len(model_ov)} | OAuth overrides: {len(oauth_ov)} | Use <code>/harnesses</code> for profiles</i>",
    ])

    await _send_to_platform_simple(source, chat_id, "\n".join(lines), parse_mode="HTML", message_thread_id=message_thread_id)


async def _lightweight_usage(source: str, chat_id: str, user_id: str, hours: int = 0,
                             *, message_thread_id: int | None = None):
    """Multi-timeframe per-OAuth usage report — lightweight, no SDK.

    When hours=0 (default from /usage with no args), shows all timeframes
    (1h, 5h, 24h, 7d) in a single table. When hours>0, shows single timeframe.
    """
    if not await _check_command_access(source, chat_id, user_id, "usage"):
        await _deny_command(source, chat_id, "usage", message_thread_id=message_thread_id)
        return

    from bot.storage.memory import get_usage_by_profile

    # Chat isolation: only admin in private DM gets global usage.
    # Admin in group/topic sees only their own chat's usage.
    _is_admin_usage = await _has_rbac_capability(source, user_id, "commands", "usage", chat_id=chat_id)
    try:
        _is_private = int(chat_id) > 0
    except (ValueError, TypeError):
        _is_private = False  # fail-closed: non-integer chat_id = not private DM
    _global_scope = _is_admin_usage and _is_private
    _sk_prefix = "" if _global_scope else f"{source}:{chat_id}"

    if hours > 0:
        # Single-timeframe mode (backward compat: /usage 48)
        by_profile = await get_usage_by_profile(hours, session_key_prefix=_sk_prefix)
        _scope_label = "" if _global_scope else " (this chat)"
        lines = [f"<b>\U0001f4b0 Usage ({hours}h){_scope_label}</b>", ""]
        if by_profile:
            for p in by_profile:
                lines.append(
                    f"\U0001f511 <b>{_html.escape(p['profile'])}</b>: "
                    f"${p['cost_usd']:.2f} \u2022 {p['queries']}q"
                )
            total = sum(p["cost_usd"] for p in by_profile)
            lines.append(f"\n<b>Total:</b> ${total:.2f}")
        else:
            lines.append("<i>No usage data.</i>")
        await _send_to_platform_simple(source, chat_id, "\n".join(lines), parse_mode="HTML", message_thread_id=message_thread_id)
        return

    # Multi-timeframe table: 1h, 5h, 24h, 7d
    timeframes = [("1h", 1), ("5h", 5), ("24h", 24), ("7d", 168)]
    # Fetch all timeframes in parallel-ish (sequential but fast — SQLite)
    tf_data: dict[str, list[dict]] = {}
    for label, h in timeframes:
        tf_data[label] = await get_usage_by_profile(h, session_key_prefix=_sk_prefix)

    # Collect all profile names across all timeframes
    all_profiles: set[str] = set()
    for profiles in tf_data.values():
        for p in profiles:
            all_profiles.add(p["profile"])

    _scope_label = "" if _global_scope else " (this chat)"
    if not all_profiles:
        await _send_to_platform_simple(source, chat_id,
            f"<b>\U0001f4b0 Usage{_scope_label}</b>\n\n<i>No usage data.</i>", parse_mode="HTML", message_thread_id=message_thread_id)
        return

    # Build cost lookup: profile → {timeframe_label → cost}
    costs: dict[str, dict[str, float]] = {}
    for prof in sorted(all_profiles):
        costs[prof] = {}
        for label, _ in timeframes:
            match = [p for p in tf_data[label] if p["profile"] == prof]
            costs[prof][label] = match[0]["cost_usd"] if match else 0.0

    # Format table
    tf_labels = [t[0] for t in timeframes]
    header = f"{'':12s}" + "".join(f"{t:>8s}" for t in tf_labels)
    lines = [
        f"<b>\U0001f4b0 Usage{_scope_label}</b>",
        "",
        f"<pre>{header}",
    ]
    for prof in sorted(all_profiles):
        row = f"{prof[:12]:12s}" + "".join(
            f"${costs[prof][t]:>6.1f}" if costs[prof][t] > 0 else f"{'—':>7s}"
            for t in tf_labels
        )
        lines.append(row)

    # Total row
    totals = {}
    for t in tf_labels:
        totals[t] = sum(costs[prof][t] for prof in all_profiles)
    total_row = f"{'TOTAL':12s}" + "".join(f"${totals[t]:>6.1f}" for t in tf_labels)
    lines.append(f"{'─' * len(header)}")
    lines.append(f"<b>{total_row}</b>")
    lines.append("</pre>")

    await _send_to_platform_simple(source, chat_id, "\n".join(lines), parse_mode="HTML", message_thread_id=message_thread_id)


async def _lightweight_claude_accounts(source: str, chat_id: str, user_id: str,
                                       *, message_thread_id: int | None = None):
    """List OAuth profiles — lightweight, no SDK. Admin sees all, employee sees own."""
    from config import list_oauth_profiles, get_chat_oauth_overrides
    if not await _check_command_access(source, chat_id, user_id, "claude_accounts"):
        await _deny_command(source, chat_id, "claude_accounts", message_thread_id=message_thread_id)
        return
    # Full view (token prefixes, overrides) if user can manage OAuth; filtered view otherwise
    _full_view = await _has_rbac_capability(source, user_id, "tools", "manage_oauth", chat_id=chat_id)

    profiles = list_oauth_profiles()
    overrides = get_chat_oauth_overrides()

    # Filtered view: show current + allowed profiles (no token prefixes, no overrides)
    if not _full_view:
        _my_key = f"{source}:{chat_id}"
        # Check if this chat has an EXPLICIT OAuth assignment (not just the default fallback)
        from config_overrides import get_chat_oauth_overrides as _get_oov
        _explicit_overrides = _get_oov()
        _my_label = _explicit_overrides.get(_my_key, "")  # empty if no explicit assignment

        # Filter profiles by allowed_credentials (harness + per-chat override)
        # Default to [] (deny-all) — fail-closed on errors
        from bot.harness import is_credential_unrestricted as _is_unr_ca
        _allowed_creds: list[str] = []
        try:
            from bot.harness import get_harness_for_chat as _get_h_ca, resolve_field as _rf_ca
            _h_ca = _get_h_ca(_my_key)
            _allowed_creds = _rf_ca(_h_ca, "allowed_credentials", _my_key) or []
        except Exception as _exc:
            log.debug("Suppressed: %s", _exc)

        filtered_profiles = []
        for p in profiles:
            label = p["label"]
            if _is_unr_ca(_allowed_creds):
                filtered_profiles.append(p)
            elif f"claude:{label}" in _allowed_creds or label in _allowed_creds:
                filtered_profiles.append(p)
            elif label == _my_label:
                # Show explicitly assigned profile (not the default fallback)
                filtered_profiles.append(p)

        _display_label = _my_label or "<i>not configured</i>"
        lines = [
            "<b>\U0001f511 Your Claude Account</b>",
            "",
            f"Current profile: <b>{_display_label if _my_label else _display_label}</b>",
            "",
            "Available profiles:",
        ]
        if not filtered_profiles:
            lines.append(" <i>No profiles available. Run /claude-auth &lt;label&gt; to set up.</i>")
        for p in filtered_profiles:
            label = p["label"]
            exists = p.get("exists", False)
            is_active = label == _my_label
            icon = "\u25c9" if is_active else "\u25cb"
            status = "\u2705" if exists else "\u274c"
            lines.append(f" {icon} {status} {_html.escape(label)}")
        lines.extend([
            "",
            "<i>Switch: /claude-set-oauth &lt;label&gt;</i>",
            "<i>Auth new: /claude-auth &lt;label&gt;</i>",
        ])
        await _send_to_platform_simple(source, chat_id, "\n".join(lines), parse_mode="HTML", message_thread_id=message_thread_id)
        return

    # Full view: token prefixes, per-chat overrides, usage counts
    lines = ["<b>\U0001f511 Claude Accounts</b>", ""]
    for i, p in enumerate(profiles, 1):
        label = p["label"]
        exists = p.get("exists", False)
        is_def = p.get("is_default", False)
        status = "\u2705" if exists else "\u274c"
        prefix = p.get("token_prefix", "")
        default_tag = " \u2b50 <i>default</i>" if is_def else ""
        token_tag = f" <code>{_html.escape(prefix)}</code>" if prefix else ""
        # Count how many chats use this profile (as explicit override)
        usage_count = sum(1 for v in overrides.values() if v == label)
        usage_tag = f" \u2022 {usage_count} chat(s)" if usage_count > 0 else ""
        lines.append(f" {i}. {status} <b>{_html.escape(label)}</b>{default_tag}{token_tag}{usage_tag}")

    lines.extend([
        "",
        "<i>Add: \"manage oauth add &lt;label&gt; &lt;token&gt;\"</i>",
        "<i>Set default: \"manage oauth set-default &lt;label&gt;\"</i>",
        "<i>Auth: /claude-auth [label]</i>",
    ])

    await _send_to_platform_simple(source, chat_id, "\n".join(lines), parse_mode="HTML", message_thread_id=message_thread_id)


async def _lightweight_accounts(source: str, chat_id: str, user_id: str,
                                *, message_thread_id: int | None = None):
    """Unified credential status — all providers, filtered by allowed_credentials."""
    if not await _check_command_access(source, chat_id, user_id, "accounts"):
        await _deny_command(source, chat_id, "accounts", message_thread_id=message_thread_id)
        return

    _is_admin_view = await _has_rbac_capability(source, user_id, "tools", "manage_oauth", chat_id=chat_id)
    _my_key = f"{source}:{chat_id}"

    from bot.harness import is_credential_unrestricted
    # Get allowed_credentials for this chat
    _allowed_creds: list[str] = []
    if not _is_admin_view:
        try:
            from bot.harness import get_harness_for_chat, resolve_field
            _h = get_harness_for_chat(_my_key)
            _allowed_creds = resolve_field(_h, "allowed_credentials", _my_key) or []
        except Exception as _h_err:
            log.warning("accounts: harness lookup failed for %s: %s", _my_key, _h_err)

    lines = ["<b>\U0001f511 Your Connected Accounts</b>", ""]

    # --- Claude ---
    from config import list_oauth_profiles
    from config_overrides import get_chat_oauth_overrides as _get_oov_acc
    _oauth_ov = _get_oov_acc()
    _active_claude = _oauth_ov.get(_my_key, "")
    _claude_profiles = list_oauth_profiles()
    _my_claude = []
    for p in _claude_profiles:
        label = p["label"]
        if _is_admin_view or is_credential_unrestricted(_allowed_creds):
            _my_claude.append(p)
        elif f"claude:{label}" in _allowed_creds or label in _allowed_creds:
            _my_claude.append(p)
    if _my_claude:
        lines.append("<b>Claude AI</b>")
        for p in _my_claude:
            _lbl = p["label"]
            _icon = "\u2705" if p.get("exists") else "\u274c"
            _active = " \u25c0 active" if _lbl == _active_claude else ""
            lines.append(f"  {_icon} {_html.escape(_lbl)}{_active}")
    else:
        lines.append("<b>Claude AI</b> — <i>not configured</i>")
        lines.append("  Run <code>/claude-auth &lt;label&gt;</code> to set up")

    # --- Google ---
    try:
        from bot.auth.credentials import list_credentials
        _google_creds = await list_credentials(owner=f"{source}:{user_id}", cred_type="google-workspace")
        if not _google_creds and _is_admin_view:
            _google_creds = await list_credentials(cred_type="google-workspace")
    except Exception:
        _google_creds = []

    # Shared filter: check if credential is in allowed_credentials (normalize both sides)
    from bot.auth.credentials import normalize_cred_id as _norm_cid

    def _filter_creds(creds: list[dict]) -> list[dict]:
        if _is_admin_view or is_credential_unrestricted(_allowed_creds):
            return creds
        result = []
        for c in creds:
            cid = c.get("id", "")
            if cid in _allowed_creds or c.get("account", "") in _allowed_creds:
                result.append(c)
            elif any(_norm_cid(ac) == _norm_cid(cid) for ac in _allowed_creds):
                result.append(c)
        return result

    _filtered_google = _filter_creds(_google_creds)
    lines.append("")
    def _owner_tag(c: dict) -> str:
        """Show credential owner for admin view (CB-138)."""
        if not _is_admin_view:
            return ""
        owner = c.get("owner", "")
        if not owner:
            return " <i>(system)</i>"
        # Shorten: "telegram:123456789" → "123456789", "teams:aad-id" → "aad-id"
        _short = owner.split(":", 1)[-1] if ":" in owner else owner
        return f" <i>(owner: {_html.escape(_short[:20])})</i>"

    if _filtered_google:
        lines.append("<b>Google Workspace</b>")
        for c in _filtered_google:
            lines.append(f"  \u2705 {_html.escape(c.get('account', '?'))}{_owner_tag(c)}")
    else:
        lines.append("<b>Google Workspace</b> — <i>not configured</i>")
        lines.append("  Run <code>/google-auth</code> to connect")

    # --- M365 ---
    try:
        _m365_creds = await list_credentials(owner=f"{source}:{user_id}", cred_type="ms365")
        if not _m365_creds and _is_admin_view:
            _m365_creds = await list_credentials(cred_type="ms365")
    except Exception:
        _m365_creds = []
    _filtered_m365 = _filter_creds(_m365_creds)
    lines.append("")
    if _filtered_m365:
        lines.append("<b>Microsoft 365</b>")
        for c in _filtered_m365:
            lines.append(f"  \u2705 {_html.escape(c.get('account', '?'))}{_owner_tag(c)}")
    else:
        lines.append("<b>Microsoft 365</b> — <i>not configured</i>")
        lines.append("  Run <code>/m365-auth</code> to connect")

    # --- GitHub (per-user only, no shared creds — filtered by allowed_credentials) ---
    _gh_visible = False
    try:
        from bot.auth.oauth_flow import get_user_oauth_token_metadata
        _gh_meta = await get_user_oauth_token_metadata(f"{source}:{user_id}", "github")
        if _gh_meta and _gh_meta.get("username"):
            _gh_username = _gh_meta["username"]
            _gh_cred_id = f"github:{_gh_username}"
            if _is_admin_view or is_credential_unrestricted(_allowed_creds) or _gh_cred_id in _allowed_creds:
                _gh_visible = True
    except Exception:
        _gh_meta = None
    lines.append("")
    if _gh_visible and _gh_meta:
        lines.append("<b>GitHub</b>")
        _gh_owner = f" <i>(owner: {_html.escape(str(user_id)[:20])})</i>" if _is_admin_view else ""
        lines.append(f"  \u2705 {_html.escape(_gh_meta.get('username', '?'))}{_gh_owner}")
    else:
        lines.append("<b>GitHub</b> — <i>not configured</i>")
        lines.append("  Run <code>/github-auth</code> to connect")

    lines.extend(["", "<i>Use /revoke &lt;type&gt; to disconnect an account</i>"])

    await _send_to_platform_simple(source, chat_id, "\n".join(lines), parse_mode="HTML", message_thread_id=message_thread_id)


async def _lightweight_knowledge(source: str, chat_id: str, user_id: str,
                                 *, message_thread_id: int | None = None):
    """Show knowledge domains — employee sees accessible, admin sees all."""
    if not await _check_command_access(source, chat_id, user_id, "knowledge"):
        await _deny_command(source, chat_id, "knowledge", message_thread_id=message_thread_id)
        return

    _is_admin_kn = await _has_rbac_capability(source, user_id, "tools", "manage_domain", chat_id=chat_id)

    try:
        from bot.storage.domains import list_domains
        domains = await list_domains()
    except Exception as e:
        await _send_to_platform_simple(source, chat_id,
            f"\u274c Knowledge system unavailable: {_html.escape(str(e)[:100])}",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return

    if not domains:
        await _send_to_platform_simple(source, chat_id,
            "<b>\U0001f4da Knowledge Domains</b>\n\n<i>No domains registered yet.</i>\n"
            "Admin can create domains via <code>manage_domain</code> tool.",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return

    # Filter: admin sees all, employee sees only RBAC-accessible domains
    visible = []
    if _is_admin_kn:
        visible = domains
    else:
        try:
            from bot.rbac import get_engine
            from bot.core.access import build_rbac_subject
            engine = get_engine()
            _kn_subject = build_rbac_subject(source, f"{source}:{user_id}", chat_id)
            for d in domains:
                decision = await engine.check_access(
                    subject=_kn_subject,
                    privilege="read",
                    object_type="knowledge_domain",
                    object_id=d["domain_id"],
                )
                if decision.allowed:
                    visible.append(d)
        except Exception:
            # Fail-closed: RBAC unavailable → show nothing (not all active domains)
            visible = []

    # CB-116: Get chat's domain config for context
    _chat_key = f"{source}:{chat_id}"
    _chat_wd = ""
    _chat_refs: list[str] = []
    try:
        # CB-5: Domain config from harness (single source of truth)
        from bot.harness import get_harness_for_chat, derive_reference_domains
        _kn_h = get_harness_for_chat(_chat_key)
        _chat_wd = _kn_h.write_domain or ""
        _chat_refs = derive_reference_domains(_kn_h)
    except Exception as _exc:
        log.debug("Suppressed: %s", _exc)
    lines = ["<b>\U0001f4da Knowledge Domains</b>", ""]

    # Show this chat's domain config
    if _chat_wd:
        lines.append(f"\U0001f4dd <b>Write domain:</b> <code>{_html.escape(_chat_wd)}</code>")
    if _chat_refs:
        lines.append(f"\U0001f4d6 <b>Reference:</b> {', '.join(f'<code>{_html.escape(r)}</code>' for r in _chat_refs)}")
    if _chat_wd or _chat_refs:
        lines.append("")

    if not visible:
        lines.append("<i>No domains accessible. Ask admin for access.</i>")
    else:
        for d in visible:
            status = d.get("status", "?")
            icon = "\u2705" if status == "active" else "\u26a0\ufe0f" if status == "error" else "\U0001f504"
            chunks = d.get("chunk_count", 0)
            name = _html.escape(d.get("name", d["domain_id"]))
            did = _html.escape(d["domain_id"])
            desc = _html.escape((d.get("description") or "")[:80])

            # Role tag
            _role_tag = ""
            if d["domain_id"] == _chat_wd:
                _role_tag = " [WRITE]"
            elif d["domain_id"] in _chat_refs:
                _role_tag = " [READ]"
            elif d["domain_id"] == "core":
                _role_tag = " [CORE]"

            lines.append(f"{icon} <b>{name}</b> (<code>{did}</code>){_role_tag}")
            if desc:
                lines.append(f"  {desc}")
            lines.append(f"  {chunks} chunks | status: {_html.escape(status)}")

            if _is_admin_kn:
                # Owner view: comprehensive domain state (CB-116)
                sync = d.get("last_sync_at", "never")[:16] if d.get("last_sync_at") else "never"
                err = d.get("error_message", "")
                owner = d.get("owner", "")
                lines.append(f"  Last sync: {sync}")
                if owner:
                    lines.append(f"  Owner: <code>{_html.escape(owner[:30])}</code>")
                if err:
                    lines.append(f"  \u274c {_html.escape(err[:80])}")

                # Fact count for this domain
                try:
                    from bot.storage.db import open_db as _open_db_kn
                    async with _open_db_kn() as _db_fc:
                        _cursor_fc = await _db_fc.execute(
                            "SELECT COUNT(*) FROM message_facts WHERE domain_id = ? AND expired = 0",
                            (d["domain_id"],),
                        )
                        _fc = (await _cursor_fc.fetchone())[0]
                    lines.append(f"  Facts: {_fc}")
                except Exception as _exc:
                    log.debug("Suppressed: %s", _exc)
                try:
                    from bot.storage.domain_optimize import list_recommendations as _list_recs_kn
                    _recs = await _list_recs_kn(d["domain_id"], status="pending", limit=100)
                    if _recs:
                        lines.append(f"  \U0001f4cb Recommendations: {len(_recs)} pending")
                except Exception as _exc:
                    log.debug("Suppressed: %s", _exc)
                try:
                    import json as _json_kn
                    _skills = _json_kn.loads(d.get("skills", "[]") or "[]")
                    _auto = _json_kn.loads(d.get("auto_load_skills", "[]") or "[]")
                    _modules = _json_kn.loads(d.get("prompt_modules", "[]") or "[]")
                    _cats = _json_kn.loads(d.get("facts_categories", "[]") or "[]")
                    if _skills:
                        lines.append(f"  Skills: {', '.join(_html.escape(s) for s in _skills[:8])}")
                    if _auto:
                        lines.append(f"  Auto-load: {', '.join(_html.escape(s) for s in _auto[:5])}")
                    if _modules:
                        lines.append(f"  Modules: {', '.join(_html.escape(s) for s in _modules[:5])}")
                    if _cats:
                        lines.append(f"  Fact categories: {', '.join(_html.escape(s) for s in _cats[:5])}")
                except Exception as _exc:
                    log.debug("Suppressed: %s", _exc)
                try:
                    from bot.chat_registry import all_entries as _all_kn
                    _all_entries = _all_kn()
                    _writers = [k for k, v in _all_entries.items() if v.get("write_domain") == d["domain_id"]]
                    if _writers:
                        def _fmt_writer(k):
                            _entry = _all_entries.get(k, {})
                            _title = _entry.get("title", "")
                            return _html.escape(_title or k)
                        _w_display = ", ".join(_fmt_writer(w) for w in _writers[:5])
                        lines.append(f"  Writers: {_w_display}")
                except Exception as _exc:
                    log.debug("Suppressed: %s", _exc)
            lines.append("")

    # Summary
    active = sum(1 for d in domains if d.get("status") == "active")
    total_chunks = sum(d.get("chunk_count", 0) for d in domains)
    if _is_admin_kn:
        lines.append(f"<i>{len(visible)} domains ({active} active) | {total_chunks} total chunks</i>")
    else:
        lines.append(f"<i>{len(visible)} domains accessible | {total_chunks} chunks</i>")

    if _is_admin_kn:
        lines.append("\n<i>Manage: <code>manage_domain</code> | Actions: sync, analyze, export_facts, run_hygiene</i>")
    else:
        lines.append("\n<i>Search: <code>search_knowledge</code> tool</i>")

    await _send_to_platform_simple(source, chat_id, "\n".join(lines),
                                    parse_mode="HTML", message_thread_id=message_thread_id)


async def _lightweight_daemons(source: str, chat_id: str, user_id: str,
                               *, message_thread_id: int | None = None):
    """List active Claude relay chats with linked sessions, daemons, and pool state."""
    from config import get_default_oauth_label
    import bot.chat_registry as _cr
    if not await _check_command_access(source, chat_id, user_id, "relays"):
        await _deny_command(source, chat_id, "relays", message_thread_id=message_thread_id)
        return
    from bot import desktop_relay
    if not desktop_relay.is_enabled():
        await _send_to_platform_simple(source, chat_id, "\u274c Employee workstation control disabled.", message_thread_id=message_thread_id)
        return

    # Gather all data sources
    registry = await desktop_relay.get_all_links()
    mac = await desktop_relay.list_daemons()
    try:
        from bot.relay.pool import get_pool
        pool_health = {str(c.get("chat_id")): c for c in get_pool().health_all()}
    except Exception:
        pool_health = {}

    # Index Mac daemons by chat_id
    mac_ok = mac.get("ok", False)
    mac_by_chat: dict[str, dict] = {}
    if mac_ok:
        for d in mac.get("daemons", []):
            mac_by_chat[str(d.get("chat_id", ""))] = d

    # Registry is source of truth — mac/pool are status overlays only
    all_cids = set(registry.keys())

    # Resolve chat names: session_name from registry, TG group name from API
    chat_names: dict[str, str] = {}
    tg_group_names: dict[str, str] = {}  # cid → resolved TG group title

    async def _resolve_relay_tg_name(cid_str: str) -> None:
        """Resolve TG group name for a relay chat."""
        if not cid_str.lstrip("-").isdigit():
            return
        cid_int = int(cid_str)
        # Check chat_registry cache first
        _cr_entry = _cr.get(f"telegram:{cid_int}")
        cached_title = (_cr_entry or {}).get("title", "")
        if cached_title and not _GENERIC_TITLE_RE.match(cached_title):
            tg_group_names[cid_str] = cached_title
            return
        # Resolve via TG API
        resolved = await _resolve_tg_title(cid_int)
        if resolved:
            tg_group_names[cid_str] = resolved
            # Cache back to chat_registry
            if _cr_entry:
                entry_copy = dict(_cr_entry)
                entry_copy["title"] = resolved
                _cr.register(f"telegram:{cid_int}", **entry_copy)

    await asyncio.gather(
        *[_resolve_relay_tg_name(cid) for cid in all_cids],
        return_exceptions=True,
    )

    for cid in all_cids:
        entry = registry.get(cid, {})
        # Prefer session_name from registry (e.g. "relay_tao-diet" → "tao-diet")
        sname = entry.get("session_name", "")
        if sname.startswith("relay_"):
            sname = sname[6:]
        if sname:
            chat_names[cid] = sname
        else:
            # Fallback: TG group name or cid
            chat_names[cid] = tg_group_names.get(cid, cid)

    lines = [f"<b>\U0001f5a5 Active relays</b> ({len(all_cids)})", ""]

    if not mac_ok:
        lines.append(f"\u26a0\ufe0f Mac offline: <code>{_html.escape(str(mac.get('error', ''))[:120])}</code>\n")

    idx = 1
    for cid in sorted(all_cids, key=lambda c: chat_names.get(c, c)):
        name = chat_names.get(cid, cid)
        entry = registry.get(cid, {})
        md = mac_by_chat.get(cid)
        pc = pool_health.get(cid)

        # Project path (short: just last component)
        proj = entry.get("project_path", "")
        proj_short = proj.rsplit("/", 1)[-1] if proj else ""

        # JSONL UUID (first 8 chars)
        jsonl = entry.get("pinned_jsonl_path", "")
        uuid_short = os.path.basename(jsonl).replace(".jsonl", "")[:8] if jsonl else "\u2014"

        # Status emoji
        if md and md.get("status") == "ALIVE" and pc and pc.get("state") == "ready":
            status = "\u2705"  # green check — alive + ready
        elif md and md.get("status") == "ALIVE" and pc and pc.get("state") == "busy":
            status = "\U0001f7e0"  # orange circle — alive + busy
        elif md or pc:
            status = "\u26a0\ufe0f"  # warning — partial health
        else:
            status = "\u274c"  # red X — no daemon, no pool

        # Account label + Mac-side model + settings (no bot harness — relays use Mac-side config)
        acct = entry.get("account_label", get_default_oauth_label())
        acct_tag = f" \U0001f511{_html.escape(acct)}" if acct else ""
        mac_model = md.get("model") if md else None
        model_tag = f" <i>({_html.escape(_format_model_short(mac_model))})</i>" if mac_model else ""
        settings_tag = _daemon_settings_tag(md or {})

        lines.append(f" {idx}. {status} <b>{_html.escape(name)}</b>{acct_tag}{model_tag}{settings_tag}")
        if proj_short:
            tg_name = tg_group_names.get(cid)
            tg_label = f"<code>{_html.escape(tg_name[:25])}</code>" if tg_name else f"<code>{cid}</code>"
            lines.append(f"    \U0001f4c1 <code>{_html.escape(proj_short)}</code>  jsonl:<code>{uuid_short}</code>  tg:{tg_label}")

        # Daemon + pool status on one line
        parts = []
        if md:
            hb = md.get("heartbeat_age_s")
            hb_s = f"{hb}s" if hb is not None else "?"
            parts.append(f"daemon={md['status'].lower()} hb={hb_s}")
        elif mac_ok:
            parts.append("daemon=none")
        if pc:
            parts.append(f"pool={pc.get('state')}")
        else:
            parts.append("pool=none")
        lines.append(f"    {' | '.join(parts)}")
        lines.append("")
        idx += 1

    if not all_cids:
        lines.append("<i>No relay chats configured.</i>")

    await _send_to_platform_simple(source, chat_id, "\n".join(lines), message_thread_id=message_thread_id)


async def _lightweight_daemon_reap(source: str, chat_id: str, user_id: str,
                                   *, message_thread_id: int | None = None):
    """Claude relay — sweep dead daemon pidfiles on Mac. Admin only, no LLM."""
    if not await _check_command_access(source, chat_id, user_id, "relay_reap"):
        await _deny_command(source, chat_id, "relay_reap", message_thread_id=message_thread_id)
        return
    from bot import desktop_relay
    if not desktop_relay.is_enabled():
        await _send_to_platform_simple(source, chat_id, "\u274c Employee workstation control disabled.", message_thread_id=message_thread_id)
        return
    res = await desktop_relay.reap_orphans()
    if res.get("ok"):
        n = res.get("reaped_count", 0)
        await _send_to_platform_simple(
            source, chat_id,
            f"\U0001f9f9 Reaped <b>{n}</b> orphan daemon pidfile(s).", message_thread_id=message_thread_id
        )
    else:
        await _send_to_platform_simple(
            source, chat_id,
            f"\u26a0\ufe0f Reap failed: <code>{_html.escape(str(res.get('error', ''))[:200])}</code>", message_thread_id=message_thread_id
        )


async def _lightweight_daemon_kill(source: str, chat_id: str, user_id: str, target_arg: str,
                                   *, message_thread_id: int | None = None):
    """Claude relay — reset daemon (kill + respawn). Accepts N from /relays or raw chat_id."""
    if not await _check_command_access(source, chat_id, user_id, "relay_reset"):
        await _deny_command(source, chat_id, "relay_reset", message_thread_id=message_thread_id)
        return
    from bot import desktop_relay
    if not desktop_relay.is_enabled():
        await _send_to_platform_simple(source, chat_id, "\u274c Employee workstation control disabled.", message_thread_id=message_thread_id)
        return
    # Resolve: small positive = relay number from /relays, negative/large = raw chat_id
    target_chat_id = target_arg
    try:
        num = int(target_arg)
        if 1 <= num <= 200:  # Likely a relay number, not a raw chat_id
            registry = await desktop_relay.get_all_links()
            chat_names: dict[str, str] = {}
            for cid, entry in registry.items():
                sname = entry.get("session_name", "")
                if sname.startswith("relay_"):
                    sname = sname[6:]
                chat_names[cid] = sname or cid
            sorted_cids = sorted(registry.keys(), key=lambda c: chat_names.get(c, c))
            if num <= len(sorted_cids):
                target_chat_id = sorted_cids[num - 1]
            else:
                await _send_to_platform_simple(source, chat_id,
                    f"\u274c Invalid relay number {num}. Run /relays to see list.",
                    parse_mode="HTML", message_thread_id=message_thread_id)
                return
    except ValueError:
        pass  # Not a number — use as-is (shouldn't happen with regex)
    mac_res = await desktop_relay.kill_daemon(target_chat_id)
    pool_released = False
    try:
        from bot.relay.pool import get_pool
        await get_pool().release(target_chat_id)
        pool_released = True
    except Exception as e:
        log.warning("relay-kill: pool.release failed chat=%s: %s", target_chat_id, e)

    if mac_res.get("ok"):
        # Eagerly respawn daemon
        respawn_ok = False
        try:
            from bot.relay.pool import get_pool
            pool = get_pool()
            # Acquire will create a new client; ensure_ready spawns the daemon
            entry = desktop_relay.get_relay_mode(target_chat_id)
            if entry:
                uuid = entry.get("pinned_jsonl_path", "").replace(".jsonl", "").split("/")[-1]
                cwd = entry.get("project_path", "/tmp")
                client = await pool.acquire(target_chat_id, uuid, cwd)
                await client.ensure_ready()
                respawn_ok = True
        except Exception as e:
            log.warning("relay-reset: respawn failed chat=%s: %s", target_chat_id, e)
        respawn_tag = " \u2705 Respawned." if respawn_ok else " \u26a0\ufe0f Respawn failed \u2014 will retry on next message."
        msg = (
            f"\U0001f504 Reset daemon for <code>{_html.escape(target_chat_id)}</code>: "
            f"killed pid={mac_res.get('killed_pid')}.{respawn_tag}"
        )
    else:
        msg = (
            f"\u26a0\ufe0f Kill failed: <code>{_html.escape(str(mac_res.get('error', ''))[:200])}</code>. "
            f"Pool client {'released' if pool_released else 'untouched'}."
        )
    await _send_to_platform_simple(source, chat_id, msg, message_thread_id=message_thread_id)


async def _lightweight_create_relay(source: str, chat_id: str, user_id: str, number: int,
                                    *, message_thread_id: int | None = None):
    """Create a new relay for Mac session #N (from /mac-sessions numbered list)."""
    if not await _check_command_access(source, chat_id, user_id, "create_relay"):
        await _deny_command(source, chat_id, "create_relay", message_thread_id=message_thread_id)
        return
    from bot import desktop_relay
    if not desktop_relay.is_enabled():
        await _send_to_platform_simple(source, chat_id, "\u274c Employee workstation control disabled.", message_thread_id=message_thread_id)
        return
    # Re-fetch the same list that /mac-sessions shows (numbered by project, sorted)
    jsonls = await desktop_relay.list_all_jsonls(limit=30)
    if not jsonls:
        await _send_to_platform_simple(source, chat_id,
            "\u26a0\ufe0f No Mac sessions found. Mac offline or TOTP expired.", message_thread_id=message_thread_id)
        return
    # Group by project path (same order as _lightweight_mac_sessions)
    by_project: dict[str, list[dict]] = {}
    for jl in jsonls:
        by_project.setdefault(jl["project_path"], []).append(jl)
    # Build flat numbered list of projects (not individual JSONLs)
    projects = sorted(by_project.keys())
    if number < 1 or number > len(projects):
        await _send_to_platform_simple(source, chat_id,
            f"\u274c Invalid number {number}. Run /mac-sessions to see available projects (1-{len(projects)}).",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return
    project_path = projects[number - 1]
    session_name = f"relay_{project_path.rstrip('/').split('/')[-1]}"
    # Check if already linked
    registry = await desktop_relay.get_all_links()
    for cid, entry in registry.items():
        if entry.get("project_path") == project_path and entry.get("relay_mode"):
            await _send_to_platform_simple(source, chat_id,
                f"\u26a0\ufe0f Project already has a relay: tg:<code>{cid}</code>. "
                f"Delete it first with /delete-relay.",
                parse_mode="HTML", message_thread_id=message_thread_id)
            return
    await _send_to_platform_simple(source, chat_id,
        f"\U0001f504 Creating relay for <b>{_html.escape(project_path.split('/')[-1])}</b>...",
        parse_mode="HTML", message_thread_id=message_thread_id)
    # Delegate to the existing MCP tool logic via desktop_relay action
    # We call the MCP tool handler directly — it expects an args dict
    from bot.mcp_tools import desktop_relay_tool
    result = await desktop_relay_tool.handler({
        "action": "create_relay_group",
        "session_name": session_name,
        "project_path": project_path,
        "user_ids": user_id,
    })
    # Extract and format result
    content = result.get("content", [{}])
    raw = content[0].get("text", "{}") if content else "{}"
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        data = {"text": raw}
    if data.get("success"):
        _he = _html.escape
        text_out = (
            f"\u2705 Relay <b>{_he(data.get('title', session_name))}</b> created\n"
            f"\u2022 Chat: <code>{data.get('chat_id', '?')}</code>\n"
            f"\u2022 Project: <code>{_he(data.get('project_path', project_path))}</code>"
        )
        if data.get("auto_pinned"):
            text_out += f"\n\u2022 Pinned JSONL: <code>{_he(str(data['auto_pinned']).split('/')[-1][:12])}...</code>"
        if data.get("auto_pin_note"):
            text_out += f"\n\u26a0\ufe0f {_he(data['auto_pin_note'])}"
    else:
        text_out = f"\u274c {_html.escape(data.get('error', raw))}"
    await _send_to_platform_simple(source, chat_id, text_out, parse_mode="HTML", message_thread_id=message_thread_id)


async def _auto_cleanup_stale_relay_chats() -> list[str]:
    """Remove relay groups with no activity for RELAY_STALE_DAYS (default 30).

    Runs once per day from scheduler_loop. Uses last_used from relay_sessions.json
    (kept fresh by _touch_last_used called on every _handle_relay_message dispatch).
    Set RELAY_STALE_DAYS=0 to disable.
    """
    # Import and update the global in main.py
    import main as _main
    max_days = int(os.environ.get("RELAY_STALE_DAYS", "30"))
    if max_days <= 0:
        _main._relay_cleanup_last = time.time()
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_days)).isoformat()
    try:
        from bot import desktop_relay
        registry = await desktop_relay.get_all_links()
    except Exception as e:
        log.warning("[Relay cleanup] Could not load registry: %s", e)
        return []
    # Mark last run only after successfully loading registry
    _main._relay_cleanup_last = time.time()
    removed: list[str] = []
    for cid, entry in list(registry.items()):
        last_used = entry.get("last_used", entry.get("created_at", ""))
        if last_used and last_used < cutoff:
            log.info("[Relay cleanup] Removing stale relay cid=%s last_used=%s", cid, last_used[:10])
            try:
                from bot.mcp_tools import desktop_relay_tool
                await desktop_relay_tool.handler({"action": "delete_relay_group", "chat_id": str(cid)})
                removed.append(str(cid))
            except Exception as e:
                log.warning("[Relay cleanup] Failed to delete cid=%s: %s", cid, e)
    if removed:
        log.info("[Relay cleanup] Auto-removed %d stale relay(s): %s", len(removed), removed)
    return removed


async def _lightweight_delete_relay(source: str, chat_id: str, user_id: str, number: int,
                                    *, message_thread_id: int | None = None):
    """Delete relay #N (from /relays numbered list)."""
    if not await _check_command_access(source, chat_id, user_id, "delete_relay"):
        await _deny_command(source, chat_id, "delete_relay", message_thread_id=message_thread_id)
        return
    from bot import desktop_relay
    if not desktop_relay.is_enabled():
        await _send_to_platform_simple(source, chat_id, "\u274c Employee workstation control disabled.", message_thread_id=message_thread_id)
        return
    # Re-fetch registry (same order as _lightweight_daemons)
    registry = await desktop_relay.get_all_links()
    # Build sorted list matching _lightweight_daemons order
    chat_names: dict[str, str] = {}
    for cid, entry in registry.items():
        sname = entry.get("session_name", "")
        if sname.startswith("relay_"):
            sname = sname[6:]
        chat_names[cid] = sname or cid
    sorted_cids = sorted(registry.keys(), key=lambda c: chat_names.get(c, c))
    if number < 1 or number > len(sorted_cids):
        await _send_to_platform_simple(source, chat_id,
            f"\u274c Invalid number {number}. Run /relays to see list (1-{len(sorted_cids)}).",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return
    target_cid = sorted_cids[number - 1]
    target_name = chat_names.get(target_cid, target_cid)
    await _send_to_platform_simple(source, chat_id,
        f"\U0001f5d1 Deleting relay <b>{_html.escape(target_name)}</b> (tg:{target_cid})...",
        parse_mode="HTML", message_thread_id=message_thread_id)
    from bot.mcp_tools import desktop_relay_tool
    result = await desktop_relay_tool.handler({
        "action": "delete_relay_group",
        "chat_id": target_cid,
    })
    content = result.get("content", [{}])
    raw = content[0].get("text", "{}") if content else "{}"
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        data = {"text": raw}
    if data.get("success"):
        text_out = f"\u2705 Relay <b>{_html.escape(target_name)}</b> deleted"
    elif data.get("error"):
        text_out = f"\u274c {_html.escape(data['error'])}"
    else:
        text_out = "\u2705 Done"
    await _send_to_platform_simple(source, chat_id, text_out, parse_mode="HTML", message_thread_id=message_thread_id)


async def _lightweight_delete_chat(source: str, chat_id: str, user_id: str, number: int,
                                   *, confirm: bool = False,
                                   message_thread_id: int | None = None):
    """Remove external chat #N (from /chats numbered list)."""
    from config import TG_ADMIN_CHAT_IDS
    import bot.chat_registry as _cr
    if not await _check_command_access(source, chat_id, user_id, "delete_chat"):
        await _deny_command(source, chat_id, "delete_chat", message_thread_id=message_thread_id)
        return
    # Build same filtered list as /chats to keep numbering in sync.
    dc_list, _ = await _get_ec_display_list()
    if number < 1 or number > len(dc_list):
        await _send_to_platform_simple(source, chat_id,
            f"\u274c Invalid number {number}. Run /chats to see list (1-{len(dc_list)}).",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return
    cid, info = dc_list[number - 1]
    title = info.get("title", str(cid))
    mode = info.get("mode", "silent")
    # Safety: relay check is a hard block (must delete relay first, no confirm bypass)
    if mode == "active_plus":
        from bot import desktop_relay
        if desktop_relay.is_enabled():
            entry = desktop_relay.get_relay_mode(str(cid))
            if entry:
                await _send_to_platform_simple(source, chat_id,
                    f"\u26a0\ufe0f <b>{_html.escape(title[:30])}</b> has an active relay. "
                    f"Run /delete-relay first, then /delete-chat {number}.",
                    parse_mode="HTML", message_thread_id=message_thread_id)
                return
    # Safety: require confirmation for active/active_plus chats
    if mode in ("active", "active_plus") and not confirm:
        await _send_to_platform_simple(source, chat_id,
            f"\u26a0\ufe0f <b>{_html.escape(title[:30])}</b> is [{mode}]. "
            f"This will remove it + leave the group.\n"
            f"Run <code>/delete-chat {number} confirm</code> to proceed.",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return
    _cr.unregister(f"telegram:{cid}")
    if cid in TG_ADMIN_CHAT_IDS:
        TG_ADMIN_CHAT_IDS.remove(cid)
    # Leave the TG group (fire-and-forget)
    if cid < 0:  # negative = group chat
        try:
            from integrations.telegram import leave_chat
            await leave_chat(cid)
        except Exception as e:
            log.debug(f"Failed to leave chat {cid}: {e}")
    await _send_to_platform_simple(source, chat_id,
        f"\U0001f5d1 Removed <b>{_html.escape(title[:30])}</b> (tg:{cid}) [{mode}] + left group",
        parse_mode="HTML", message_thread_id=message_thread_id)
    log.info("Deleted external chat: cid=%s title=%s mode=%s", cid, title, mode)


async def _lightweight_claude_auth(source: str, chat_id: str, user_id: str, label: str = "",
                                   *, message_thread_id: int | None = None):
    """In-container OAuth via setup-token: same paste-back flow as /mac-reauth.

    Step 1: Run `claude setup-token` → prints OAuth URL + waits for code on stdin
    Step 2: User pastes code back to chat → bot pipes it to the waiting process
    Reuses _pending_mac_reauth dict for the paste-back interception.
    """
    # RBAC command gate
    if not await _check_command_access(source, chat_id, user_id, "claude_auth"):
        await _deny_command(source, chat_id, "claude_auth", message_thread_id=message_thread_id)
        return
    label = (label or "").strip().lower()
    key = (source, chat_id, user_id)
    if not label or label in ("help", "?"):
        await _send_to_platform_simple(source, chat_id,
            "Usage: <code>/claude-auth &lt;label&gt;</code>\n\n"
            "Label is required \u2014 the captured token is saved as a named profile.\n"
            "Example: <code>/claude-auth main</code>",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return
    if label == "default":
        await _send_to_platform_simple(source, chat_id,
            "\u26d4 <code>default</code> is reserved. Use a profile name like "
            "<code>main</code> or <code>primary</code>.\n\n"
            "Example: <code>/claude-auth main</code>",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return
    if label == "cancel":
        pending = _pending_mac_reauth.pop(key, None)
        if pending:
            _ds = pending.get("drain_stop")
            _dt = pending.get("drain_task")
            if _ds:
                _ds.set()
            if _dt:
                _dt.cancel()
            proc = pending.get("claude_auth_proc")
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                except Exception as _exc:
                    log.debug("Suppressed: %s", _exc)
            fd = pending.get("pty_master_fd")
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            await _send_to_platform_simple(source, chat_id,
                "\u2705 Auth flow cancelled. Run <code>/claude-auth label</code> to start a new one.",
                parse_mode="HTML", message_thread_id=message_thread_id)
        else:
            await _send_to_platform_simple(source, chat_id,
                "No active auth flow to cancel.", message_thread_id=message_thread_id)
        return
    if key in _pending_mac_reauth:
        await _send_to_platform_simple(source, chat_id,
            "\u26a0\ufe0f Auth flow already in progress. Paste the code or <code>/claude-auth cancel</code> to abort.",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return
    await _send_to_platform_simple(source, chat_id,
        "\U0001f511 Starting Claude OAuth (in-container)...",
        parse_mode="HTML", message_thread_id=message_thread_id)
    master_fd = None
    try:
        import pty as _pty
        cli_path = "/usr/local/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude"
        # Create PTY so Ink (Node.js terminal UI) sees a real TTY on stdin
        master_fd, slave_fd = _pty.openpty()
        # Widen terminal to prevent URL line-wrapping (default 80 cols truncates OAuth URLs)
        import struct as _struct, fcntl as _fcntl, termios as _termios
        _winsize = _struct.pack('HHHH', 24, 500, 0, 0)
        _fcntl.ioctl(slave_fd, _termios.TIOCSWINSZ, _winsize)
        proc = await asyncio.create_subprocess_exec(
            cli_path, "setup-token",
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env={**os.environ, "TERM": "xterm-256color",
                 "NO_BROWSER": "1", "CLAUDE_NO_BROWSER": "1",
                 "BROWSER": "/bin/echo"},
        )
        os.close(slave_fd)  # Close slave in parent; child inherits it
        # Read from PTY master — Ink writes URL to terminal, not stdout
        import asyncio as _aio
        _ansi_pat = re.compile(r'\x1b[\[\]()#;?]*[0-9;]*[a-zA-Z\x07]|\x1b\[[0-9;?]*[hlm]')
        raw_buf = b""
        try:
            deadline = asyncio.get_event_loop().time() + 10.0
            while asyncio.get_event_loop().time() < deadline:
                remaining = max(0.1, deadline - asyncio.get_event_loop().time())
                try:
                    chunk = await _aio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            None, os.read, master_fd, 4096),
                        timeout=remaining)
                    raw_buf += chunk
                    text_so_far = raw_buf.decode("utf-8", errors="replace")
                    if "https://" in text_so_far and (
                            ".anthropic.com" in text_so_far
                            or "claude.ai" in text_so_far
                            or "claude.com" in text_so_far):
                        # URL found — grab trailing bytes and stop
                        await _aio.sleep(0.3)
                        try:
                            extra = await _aio.wait_for(
                                asyncio.get_event_loop().run_in_executor(
                                    None, os.read, master_fd, 4096),
                                timeout=0.5)
                            raw_buf += extra
                        except (asyncio.TimeoutError, OSError):
                            pass
                        break
                except _aio.TimeoutError:
                    break
                except OSError:
                    break
        except Exception as _exc:
            log.debug("Suppressed: %s", _exc)
        output = _ansi_pat.sub("", raw_buf.decode("utf-8", errors="replace"))
        import re as _re_mod
        # Match only Anthropic/Claude OAuth URLs, not random npm dependency URLs
        url_match = _re_mod.search(r'(https://(?:console\.anthropic\.com|claude\.ai|claude\.com|accounts\.anthropic\.com)[^\s]+)', output)
        if not url_match:
            # Fallback: any URL that looks like an OAuth flow (has 'auth', 'login', 'oauth' in path)
            url_match = _re_mod.search(r'(https://[^\s]*(?:auth|login|oauth)[^\s]*)', output)
        if url_match:
            url = url_match.group(1).replace("\r", "").replace("\n", "")
            # Start background PTY drain — prevents Ink buffer fill during OAuth wait
            drain_stop = asyncio.Event()
            drain_task = asyncio.create_task(_pty_drain_loop(master_fd, drain_stop))
            _pending_mac_reauth[key] = {
                "started": time.time(),
                "label": label,
                "claude_auth_proc": proc,  # Keep process alive for paste-back
                "pty_master_fd": master_fd,  # For writing code via PTY
                "drain_task": drain_task,
                "drain_stop": drain_stop,
            }
            master_fd = None  # Prevent cleanup below — fd now owned by pending dict
            label_note = f"\n\nLabel: <code>{_html.escape(label)}</code>" if label else ""
            await _send_to_platform_simple(source, chat_id,
                f"\U0001f517 Open this URL:\n\n"
                f"<a href=\"{_html.escape(url)}\">{_html.escape(url)}</a>\n\n"
                f"Complete OAuth in browser, then <b>paste the code here</b>.{label_note}",
                parse_mode="HTML", message_thread_id=message_thread_id)
        else:
            # No URL — maybe already authed or setup-token not available
            await _send_to_platform_simple(source, chat_id,
                f"\u26a0\ufe0f No OAuth URL found. Output:\n<pre>{_html.escape(output[:300])}</pre>\n"
                f"Check CLI logs for details.",
                parse_mode="HTML", message_thread_id=message_thread_id)
    except Exception as e:
        await _send_to_platform_simple(source, chat_id,
            f"\u274c Auth error: <code>{_html.escape(str(e)[:200])}</code>",
            parse_mode="HTML", message_thread_id=message_thread_id)
    finally:
        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass


async def _lightweight_m365_auth(source: str, chat_id: str, user_id: str,
                                 *, message_thread_id: int | None = None,
                                 cancel: bool = False):
    """In-container M365 device code auth — no SDK, no host scripts needed.

    Flow:
      1. Bot requests device code from Azure AD
      2. Sends URL + user code to chat
      3. Background asyncio task polls for token (up to 15 min)
      4. Writes MSAL cache (Softeria envelope) to both cache paths
      5. Verifies via Graph API
      6. Tells user to /reset
    """
    # RBAC command gate
    if not await _check_command_access(source, chat_id, user_id, "m365_auth"):
        await _deny_command(source, chat_id, "m365_auth", message_thread_id=message_thread_id)
        return

    # Handle cancel
    if cancel:
        key = (source, chat_id)
        entry = _pending_m365_auth.pop(key, None)
        if entry:
            entry["done"] = True
            _task = entry.get("task")
            if _task and not _task.done():
                _task.cancel()
            await _send_to_platform_simple(source, chat_id,
                "\u2705 M365 auth cancelled.",
                message_thread_id=message_thread_id)
        else:
            await _send_to_platform_simple(source, chat_id,
                "No active M365 auth flow to cancel.",
                message_thread_id=message_thread_id)
        return

    key = (source, chat_id)
    existing = _pending_m365_auth.get(key)
    if existing and not existing.get("task", asyncio.Future()).done():
        await _send_to_platform_simple(source, chat_id,
            "\u26a0\ufe0f M365 auth already in progress.\n"
            "Run <code>/m365-auth cancel</code> to abort, or wait for it to complete.",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return

    # Read credentials from config (env vars already popped into module constants)
    from config import MS365_MCP_CLIENT_ID, MS365_MCP_CLIENT_SECRET, MS365_MCP_TENANT_ID
    m365_client_id = MS365_MCP_CLIENT_ID
    m365_tenant_id = MS365_MCP_TENANT_ID
    m365_client_secret = MS365_MCP_CLIENT_SECRET
    if not m365_client_id or not m365_tenant_id:
        await _send_to_platform_simple(source, chat_id,
            "\u274c MS365_MCP_CLIENT_ID / MS365_MCP_TENANT_ID not set in .env.",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return

    authority = f"https://login.microsoftonline.com/{m365_tenant_id}"
    scopes = ("https://graph.microsoft.com/Mail.Read "
              "https://graph.microsoft.com/Calendars.ReadWrite "
              "https://graph.microsoft.com/User.Read "
              "offline_access")

    # Step 1: Request device code
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{authority}/oauth2/v2.0/devicecode",
                data={"client_id": m365_client_id, "scope": scopes},
            )
            if resp.status_code != 200:
                await _send_to_platform_simple(source, chat_id,
                    f"\u274c Azure AD device code request failed (HTTP {resp.status_code}):\n"
                    f"<pre>{_html.escape(resp.text[:300])}</pre>",
                    parse_mode="HTML", message_thread_id=message_thread_id)
                return
            flow = resp.json()
    except Exception as e:
        await _send_to_platform_simple(source, chat_id,
            f"\u274c Failed to contact Azure AD: <code>{_html.escape(str(e)[:200])}</code>",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return

    device_code = flow["device_code"]
    user_code = flow.get("user_code", "")
    interval = flow.get("interval", 5)
    expires_in = flow.get("expires_in", 900)
    verification_uri = flow.get("verification_uri", "https://microsoft.com/devicelogin")

    # Step 2: Send code to user
    await _send_to_platform_simple(source, chat_id,
        f"\U0001f511 <b>M365 Device Code Auth</b>\n\n"
        f"1. Open: <a href=\"{_html.escape(verification_uri)}\">{_html.escape(verification_uri)}</a>\n"
        f"2. Enter code: <code>{_html.escape(user_code)}</code>\n"
        f"3. Sign in with your M365 account\n\n"
        f"I'll poll automatically \u2014 no paste-back needed. "
        f"Expires in {expires_in // 60} min.",
        parse_mode="HTML", message_thread_id=message_thread_id)

    # Step 3: Background poll task
    async def _poll_and_save():
        try:
            deadline = time.time() + expires_in
            poll_interval = interval
            async with httpx.AsyncClient(timeout=30) as sess:
                while time.time() < deadline:
                    await asyncio.sleep(poll_interval)
                    try:
                        tresp = await sess.post(
                            f"{authority}/oauth2/v2.0/token",
                            data={
                                "client_id": m365_client_id,
                                "client_secret": m365_client_secret,
                                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                "device_code": device_code,
                            },
                        )
                        result = tresp.json()
                    except Exception as e:
                        log.warning(f"[M365 auth] Poll error: {e}")
                        continue

                    error = result.get("error", "")
                    if error == "authorization_pending":
                        continue
                    elif error == "slow_down":
                        poll_interval += 5
                        continue
                    elif error == "expired_token":
                        await _send_to_platform_simple(source, chat_id,
                            "\u274c Device code expired. Run <code>/m365-auth</code> again.",
                            parse_mode="HTML", message_thread_id=message_thread_id)
                        return
                    elif error:
                        await _send_to_platform_simple(source, chat_id,
                            f"\u274c Auth failed: {_html.escape(error)}\n"
                            f"{_html.escape(result.get('error_description', '')[:300])}",
                            parse_mode="HTML", message_thread_id=message_thread_id)
                        return
                    else:
                        # Token acquired!
                        await _send_to_platform_simple(source, chat_id,
                            "\u2705 Token acquired! Saving cache...",
                            parse_mode="HTML", message_thread_id=message_thread_id)
                        await _save_m365_token(source, chat_id, user_id, result,
                                               m365_client_id, m365_tenant_id, scopes)
                        return

            await _send_to_platform_simple(source, chat_id,
                "\u274c Polling timed out. Run <code>/m365-auth</code> again.",
                parse_mode="HTML", message_thread_id=message_thread_id)
        except asyncio.CancelledError:
            log.info("[M365 auth] Poll task cancelled")
        except Exception as e:
            log.error(f"[M365 auth] Unexpected error: {e}", exc_info=True)
            await _send_to_platform_simple(source, chat_id,
                f"\u274c Unexpected error: <code>{_html.escape(str(e)[:200])}</code>",
                parse_mode="HTML", message_thread_id=message_thread_id)
        finally:
            _pending_m365_auth.pop(key, None)

    task = asyncio.create_task(_poll_and_save())
    _pending_m365_auth[key] = {"task": task, "started": time.time()}


async def _save_m365_token(source: str, chat_id: str, user_id: str, token_result: dict,
                           client_id: str, tenant_id: str, scopes: str):
    """Build MSAL cache in Softeria envelope format and write to both paths."""
    import base64 as _b64

    access_token = token_result["access_token"]

    # Decode JWT payload to extract user info
    parts = access_token.split(".")
    if len(parts) < 2:
        log.error("[M365 auth] Access token is not a valid JWT")
        await _send_to_platform_simple(source, chat_id,
            "\u274c Token received but is not a valid JWT. Check Azure AD app config.",
            parse_mode="HTML")
        return
    payload_b64 = parts[1]
    pad = len(payload_b64) % 4
    if pad:
        payload_b64 += "=" * (4 - pad)
    payload = json.loads(_b64.urlsafe_b64decode(payload_b64))

    user_oid = payload.get("oid", "")
    user_tid = payload.get("tid", tenant_id)
    username = payload.get("preferred_username", payload.get("upn", "unknown"))
    name = payload.get("name", username)
    home_account_id = f"{user_oid}.{user_tid}"
    scopes_no_offline = scopes.replace("offline_access", "").strip()

    now = int(time.time())
    expires_on = now + token_result.get("expires_in", 3600)
    ext_expires_on = now + token_result.get("ext_expires_in",
                                            token_result.get("expires_in", 3600))

    cache_data: dict[str, Any] = {
        "Account": {
            f"{home_account_id}-login.microsoftonline.com-{user_tid}": {
                "home_account_id": home_account_id,
                "environment": "login.microsoftonline.com",
                "realm": user_tid,
                "local_account_id": user_oid,
                "username": username,
                "name": name,
                "authority_type": "MSSTS",
                "client_info": _b64.urlsafe_b64encode(
                    json.dumps({"uid": user_oid, "utid": user_tid}).encode()
                ).decode().rstrip("="),
            }
        },
        "AccessToken": {
            f"{home_account_id}-login.microsoftonline.com-accesstoken-{client_id}-{user_tid}-{scopes_no_offline}": {
                "home_account_id": home_account_id,
                "environment": "login.microsoftonline.com",
                "credential_type": "AccessToken",
                "client_id": client_id,
                "secret": access_token,
                "realm": user_tid,
                "target": scopes_no_offline,
                "cached_at": str(now),
                "expires_on": str(expires_on),
                "extended_expires_on": str(ext_expires_on),
            }
        },
        "RefreshToken": {},
        "IdToken": {},
        "AppMetadata": {
            f"appmetadata-login.microsoftonline.com-{client_id}": {
                "client_id": client_id,
                "environment": "login.microsoftonline.com",
            }
        },
    }

    # Add refresh token
    rt = token_result.get("refresh_token", "")
    if rt:
        cache_data["RefreshToken"][
            f"{home_account_id}-login.microsoftonline.com-refreshtoken-{client_id}--{scopes_no_offline}"
        ] = {
            "home_account_id": home_account_id,
            "environment": "login.microsoftonline.com",
            "credential_type": "RefreshToken",
            "client_id": client_id,
            "secret": rt,
            "target": scopes_no_offline,
        }

    # Add ID token
    id_token = token_result.get("id_token", "")
    if id_token:
        cache_data["IdToken"][
            f"{home_account_id}-login.microsoftonline.com-idtoken-{client_id}-{user_tid}-"
        ] = {
            "home_account_id": home_account_id,
            "environment": "login.microsoftonline.com",
            "credential_type": "IdToken",
            "client_id": client_id,
            "secret": id_token,
            "realm": user_tid,
        }

    # Softeria envelope format
    now_ms = int(time.time() * 1000)
    cache_wrapped = json.dumps({
        "_cacheEnvelope": True,
        "data": json.dumps(cache_data),
        "savedAt": now_ms,
    })
    selected_wrapped = json.dumps({
        "_cacheEnvelope": True,
        "data": json.dumps({"accountId": home_account_id}),
        "savedAt": now_ms,
    })

    # Write to BOTH cache paths (dual-path mismatch workaround)
    paths = [
        "/app/data/ms365-mcp",
        "/app/data/credentials/ms365-mcp",
    ]
    for dir_path in paths:
        os.makedirs(dir_path, exist_ok=True)
        cache_file = os.path.join(dir_path, "msal_cache.json")
        selected_file = os.path.join(dir_path, "selected_account.json")
        # Atomic write
        tmp = cache_file + ".tmp"
        with open(tmp, "w") as f:
            f.write(cache_wrapped)
        os.replace(tmp, cache_file)
        tmp = selected_file + ".tmp"
        with open(tmp, "w") as f:
            f.write(selected_wrapped)
        os.replace(tmp, selected_file)

    log.info(f"[M365 auth] Cache written to both paths for {username}")

    # Verify via Graph API
    verified = False
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15) as sess:
            resp = await sess.get(
                "https://graph.microsoft.com/v1.0/me",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"$select": "displayName,mail,userPrincipalName"},
            )
            if resp.status_code == 200:
                user = resp.json()
                verified = True
                display = user.get("displayName", "?")
                mail = user.get("mail", user.get("userPrincipalName", "?"))
                log.info(f"[M365 auth] Graph API verified: {display} ({mail})")
    except Exception as e:
        log.warning(f"[M365 auth] Graph API verification failed: {e}")

    has_rt = "\u2705" if rt else "\u274c"
    verify_icon = "\u2705" if verified else "\u26a0\ufe0f"

    await _send_to_platform_simple(source, chat_id,
        f"\u2705 <b>M365 auth complete!</b>\n\n"
        f"User: <b>{_html.escape(name)}</b> ({_html.escape(username)})\n"
        f"Refresh token: {has_rt}\n"
        f"Graph API: {verify_icon}\n"
        f"Cache written to both paths.\n\n"
        f"Run <code>/reset</code> to pick up the fresh token.",
        parse_mode="HTML")

    # Register credential metadata for RBAC + audit
    try:
        from bot.auth.credentials import register_credential_metadata
        _m365_account = username or mail or name or "m365-admin"
        await register_credential_metadata(
            cred_id=f"m365:{_m365_account}",
            cred_type="ms365",
            account=_m365_account,
            scope="user" if source and chat_id else "system",
            owner=f"{source}:{chat_id}" if source and chat_id else "",
            file_path=str(selected_file),
            extra_metadata={
                "scopes": scopes,
                "registered_via": "/m365-auth",
                "verified": verified,
                "display_name": name,
            },
        )
    except Exception as e:
        log.warning("Failed to register M365 credential metadata: %s", e)

    # Store email in user oauth tokens for session-level auto-routing.
    # DM-only: chat_id == user_id in TG DMs. Skip group chats (TG: negative IDs,
    # Teams: thread IDs contain '@'). This is a known limitation — credential routing
    # requires auth in the user's own DM.
    _is_group = (str(chat_id).startswith("-") or "@" in str(chat_id)) if chat_id else True
    if not _is_group and source:
        try:
            from bot.auth.oauth_flow import store_user_oauth_token
            import re as _re_m365
            _m365_email = _re_m365.sub(r"[^a-zA-Z0-9@._-]", "_", username or mail or name or "unknown")
            await store_user_oauth_token(
                user_id=f"{source}:{user_id}",
                provider="m365",
                token=rt or access_token,
                metadata={"email": _m365_email, "display_name": name, "scopes": scopes, "registered_via": "/m365-auth"},
            )
            log.info("M365 email stored in user_oauth_tokens: %s", _m365_email)
        except Exception as e:
            log.warning("Failed to store M365 oauth token: %s", e)

    # Auto-add to allowed_credentials so the user can actually use M365 tools
    if not _is_group and source:
        try:
            _m_chat_key = f"{source}:{chat_id}"
            from bot.harness import get_harness_for_chat, set_chat_override, resolve_field, get_chat_overrides, is_credential_unrestricted
            _m_h = get_harness_for_chat(_m_chat_key)
            _m_existing = get_chat_overrides(_m_chat_key).get("allowed_credentials")
            _m_ac = _m_existing if _m_existing is not None else resolve_field(_m_h, "allowed_credentials", _m_chat_key) or []
            import re as _re_m365_cred
            _m_account = _re_m365_cred.sub(r"[^a-zA-Z0-9@._-]", "_", username or mail or name or "m365")
            _m_cred_id = f"m365:{_m_account}"
            if is_credential_unrestricted(_m_ac):
                pass  # wildcard — no need to add individual creds
            elif _m_cred_id not in _m_ac:
                set_chat_override(_m_chat_key, "allowed_credentials", list(_m_ac) + [_m_cred_id])
                log.info("Auto-granted M365 credential %s to chat %s", _m_cred_id, _m_chat_key)
        except Exception as _e:
            log.warning("Failed to auto-grant M365 credential: %s", _e)

    # DMs: chat_id == user_id. Group chats (admin): no-ops harmlessly (negative chat_id)
    await _mark_onboarding_step_done(source, chat_id, user_id, "m365_auth")
    await _auto_reset_session_after_auth(source, chat_id, "m365")


# ── Google OAuth ─────────────────────────────────────────────────────

# Canonical location: bot/auth/oauth_callbacks.py (avoids circular dependency)
from bot.auth.oauth_callbacks import pending_google_auth as _pending_google_auth


async def _lightweight_google_auth(source: str, chat_id: str, user_id: str,
                                    *, message_thread_id: int | None = None,
                                    cancel: bool = False):
    """In-container Google OAuth flow — admin or employee in own DM.

    Flow:
      1. Build authorization URL with Gmail + Calendar scopes
      2. Send URL to chat
      3. User opens URL, authorizes, gets redirect with code in URL bar
      4. User pastes the authorization code back to chat
      5. Bot exchanges code for tokens
      6. Saves credentials in workspace-mcp format
      7. Tells user to /reset
    """
    # RBAC command gate
    if not await _check_command_access(source, chat_id, user_id, "google_auth"):
        await _deny_command(source, chat_id, "google_auth", message_thread_id=message_thread_id)
        return

    # Handle cancel
    if cancel:
        key = (source, chat_id)
        entry = _pending_google_auth.pop(key, None)
        if entry:
            # Also clean up nonce-keyed entry (used by OAuth callback)
            _nonce = entry.get("nonce")
            if _nonce:
                _pending_google_auth.pop(f"nonce:{_nonce}", None)
            await _send_to_platform_simple(source, chat_id,
                "\u2705 Google auth cancelled.",
                message_thread_id=message_thread_id)
        else:
            await _send_to_platform_simple(source, chat_id,
                "No active Google auth flow to cancel.",
                message_thread_id=message_thread_id)
        return

    key = (source, chat_id)

    # Cleanup stale entry (10-min timeout)
    existing = _pending_google_auth.get(key)
    if existing and time.time() - existing.get("started", 0) > 600:
        _pending_google_auth.pop(key, None)
        existing = None
    if existing and existing.get("awaiting_code"):
        # This call is the code paste — don't restart the flow
        return

    # Check for Google OAuth client credentials
    from config import GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET
    g_client_id = GOOGLE_OAUTH_CLIENT_ID
    g_client_secret = GOOGLE_OAUTH_CLIENT_SECRET
    if not g_client_id or not g_client_secret:
        await _send_to_platform_simple(source, chat_id,
            "\u274c Google OAuth not configured.\n\n"
            "Set <code>GOOGLE_OAUTH_CLIENT_ID</code> and <code>GOOGLE_OAUTH_CLIENT_SECRET</code> "
            "in <code>.env</code> (from Google Cloud Console \u2192 APIs \u2192 Credentials \u2192 "
            "OAuth 2.0 Client ID, type: Desktop).",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return

    # Build authorization URL
    scopes = [
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "openid",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.labels",
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.events",
    ]

    import urllib.parse

    # Build redirect URI from reports server (publicly accessible)
    from config import REPORTS_BASE_URL, WEBHOOK_BASE_URL, REPORTS_PORT
    if REPORTS_BASE_URL:
        redirect_uri = f"{REPORTS_BASE_URL.rstrip('/')}/oauth/google/callback"
    elif WEBHOOK_BASE_URL:
        from urllib.parse import urlparse as _urlparse
        _parsed = _urlparse(WEBHOOK_BASE_URL)
        redirect_uri = f"{_parsed.scheme}://{_parsed.hostname}:{REPORTS_PORT}/oauth/google/callback"
    else:
        await _send_to_platform_simple(source, chat_id,
            "\u274c Cannot determine callback URL. Set <code>REPORTS_BASE_URL</code> "
            "or <code>WEBHOOK_BASE_URL</code> in .env.",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return

    # Create HMAC-signed state with routing info only (no secrets in URL).
    # Secrets stored server-side in _pending_google_auth, looked up by nonce.
    import secrets as _secrets
    nonce = _secrets.token_urlsafe(16)
    from bot.auth.oauth_callbacks import sign_oauth_state as _sign_oauth_state
    state_data = {
        "nonce": nonce,
        "source": source,
        "chat_id": chat_id,
        "user_id": user_id,
        "thread_id": message_thread_id,
        "ts": time.time(),
    }
    state_token = _sign_oauth_state(state_data)

    params = {
        "client_id": g_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "state": state_token,
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)

    # Store secrets server-side, keyed by both chat key (for paste fallback) and nonce (for callback)
    _flow_data = {
        "awaiting_code": True,
        "source": source,
        "chat_id": chat_id,
        "user_id": user_id,
        "nonce": nonce,
        "state": state_token,
        "client_id": g_client_id,
        "client_secret": g_client_secret,
        "redirect_uri": redirect_uri,
        "scopes": scopes,
        "started": time.time(),
    }
    _pending_google_auth[key] = _flow_data
    _pending_google_auth[f"nonce:{nonce}"] = _flow_data  # callback lookup by nonce

    await _send_to_platform_simple(source, chat_id,
        "\U0001f510 <b>Google OAuth Setup</b>\n\n"
        f"<a href=\"{auth_url}\">Tap here to sign in to Google</a>\n\n"
        "After authorizing, you'll be redirected automatically \u2014 "
        "no need to copy any code.\n\n"
        "<i>If the redirect doesn't work, copy the code from the page "
        "and paste it here.</i>\n"
        "<i>Link expires in 10 minutes.</i>",
        parse_mode="HTML", message_thread_id=message_thread_id)


async def _handle_google_auth_code(source: str, chat_id: str, code: str,
                                    *, message_thread_id: int | None = None):
    """Exchange the Google authorization code for tokens and save credentials."""
    import httpx

    key = (source, chat_id)
    state = _pending_google_auth.pop(key, None)
    if not state:
        return
    # Also clean up the nonce-keyed entry (prevents callback replay after paste)
    _nonce = state.get("nonce", "")
    if _nonce:
        _pending_google_auth.pop(f"nonce:{_nonce}", None)

    client_id = state["client_id"]
    client_secret = state["client_secret"]
    redirect_uri = state["redirect_uri"]
    scopes = state["scopes"]
    user_id = state.get("user_id", "")

    # Exchange code for tokens
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code.strip(),
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            if resp.status_code != 200:
                try:
                    err = resp.json().get("error_description", resp.text[:200])
                except Exception:
                    err = resp.text[:200]
                await _send_to_platform_simple(source, chat_id,
                    f"\u274c Token exchange failed: {_html.escape(str(err))}\n\nTry <code>/google-auth</code> again.",
                    parse_mode="HTML", message_thread_id=message_thread_id)
                return
            token_data = resp.json()
    except Exception as e:
        await _send_to_platform_simple(source, chat_id,
            f"\u274c Token exchange error: {_html.escape(str(e)[:100])}\n\nTry <code>/google-auth</code> again.",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return

    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")

    if not refresh_token:
        await _send_to_platform_simple(source, chat_id,
            "\u26a0\ufe0f Got access token but <b>no refresh token</b>. "
            "Credentials will expire in ~1 hour and cannot be renewed.\n"
            "Not saving. Try <code>/google-auth</code> again — Google only issues "
            "refresh tokens on first consent (revoke access at "
            "<a href=\"https://myaccount.google.com/permissions\">myaccount.google.com/permissions</a> first).",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return

    # Get user email for filename
    email = "unknown"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code == 200:
                email = resp.json().get("email", "unknown")
    except Exception as _exc:
        log.debug("Suppressed: %s", _exc)
    from config import DATA_DIR
    import json as _json
    import re as _re
    # NOTE: _html is the module-level import (line 10). Do NOT re-import locally
    # — that causes UnboundLocalError in error paths above (Python scoping rule).

    creds_dir = DATA_DIR / "google-workspace-creds"
    creds_dir.mkdir(parents=True, exist_ok=True)
    # Sanitize email for filename (defense-in-depth against path traversal)
    safe_email = _re.sub(r"[^a-zA-Z0-9@._-]", "_", email)
    creds_file = creds_dir / f"{safe_email}.json"
    if not creds_file.resolve().is_relative_to(creds_dir.resolve()):
        log.error("Path traversal attempt in Google email: %s", email)
        await _send_to_platform_simple(source, chat_id,
            "\u274c Error: invalid email for credential filename.",
            message_thread_id=message_thread_id)
        return

    creds_data = {
        "token": access_token,
        "refresh_token": refresh_token,
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": client_id,
        "client_secret": client_secret,
        "scopes": scopes,
    }

    # Atomic write with restrictive permissions (contains client_secret + tokens)
    tmp = creds_file.with_suffix(".tmp")
    import os as _os_gauth
    _fd_gauth = _os_gauth.open(str(tmp), _os_gauth.O_WRONLY | _os_gauth.O_CREAT | _os_gauth.O_TRUNC, 0o600)
    with _os_gauth.fdopen(_fd_gauth, "w") as _f_gauth:
        _f_gauth.write(_json.dumps(creds_data, indent=2))
    tmp.rename(creds_file)

    await _send_to_platform_simple(source, chat_id,
        f"\u2705 <b>Google account connected: {_html.escape(safe_email)}</b>\n\n"
        f"Credentials saved to <code>{_html.escape(creds_file.name)}</code>\n"
        f"Scopes: Gmail (read/send/modify) + Calendar (full)\n\n"
        f"Run <code>/reset</code> to pick up the fresh credentials.\n"
        f"Run <code>/google-auth</code> again to add another account.",
        parse_mode="HTML", message_thread_id=message_thread_id)

    log.info("Google OAuth completed for %s (saved to %s)", email, creds_file)

    # Register credential metadata for RBAC + audit
    try:
        from bot.auth.credentials import register_credential_metadata
        _owner = f"{source}:{user_id}" if user_id else ""
        await register_credential_metadata(
            cred_id=f"google:{safe_email}",
            cred_type="google-workspace",
            account=safe_email,
            scope="user" if user_id else "system",
            owner=_owner,
            file_path=str(creds_file),
            extra_metadata={"scopes": scopes, "registered_via": "/google-auth"},
        )
    except Exception as e:
        log.warning("Failed to register Google credential metadata: %s", e)

    # Store email in user oauth tokens for session-level auto-routing
    if user_id:
        try:
            from bot.auth.oauth_flow import store_user_oauth_token
            await store_user_oauth_token(
                user_id=f"{source}:{user_id}",
                provider="google",
                token=refresh_token or access_token,
                metadata={"email": safe_email, "scopes": scopes, "registered_via": "/google-auth"},
            )
            log.info("Google email stored in user_oauth_tokens: %s", safe_email)
        except Exception as e:
            log.warning("Failed to store Google oauth token: %s", e)

    # Auto-add to allowed_credentials so the user can actually use Google tools
    if user_id:
        try:
            _g_chat_key = f"{source}:{user_id}"
            from bot.harness import get_harness_for_chat, set_chat_override, resolve_field, get_chat_overrides, is_credential_unrestricted
            _g_h = get_harness_for_chat(_g_chat_key)
            _g_existing = get_chat_overrides(_g_chat_key).get("allowed_credentials")
            _g_ac = _g_existing if _g_existing is not None else resolve_field(_g_h, "allowed_credentials", _g_chat_key) or []
            _g_cred_id = f"google:{safe_email}"
            if is_credential_unrestricted(_g_ac):
                pass  # wildcard — no need to add individual creds
            elif _g_cred_id not in _g_ac:
                set_chat_override(_g_chat_key, "allowed_credentials", list(_g_ac) + [_g_cred_id])
                log.info("Auto-granted Google credential %s to chat %s", _g_cred_id, _g_chat_key)
        except Exception as _e:
            log.warning("Failed to auto-grant Google credential: %s", _e)

    await _mark_onboarding_step_done(source, chat_id, user_id, "google_auth")
    await _auto_reset_session_after_auth(source, chat_id, "google")


# ── GitHub OAuth (Device Flow) ────────────────────────────────────────

_pending_github_auth: dict[tuple | str, dict] = {}  # (source, chat_id) -> state

async def _lightweight_github_auth(source: str, chat_id: str, user_id: str,
                                    *, message_thread_id: int | None = None,
                                    cancel: bool = False):
    """GitHub device code OAuth flow — same pattern as /m365-auth."""
    if not await _check_command_access(source, chat_id, user_id, "github_auth"):
        await _deny_command(source, chat_id, "github_auth", message_thread_id=message_thread_id)
        return

    # Handle cancel
    if cancel:
        key = (source, chat_id)
        entry = _pending_github_auth.pop(key, None)
        if entry:
            entry["done"] = True
            _task = entry.get("task")
            if _task and not _task.done():
                _task.cancel()
            await _send_to_platform_simple(source, chat_id,
                "\u2705 GitHub auth cancelled.",
                message_thread_id=message_thread_id)
        else:
            await _send_to_platform_simple(source, chat_id,
                "No active GitHub auth flow to cancel.",
                message_thread_id=message_thread_id)
        return

    from config import GITHUB_OAUTH_CLIENT_ID
    if not GITHUB_OAUTH_CLIENT_ID:
        await _send_to_platform_simple(source, chat_id,
            "\u274c GitHub OAuth not configured.\n\n"
            "Set <code>GITHUB_OAUTH_CLIENT_ID</code> in .env\n"
            "(GitHub \u2192 Settings \u2192 Developer settings \u2192 OAuth Apps \u2192 New, enable Device Flow).",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return

    key = (source, chat_id)
    existing = _pending_github_auth.get(key)
    if existing and not existing.get("done") and time.time() - existing.get("started", 0) < 900:
        await _send_to_platform_simple(source, chat_id,
            "\u26a0\ufe0f GitHub auth already in progress.\n"
            "Run <code>/github-auth cancel</code> to abort, or wait for it to complete.",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return

    # Request device code
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://github.com/login/device/code",
                data={
                    "client_id": GITHUB_OAUTH_CLIENT_ID,
                    "scope": "repo read:org",
                },
                headers={"Accept": "application/json"},
            )
            if resp.status_code != 200:
                await _send_to_platform_simple(source, chat_id,
                    f"\u274c GitHub device code request failed: {_html.escape(resp.text[:200])}",
                    parse_mode="HTML", message_thread_id=message_thread_id)
                return
            data = resp.json()
    except Exception as e:
        await _send_to_platform_simple(source, chat_id,
            f"\u274c GitHub API error: {_html.escape(str(e)[:100])}",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return

    user_code = data.get("user_code", "")
    verification_uri = data.get("verification_uri", "https://github.com/login/device")
    device_code = data.get("device_code", "")
    interval = data.get("interval", 5)
    expires_in = data.get("expires_in", 900)

    _pending_github_auth[key] = {
        "device_code": device_code,
        "interval": interval,
        "client_id": GITHUB_OAUTH_CLIENT_ID,
        "started": time.time(),
        "done": False,
    }

    await _send_to_platform_simple(source, chat_id,
        f"\U0001f510 <b>GitHub Authentication</b>\n\n"
        f"1. Open <a href=\"{verification_uri}\">{verification_uri}</a>\n"
        f"2. Enter code: <code>{user_code}</code>\n"
        f"3. Authorize the app\n\n"
        f"Polling automatically... (expires in {expires_in // 60} min)",
        parse_mode="HTML", message_thread_id=message_thread_id)

    # Background polling task
    async def _poll():
        import httpx as _httpx
        _interval = interval
        _deadline = time.time() + expires_in
        while time.time() < _deadline:
            await asyncio.sleep(_interval)
            try:
                async with _httpx.AsyncClient(timeout=15) as _client:
                    _resp = await _client.post(
                        "https://github.com/login/oauth/access_token",
                        data={
                            "client_id": GITHUB_OAUTH_CLIENT_ID,
                            "device_code": device_code,
                            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        },
                        headers={"Accept": "application/json"},
                    )
                    _data = _resp.json()
                    error = _data.get("error")
                    if error == "authorization_pending":
                        continue
                    elif error == "slow_down":
                        _interval += 5
                        continue
                    elif error == "expired_token":
                        await _send_to_platform_simple(source, chat_id,
                            "\u274c GitHub auth expired. Run <code>/github-auth</code> again.",
                            parse_mode="HTML", message_thread_id=message_thread_id)
                        break
                    elif error == "access_denied":
                        await _send_to_platform_simple(source, chat_id,
                            "\u274c GitHub auth denied by user.",
                            message_thread_id=message_thread_id)
                        break
                    elif error:
                        await _send_to_platform_simple(source, chat_id,
                            f"\u274c GitHub auth error: {_html.escape(error)}",
                            parse_mode="HTML", message_thread_id=message_thread_id)
                        break
                    elif _data.get("access_token"):
                        # Success!
                        _token = _data["access_token"]
                        _scope = _data.get("scope", "")

                        # Get GitHub username
                        _username = "unknown"
                        try:
                            async with _httpx.AsyncClient(timeout=10) as _uc:
                                _uresp = await _uc.get(
                                    "https://api.github.com/user",
                                    headers={"Authorization": f"Bearer {_token}",
                                             "Accept": "application/vnd.github+json"},
                                )
                                if _uresp.status_code == 200:
                                    _username = _uresp.json().get("login", "unknown")
                        except Exception as _exc:
                            log.debug("Suppressed: %s", _exc)
                        try:
                            from bot.auth.credentials import register_credential_metadata
                            await register_credential_metadata(
                                cred_id=f"github:{_username}",
                                cred_type="github",
                                account=_username,
                                scope="user",
                                owner=f"{source}:{user_id}",
                                extra_metadata={
                                    "scopes": _scope,
                                    "registered_via": "/github-auth",
                                },
                            )
                        except Exception as _e:
                            log.warning("Failed to register GitHub credential: %s", _e)

                        # Store token encrypted for session injection
                        try:
                            from bot.auth.oauth_flow import store_user_oauth_token
                            await store_user_oauth_token(
                                user_id=f"{source}:{user_id}",
                                provider="github",
                                token=_token,
                                metadata={"scope": _scope, "username": _username},
                            )
                        except Exception as _e:
                            log.warning("Failed to store GitHub token: %s", _e)

                        await _send_to_platform_simple(source, chat_id,
                            f"\u2705 <b>GitHub connected: {_html.escape(_username)}</b>\n\n"
                            f"Scopes: {_html.escape(_scope)}\n"
                            f"Run <code>/reset</code> to activate git + gh access.",
                            parse_mode="HTML", message_thread_id=message_thread_id)

                        # Auto-add to allowed_credentials
                        try:
                            _gh_chat_key = f"{source}:{user_id}"
                            from bot.harness import get_harness_for_chat, set_chat_override, resolve_field, get_chat_overrides, is_credential_unrestricted
                            _gh_h = get_harness_for_chat(_gh_chat_key)
                            _gh_existing = get_chat_overrides(_gh_chat_key).get("allowed_credentials")
                            _gh_ac = _gh_existing if _gh_existing is not None else resolve_field(_gh_h, "allowed_credentials", _gh_chat_key) or []
                            _gh_cred_id = f"github:{_username}"
                            if is_credential_unrestricted(_gh_ac):
                                pass  # wildcard — no need to add individual creds
                            elif _gh_cred_id not in _gh_ac:
                                set_chat_override(_gh_chat_key, "allowed_credentials", list(_gh_ac) + [_gh_cred_id])
                                log.info("Auto-granted GitHub credential %s to chat %s", _gh_cred_id, _gh_chat_key)
                        except Exception as _e2:
                            log.warning("Failed to auto-grant GitHub credential: %s", _e2)

                        await _mark_onboarding_step_done(source, chat_id, user_id, "github_auth")
                        await _auto_reset_session_after_auth(source, chat_id, "github")
                        break
            except Exception as _e:
                log.warning("GitHub auth poll error: %s", _e)
                await asyncio.sleep(_interval)

        _pending_github_auth.pop(key, None)

    _gh_task = asyncio.create_task(_poll())
    _pending_github_auth[key]["task"] = _gh_task


# ── Credential Revocation ────────────────────────────────────────────

async def _lightweight_revoke(source: str, chat_id: str, user_id: str,
                               cred_type: str = "", label: str = "",
                               *, message_thread_id: int | None = None):
    """Revoke credentials — /revoke <type> [label]. Own DM or admin."""
    if not await _check_command_access(source, chat_id, user_id, "revoke"):
        await _deny_command(source, chat_id, "revoke", message_thread_id=message_thread_id)
        return

    if not cred_type:
        await _send_to_platform_simple(source, chat_id,
            "Usage: <code>/revoke &lt;type&gt; [label]</code>\n\n"
            "Types:\n"
            "\u2022 <code>claude [label]</code> \u2014 remove Claude OAuth profile\n"
            "\u2022 <code>google [email]</code> \u2014 remove Google Workspace credentials\n"
            "\u2022 <code>m365</code> \u2014 remove Microsoft 365 credentials\n"
            "\u2022 <code>github</code> \u2014 remove GitHub credentials\n\n"
            "Example: <code>/revoke claude x</code>",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return

    _can_manage = await _has_rbac_capability(source, user_id, "tools", "manage_oauth", chat_id=chat_id)
    cred_type = cred_type.lower()

    if cred_type == "claude":
        if not label:
            await _send_to_platform_simple(source, chat_id,
                "Usage: <code>/revoke claude &lt;label&gt;</code>\n"
                "Use <code>/accounts</code> to see all connected accounts.",
                parse_mode="HTML", message_thread_id=message_thread_id)
            return
        # Non-admin can only revoke their own namespaced profiles
        if not _can_manage and not label.startswith(f"user-{user_id}-"):
            await _send_to_platform_simple(source, chat_id,
                f"\u274c You can only revoke your own profiles (prefixed <code>user-{user_id}-</code>).",
                parse_mode="HTML", message_thread_id=message_thread_id)
            return
        try:
            from config import remove_oauth_profile
            removed = remove_oauth_profile(label)
            if removed:
                await _send_to_platform_simple(source, chat_id,
                    f"\u2705 Claude profile <code>{_html.escape(label)}</code> removed.\n"
                    f"Run <code>/reset</code> to apply.",
                    parse_mode="HTML", message_thread_id=message_thread_id)
                # Also remove from allowed_credentials if present
                try:
                    from bot.harness import get_chat_overrides, set_chat_override
                    _emp_key = f"{source}:{chat_id}"
                    _ov = get_chat_overrides(_emp_key).get("allowed_credentials")
                    if _ov and f"claude:{label}" in _ov:
                        _new = [c for c in _ov if c != f"claude:{label}"]
                        set_chat_override(_emp_key, "allowed_credentials", _new)
                except Exception as _exc:
                    log.debug("Suppressed: %s", _exc)
            else:
                await _send_to_platform_simple(source, chat_id,
                    f"\u274c Profile <code>{_html.escape(label)}</code> not found.",
                    parse_mode="HTML", message_thread_id=message_thread_id)
        except ValueError as e:
            await _send_to_platform_simple(source, chat_id,
                f"\u274c {_html.escape(str(e))}",
                parse_mode="HTML", message_thread_id=message_thread_id)

    elif cred_type == "google":
        from config import DATA_DIR
        import re as _re
        creds_dir = DATA_DIR / "google-workspace-creds"
        if label:
            safe = _re.sub(r"[^a-zA-Z0-9@._-]", "_", label)
            target = creds_dir / f"{safe}.json"
        else:
            # List available credentials for user to choose
            files = list(creds_dir.glob("*.json")) if creds_dir.exists() else []
            if not files:
                await _send_to_platform_simple(source, chat_id,
                    "\u274c No Google credentials found.",
                    message_thread_id=message_thread_id)
                return
            names = [f.stem for f in files]
            await _send_to_platform_simple(source, chat_id,
                "Usage: <code>/revoke google &lt;email&gt;</code>\n\n"
                "Available:\n" + "\n".join(f"\u2022 <code>{_html.escape(n)}</code>" for n in names),
                parse_mode="HTML", message_thread_id=message_thread_id)
            return
        if target.exists():
            target.unlink()
            # Remove from credential store
            try:
                from bot.auth.credentials import delete_credential
                await delete_credential(f"google:{safe}")
            except Exception as _exc:
                log.warning("Failed to delete Google credential: %s", _exc)
            try:
                from bot.auth.oauth_flow import delete_user_oauth_token
                await delete_user_oauth_token(f"{source}:{user_id}", "google")
            except Exception as _exc:
                log.warning("Failed to delete Google OAuth token: %s", _exc)
            await _send_to_platform_simple(source, chat_id,
                f"\u2705 Google credentials for <code>{_html.escape(safe)}</code> removed.\n"
                f"Run <code>/reset</code> to apply.",
                parse_mode="HTML", message_thread_id=message_thread_id)
        else:
            await _send_to_platform_simple(source, chat_id,
                f"\u274c No credentials found for <code>{_html.escape(label)}</code>.",
                parse_mode="HTML", message_thread_id=message_thread_id)

    elif cred_type == "m365":
        from config import DATA_DIR
        # M365 creds are in multiple locations
        m365_dir = DATA_DIR / "ms365-mcp"
        creds_dir = DATA_DIR / "credentials" / "ms365-mcp"
        deleted = 0
        for d in [m365_dir, creds_dir]:
            if d.exists():
                for f in d.iterdir():
                    if f.is_file() and f.suffix in (".json", ".bin"):
                        f.unlink()
                        deleted += 1
        if deleted:
            # Remove OAuth token metadata (stops auto-routing in volatile header)
            try:
                from bot.auth.oauth_flow import delete_user_oauth_token
                await delete_user_oauth_token(f"{source}:{user_id}", "m365")
            except Exception as _exc:
                log.warning("Failed to delete M365 OAuth token: %s", _exc)
            try:
                from bot.auth.credentials import delete_credential, list_credentials
                _m365_creds = await list_credentials(owner=f"{source}:{user_id}", cred_type="ms365")
                for _mc in _m365_creds:
                    await delete_credential(_mc["id"])
            except Exception as _exc:
                log.warning("Failed to delete M365 credentials: %s", _exc)
            await _send_to_platform_simple(source, chat_id,
                f"\u2705 M365 credentials removed ({deleted} files).\n"
                f"Run <code>/reset</code> to apply.\n"
                f"Re-authenticate with <code>/m365-auth</code> when needed.",
                parse_mode="HTML", message_thread_id=message_thread_id)
        else:
            await _send_to_platform_simple(source, chat_id,
                "\u274c No M365 credentials found.",
                message_thread_id=message_thread_id)
    elif cred_type == "github":
        try:
            from bot.auth.oauth_flow import delete_user_oauth_token
            await delete_user_oauth_token(f"{source}:{user_id}", "github")
            try:
                from bot.auth.credentials import delete_credential
                await delete_credential(f"github:{label or user_id}")
            except Exception as _exc:
                log.warning("Failed to delete GitHub credential: %s", _exc)
            await _send_to_platform_simple(source, chat_id,
                "\u2705 GitHub credentials removed.\n"
                "Run <code>/reset</code> to apply.\n"
                "Re-authenticate with <code>/github-auth</code> when needed.",
                parse_mode="HTML", message_thread_id=message_thread_id)
        except Exception as e:
            await _send_to_platform_simple(source, chat_id,
                f"\u274c Failed to revoke GitHub credentials: {_html.escape(str(e)[:100])}",
                parse_mode="HTML", message_thread_id=message_thread_id)
    else:
        await _send_to_platform_simple(source, chat_id,
            f"\u274c Unknown type: <code>{_html.escape(cred_type)}</code>. Use: claude, google, m365, github",
            parse_mode="HTML", message_thread_id=message_thread_id)


async def _lightweight_permissions(source: str, chat_id: str, user_id: str,
                                    target: str = "",
                                    *, message_thread_id: int | None = None):
    """Show full permission profile for a user or chat — admin only."""
    if not await _check_command_access(source, chat_id, user_id, "permissions"):
        await _deny_command(source, chat_id, "permissions", message_thread_id=message_thread_id)
        return

    # CB-139: Non-admin can only check own permissions (no target arg)
    _is_perm_admin = await _has_rbac_capability(source, user_id, "commands", "permissions_admin", chat_id=chat_id)

    # Resolve target: user ID, chat ID, or "self"
    if not target:
        target = f"{source}:{chat_id}"
    elif not _is_perm_admin:
        # Non-admin can only check own chat
        target = f"{source}:{chat_id}"

    # Normalize: if bare number, assume telegram
    if target.lstrip("-").isdigit():
        target = f"telegram:{target}"

    lines = [f"\U0001f50e <b>Permissions: <code>{_html.escape(target)}</code></b>", ""]

    # 1. Chat registry
    import bot.chat_registry as _cr_perm
    _entry = _cr_perm.get(target)
    if _entry:
        lines.append(f"\U0001f4cb <b>Registry:</b> {_html.escape(_entry.get('title', '?'))} | mode={_html.escape(str(_entry.get('mode', '?')))}")
    else:
        lines.append("\U0001f4cb <b>Registry:</b> not registered")

    # 2. RBAC roles (engine/subject reused in sections 5+7)
    engine = None
    subject = None
    try:
        from bot.rbac import get_engine
        from bot.rbac.models import Subject
        engine = get_engine()
        await engine._refresh_cache()
        # Check as user
        subject = Subject(user_id=target)
        role_ids = engine._get_role_ids(subject)
        lines.append(f"\U0001f6e1 <b>RBAC roles:</b> {', '.join(sorted(role_ids)) if role_ids else 'none'}")
    except Exception as e:
        log.warning("permissions: RBAC lookup failed for %s: %s", target, e)
        lines.append("\U0001f6e1 <b>RBAC:</b> lookup failed")

    # 3. Harness
    try:
        from bot.harness import get_harness_for_chat, get_chat_overrides, resolve_field
        _h = get_harness_for_chat(target)
        _ov = get_chat_overrides(target)
        lines.append(f"\U0001f39b <b>Harness:</b> {_html.escape(_h.label)}")
        if _ov:
            _ov_str = ", ".join(f"{k}={v}" for k, v in _ov.items())
            lines.append(f"  overrides: {_html.escape(_ov_str[:200])}")
        # Allowed credentials
        from bot.harness import is_credential_unrestricted as _is_unr_perm
        _ac = resolve_field(_h, "allowed_credentials", target)
        if _ac and _is_unr_perm(_ac):
            lines.append("  credentials: unrestricted")
        elif not _ac:
            lines.append("  credentials: [] (deny-all)")
        else:
            lines.append(f"  credentials: {', '.join(_html.escape(str(c)) for c in _ac)}")
    except Exception as e:
        log.warning("permissions: harness lookup failed for %s: %s", target, e)
        lines.append("\U0001f39b <b>Harness:</b> lookup failed")

    # 4. OAuth profile
    try:
        from config import get_oauth_for_chat
        _oauth = get_oauth_for_chat(target)
        lines.append(f"\U0001f511 <b>OAuth:</b> {_html.escape(_oauth)}")
    except Exception:
        lines.append("\U0001f511 <b>OAuth:</b> default")

    # 5. Execution scope
    if engine and subject:
        try:
            scope = await engine.get_execution_scope(subject)
            lines.append(f"\U0001f4e6 <b>Execution:</b> filesystem={scope.get('filesystem', '?')}, network={scope.get('network', '?')}")
        except Exception as e:
            log.warning("permissions: execution scope failed for %s: %s", target, e)

    # 6. Onboarding
    try:
        from bot.onboarding.store import get_onboarding
        _onb = await get_onboarding(target)
        if _onb:
            _done = len(_onb.completed_steps)
            from bot.onboarding.models import get_applicable_steps, get_applicable_admin_steps
            _applicable = get_applicable_admin_steps() if _onb.is_admin else get_applicable_steps()
            _total = len(_applicable)
            lines.append(f"\U0001f393 <b>Onboarding:</b> {_onb.status} ({_done}/{_total} steps)")
        else:
            lines.append("\U0001f393 <b>Onboarding:</b> not started")
    except Exception as e:
        log.warning("permissions: onboarding lookup failed for %s: %s", target, e)

    # 7. Allowed commands (sample)
    if engine and subject:
        try:
            _cmd_samples = ["chats", "deploy", "claude_auth", "reset", "status"]
            allowed = []
            denied = []
            for cmd in _cmd_samples:
                d = await engine.check_access(subject, "use", "commands", cmd, log_decision=False)
                (allowed if d.allowed else denied).append(cmd)
            lines.append(f"\U0001f4dd <b>Commands:</b> {', '.join(allowed) if allowed else 'none'}")
            if denied:
                lines.append(f"  denied: {', '.join(denied)}")
        except Exception as e:
            log.warning("permissions: command check failed for %s: %s", target, e)

    await _send_to_platform_simple(source, chat_id, "\n".join(lines),
                                    parse_mode="HTML", message_thread_id=message_thread_id)


async def _lightweight_rollback(source: str, chat_id: str, user_id: str, confirm: bool,
                                *, message_thread_id: int | None = None):
    """Admin command: revert to last healthy deploy — no LLM."""
    if not await _check_command_access(source, chat_id, user_id, "rollback"):
        await _deny_command(source, chat_id, "rollback", message_thread_id=message_thread_id)
        return

    import subprocess as _sp

    # last_healthy_commit is in deploy/ (parent), NOT deploy/triggers/ (mounted
    # as /deploy-triggers). Try both paths for robustness.
    healthy_file = Path("/host-repo/deploy/last_healthy_commit")
    if not healthy_file.exists():
        # Fallback to legacy path (pre-fix)
        healthy_file = Path("/deploy-triggers/last_healthy_commit")
    if not healthy_file.exists():
        await _send_to_platform_simple(
            source, chat_id,
            "\u274c No healthy commit marker found \u2014 nothing to rollback to.",
            message_thread_id=message_thread_id,
        )
        return

    healthy = healthy_file.read_text().strip()
    if not healthy or not re.match(r"^[0-9a-f]{7,40}$", healthy):
        await _send_to_platform_simple(
            source, chat_id, "\u274c Healthy commit file is empty or corrupt.", message_thread_id=message_thread_id)
        return

    # Current HEAD
    try:
        head = _sp.run(
            ["git", "rev-parse", "HEAD"], cwd="/host-repo",
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception as e:
        await _send_to_platform_simple(source, chat_id, f"\u274c Git error: {e}", message_thread_id=message_thread_id)
        return

    if head[:12] == healthy[:12]:
        await _send_to_platform_simple(
            source, chat_id,
            "\u2705 Already at the last healthy commit \u2014 nothing to rollback.",
            message_thread_id=message_thread_id,
        )
        return

    # Commits between healthy and HEAD
    try:
        log_out = _sp.run(
            ["git", "log", "--oneline", f"{healthy}..HEAD"],
            cwd="/host-repo", capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception as e:
        log.warning(f"rollback: git log failed: {e}")
        log_out = "(could not retrieve git log)"

    # --- Preview mode ---
    if not confirm:
        msg = (
            f"\U0001f504 <b>Rollback preview</b>\n\n"
            f"Current: <code>{head[:8]}</code>\n"
            f"Target:  <code>{healthy[:8]}</code> (last healthy)\n\n"
            f"<b>Commits to revert:</b>\n"
            f"<pre>{_html.escape(log_out[:1500]) if log_out else '(none)'}</pre>\n\n"
            f"Send <code>/rollback confirm</code> to execute."
        )
        await _send_to_platform_simple(source, chat_id, msg, parse_mode="HTML", message_thread_id=message_thread_id)
        return

    # --- Execute rollback ---
    # Check for in-progress deploy
    trigger_path = Path("/host-repo/deploy/triggers/deploy_trigger.json")
    if trigger_path.exists():
        await _send_to_platform_simple(
            source, chat_id,
            "\u274c Deploy already in progress \u2014 wait for it to finish first.",
            message_thread_id=message_thread_id,
        )
        return

    # Check for uncommitted changes
    try:
        status = _sp.run(
            ["git", "status", "--porcelain"], cwd="/host-repo",
            capture_output=True, text=True, timeout=10,
        )
        if status.stdout.strip():
            await _send_to_platform_simple(
                source, chat_id,
                "\u274c Uncommitted changes in /host-repo \u2014 commit or discard first.",
                message_thread_id=message_thread_id,
            )
            return
    except Exception as _exc:
        log.debug("Suppressed: %s", _exc)
    try:
        # Restore all tracked files to healthy state
        _sp.run(
            ["git", "checkout", healthy, "--", "."],
            cwd="/host-repo", capture_output=True, text=True, timeout=30,
            check=True,
        )
        # Stage everything (including deletions)
        _sp.run(["git", "add", "-A"], cwd="/host-repo", timeout=10, check=True)
        # Commit
        _sp.run(
            ["git", "commit", "-m",
             f"rollback: restore {healthy[:8]} (revert from {head[:8]})"],
            cwd="/host-repo", capture_output=True, text=True, timeout=10,
            check=True,
        )
        # Push
        _sp.run(
            ["git", "push", "origin", "main"],
            cwd="/host-repo", capture_output=True, text=True, timeout=30,
            check=True,
        )
    except _sp.CalledProcessError as e:
        err = (e.stderr or e.stdout or str(e))[:500]
        await _send_to_platform_simple(
            source, chat_id,
            f"\u274c Rollback git step failed:\n<pre>{_html.escape(err)}</pre>",
            parse_mode="HTML", message_thread_id=message_thread_id,
        )
        return

    # Write deploy trigger (skip pre-deploy gates — restoring known-good code)
    trigger = {
        "action": "rebuild",
        "reason": f"rollback to {healthy[:8]}",
        "timestamp": datetime.now().isoformat(),
        "qa_results": [f"\U0001f504 Rollback from {head[:8]} to {healthy[:8]}"],
    }
    tmp_path = trigger_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(trigger, indent=2))
    tmp_path.rename(trigger_path)
    Path("/app/data/deploy_pending").write_text("rebuild")

    log.warning(
        f"ROLLBACK_EXECUTED: user_id={user_id} from={head[:8]} to={healthy[:8]}"
    )

    await _send_to_platform_simple(
        source, chat_id,
        f"\u2705 Rolled back to <code>{healthy[:8]}</code> and triggered rebuild.\n"
        f"Bot will restart in ~30\u2013120s.",
        parse_mode="HTML", message_thread_id=message_thread_id,
    )


async def _lightweight_harnesses(source: str, chat_id: str, user_id: str, specific: str | None = None,
                                  *, message_thread_id: int | None = None):
    """Admin command: list harness definitions or show one specific harness."""
    if not await _check_command_access(source, chat_id, user_id, "harnesses"):
        await _deny_command(source, chat_id, "harnesses", message_thread_id=message_thread_id)
        return

    # CB-139: Admin sees all harnesses, non-admin sees only their own
    _is_harness_admin = await _has_rbac_capability(source, user_id, "commands", "harnesses_admin", chat_id=chat_id)
    from bot.harness import list_harnesses, get_harness, format_harness, get_chat_assignments, get_chat_harness_label

    if specific:
        h = get_harness(specific)
        if not h:
            await _send_to_platform_simple(source, chat_id,
                f"\u274c Harness <code>{_html.escape(specific)}</code> not found.",
                parse_mode="HTML", message_thread_id=message_thread_id)
            return
        # Show detailed view + which chats use it
        assignments = get_chat_assignments()
        using = [k for k, v in assignments.items() if v == specific]
        msg = format_harness(h)
        if using:
            msg += f"\n\n<i>Used by: {', '.join(using[:10])}</i>"
        await _send_to_platform_simple(source, chat_id, msg, parse_mode="HTML", message_thread_id=message_thread_id)
        return

    # List harnesses — admin sees all, non-admin sees own + canonical descriptions
    harnesses = list_harnesses()
    assignments = get_chat_assignments()
    if not _is_harness_admin:
        # Non-admin: show only their own harness
        _own_label = get_chat_harness_label(f"{source}:{chat_id}")
        if _own_label:
            harnesses = {k: v for k, v in harnesses.items() if k == _own_label}
        else:
            harnesses = {}
    from pathlib import Path as _P
    _skills_dir = _P("/app/.claude/skills")
    lines = ["<b>\u2699\ufe0f Harnesses</b>", ""]
    for label, h in sorted(harnesses.items()):
        # Only count user-facing chats (telegram:*, wa:*), exclude system/PM keys
        count = sum(1 for k, v in assignments.items()
                    if v == label and not k.startswith("system:"))
        compact = format_harness(h, compact=True)
        chat_str = f" ({count} chat{'s' if count != 1 else ''})" if count else ""
        lines.append(f"  <b>{label}</b>{chat_str} \u2014 {compact}")
        # Show modules / skills / domain config (CB-5)
        details = []
        # Domain + role config
        _h_domains = getattr(h, "domains", None)
        if _h_domains:
            details.append(f"\U0001f310 domains: {', '.join(_h_domains)}")
        _h_wd = getattr(h, "write_domain", None)
        if _h_wd:
            details.append(f"\U0001f4dd write: {_h_wd}")
        _h_role = getattr(h, "default_role", None)
        if _h_role:
            details.append(f"\U0001f511 role: {_h_role}")
        # CLAUDE.md filter (domain:component notation)
        _cmd_modules = getattr(h, "claude_md_modules", None)
        if _cmd_modules:
            preloaded = [m for m in _cmd_modules if (_skills_dir / m).is_dir()]
            modules_only = [m for m in _cmd_modules if not (_skills_dir / m).is_dir()]
            if preloaded:
                details.append(f"\U0001f4d6 skills: {', '.join(preloaded)}")
            if modules_only:
                details.append(f"\U0001f4c4 modules: {', '.join(modules_only)}")
        for d in details:
            lines.append(f"    <i>{d}</i>")
    lines.append("")
    lines.append(f"<i>{len(harnesses)} harness(es). Manage via MCP tool or <code>/set-harness</code>.</i>")
    await _send_to_platform_simple(source, chat_id, "\n".join(lines), parse_mode="HTML", message_thread_id=message_thread_id)


async def _lightweight_set_harness(source: str, chat_id: str, user_id: str, label: str,
                                   *, message_thread_id: int | None = None):
    """Admin command: assign a harness to the current chat."""
    if not await _check_command_access(source, chat_id, user_id, "set_harness"):
        await _deny_command(source, chat_id, "set_harness", message_thread_id=message_thread_id)
        return

    from bot.harness import assign_chat_harness, get_harness
    h = get_harness(label)
    if not h:
        await _send_to_platform_simple(source, chat_id,
            f"\u274c Harness <code>{_html.escape(label)}</code> not found. "
            f"Use <code>/harnesses</code> to see available.",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return

    try:
        # CB-5: Use assign_chat_harness for unified harness + RBAC assignment
        chat_key = f"{source}:{chat_id}"
        h = await assign_chat_harness(chat_key, label)
        from bot.harness import format_harness
        # Trigger session reset so new harness takes effect immediately
        try:
            from bot.agent import client_pool
            if client_pool:
                client_pool.schedule_session_reset(chat_key)
                reset_note = "Session resetting \u2014 new harness active on next message."
            else:
                reset_note = "Takes effect on next session reset."
        except Exception:
            reset_note = "Takes effect on next session reset."
        await _send_to_platform_simple(source, chat_id,
            f"\u2705 Chat switched to harness <code>{_html.escape(label)}</code>.\n"
            f"{reset_note}\n\n"
            f"{format_harness(h, compact=True)}",
            parse_mode="HTML", message_thread_id=message_thread_id)
    except ValueError as e:
        await _send_to_platform_simple(source, chat_id, f"\u274c {e}", message_thread_id=message_thread_id)


async def _lightweight_switch_oauth(
    source: str,
    chat_id: str,
    user_id: str,
    label: str,
    all_chats: str = "",
    *,
    message_thread_id: int | None = None,
) -> None:
    """Switch OAuth profile for this chat (or all chats) — no inference.

    Access: Admin can switch any chat + use 'all' flag.
    Employee can switch their own DM only (no 'all' flag).
    """
    from config import (
        set_oauth_for_chat,
        get_chat_oauth_overrides,
        get_default_oauth_label,
    )
    from config_overrides import is_default_oauth

    if not await _check_command_access(source, chat_id, user_id, "claude_set_oauth"):
        await _deny_command(source, chat_id, "claude_set_oauth", message_thread_id=message_thread_id)
        return

    _can_manage_all = await _has_rbac_capability(source, user_id, "tools", "manage_oauth", chat_id=chat_id)

    # Handle "set-default <label>" — admin-only, sets the global default profile
    if label and label.lower() == "set-default":
        if not _can_manage_all:
            await _send_to_platform_simple(source, chat_id,
                "\u274c Only admins can set the default OAuth profile.",
                message_thread_id=message_thread_id)
            return
        default_label = all_chats  # group(2) captured as "all_chats" by dispatch
        if not default_label:
            await _send_to_platform_simple(source, chat_id,
                "Usage: <code>/claude-set-oauth set-default &lt;label&gt;</code>",
                parse_mode="HTML", message_thread_id=message_thread_id)
            return
        try:
            from config_overrides import set_default_oauth_label
            set_default_oauth_label(str(default_label))
            await _send_to_platform_simple(source, chat_id,
                f"\u2705 Default OAuth profile set to <code>{_html.escape(str(default_label))}</code>.\n"
                f"All sessions without per-chat overrides now use this profile.\n"
                f"Run <code>/reset</code> on active sessions to apply.",
                parse_mode="HTML", message_thread_id=message_thread_id)
        except Exception as e:
            await _send_to_platform_simple(source, chat_id,
                f"\u274c Failed: {_html.escape(str(e)[:100])}",
                parse_mode="HTML", message_thread_id=message_thread_id)
        return

    _is_all = all_chats.lower() == "all" if all_chats else False
    if not _can_manage_all and _is_all:
        await _send_to_platform_simple(source, chat_id,
            "\u274c Employees can only switch OAuth for their own chat, not all chats.",
            message_thread_id=message_thread_id)
        return

    effective = get_default_oauth_label() if is_default_oauth(label) else label

    # Credential gate: users without manage_oauth must have profile in allowed_credentials
    if not _can_manage_all:
        try:
            from bot.harness import get_harness_for_chat as _get_h_sw, resolve_field as _rf_sw, is_credential_unrestricted as _is_unr_sw
            _h_sw = _get_h_sw(f"{source}:{chat_id}")
            _ac = _rf_sw(_h_sw, "allowed_credentials", f"{source}:{chat_id}") or []
            if not _is_unr_sw(_ac):
                _cred_id = f"claude:{effective}"
                if _cred_id not in _ac and effective not in _ac:
                    await _send_to_platform_simple(source, chat_id,
                        f"\u274c Profile '{effective}' not in your allowed credentials.",
                        message_thread_id=message_thread_id)
                    return
        except Exception:
            await _send_to_platform_simple(source, chat_id,
                "\u274c Credential check failed. Try again or contact admin.",
                message_thread_id=message_thread_id)
            return

    try:
        if _is_all:
            # Switch every chat that already has an explicit override, PLUS the current chat.
            overrides = get_chat_oauth_overrides()
            keys_to_update: list[str] = list(overrides.keys())
            current_key = f"{source}:{chat_id}"
            if current_key not in keys_to_update:
                keys_to_update.append(current_key)

            for key in keys_to_update:
                set_oauth_for_chat(key, effective)

            # Reset sessions for all updated chats.
            reset_count = 0
            try:
                from bot.agent import client_pool
                if client_pool:
                    for key in keys_to_update:
                        client_pool.schedule_session_reset(key)
                        reset_count += 1
            except Exception as _exc:
                log.debug("Suppressed: %s", _exc)
            display = _html.escape(effective)
            await _send_to_platform_simple(
                source, chat_id,
                f"\u2705 Switched <b>{len(keys_to_update)}</b> chat(s) to OAuth <code>{display}</code>.\n"
                f"{reset_count} session(s) reset \u2014 new OAuth active on next message.",
                parse_mode="HTML", message_thread_id=message_thread_id,
            )
        else:
            session_key = f"{source}:{chat_id}"
            set_oauth_for_chat(session_key, effective)

            reset_note = "Takes effect on next session reset."
            try:
                from bot.agent import client_pool
                if client_pool:
                    client_pool.schedule_session_reset(session_key)
                    reset_note = "Session resetting \u2014 new OAuth active on next message."
            except Exception as _exc:
                log.debug("Suppressed: %s", _exc)
            display = _html.escape(effective)
            await _send_to_platform_simple(
                source, chat_id,
                f"\u2705 Chat OAuth switched to <code>{display}</code>.\n{reset_note}",
                parse_mode="HTML", message_thread_id=message_thread_id,
            )
    except ValueError as e:
        await _send_to_platform_simple(source, chat_id, f"\u274c {_html.escape(str(e))}", parse_mode="HTML", message_thread_id=message_thread_id)


async def _lightweight_onboarding(source: str, chat_id: str, user_id: str,
                                  *, message_thread_id: int | None = None):
    """Show onboarding progress or admin dashboard (RBAC-gated)."""
    _can_admin_onboard = await _has_rbac_capability(source, str(user_id), "commands", "chats", chat_id=chat_id)

    if _can_admin_onboard:
        # Admin: show dashboard of all users
        try:
            from bot.onboarding.steps import format_admin_dashboard
            dashboard = await format_admin_dashboard()
            await _send_to_platform_simple(source, chat_id, dashboard, parse_mode="HTML", message_thread_id=message_thread_id)
        except Exception as e:
            await _send_to_platform_simple(source, chat_id,
                f"\u26a0\ufe0f Onboarding dashboard error: {_html.escape(str(e)[:200])}",
                parse_mode="HTML", message_thread_id=message_thread_id)
    else:
        # Employee: show their own progress
        subject_id = f"{source}:{user_id}"
        try:
            from bot.onboarding.store import get_onboarding
            from bot.onboarding.engine import show_progress
            state = await get_onboarding(subject_id)
            if state:
                await show_progress(state, source=source, chat_id=chat_id)
            else:
                await _send_to_platform_simple(source, chat_id,
                    "No onboarding record found. Send any message to start the setup wizard.",
                    parse_mode="HTML", message_thread_id=message_thread_id)
        except Exception as e:
            log.error("Onboarding progress error for %s: %s", subject_id, e, exc_info=True)
            await _send_to_platform_simple(source, chat_id,
                "\u26a0\ufe0f An error occurred loading your setup progress. Please try again later.",
                parse_mode="HTML", message_thread_id=message_thread_id)


async def _lightweight_help(source: str, chat_id: str, user_id: str,
                            *, message_thread_id: int | None = None):
    """Show available commands based on user role AND chat context — no LLM.

    Three distinct menus:
    - Relay chat: session/daemon commands + essentials (compact, relay-focused)
    - Non-relay admin: full admin menu (no relay-in-chat commands)
    - Non-admin: essentials only
    """
    # RBAC-based display: show sections based on command capabilities
    _can_admin = await _has_rbac_capability(source, user_id, "commands", "chats", chat_id=chat_id)
    _can_auth = await _has_rbac_capability(source, user_id, "commands", "claude_auth", chat_id=chat_id)

    def _cmd(line: str) -> str:
        """Wrap leading /command in <code> tags for tap-to-copy UX."""
        if line.startswith("/"):
            parts = line.split(" \u2014 ", 1)
            if len(parts) == 2:
                return f"<code>{parts[0]}</code> \u2014 {parts[1]}"
            return f"<code>{line}</code>"
        return line

    # Detect relay chat context
    from bot import desktop_relay
    is_relay = (desktop_relay.is_enabled()
                and desktop_relay.get_relay_mode(chat_id))

    if is_relay and _can_admin:
        # ── RELAY CHAT MENU (admin only — relay chats are admin-created) ──
        lines = [
            "\U0001f517 <b>Relay Chat Commands</b>",
            "",
            "\U0001f4ac <b>Session</b>",
            _cmd("/session \u2014 show current binding + overrides"),
            _cmd("/session list \u2014 JSONLs in project"),
            _cmd("/session pin N \u2014 switch to JSONL #N or uuid prefix"),
            _cmd("/session model NAME \u2014 set model override"),
            _cmd("/session effort LEVEL \u2014 set effort override"),
            _cmd("/session context 1m|200k \u2014 toggle context window"),
            _cmd("/session settings \u2014 show effective settings"),
            _cmd("/session reset-settings \u2014 clear all overrides"),
            "",
            "\u2699\ufe0f <b>Controls</b>",
            _cmd("/relay-reset \u2014 restart daemon (kill + respawn)"),
            _cmd("/status \u2014 health, tasks, sessions"),
            _cmd("/cancel \u2014 stop active task"),
            _cmd("/help \u2014 this menu"),
            "",
            "<i>All other messages are forwarded to Mac Claude Code.</i>",
            "<i>Messages sent mid-turn are queued and processed after the current turn.</i>",
            "",
            "<i>Note: Desktop CLI slashes (<code>/cost</code>, <code>/clear</code>, <code>/compact</code>, etc.) are interactive-REPL only \u2014 type them in Desktop Claude.app, not here.</i>",
        ]
    else:
        # ── NON-RELAY CHAT MENU ──
        lines = [
            "<b>\U0001f4cb Commands</b>",
            "",
            _cmd("/status \u2014 health, tasks, sessions"),
            _cmd("/cancel \u2014 stop active task"),
            _cmd("/continue \u2014 resume paused task"),
            _cmd("/onboarding \u2014 setup wizard / progress"),
            _cmd("/help \u2014 this menu"),
        ]
        # Self-service commands (shown when user has auth command access)
        if _can_auth and not _can_admin:
            lines.extend([
                "",
                "\U0001f464 <b>Your Account</b>",
                _cmd("/claude-auth LABEL \u2014 set up Claude access"),
                _cmd("/claude-set-oauth LABEL \u2014 switch your OAuth profile"),
                _cmd("/google-auth EMAIL \u2014 connect Google account"),
                _cmd("/m365-auth \u2014 connect Microsoft 365"),
                _cmd("/github-auth \u2014 connect GitHub account"),
                _cmd("/revoke TYPE [label] \u2014 remove credentials"),
                _cmd("/reset \u2014 reset your session"),
            ])
        if _can_admin:
            lines.extend([
                "",
                "\U0001f512 <b>Admin</b>",
                _cmd("/chats \u2014 all chats with models + OAuth"),
                _cmd("/usage [hours] \u2014 cost breakdown (1h/5h/24h/7d)"),
                _cmd("/claude-accounts \u2014 OAuth profiles"),
                _cmd("/claude-auth LABEL \u2014 obtain new OAuth token"),
                _cmd("/m365-auth \u2014 M365 device code reauth"),
                _cmd("/google-auth \u2014 Google OAuth code-paste flow"),
                _cmd("/delete-chat N \u2014 remove external chat #N"),
                _cmd("/diagnose \u2014 full self-diagnostics"),
                _cmd("/reset \u2014 reset current SDK session"),
                _cmd("/deploy [no-qa|force] \u2014 authorize deployment"),
                _cmd("/rollback [confirm] \u2014 revert to last healthy deploy"),
                _cmd("/harnesses [label] \u2014 list harness profiles"),
                _cmd("/set-harness LABEL \u2014 assign harness to this chat"),
                _cmd("/permissions [ID] \u2014 audit user/chat permissions"),
                _cmd("/claude-set-oauth LABEL [all] \u2014 switch OAuth profile"),
            ])
            if desktop_relay.is_enabled():
                lines.extend([
                    "",
                    "\U0001f5a5 <b>Mac</b>",
                    _cmd("/auth XXXXXX \u2014 TOTP authenticate to Mac"),
                    _cmd("/mac \u2014 Mac status (online, auth, daemons)"),
                    _cmd("/lock \u2014 revoke Mac auth session"),
                    "",
                    "\U0001f517 <b>Relays</b>",
                    _cmd("/mac-sessions \u2014 Mac projects (numbered)"),
                    _cmd("/create-relay N \u2014 create relay for project #N"),
                    _cmd("/delete-relay N \u2014 remove relay #N"),
                    _cmd("/relays \u2014 active relays with status + OAuth"),
                    _cmd("/relay-reset N \u2014 restart daemon #N"),
                    _cmd("/relay-reap \u2014 clean up dead daemon PIDs"),
                ])
            lines.extend([
                "",
                "<i>All other messages are processed by AI.</i>",
            ])
    await _send_to_platform_simple(source, chat_id, "\n".join(lines), parse_mode="HTML", message_thread_id=message_thread_id)


async def _lightweight_mac_sessions(source: str, chat_id: str, user_id: str,
                                    *, message_thread_id: int | None = None):
    """Admin DM: list ALL Mac JSONL sessions across ALL projects, numbered.

    Cross-project view powered by desktop_relay.list_all_jsonls(). Output
    is grouped by project, newest session first within each project.
    """
    if not await _check_command_access(source, chat_id, user_id, "mac_sessions"):
        await _deny_command(source, chat_id, "mac_sessions", message_thread_id=message_thread_id)
        return
    from bot import desktop_relay
    from html import escape as _esc
    if not desktop_relay.is_enabled():
        await _send_to_platform_simple(source, chat_id,
            "\u274c Employee workstation control disabled.", message_thread_id=message_thread_id)
        return
    jsonls = await desktop_relay.list_all_jsonls(limit=30)
    if not jsonls:
        await _send_to_platform_simple(source, chat_id,
            "\u26a0\ufe0f No JSONL sessions found. Mac may be offline or "
            "TOTP expired.", parse_mode="HTML", message_thread_id=message_thread_id)
        return
    # Group by project_path for readability.
    by_project: dict[str, list[dict]] = {}
    for jl in jsonls:
        by_project.setdefault(jl["project_path"], []).append(jl)
    # Check which projects already have relays
    relay_projects: set[str] = set()
    try:
        reg = await desktop_relay.get_all_links()
        for entry in reg.values():
            pp = entry.get("project_path", "")
            if pp and entry.get("relay_mode"):
                relay_projects.add(pp)
    except Exception as _exc:
        log.debug("Suppressed: %s", _exc)
    lines = [f"\U0001f5a5\ufe0f <b>Mac Sessions</b> "
             f"({len(by_project)} projects, {len(jsonls)} JSONLs):", ""]
    proj_idx = 1
    for proj in sorted(by_project.keys()):
        items = by_project[proj]
        proj_short = proj.rstrip("/").split("/")[-1]
        has_relay = proj in relay_projects
        relay_tag = " \U0001f517" if has_relay else ""
        lines.append(f" {proj_idx}. <b>{_esc(proj_short)}</b>{relay_tag}")
        for jl in items[:3]:  # Show max 3 JSONLs per project
            size_kb = max(jl["size"] // 1024, 1)
            lines.append(
                f"    <code>{jl['uuid'][:8]}</code>\u2026 "
                f"{size_kb} KB, {_esc(jl['mtime_str'])}"
            )
        if len(items) > 3:
            lines.append(f"    <i>...+{len(items) - 3} more</i>")
        proj_idx += 1
        lines.append("")
    lines.append(
        "<i>/create-relay N \u2014 attach project #N as relay</i>"
    )
    await _send_to_platform_simple(source, chat_id, "\n".join(lines),
                                    parse_mode="HTML", message_thread_id=message_thread_id)


async def _session_set_override(source: str, chat_id: str,
                                field: str, value: str | bool | None) -> None:
    """Set or clear a relay override field in the registry."""
    from bot.relay.registry import (
        _registry_lock, _load_registry, _save_registry,
    )
    async with _registry_lock:
        registry = _load_registry()
        key = str(chat_id)
        if key not in registry:
            await _send_to_platform_simple(source, chat_id,
                "\u274c No registry entry for this chat.")
            return
        if value is None:
            registry[key].pop(field, None)
            action = "cleared"
            display = "(explicitly add)"
        else:
            registry[key][field] = value
            action = "set"
            display = str(value)
        _save_registry(registry)
    label = field.replace("_override", "").replace("_", " ")
    await _send_to_platform_simple(source, chat_id,
        f"\u2705 Relay {label} {action}: <code>{_html.escape(display)}</code>\n"
        f"Takes effect on next daemon respawn. Use /relay-reset to respawn now.",
        parse_mode="HTML")


async def _lightweight_session(source: str, chat_id: str, user_id: str,
                                args_str: str,
                                *, message_thread_id: int | None = None):
    """Relay-chat /session picker: show binding, list JSONL
    sessions in the project, re-pin to a different session UUID.

    Admin-only. Active only inside a chat that has a relay binding.
    Subcommands:
      (none) / info    — show current project + pinned UUID
      list             — list recent JSONL sessions in the project
      pin <uuid-prefix>— re-pin the chat to a different session (≥4 hex chars)
    """
    if not await _check_command_access(source, chat_id, user_id, "session"):
        await _deny_command(source, chat_id, "session", message_thread_id=message_thread_id)
        return
    from bot import desktop_relay
    if not desktop_relay.is_enabled():
        await _send_to_platform_simple(source, chat_id,
            "\u274c Employee workstation control disabled.", message_thread_id=message_thread_id)
        return
    entry = desktop_relay.get_relay_mode(chat_id)
    if not entry:
        # Non-relay admin DM: route bare "/session" (or "info") to the
        # cross-project Mac sessions view. Subcommands like `pin <N>`
        # or `list <project>` only make sense inside a bound relay chat —
        # reject them with a clear message instead of silently showing
        # the list and appearing to ignore the args.
        _args = (args_str or "").strip().lower()
        if _args in ("", "info", "list"):
            await _lightweight_mac_sessions(source, chat_id, user_id, message_thread_id=message_thread_id)
            return
        await _send_to_platform_simple(
            source, chat_id,
            "\u2139\ufe0f /session "
            f"{_html.escape(args_str or '')} only works inside a "
            "relay chat (needs a session binding). Use /sessions "
            "here to see all Mac sessions across projects.",
            parse_mode="HTML", message_thread_id=message_thread_id,
        )
        return

    import re as _re
    args = (args_str or "").strip().split(None, 1)
    sub = args[0].lower() if args else ""

    project_path = entry.get("project_path", "")
    pinned = entry.get("pinned_jsonl_path", "") or ""
    session_name = entry.get("session_name", "")
    m = _re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
        pinned,
    )
    cur_uuid = m.group(1) if m else ""

    def _esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    if not sub or sub == "info":
        # Show overrides if any
        ovr_lines: list[str] = []
        m_ovr = entry.get("model_override", "")
        e_ovr = entry.get("effort_override", "")
        c_1m = entry.get("context_1m", False)
        if m_ovr or e_ovr or c_1m:
            ovr_parts: list[str] = []
            if m_ovr:
                ovr_parts.append(f"model={_esc(m_ovr)}")
            if c_1m:
                ovr_parts.append("context=1m")
            if e_ovr:
                ovr_parts.append(f"effort={_esc(e_ovr)}")
            ovr_lines.append(f"Overrides: <code>{' '.join(ovr_parts)}</code>")
        lines = [
            "\U0001f4ce <b>Relay session info</b>",
            f"Session: <code>{_esc(session_name)}</code>",
            f"Project: <code>{_esc(project_path)}</code>",
            f"Pinned UUID: <code>{_esc(cur_uuid) or '(none)'}</code>",
            *ovr_lines,
            "",
            "<i>Commands:</i>",
            "  /session list \u2014 recent JSONL sessions",
            "  /session pin &lt;N&gt; \u2014 re-pin by list number",
            "  /session model &lt;name&gt; \u2014 set model override",
            "  /session effort &lt;level&gt; \u2014 set effort override",
            "  /session context 1m|200k \u2014 toggle 1M context",
            "  /session settings \u2014 show effective settings",
            "  /session reset-settings \u2014 clear all overrides",
        ]
        await _send_to_platform_simple(source, chat_id, "\n".join(lines),
                                        parse_mode="HTML", message_thread_id=message_thread_id)
        return

    if sub == "list":
        jsonls = await desktop_relay.list_project_jsonls(project_path, limit=15)
        if not jsonls:
            await _send_to_platform_simple(source, chat_id,
                "\u26a0\ufe0f No JSONL sessions found for this project. "
                "Mac may be offline or TOTP expired.", parse_mode="HTML", message_thread_id=message_thread_id)
            return
        lines = [f"\U0001f4cb <b>Sessions in <code>{_esc(project_path)}</code>:</b>",
                 ""]
        for i, jl in enumerate(jsonls, 1):
            uuid = jl["uuid"]
            size_kb = max(jl["size"] // 1024, 1)
            marker = " \U0001f4cc" if uuid == cur_uuid else ""
            lines.append(
                f"{i}. <code>{uuid[:8]}</code>\u2026{marker} "
                f"\u2014 {size_kb} KB, {_esc(jl['mtime_str'])}"
            )
        lines.extend([
            "",
            "<i>Re-pin by number:</i> /session pin 1 "
            "(or /session pin &lt;short-uuid&gt;)",
        ])
        await _send_to_platform_simple(source, chat_id, "\n".join(lines),
                                        parse_mode="HTML", message_thread_id=message_thread_id)
        return

    if sub == "pin":
        if len(args) < 2 or not args[1].strip():
            await _send_to_platform_simple(source, chat_id,
                "Usage: <code>/session pin</code> &lt;N&gt; (list number) or "
                "<code>/session pin</code> &lt;uuid-prefix&gt; (at least 4 hex chars)",
                parse_mode="HTML", message_thread_id=message_thread_id)
            return
        token = args[1].strip().lower()
        # List of recent sessions (used for both number-index and uuid-prefix matching).
        # limit=15 matches what `/session list` displays so number-index is consistent.
        jsonls = await desktop_relay.list_project_jsonls(project_path, limit=15)
        # Number shortcut: pin by list index (1-based) as shown in `/session list`.
        if token.isdigit():
            idx = int(token)
            if idx < 1 or idx > len(jsonls):
                await _send_to_platform_simple(source, chat_id,
                    f"\u274c Number <code>{idx}</code> out of range "
                    f"(1\u2013{len(jsonls)}). Try /session list.",
                    parse_mode="HTML", message_thread_id=message_thread_id)
                return
            target_uuid = jsonls[idx - 1]["uuid"]
        else:
            if len(token) < 4:
                await _send_to_platform_simple(source, chat_id,
                    "\u274c UUID prefix must be at least 4 hex chars.",
                    parse_mode="HTML", message_thread_id=message_thread_id)
                return
            if not _re.fullmatch(r"[0-9a-f-]+", token):
                await _send_to_platform_simple(source, chat_id,
                    "\u274c Invalid UUID prefix (hex digits + dashes only).",
                    parse_mode="HTML", message_thread_id=message_thread_id)
                return
            # Widen search to 50 for prefix matching in case target is older than
            # the displayed 15.
            jsonls_wide = await desktop_relay.list_project_jsonls(project_path, limit=50)
            matches = [jl for jl in jsonls_wide if jl["uuid"].startswith(token)]
            if not matches:
                await _send_to_platform_simple(source, chat_id,
                    f"\u274c No session with prefix <code>{_esc(token)}</code>. "
                    f"Try /session list.", parse_mode="HTML", message_thread_id=message_thread_id)
                return
            if len(matches) > 1:
                lines = [f"\u26a0\ufe0f Multiple matches for "
                         f"<code>{_esc(token)}</code> \u2014 be more specific:"]
                for jl in matches[:6]:
                    lines.append(f"  <code>{jl['uuid']}</code>")
                await _send_to_platform_simple(source, chat_id, "\n".join(lines),
                                                parse_mode="HTML", message_thread_id=message_thread_id)
                return
            target_uuid = matches[0]["uuid"]
        result = await desktop_relay.update_pinned_jsonl(chat_id, target_uuid)
        if result.get("success"):
            # Force a clean respawn so the next message attaches a fresh daemon
            # to the new UUID — daemon's __init__ then emits a "first_run"
            # catchup for the new JSONL. Without this, the live daemon keeps
            # running on the OLD uuid until manually killed and the new pin
            # has no visible effect.
            kill_status = ""
            try:
                mac_kill = await desktop_relay.kill_daemon(chat_id)
                from bot.relay.pool import get_pool
                await get_pool().release(chat_id)
                # Mark this chat as recently re-pinned so the upcoming catchup
                # event is rendered with a "Re-pinned" header rather than the
                # generic "New relay chat" header.
                from main import mark_relay_repinned
                mark_relay_repinned(chat_id)
                # Eager-respawn — pin's primary value is the delta catchup sync
                # for the new JSONL; lazy-spawn would delay that until the
                # user's next message. Registry already has the new uuid from
                # the earlier `pin_session` call, so eager_respawn_daemon picks
                # it up correctly. Fire-and-forget; offline Mac logs a warning.
                respawn_note = ""
                try:
                    respawn = await desktop_relay.eager_respawn_daemon(chat_id)
                    if respawn.get("ok"):
                        respawn_note = " + respawning now (catchup in ~5-15s)"
                    elif respawn.get("reason") == "missing_uuid_or_cwd":
                        respawn_note = (
                            " (registry uuid/cwd missing \u2014 lazy-spawn "
                            "on next message)"
                        )
                except Exception as _re:
                    log.warning("session_pin: eager_respawn failed chat=%s: %s",
                                chat_id, _re)
                if mac_kill.get("ok"):
                    kill_status = (
                        f"\nKilled old daemon (pid {mac_kill.get('killed_pid')})"
                        f"{respawn_note}."
                    )
                else:
                    # No live daemon (or kill failed) — pool released anyway.
                    kill_status = f"\nNo live daemon to kill (pool released){respawn_note}."
            except Exception as _ke:
                log.warning("session_pin: daemon kill/release failed chat=%s: %s",
                            chat_id, _ke)
                kill_status = (f"\n\u26a0\ufe0f Daemon kill failed "
                               f"(<code>{_esc(str(_ke)[:120])}</code>) \u2014 "
                               f"send a new message to force respawn.")
            await _send_to_platform_simple(source, chat_id,
                f"\u2705 Re-pinned to <code>{target_uuid}</code>\n"
                f"Path: <code>{_esc(result.get('new_path',''))}</code>"
                f"{kill_status}",
                parse_mode="HTML", message_thread_id=message_thread_id)
        else:
            await _send_to_platform_simple(source, chat_id,
                f"\u274c Re-pin failed: {_esc(result.get('error','unknown'))}",
                parse_mode="HTML", message_thread_id=message_thread_id)
        return

    # --- Relay override subcommands ---

    if sub == "model":
        val = args[1].strip() if len(args) > 1 else ""
        if not val:
            await _send_to_platform_simple(source, chat_id,
                "Usage: <code>/session model opus</code> (or sonnet, claude-opus-4-6, etc.)\n"
                "<code>/session model clear</code> to remove override",
                parse_mode="HTML", message_thread_id=message_thread_id)
            return
        if val != "clear" and not _MODEL_OVERRIDE_RE.fullmatch(val):
            await _send_to_platform_simple(source, chat_id,
                "\u274c Invalid model name. Use alphanumeric, dash, dot, "
                "brackets only (max 64 chars).",
                parse_mode="HTML", message_thread_id=message_thread_id)
            return
        await _session_set_override(source, chat_id,
                                    "model_override", None if val == "clear" else val)
        return

    if sub == "effort":
        val = args[1].strip().lower() if len(args) > 1 else ""
        valid_efforts = ("low", "medium", "high", "xhigh", "clear")
        if val not in valid_efforts:
            await _send_to_platform_simple(source, chat_id,
                "Usage: <code>/session effort high</code>\n"
                "Values: low, medium, high, xhigh, clear",
                parse_mode="HTML", message_thread_id=message_thread_id)
            return
        await _session_set_override(source, chat_id,
                                    "effort_override", None if val == "clear" else val)
        return

    if sub == "context":
        val = (args[1].strip().lower() if len(args) > 1 else "").replace(" ", "")
        if val == "1m":
            await _session_set_override(source, chat_id, "context_1m", True)
            # Warn if no model override set — context_1m needs model to attach [1m] to
            if not entry.get("model_override"):
                await _send_to_platform_simple(source, chat_id,
                    "\u26a0\ufe0f <b>Note:</b> 1M context requires a model override. "
                    "Set one with <code>/session model opus</code> — the [1m] suffix "
                    "will be appended automatically.",
                    parse_mode="HTML", message_thread_id=message_thread_id)
        elif val in ("200k", "default", "clear"):
            await _session_set_override(source, chat_id, "context_1m", None)
        else:
            await _send_to_platform_simple(source, chat_id,
                "Usage: <code>/session context 1m</code> or "
                "<code>/session context 200k</code>",
                parse_mode="HTML", message_thread_id=message_thread_id)
        return

    if sub == "settings":
        # Show effective settings (overrides + explicitly added from Mac)
        m_ovr = entry.get("model_override", "")
        e_ovr = entry.get("effort_override", "")
        c_1m = entry.get("context_1m", False)
        lines = ["\u2699\ufe0f <b>Relay settings</b>"]
        lines.append(f"Model override: <code>{_esc(m_ovr) or '(explicitly add)'}</code>")
        lines.append(f"Context: <code>{'1M' if c_1m else '200k (default)'}</code>")
        lines.append(f"Effort override: <code>{_esc(e_ovr) or '(explicitly add)'}</code>")
        # Try to show live daemon settings for comparison
        daemon_found = False
        try:
            from bot.relay.sessions import list_daemons
            result = await list_daemons()
            if result.get("ok"):
                for d in result.get("daemons", []):
                    if d.get("chat_id") == str(chat_id):
                        daemon_found = True
                        d_model = d.get("model")
                        d_effort = d.get("effort")
                        # Fallback: if daemon reports no model, show override as effective
                        if not d_model and m_ovr:
                            d_model = f"{m_ovr} (from override)"
                        if not d_effort and e_ovr:
                            d_effort = f"{e_ovr} (from override)"
                        lines.append("")
                        lines.append(f"<i>Live daemon ({d.get('status', '?')}):</i>")
                        lines.append(f"  model: <code>{_esc(d_model or '(pending — will detect on first query)')}</code>")
                        lines.append(f"  effort: <code>{_esc(d_effort or '(default)')}</code>")
                        break
        except Exception as _exc:
            log.debug("Suppressed: %s", _exc)
        if not daemon_found:
            lines.append("")
            lines.append("<i>No live daemon (will spawn on next message).</i>")
        lines.append("")
        lines.append("<i>Changes take effect on next daemon respawn.</i>")
        await _send_to_platform_simple(source, chat_id, "\n".join(lines),
                                        parse_mode="HTML", message_thread_id=message_thread_id)
        return

    if sub == "reset-settings":
        from bot.relay.registry import (
            _registry_lock, _load_registry, _save_registry,
        )
        async with _registry_lock:
            registry = _load_registry()
            key = str(chat_id)
            if key in registry:
                for field in ("model_override", "effort_override", "context_1m"):
                    registry[key].pop(field, None)
                _save_registry(registry)
        await _send_to_platform_simple(source, chat_id,
            "\u2705 All relay overrides cleared. Daemon will use Mac defaults "
            "on next respawn.\n\nUse /relay-reset to respawn now.",
            parse_mode="HTML", message_thread_id=message_thread_id)
        return

    await _send_to_platform_simple(source, chat_id,
        f"Unknown subcommand: <code>{_esc(sub)}</code>. Try <code>/session</code>, "
        f"<code>/session list</code>, or <code>/session pin</code> &lt;uuid&gt;",
        parse_mode="HTML", message_thread_id=message_thread_id)


async def _lightweight_status(source: str, chat_id: str,
                              user_id: str = "",
                              *, message_thread_id: int | None = None):
    """Instant bot status without touching the SDK. Responds in <1s.

    Non-admins see only: uptime, their own session, task count (no details).
    Admins see everything: all sessions, all tasks, harnesses, OAuth profiles.
    """
    import config
    from config import (
        BOT_STARTED, ORG_TIMEZONE,
    )
    import bot.chat_registry as _cr
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(ORG_TIMEZONE)
    from bot.agent import client_pool
    from main import task_manager, persistent_queue

    # RBAC + chat isolation: only admin in private DM gets global view.
    # Admin in group/topic sees only their own chat's sessions + tasks.
    _is_admin_status = await _has_rbac_capability(source, user_id, "commands", "chats", chat_id=chat_id)
    _own_session_key = f"{source}:{chat_id}"
    try:
        _is_private = int(chat_id) > 0
    except (ValueError, TypeError):
        _is_private = False  # fail-closed: non-integer chat_id = not private DM
    _global_scope = _is_admin_status and _is_private

    pool = client_pool.get_status()
    if not _global_scope:
        # Filter pool to only caller's own session (+ topics under same chat)
        pool = {k: v for k, v in pool.items() if k == _own_session_key or k.startswith(f"{_own_session_key}:")}
    active = task_manager.get_active_tasks()
    if not _global_scope:
        # Filter tasks to own chat only
        active = [t for t in active if f"{t.source}:{t.chat_id}" == _own_session_key]
    pq_count = await persistent_queue.get_pending_count() if persistent_queue else 0
    uptime_min = int((datetime.now(TZ) - BOT_STARTED.astimezone(TZ)).total_seconds() / 60)

    live_count = sum(1 for v in pool.values() if v.get("connected"))
    disconn_count = len(pool) - live_count
    session_summary = f"{live_count} live"
    if disconn_count:
        session_summary += f", {disconn_count} disconnected"
    # Rate limit summary — grouped by OAuth profile
    from bot.agent import get_rate_limit_state
    rl_state = get_rate_limit_state()
    rl_lines = []
    # Group by oauth_label
    rl_by_profile: dict[str, list[str]] = {}
    for rl_key, rl_val in rl_state.items():
        util = rl_val.get("utilization")
        if util is not None:
            icon = "\U0001f534" if util >= 0.9 else "\u26a0\ufe0f" if util >= 0.75 else "\U0001f7e2"
            profile = rl_val.get("oauth_label", "default")
            rl_type = rl_val.get("rate_limit_type", rl_key)
            rl_by_profile.setdefault(profile, []).append(f"{icon} {rl_type}: {util:.0%}")
    from html import escape as _esc
    for profile, items in sorted(rl_by_profile.items()):
        rl_lines.append(f"  <b>{_esc(profile)}</b>: {', '.join(items)}")

    from config import get_model_primary, get_chat_oauth_overrides, get_chat_model_overrides
    from bot.harness import get_chat_assignments, get_chat_overrides as _get_chat_ov, get_chat_harness_label
    current_model = get_model_primary()
    oauth_ov = get_chat_oauth_overrides()

    def _chat_label(src: str, cid: str, topic_id: str = "") -> str:
        if src == "telegram":
            int_cid = int(cid) if cid.lstrip("-").isdigit() else 0
            # Check topic entry first (e.g. telegram:-100XXX:7)
            if topic_id:
                _topic_entry = _cr.get(f"telegram:{int_cid}:{topic_id}")
                if _topic_entry:
                    _title = _topic_entry.get("title", f"topic {topic_id}")
                    return f"\U0001f4ac {_esc(str(_title))}"
                # Unregistered topic: show parent name + topic ID to distinguish
                _parent_entry = _cr.get(f"telegram:{int_cid}")
                _parent_name = _parent_entry.get("title", cid) if _parent_entry else cid
                return f"\U0001f4ac {_esc(str(_parent_name))} \u203a #{topic_id}"
            if int_cid == config.TG_CHAT_ID:
                return "\U0001f4e2 Notifications"
            _entry = _cr.get(f"telegram:{int_cid}")
            if _entry:
                _title = _entry.get("title", cid)
                return f"\U0001f50d {_esc(str(_title))}"
            return f"TG {cid}"
        if src == "whatsapp":  # legacy — WhatsApp removed
            return f"WA {cid[:20]}"
        if src == "system":
            return "\u2699\ufe0f Scheduled"
        return f"{src}:{cid}"

    def _chat_key_label(ck: str) -> str:
        parts = ck.split(":", 1)
        if len(parts) == 2:
            # Normalize short prefixes: tg → telegram, wa → whatsapp
            src = parts[0]
            if src == "tg":
                src = "telegram"
            elif src == "wa":
                src = "whatsapp"
            return _chat_label(src, parts[1])
        return ck

    model_ov_status = get_chat_model_overrides()
    model_line = f"\U0001f916 Model: <code>{_format_model_short(current_model)}</code>"
    if model_ov_status:
        m_parts = [f"  \u2022 {_chat_key_label(k)} \u2192 <code>{_esc(_format_model_short(v))}</code>"
                    for k, v in sorted(model_ov_status.items())]
        model_line += "\n" + "\n".join(m_parts)
    harness_assigns = get_chat_assignments()
    if harness_assigns:
        h_parts = []
        for ck, hl in sorted(harness_assigns.items()):
            if not hl:
                continue
            name = _chat_key_label(ck)
            ov = _get_chat_ov(ck)
            ov_str = ""
            if ov:
                ov_items = [f"{_esc(str(k))}={_esc(str(v))}" for k, v in ov.items()]
                ov_str = f" <i>+{', '.join(ov_items)}</i>"
            h_parts.append(f"  \u2022 {name} \u2192 <code>{hl}</code>{ov_str}")
        if h_parts:
            model_line += "\n\U0001f39b Harnesses:\n" + "\n".join(h_parts)

    # Show OAuth: list profiles + any per-chat overrides
    from config import list_oauth_profiles
    profiles = list_oauth_profiles()
    profile_names = [
        f"\u2b50{p['label']}" if p.get("is_default") else p["label"]
        for p in profiles if p.get("exists")
    ]
    oauth_line = f"\n\U0001f511 OAuth: {', '.join(profile_names) if profile_names else 'default only'}"
    if oauth_ov:
        oauth_parts = [f"  \u2022 {_chat_key_label(k)} \u2192 {v}" for k, v in oauth_ov.items()]
        oauth_line += "\n" + "\n".join(oauth_parts)

    if _global_scope:
        lines = [
            f"\u2705 <b>Bot online</b> \u2014 uptime {uptime_min}m",
            f"\U0001f4cb Tasks: {len(active)} active, {pq_count} queued",
            f"\U0001f9e0 Sessions: {session_summary}",
            model_line + oauth_line,
        ]
    else:
        # Non-admin: minimal info — no session counts, no OAuth profiles, no harnesses
        lines = [
            f"\u2705 <b>Bot online</b> \u2014 uptime {uptime_min}m",
            f"\U0001f4cb Your tasks: {len(active)} active",
        ]
    if rl_lines and _is_admin_status:
        lines.append("\U0001f4ca Rate limits:")
        lines.extend(rl_lines)

    # --- Build chat-grouped tree ---
    # (_chat_label and _esc already defined above)

    # Group sessions by (source, chat_id) — topics grouped under parent
    chat_sessions: dict[tuple[str, str], list[tuple[str, dict, str]]] = {}
    for skey, info in pool.items():
        parts = skey.split(":")
        src = parts[0] if parts else "?"
        cid = parts[1] if len(parts) > 1 else "?"
        topic_id = parts[2] if len(parts) > 2 else ""
        display_key = (src, cid, topic_id)  # include topic for labeling
        chat_sessions.setdefault(display_key, []).append((skey, info, "main"))

    # Index active tasks by (source, chat_id)
    task_by_chat: dict[tuple[str, str], list] = {}
    for t in active:
        task_by_chat.setdefault((t.source, str(t.chat_id)), []).append(t)

    if chat_sessions:
        lines.append("")
        # Sort: group parent chats first, topics nested under them
        sorted_chats = sorted(
            chat_sessions.keys(),
            key=lambda k: (0 if (config.TG_CHAT_ID and k[1] == str(config.TG_CHAT_ID)) else 1, k[1], k[2]),
        )
        _last_parent = None
        for chat_key in sorted_chats:
            src, cid, topic_id = chat_key
            sessions = chat_sessions[chat_key]
            # Hierarchical: show parent group header, indent topics
            _skey_full = f"{src}:{cid}:{topic_id}" if topic_id else f"{src}:{cid}"
            if topic_id:
                # Topic — show indented under parent
                if _last_parent != (src, cid):
                    # Parent header not yet shown — show it
                    parent_label = _chat_label(src, cid)
                    lines.append(f"<b>{parent_label}</b>")
                    _last_parent = (src, cid)
                topic_label = _chat_label(src, cid, topic_id)
                h_label = get_chat_harness_label(_skey_full)
                h_tag = f" <code>[{_esc(h_label)}]</code>" if h_label else ""
                indent = "  "
                lines.append(f"{indent}<b>{topic_label}</b>{h_tag}")
            else:
                label = _chat_label(src, cid)
                h_label = get_chat_harness_label(_skey_full)
                h_tag = f" <code>[{_esc(h_label)}]</code>" if h_label else ""
                lines.append(f"<b>{label}</b>{h_tag}")
                _last_parent = (src, cid)

            chat_tasks = task_by_chat.get((src, cid), [])

            for skey, info, _kind in sessions:
                icon = "\U0001f7e2" if info.get("connected") else "\u26aa"
                idle = info.get("idle_min", 0)
                queries = info.get("queries", 0)
                cost = info.get("cost_usd", 0)
                cost_str = f" ${cost:.2f}" if cost > 0 else ""

                active_task = next((t for t in chat_tasks), None)
                if active_task:
                    lines.append(f"  {icon} q={queries}{cost_str} \u26a1 {active_task.phase} {active_task.elapsed()}")
                    lines.append(f"      \u2514 {_esc(active_task.text[:35])}")
                else:
                    idle_str = f"{idle}m idle" if idle > 0 else "ready"
                    lines.append(f"  {icon} q={queries}{cost_str} {idle_str}")

    # Active tasks without a matching session (queued / pre-session)
    shown_ids: set[str] = set()
    for chat_key in chat_sessions:
        for t in task_by_chat.get(chat_key, []):
            shown_ids.add(t.task_id)
    unmatched = [t for t in active if t.task_id not in shown_ids]
    if unmatched:
        lines.append("")
        lines.append("<b>\u23f3 Pending</b>")
        for t in unmatched[:5]:
            lines.append(f"  \u2022 <code>{t.task_id[:8]}</code> {_esc(t.text[:40])} ({t.phase}, {t.elapsed()})")

    await _send_to_platform_simple(source, chat_id, "\n".join(lines), parse_mode="HTML", message_thread_id=message_thread_id)


async def _handle_reset_session(source: str, chat_id: str, user_id: str, text: str,
                                *, message_thread_id: int | None = None):
    """Reset SDK session. Admin can reset any chat; employees can reset their own.

    Usage:
      "reset"                    — reset current chat's session
      "reset <chat_id>"          — reset a specific chat's TG session (admin only)
      "reset telegram:<chat_id>" — reset a specific session key (admin only)
    """
    if not await _check_command_access(source, chat_id, user_id, "reset"):
        await _deny_command(source, chat_id, "reset", message_thread_id=message_thread_id)
        return

    _can_reset_others = await _has_rbac_capability(source, user_id, "commands", "chats", chat_id=chat_id)  # admin-level
    from bot.agent import client_pool
    match = _RESET_RE.match(text)
    target = (match.group(1) or "").strip() if match else ""

    if target:
        # Only users with admin capability can reset other sessions
        if not _can_reset_others:
            await _send_to_platform_simple(source, chat_id,
                "\u26d4 Employees can only reset their own session. Use <code>/reset</code> without arguments.",
                parse_mode="HTML", message_thread_id=message_thread_id)
            return
        # If target contains ":", treat as full session_key; otherwise assume telegram:<id>
        if ":" in target:
            session_key = target
        else:
            session_key = f"telegram:{target}"
    else:
        session_key = f"{source}:{chat_id}"

    from html import escape as _esc

    try:
        # Check if session exists
        pool_status = client_pool.get_status()
        existed = session_key in pool_status
        saved_sid = await client_pool.get_session_id(session_key)

        await client_pool.reset_session(session_key)
    except Exception as e:
        log.error(f"Session reset failed for {session_key}: {e}", exc_info=True)
        await _send_to_platform_simple(source, chat_id, f"\u274c Reset failed: {_esc(str(e))}", parse_mode="HTML", message_thread_id=message_thread_id)
        return

    sk_safe = _esc(session_key)
    parts = [f"\U0001f504 Session reset: <code>{sk_safe}</code>"]
    if existed:
        parts.append("\u2022 SDK client disconnected + removed")
    if saved_sid:
        parts.append(f"\u2022 Saved session_id cleared (<code>{_esc(saved_sid[:8])}...</code>)")
    if not existed and not saved_sid:
        parts.append("\u2022 (no active session found \u2014 nothing to reset)")

    # Clear any pending auth flows for this session key
    _cleared_auths = []
    # Extract chat-level key from session_key (telegram:12345 → ("telegram", "12345"))
    _sk_parts = session_key.split(":")
    _auth_key = (_sk_parts[0], _sk_parts[1]) if len(_sk_parts) >= 2 else None
    if _auth_key:
        _g_entry = _pending_google_auth.pop(_auth_key, None)
        if _g_entry:
            _g_nonce = _g_entry.get("nonce")
            if _g_nonce:
                _pending_google_auth.pop(f"nonce:{_g_nonce}", None)
            _cleared_auths.append("Google")
        _m365_entry = _pending_m365_auth.pop(_auth_key, None)
        if _m365_entry:
            _m365_entry["done"] = True
            _t = _m365_entry.get("task")
            if _t and not _t.done():
                _t.cancel()
            _cleared_auths.append("M365")
        _gh_entry = _pending_github_auth.pop(_auth_key, None)
        if _gh_entry:
            _gh_entry["done"] = True
            _t = _gh_entry.get("task")
            if _t and not _t.done():
                _t.cancel()
            _cleared_auths.append("GitHub")
    if _cleared_auths:
        parts.append(f"\u2022 Cleared stuck auth flows: {', '.join(_cleared_auths)}")

    parts.append("Next message to that chat will start a fresh session.")

    await _send_to_platform_simple(source, chat_id, "\n".join(parts), parse_mode="HTML", message_thread_id=message_thread_id)
    log.info(f"Admin session reset: {session_key} by {user_id} (existed={existed}, had_sid={bool(saved_sid)}, cleared_auth={_cleared_auths})")


async def run_self_diagnose(source: str, chat_id: str,
                            *, message_thread_id: int | None = None):
    """Run self-diagnostics — gather data, format as HTML, send directly.

    No LLM session needed: diagnostics are structured data, not judgment.
    Avoids the context-overflow bug where the system prompt + facts + diagnostic
    dump exceeded 200K tokens, causing immediate compaction that lost all data.
    """
    from config import GIT_COMMIT, ORG_TIMEZONE, DATA_DIR
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo(ORG_TIMEZONE)
    LOG_DIR = Path(os.environ.get("LOG_DIR", DATA_DIR / "logs"))

    log.info("Running self-diagnosis...")
    from bot.agent import client_pool
    import subprocess
    from main import task_manager, persistent_queue

    sections: list[str] = ["<b>\U0001f50d Self-Diagnostics</b>", ""]

    # 1. Bot info
    sections.append(f"<b>Bot</b>: commit <code>{_html.escape(GIT_COMMIT[:12])}</code>")

    # 2. TG connection
    try:
        from integrations.telegram import is_connected as tg_is_connected
        tg_ok = tg_is_connected()
        sections.append(f"<b>Telegram</b>: {'\u2705 connected' if tg_ok else '\u274c disconnected'}")
    except Exception as e:
        sections.append(f"<b>Telegram</b>: \u26a0\ufe0f check failed ({_html.escape(str(e)[:120])})")

    # 3. Active tasks
    active = task_manager.get_active_tasks()
    if active:
        task_lines = [f"  \u2022 <code>{t.task_id[:8]}</code> {_html.escape(t.text[:50])} "
                      f"({t.phase}, {t.elapsed()})" for t in active]
        sections.append(f"<b>Active tasks</b> ({len(active)}):\n" + "\n".join(task_lines))
    else:
        sections.append("<b>Active tasks</b>: none")

    # 5. PQ pending
    pq_count = await persistent_queue.get_pending_count() if persistent_queue else 0
    if pq_count:
        sections.append(f"<b>Queue</b>: {pq_count} pending")

    # 6. Client pool
    pool_status = client_pool.get_status()
    pool_lines = [f"  \u2022 <code>{_html.escape(k)}</code>: {v['queries']}q, {v['age_min']}m"
                  for k, v in pool_status.items()]
    sections.append(f"<b>SDK clients</b> ({len(pool_status)}):\n" + "\n".join(pool_lines))

    # 7. Rate limits
    def _get_health_rate_limits() -> dict:
        try:
            from bot.agent import get_rate_limit_state
            return get_rate_limit_state()
        except Exception:
            return {}

    rl = _get_health_rate_limits()
    if rl:
        # Group by OAuth profile
        rl_by_prof: dict[str, list[str]] = {}
        for rtype, rinfo in rl.items():
            util = rinfo.get("utilization", 0)
            status = rinfo.get("status", "?")
            prof = rinfo.get("oauth_label", "default")
            rl_by_prof.setdefault(prof, []).append(f"{rinfo.get('rate_limit_type', rtype)}: {util:.0%} ({status})")
        rl_parts = [f"{prof}: {', '.join(items)}" for prof, items in sorted(rl_by_prof.items())]
        sections.append("<b>Rate limits</b>: " + " | ".join(rl_parts))

    # 8. Desktop relay
    try:
        from bot import desktop_relay
        if desktop_relay.is_enabled():
            mac = await desktop_relay.list_daemons()
            if mac.get("ok"):
                n = len(mac.get("daemons", []))
                sections.append(f"<b>Mac relay</b>: \u2705 {n} daemon(s)")
            else:
                sections.append(f"<b>Mac relay</b>: \u274c {_html.escape(str(mac.get('error', ''))[:100])}")
    except Exception as _exc:
        log.debug("Suppressed: %s", _exc)
    try:
        result = subprocess.run(
            ["df", "-h", "/app/data"],
            capture_output=True, text=True, timeout=5,
        )
        df_line = result.stdout.strip().split("\n")[-1].split()
        if len(df_line) >= 5:
            sections.append(f"<b>Disk</b>: {df_line[2]} used / {df_line[1]} total ({df_line[4]})")
    except Exception as _exc:
        log.debug("Suppressed: %s", _exc)
    error_lines: list[str] = []
    try:
        log_path = LOG_DIR / "bot.log"
        if log_path.exists():
            lines = log_path.read_text().split("\n")
            for line in lines[-200:]:
                if "[ERROR]" in line:
                    # Extract just the message part (after timestamp + level)
                    parts = line.split("[ERROR]", 1)
                    if len(parts) > 1:
                        error_lines.append(parts[1].strip()[:120])
    except Exception as _exc:
        log.debug("Suppressed: %s", _exc)
    if error_lines:
        shown = error_lines[-5:]  # last 5 errors
        sections.append("<b>Recent errors</b> (last 5):\n" +
                        "\n".join(f"  \u2022 <code>{_html.escape(e)}</code>" for e in shown))
    else:
        sections.append("<b>Recent errors</b>: none \u2705")

    msg = "\n".join(sections)
    await _send_to_platform_simple(source, chat_id, msg, parse_mode="HTML", message_thread_id=message_thread_id)
