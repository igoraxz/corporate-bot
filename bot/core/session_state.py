from __future__ import annotations

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from bot.core.access import AccessContext  # noqa: F401

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Per-session state management — extracted from bot.hooks (Phase 0).

# ⛔ NO PROCESS-GLOBAL SESSION STATE — see AP-15 in coding-principles.
# Session identity is resolved via _session_key in MCP tool args (injected
# by _wrap_tools_with_session_key) or explicit session_key parameters.
# NEVER add module-level variables to track "current" or "active" sessions.

All cross-SDK-boundary state is stored in _session_state, keyed by
session_key (format: "source:chat_id" or "source:chat_id:task-xxx").

Lookup path:
  1. Hooks: build_hooks(session_key) creates wrapper closures that
     eagerly register session_id -> session_key in _sid_to_skey.
     _get_state_for_hook() then resolves via _sid_to_skey only.
  2. MCP tools: per-client wrappers inject session_key into tool args.
  3. Callers (agent.py, handlers.py): pass session_key explicitly.
"""

import logging
from typing import Callable

log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════
# DEFAULT STATE TEMPLATE
# ═══════════════════════════════════════════════════════════════════════

_DEFAULT_STATE: dict = {
    "current_user_ctx": None,        # dict | None
    "tool_status_callback": None,    # Callable | None
    "send_tool_called": False,
    "reply_to_msg_id": None,         # int | None -- TG msg to reply to
    "task_last_sent_id": None,       # int | None -- last TG msg we sent
    "current_task_id": "",           # str
    "origin_chat_id": "",            # str
    "intentional_silence": False,
    "tool_use_counter": 0,
    "query_start_time": 0.0,           # time.time() at query start — for heartbeat nudges
    "heartbeat_count": 0,              # how many heartbeat nudges sent this query
    "deploy_session_key_flag": "",
    "deploy_confirmed_flag": False,
    "deploy_force_flag": False,      # /deploy force — skip ALL hook gates
    "deploy_noqa_flag": False,       # /deploy no-qa — skip staging QA gate
    # Budget-extension gate: set to True by set_extend_budget_state() when
    # the user's current message contains an explicit "extend budget" phrase.
    # Consumed by pre_tool_use_hook to allow the extend_budget MCP tool.
    "extend_budget_confirmed_flag": False,
    "is_external_chat": False,
    # Coding sandbox: when set, all file/bash operations are scoped to this dir.
    # Applied to ALL users (admin + non-admin) in project-scoped coding chats.
    # Set from harness project_dir when data_scope != "admin".
    "sandbox_dir": None,  # str | None
    # Active PM ticket ID for the current coding task.
    # Set by pm_auto_ticket, cleared when ticket is moved to done/dropped.
    "active_ticket_id": None,  # int | None
    # Outbound action gate (FB-73): set to True by handlers when user's
    # message contains the /send command (same pattern as /deploy).
    # Checked by PreToolUse for send_gmail_message / phone_call to prevent
    # the model from sending outbound messages without explicit user approval.
    # Reset at each task start; injection can upgrade to True mid-task.
    "outbound_approved": False,
    # Deploy review gate: set to True by deploy_review MCP tool after
    # automated checks pass. Reset to False when code is edited (Edit/Write
    # on /host-repo/). deploy_bot hook gate blocks without this flag.
    "deploy_review_passed": False,
    # Staging QA gate: set to True by staging_qa_run MCP tool after
    # QA scenarios pass against staging. Reset alongside deploy_review_passed.
    "qa_staging_passed": False,
    # Active sessions acknowledgement: set to True on first deploy_bot block
    # when other tasks are running. Resets with other deploy gates on code edit.
    "deploy_active_sessions_acked": False,
    # Parallel session awareness: tracks files modified in /host-repo/ by this
    # session.  Lazily initialized as set[str].  Used by PostToolUse hook to
    # detect when two admin sessions edit the same repo file and inject a warning.
    # Cleanup is automatic via cleanup_session_state().
    "modified_repo_files": None,  # set[str] | None
    # Pipeline Orchestrator gate: HMAC-signed approval token set by the
    # pipeline_orchestrate MCP tool after independent verification passes.
    # Stores the signed token (not a bare boolean) — verified at deploy time.
    # Reset alongside other deploy gates when code is edited.
    "orchestrator_approval_token": "",  # HMAC-signed token or empty
}

# Deploy gate flags that reset together when code is edited in /host-repo/
_DEPLOY_GATE_FLAGS = ("deploy_review_passed", "qa_staging_passed",
                      "deploy_active_sessions_acked", "orchestrator_approval_token")

# ═══════════════════════════════════════════════════════════════════════
# MODULE-LEVEL STATE DICTS
# ═══════════════════════════════════════════════════════════════════════

# Per-session state dict: session_key -> {field: value}
_session_state: dict[str, dict] = {}

# Access context registry: session_key -> AccessContext
# Set by agent.py before each query(), read by MCP memory tools.
# Stored separately from _session_state because AccessContext is a frozen dataclass
# and the _DEFAULT_STATE pattern doesn't accommodate typed objects cleanly.
_access_contexts: dict = {}  # dict[str, AccessContext] -- type hint avoids circular import

# Cross-task tracking: chat_id -> task_id of last message sender.
# Used to detect interleaving and trigger reply-threading.
_chat_last_task: dict[str, str] = {}

# Pending compaction info: session_key -> {"approx_tokens": int, "trigger": str, ...}
# Set by PreCompact hook, consumed by agent.py after query completes.
# Keyed by session_key (not session_id) because compaction creates a new session_id.
pending_compaction: dict[str, dict] = {}

# Sessions that were recently compacted. Set by agent.py after compaction is detected,
# consumed by process_incoming() on the NEXT query to inject a context-reload hint.
compacted_sessions: set[str] = set()

# Reverse map: SDK session_id -> session_key.  Updated by agent.py whenever a
# session_id is captured.  Used by PreCompact hook to find the session_key.
_sid_to_skey: dict[str, str] = {}

# Last known input token count per session_key.  Updated by agent.py on every
# ResultMessage.  Read by PreCompact hook to get accurate pre-compaction tokens
# (the SDK doesn't provide transcript_path reliably).
_last_input_tokens: dict[str, int] = {}

# ═══════════════════════════════════════════════════════════════════════
# ACCESS CONTEXT
# ═══════════════════════════════════════════════════════════════════════


def set_access_context(session_key: str, ctx) -> None:
    """Store AccessContext for a session. Called by agent.py before each query()."""
    _access_contexts[session_key] = ctx


def get_access_context(session_key: str = "") -> "AccessContext | None":
    """Get AccessContext for a session.

    Returns None if no context set (callers should treat None as NO_ACCESS).
    """
    if not session_key:
        log.error("get_access_context called without session_key -- "
                  "returning None (NO_ACCESS)", exc_info=True)
        return None
    return _access_contexts.get(session_key)


# ═══════════════════════════════════════════════════════════════════════
# STATE LOOKUP HELPERS
# ═══════════════════════════════════════════════════════════════════════


def _get_or_create_state(session_key: str) -> dict:
    """Get or create per-session state dict."""
    if session_key not in _session_state:
        _session_state[session_key] = dict(_DEFAULT_STATE)
    return _session_state[session_key]


def _get_state_for_hook(input_data: dict) -> dict:
    """Get state dict for a hook call, resolving session_id -> session_key.

    SECURITY: The wrapper closures in build_hooks() eagerly register
    session_id -> session_key, so the mapping should always exist.
    No fallback to any process-global — that caused the Layer 1c race
    condition where a external chat's state leaked to team chat tasks.
    """
    session_id = input_data.get("session_id", "")
    session_key = _sid_to_skey.get(session_id, "") if session_id else ""
    if session_key:
        return _get_or_create_state(session_key)
    # No mapping found -- return detached defaults. Admin checks DENY (safe default).
    log.error(f"_get_state_for_hook: no mapping for session_id={session_id[:12]}... -- "
              "state writes will be lost (this is safe: admin checks deny)", exc_info=True)
    return dict(_DEFAULT_STATE)


# ═══════════════════════════════════════════════════════════════════════
# SESSION LIFECYCLE
# ═══════════════════════════════════════════════════════════════════════


def cleanup_session_state(session_key: str) -> None:
    """Remove per-session state when a session is disconnected."""
    removed = _session_state.pop(session_key, None)
    compacted_sessions.discard(session_key)
    _last_input_tokens.pop(session_key, None)
    if removed:
        log.debug(f"Cleaned up session state for {session_key}")


# ═══════════════════════════════════════════════════════════════════════
# PUBLIC GETTERS/SETTERS -- require explicit session_key
# ═══════════════════════════════════════════════════════════════════════


def get_current_user_ctx(session_key: str = "") -> dict | None:
    if not session_key:
        log.error("get_current_user_ctx called without session_key", exc_info=True)
        return None
    return _get_or_create_state(session_key).get("current_user_ctx")


def set_current_user_ctx(ctx: dict | None, session_key: str = "") -> None:
    if not session_key:
        log.error("set_current_user_ctx called without session_key -- write lost", exc_info=True)
        return
    _get_or_create_state(session_key)["current_user_ctx"] = ctx


def get_tool_status_callback(session_key: str = "") -> Callable | None:
    if not session_key:
        log.error("get_tool_status_callback called without session_key", exc_info=True)
        return None
    return _get_or_create_state(session_key).get("tool_status_callback")


def set_tool_status_callback(cb: Callable | None, session_key: str = "") -> None:
    if not session_key:
        log.error("set_tool_status_callback called without session_key -- write lost", exc_info=True)
        return
    _get_or_create_state(session_key)["tool_status_callback"] = cb


def get_send_tool_called(session_key: str = "") -> bool:
    return _get_or_create_state(session_key).get("send_tool_called", False) if session_key else False


def set_send_tool_called(val: bool, session_key: str = "") -> None:
    if session_key:
        _get_or_create_state(session_key)["send_tool_called"] = val


def set_reply_threading(msg_id: int | None, task_id: str, chat_id: str,
                        session_key: str = "",
                        is_external_chat: bool = False,
                        message_thread_id: int | None = None) -> None:
    """Set reply-threading state. Called by handlers.py before each query."""
    if not session_key:
        log.error("set_reply_threading called without session_key -- write lost", exc_info=True)
        return
    state = _get_or_create_state(session_key)
    state["reply_to_msg_id"] = msg_id
    state["task_last_sent_id"] = None
    state["current_task_id"] = task_id
    state["origin_chat_id"] = chat_id
    state["is_external_chat"] = is_external_chat
    state["message_thread_id"] = message_thread_id
    # Mark chat activity at task start -- justified because the task creates
    # a streaming placeholder message visible in the chat. Other running tasks
    # should detect this interleaving and thread replies to user's original msg.
    if chat_id and task_id:
        mark_chat_activity(chat_id, task_id)


def get_reply_to_msg_id(session_key: str = "") -> int | None:
    if not session_key:
        return None
    return _get_or_create_state(session_key).get("reply_to_msg_id")


def get_message_thread_id(session_key: str = "") -> int | None:
    """Get the forum topic thread ID for the current session."""
    if not session_key:
        return None
    return _get_or_create_state(session_key).get("message_thread_id")


def get_task_last_sent_id(session_key: str = "") -> int | None:
    if not session_key:
        return None
    return _get_or_create_state(session_key).get("task_last_sent_id")


def set_task_last_sent_id(msg_id: int | None, session_key: str = "") -> None:
    if not session_key:
        return
    _get_or_create_state(session_key)["task_last_sent_id"] = msg_id


def get_current_task_id(session_key: str = "") -> str:
    if not session_key:
        return ""
    return _get_or_create_state(session_key).get("current_task_id", "")


def get_origin_chat_id(session_key: str = "") -> str:
    if not session_key:
        return ""
    return _get_or_create_state(session_key).get("origin_chat_id", "")



# ═══════════════════════════════════════════════════════════════════════
# CROSS-TASK CHAT ACTIVITY TRACKING
# ═══════════════════════════════════════════════════════════════════════


def mark_chat_activity(chat_id: str, task_id: str) -> None:
    """Record that a task/user sent a message to a chat."""
    _chat_last_task[chat_id] = task_id


def get_chat_last_task(chat_id: str) -> str:
    """Get the task_id of the last message sender in a chat."""
    return _chat_last_task.get(chat_id, "")


# ═══════════════════════════════════════════════════════════════════════
# SESSION ID MAPPING (for compaction tracking)
# ═══════════════════════════════════════════════════════════════════════


def save_last_input_tokens(session_key: str, tokens: int) -> None:
    """Save the latest input token count for a session (called from agent.py)."""
    if session_key and tokens > 0:
        _last_input_tokens[session_key] = tokens


def register_session_mapping(session_id: str, session_key: str) -> None:
    """Register an SDK session_id -> session_key mapping for compaction tracking."""
    if session_id:
        _sid_to_skey[session_id] = session_key


def unregister_session_mapping(session_id: str) -> None:
    """Remove session_id -> session_key mapping (called on client disconnect)."""
    _sid_to_skey.pop(session_id, None)


# ═══════════════════════════════════════════════════════════════════════
# INTENTIONAL SILENCE
# ═══════════════════════════════════════════════════════════════════════


def get_intentional_silence(session_key: str = "") -> bool:
    if not session_key:
        return False
    return _get_or_create_state(session_key).get("intentional_silence", False)


def set_intentional_silence(val: bool, session_key: str = "") -> None:
    if not session_key:
        return
    _get_or_create_state(session_key)["intentional_silence"] = val


# ═══════════════════════════════════════════════════════════════════════
# DEPLOY STATE
# ═══════════════════════════════════════════════════════════════════════


def set_deploy_state(
    session_key: str,
    confirmed: bool,
    *,
    force: bool = False,
    noqa: bool = False,
) -> None:
    """Set deploy gating state. Called by agent.py before query().

    Args:
        confirmed: True if user sent /deploy command.
        force: True if user sent /deploy force (skip ALL hook gates).
        noqa: True if user sent /deploy no-qa (skip staging QA gate).
    """
    state = _get_or_create_state(session_key)
    state["deploy_session_key_flag"] = session_key
    state["deploy_confirmed_flag"] = confirmed
    state["deploy_force_flag"] = force
    state["deploy_noqa_flag"] = noqa
    log.debug(
        "Deploy state set: session=%s, confirmed=%s, force=%s, noqa=%s",
        session_key, confirmed, force, noqa,
    )


def mark_deploy_review_passed(session_key: str | None = None) -> None:
    """Mark deploy review as passed for the given session.

    Called by the deploy_review MCP tool after automated checks pass.
    Reset automatically when code is edited (PostToolUse on Edit/Write/Bash).
    """
    if not session_key:
        log.error("mark_deploy_review_passed called without session_key -- ignored", exc_info=True)
        return
    state = _get_or_create_state(session_key)
    state["deploy_review_passed"] = True
    log.info(f"Deploy review PASSED for session={session_key}")


def mark_qa_staging_passed(session_key: str | None = None) -> None:
    """Mark staging QA as passed for the given session.

    Called by the staging_qa_run MCP tool after QA scenarios pass.
    Reset automatically when code is edited (PostToolUse on Edit/Write/Bash).
    """
    if not session_key:
        log.error("mark_qa_staging_passed called without session_key -- ignored", exc_info=True)
        return
    state = _get_or_create_state(session_key)
    state["qa_staging_passed"] = True
    log.info(f"Staging QA PASSED for session={session_key}")


def set_orchestrator_approval(session_key: str, token: str) -> None:
    """Set orchestrator approval token for the given session.

    Called ONLY by the pipeline_orchestrate MCP tool after independent
    verification passes. The token is HMAC-signed — cannot be forged.
    Reset automatically when code is edited (via _DEPLOY_GATE_FLAGS).
    """
    if not session_key:
        log.error("set_orchestrator_approval called without session_key -- ignored", exc_info=True)
        return
    state = _get_or_create_state(session_key)
    state["orchestrator_approval_token"] = token
    log.info("Orchestrator approval SET for session=%s (token=%s...)", session_key, token[:4])


def set_outbound_approved(session_key: str, approved: bool) -> None:
    """Set outbound action approval state.

    Called by handlers at task start (resets for new task) and by
    dispatch.py on mid-task injection (upgrades only — injection path
    only calls this with approved=True when text is a confirmation).
    """
    state = _get_or_create_state(session_key)
    state["outbound_approved"] = approved


def get_outbound_approved(session_key: str) -> bool:
    """Check if outbound actions are approved for this session's current task."""
    if not session_key:
        return False
    return _get_or_create_state(session_key).get("outbound_approved", False)


def set_extend_budget_state(session_key: str, confirmed: bool) -> None:
    """Set extend_budget gating state. Called by agent.py before query().

    Mirrors set_deploy_state -- the flag gates the `extend_budget` MCP tool
    in pre_tool_use_hook. Defaults to False on every query; only set True
    when the user's current message contains the 'extend budget' phrase.
    """
    state = _get_or_create_state(session_key)
    state["extend_budget_confirmed_flag"] = confirmed
    log.debug(f"Extend-budget state set: session={session_key}, confirmed={confirmed}")


# ═══════════════════════════════════════════════════════════════════════
# TOOL USE COUNTER
# ═══════════════════════════════════════════════════════════════════════


def get_tool_use_counter(session_key: str = "") -> int:
    if not session_key:
        return 0
    return _get_or_create_state(session_key).get("tool_use_counter", 0)


def set_tool_use_counter(val: int, session_key: str = "") -> None:
    if not session_key:
        return
    _get_or_create_state(session_key)["tool_use_counter"] = val


# ═══════════════════════════════════════════════════════════════════════
# USER CONTEXT
# ═══════════════════════════════════════════════════════════════════════


def set_user_context(source: str, user_id: str, user_name: str,
                     session_key: str = "") -> None:
    """Set the current user context for hook inspection."""
    if not session_key:
        log.error("set_user_context called without session_key -- write lost", exc_info=True)
        return
    # Try RBAC first, fall back to static set for bootstrap
    is_admin = False
    try:
        from bot.rbac import get_engine
        from bot.rbac.models import Subject
        engine = get_engine()
        role_ids = engine._get_role_ids(Subject(user_id=f"{source}:{user_id}"))
        is_admin = "bot_admin" in role_ids
    except Exception:
        # Fallback to static set during bootstrap (before RBAC engine loads)
        from config import is_admin_user
        is_admin = is_admin_user(source, user_id)
    log.info(f"Admin check: ({source}, {user_id!r}) admin={is_admin}")
    state = _get_or_create_state(session_key)
    # user_id stored in "source:id" format to match RBAC subject_id.
    # Bare platform ID available via "platform_user_id" for non-RBAC lookups.
    bare_uid = str(user_id)
    # Guard against double-prefixing (if caller already passed "telegram:123456789")
    if source and bare_uid and not bare_uid.startswith(f"{source}:"):
        prefixed_uid = f"{source}:{bare_uid}"
    else:
        prefixed_uid = bare_uid
    state["current_user_ctx"] = {
        "source": source,
        "user_id": prefixed_uid,            # "telegram:123456789" — matches RBAC
        "platform_user_id": bare_uid,       # "123456789" — bare platform ID
        "user_name": user_name,
        "is_admin": is_admin,
    }
    state["send_tool_called"] = False


# ═══════════════════════════════════════════════════════════════════════
# PARALLEL SESSION FILE AWARENESS
# ═══════════════════════════════════════════════════════════════════════


def record_repo_file(session_key: str, file_path: str) -> None:
    """Record that a session modified a file in /host-repo/.

    Called by PostToolUse after Edit/Write on repo files.  The set is lazily
    initialized and automatically cleaned up by cleanup_session_state().
    """
    state = _get_or_create_state(session_key)
    files = state.get("modified_repo_files")
    if files is None:
        files = set()
        state["modified_repo_files"] = files
    files.add(file_path)


def check_repo_conflicts(session_key: str, file_path: str) -> list[str]:
    """Check if OTHER sessions have modified the same repo file.

    Returns list of conflicting session_keys (empty if no conflicts).
    Iterates _session_state — O(n) but n is tiny (max ~5 concurrent sessions).
    """
    conflicts = []
    for key, st in _session_state.items():
        if key != session_key:
            files = st.get("modified_repo_files")
            if files and file_path in files:
                conflicts.append(key)
    return conflicts


