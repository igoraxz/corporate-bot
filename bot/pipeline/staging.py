# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Staging test infrastructure.

Buffers responses intercepted during staging mode so tests can assert on them.
Provides handler logic for /test/inject, /test/responses, and /test/clear endpoints.
Route decorators remain in main.py (they need the FastAPI `app` object).
"""

import asyncio
import json
import logging
import time

from config import TG_ADMIN_CHAT_IDS

# IS_STAGING is read lazily via _is_staging() so that test patches on
# either config.IS_STAGING or main.IS_STAGING take effect at call time.
import config as _config_mod

log = logging.getLogger(__name__)

# Key: chat_id (str), Value: list of {"tool": str, "args": dict, "ts": float}
_staging_responses: dict[str, list[dict]] = {}

# Completion events: chat_id → asyncio.Event (set when task completes)
_staging_completion_events: dict[str, asyncio.Event] = {}

_STAGING_MAX_RESPONSES_PER_CHAT = 100
_STAGING_MAX_CHATS = 50


def staging_signal_completion(chat_id: str) -> None:
    """Signal that a staging-injected task for chat_id has completed."""
    evt = _staging_completion_events.get(chat_id)
    if evt:
        evt.set()


def _staging_insert_entry(chat_id: str, tool_name: str, args: dict) -> None:
    """Insert one entry into _staging_responses (shared helper, no completion signal).

    Handles FIFO eviction for both the per-chat cap and the global chat cap.
    Called by staging_buffer_response and staging_buffer_tool_call so that
    the cap/format logic lives in exactly one place.
    """
    if len(_staging_responses) >= _STAGING_MAX_CHATS and chat_id not in _staging_responses:
        oldest_key = next(iter(_staging_responses))
        _staging_responses.pop(oldest_key, None)
    if chat_id not in _staging_responses:
        _staging_responses[chat_id] = []
    entries = _staging_responses[chat_id]
    if len(entries) >= _STAGING_MAX_RESPONSES_PER_CHAT:
        entries.pop(0)  # drop oldest
    entries.append({
        "tool": tool_name,
        "args": args,
        "ts": time.time(),
    })


def staging_buffer_response(chat_id: str, tool_name: str, args: dict) -> None:
    """Buffer a send-tool response in staging mode and signal task completion.

    Called from send_message MCP handlers (telegram_send_message, etc.) and
    the fallback response path in handlers.py.  The completion signal causes
    /test/inject to return once this (the final reply) has been buffered.
    """
    if not _config_mod.IS_STAGING:
        return
    _staging_insert_entry(chat_id, tool_name, args)
    entries = _staging_responses.get(chat_id, [])
    has_event = chat_id in _staging_completion_events
    log.info(f"[STAGING-BUF] buffer_response: chat_id={chat_id!r}, tool={tool_name}, "
             f"event_exists={has_event}, total={len(entries)}")
    staging_signal_completion(chat_id)


def staging_buffer_tool_call(chat_id: str, tool_name: str, args: dict) -> None:
    """Buffer a non-send tool call in staging mode WITHOUT signaling completion.

    Unlike staging_buffer_response, this does NOT fire the completion event.
    Used by PostToolUse hook to record Bash, pm_list_projects, etc. so that
    expect_tools QA assertions can verify specific tools were called.

    Completion is intentionally left to staging_buffer_response (send_message
    tools) so the /test/inject endpoint keeps waiting for the final reply.
    """
    if not _config_mod.IS_STAGING:
        return
    _staging_insert_entry(chat_id, tool_name, args)
    log.info(f"[STAGING-BUF] tool_call: chat_id={chat_id!r}, tool={tool_name}")


def _check_staging_auth(request) -> str | None:
    """Validate staging test endpoint auth. Returns error string or None if OK."""
    from config import STAGING_TEST_TOKEN
    if not STAGING_TEST_TOKEN:
        return None  # No token configured = open access (local dev)
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return "Missing or malformed Authorization header (expected: Bearer <token>)"
    import hmac
    if not hmac.compare_digest(auth_header[7:], STAGING_TEST_TOKEN):
        return "Invalid staging test token"
    return None


async def test_inject(request) -> dict:
    """Inject a synthetic message into the processing pipeline (staging only).

    Body JSON: {
        "text": str,           — message text
        "chat_id": str|int,    — target chat (default: staging test chat)
        "source": str,         — "telegram" or "teams" (default: "telegram")
        "user_id": int,        — sender user ID (default: first admin)
        "is_admin": bool,      — admin flag (default: False for safety)
        "timeout": float       — max seconds to wait for response (default: 60)
        "harness_label": str   — assign this harness before processing (optional)
        "harness_config": dict  — inline harness definition; auto-creates if missing (optional)
                                  e.g. {"data_scope": "own", "model": "claude-sonnet-4-6"}
                                  project_dir auto-set to /app/data/harness-projects/<label>/
        "save_to": str         — save result JSON to this file path (optional)
    }

    Returns a dict with keys: status_code, body (JSON-serializable dict).
    The route wrapper in main.py converts this to a Response.
    """
    if not _config_mod.IS_STAGING:
        return {"status_code": 404, "body": None}

    auth_err = _check_staging_auth(request)
    if auth_err:
        return {"status_code": 401, "body": {"error": auth_err}}

    try:
        body = await request.json()
    except Exception:
        return {"status_code": 400, "body": {"error": "invalid JSON body"}}

    text = body.get("text", "")
    if not text:
        return {"status_code": 400, "body": {"error": "text is required"}}

    chat_id = str(body.get("chat_id", "staging-test"))
    # CB-133: Derive source from ROOT_ADMIN if not provided
    source = body.get("source", "")
    if not source:
        from config import ROOT_ADMIN
        source = ROOT_ADMIN[0] or "telegram"
    is_admin = body.get("is_admin", False)  # default False for safety
    # user_id default is admin-status-aware:
    # • is_admin=True  → use first real admin ID (already in ADMIN_USERS → lightweight cmds work)
    # • is_admin=False → use synthetic non-admin ID (NOT in ADMIN_USERS → is_admin_user() → False)
    # Using a real admin ID for ALL injects caused non-admin scenarios to get admin access
    # because is_admin_user(source, admin_id) always returns True.
    if is_admin:
        # CB-133: Find admin matching the source platform (not just telegram)
        from config import ADMIN_USERS as _ADMIN_USERS
        _admin_uid = next((uid for src, uid in _ADMIN_USERS if src == source), None)
        _admin_uid = _admin_uid or next((uid for src, uid in _ADMIN_USERS), None)
        _admin_uid = _admin_uid or next(iter(TG_ADMIN_CHAT_IDS), "staging-admin")
        user_id = body.get("user_id", _admin_uid)
    else:
        user_id = body.get("user_id", "staging-nonadmin")
    timeout = min(body.get("timeout", 60), 3600)  # cap at 60 minutes (PM pipelines dispatch 3 agents + fix loops)
    harness_label = body.get("harness_label")  # optional: assign harness
    harness_config = body.get("harness_config")  # optional: inline harness definition
    save_to = body.get("save_to")  # optional: save result JSON to file

    # ── Reset state from previous injects ────────────────────────────────────────
    # Clear any per-chat overrides (data_scope, project_dir, query_timeout_s) and
    # harness assignment left by previous injects that shared the same chat_id.
    # Without this, admin overrides from scenario A (e.g. data_scope=admin set by
    # is_admin=True injects) contaminate non-admin scenario B on the same session.
    # Reset happens BEFORE setting new overrides so each inject starts clean.
    _session_key_reset = f"{source}:{chat_id}"
    try:
        from bot.harness import clear_chat_override, clear_chat_harness
        clear_chat_override(_session_key_reset)  # Remove all per-chat overrides
        if not harness_label:
            clear_chat_harness(_session_key_reset)  # Reset to default harness
        log.info(f"[STAGING] Cleared overrides/harness for {_session_key_reset!r} (new inject)")
    except Exception as _e:
        log.warning(f"[STAGING] Failed to reset chat state: {_e}")
    # Also reset the SDK client session so model context doesn't leak across scenarios.
    # Harness override clearing fixes permissions but the model may still "remember"
    # admin context from a prior turn. reset_session forces a fresh CLI subprocess.
    # No-op if no client exists yet — safe to call unconditionally.
    try:
        from bot.agent import client_pool
        if client_pool:
            await client_pool.reset_session(_session_key_reset)
            log.info(f"[STAGING] Reset SDK session for {_session_key_reset!r} (fresh context)")
    except Exception as _e:
        log.info(f"[STAGING] SDK session reset skipped for {_session_key_reset!r}: {_e}")

    # Assign harness to the inject session if requested.
    # If harness_config is provided, auto-create the harness if it doesn't exist
    # on this staging instance (harnesses.json is per-volume, not git-tracked).
    # CB-5: Register QA chat in registry so lightweight commands + is_authorized work
    if _config_mod.IS_STAGING:
        try:
            from bot.chat_registry import register as _cr_register, get as _cr_get
            _inject_chat_key = f"{source}:{chat_id}"
            if not _cr_get(_inject_chat_key):
                _cr_register(_inject_chat_key, title=f"QA {chat_id}", mode="active_plus")
                log.info(f"[STAGING] Registered QA chat: {_inject_chat_key}")
        except Exception as _reg_e:
            log.warning(f"[STAGING] Failed to register QA chat: {_reg_e}")

    if harness_label and _config_mod.IS_STAGING:
        try:
            from bot.harness import (
                get_harness, create_harness, update_harness, list_harnesses,
            )
            session_key = f"{source}:{chat_id}"
            # Auto-create harness from inline config if it doesn't exist
            if harness_config and isinstance(harness_config, dict):
                existing = list_harnesses()
                if harness_label not in existing:
                    # Auto-set project_dir for sandbox harnesses
                    cfg = dict(harness_config)
                    if "project_dir" not in cfg and cfg.get("data_scope") != "admin":
                        cfg["project_dir"] = f"/app/data/harness-projects/{harness_label}"
                    create_harness(harness_label, **cfg)
                    log.info(f"[STAGING] Auto-created harness '{harness_label}' from inline config: {cfg}")
                else:
                    # Update existing harness with provided config
                    update_harness(harness_label, **harness_config)
                    log.info(f"[STAGING] Updated existing harness '{harness_label}' with inline config")
            # CB-5: Use assign_chat_harness for RBAC auto-assignment
            from bot.harness import assign_chat_harness
            await assign_chat_harness(session_key, harness_label)
            log.info(f"[STAGING] Harness '{harness_label}' assigned to {session_key} (with RBAC)")
            # Auto-promote to admin if harness has data_scope=admin.
            # Admin-scope harnesses (coding, admin) need is_admin=True for
            # hooks Layer 1b to grant Bash/Write/Edit (admin gate checks user
            # registry, not harness scope). Without this, admin-scope harnesses
            # on staging have data access but no tool access.
            harness_obj = get_harness(harness_label)
            # R4: data_scope removed from Harness. Admin detection via
            # harness label convention (admin label = admin access on staging).
            if harness_obj and harness_label == "admin":
                is_admin = True
                log.info("[STAGING] Auto-promoted to admin (harness label='admin')")
        except Exception as _e:
            log.warning(f"Failed to assign harness '{harness_label}': {_e}")

    # On staging, chat_registry may be empty (TG disabled), so user_id=0
    # and is_admin_user() always returns False. When the inject explicitly
    # requests is_admin=True, register a synthetic admin user so hooks allow
    # admin-gated tools (Bash, Edit, Write, deploy, PM tools).
    if is_admin and _config_mod.IS_STAGING:
        from config import ADMIN_USERS, is_admin_user
        # Register user_id as admin if not already recognized — covers the case
        # where TG_ADMIN_CHAT_IDS is empty (fully isolated staging env). When
        # user_id is a real admin ID (from TG_ADMIN_CHAT_IDS), is_admin_user()
        # already returns True and no registration is needed.
        _uid_str = str(user_id or "staging-admin")
        if not is_admin_user(source, _uid_str):
            ADMIN_USERS.add((source, _uid_str))
            log.info(f"[STAGING] Registered synthetic admin ({source}, {_uid_str!r})")
        if not user_id:
            user_id = "staging-admin"
        # Grant admin SDK-level tool access: default harness denies
        # Bash/Write/Edit via settings.json. Admin injects need a project
        # dir with permissive settings so the SDK makes tools visible.
        # Skip if harness_label already set (harness handles project_dir).
        if not harness_label:
            try:
                from bot.harness import set_chat_override, get_system_agent_cwd
                session_key = f"{source}:{chat_id}"
                admin_cwd = get_system_agent_cwd(["Bash", "Write", "Edit"])
                set_chat_override(session_key, "project_dir", admin_cwd)
            except Exception as _e:
                log.warning(f"Failed to set admin project dir for staging inject: {_e}")
        # CB-5: Also assign bot_admin RBAC role to the user subject for admin injects.
        # The harness assign_chat_harness gives role to the CHAT subject, but hooks
        # check the USER subject for admin gating (is_admin_user → RBAC user subject).
        try:
            from bot.rbac.store import assign_role as _rbac_assign
            _user_subject_id = f"{source}:{_uid_str}"
            await _rbac_assign("user", _user_subject_id, "bot_admin",
                               granted_by="system:staging_inject")
            # Invalidate RBAC cache so role is immediately visible to hooks
            try:
                from bot.rbac import get_engine
                get_engine().invalidate_cache()
            except Exception:
                pass
            log.info(f"[STAGING] Assigned bot_admin RBAC role to user:{_user_subject_id}")
        except Exception as _rbac_e:
            log.warning(f"[STAGING] RBAC user role assignment failed: {_rbac_e}")
        try:
            from bot.harness import set_chat_override
            session_key = f"{source}:{chat_id}"
            # Full admin scope — no sandbox path restrictions
            set_chat_override(session_key, "data_scope", "admin")
            # Disable zombie detector for admin injects — PM pipelines dispatch
            # agents internally as long-running MCP tool calls (20+ min) with no
            # SDK activity visible to the main session.
            set_chat_override(session_key, "query_timeout_s", 0)
        except Exception as _e:
            log.warning(f"Failed to set admin overrides for staging inject: {_e}")

    # Clear previous responses and create completion event BEFORE enqueue
    _staging_responses[chat_id] = []
    evt = asyncio.Event()
    _staging_completion_events[chat_id] = evt

    # Build a parsed message dict similar to what triage_and_enqueue expects
    parsed = {
        "text": text,
        "chat_id": chat_id,
        "source": source,
        "user_id": user_id,
        "user_name": body.get("user_name", "qa-tester"),
        "message_id": int(time.time() * 1000),
        "timestamp": time.time(),
        "is_admin": is_admin,
        "is_staging_inject": True,
    }

    # Route through triage_and_enqueue so that lightweight commands (/switch-oauth,
    # /status, /chats, etc.) are intercepted before the SDK — same path as real TG/WA
    # messages. triage_and_enqueue calls persistent_queue.enqueue() for non-lightweight
    # messages, completing the pipeline. Lazy import avoids circular dep.
    import main as _main_mod
    if getattr(_main_mod, "persistent_queue", None) is None:
        _staging_completion_events.pop(chat_id, None)
        return {"status_code": 503, "body": {"error": "persistent queue not initialized"}}
    from bot.pipeline.dispatch import triage_and_enqueue
    await triage_and_enqueue(parsed)

    # Wait for completion signal or timeout
    try:
        await asyncio.wait_for(evt.wait(), timeout=timeout)
        # Grace window: wait briefly for any trailing responses
        await asyncio.sleep(0.5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
    finally:
        _staging_completion_events.pop(chat_id, None)

    final_responses = _staging_responses.pop(chat_id, [])
    elapsed = time.time() - parsed["timestamp"]
    result_body = {
        "status": "ok" if final_responses else "timeout",
        "responses": final_responses,
        "elapsed_s": round(elapsed, 1),
        "chat_id": chat_id,
    }

    # Save result to file if requested (for AB test analysis)
    if save_to:
        try:
            import os
            os.makedirs(os.path.dirname(save_to), exist_ok=True)
            with open(save_to, "w") as f:
                json.dump(result_body, f, indent=2)
            log.info(f"[STAGING] Result saved to {save_to}")
        except Exception as _e:
            log.warning(f"Failed to save result to {save_to}: {_e}")

    return {"status_code": 200, "body": result_body}


async def test_get_responses(chat_id: str, request) -> dict:
    """Get buffered staging responses for a chat (staging only).

    Returns a dict with keys: status_code, body.
    """
    if not _config_mod.IS_STAGING:
        return {"status_code": 404, "body": None}
    auth_err = _check_staging_auth(request)
    if auth_err:
        return {"status_code": 401, "body": {"error": auth_err}}
    responses = _staging_responses.get(chat_id, [])
    return {"status_code": 200, "body": {"chat_id": chat_id, "responses": responses}}


async def test_clear_responses(chat_id: str, request) -> dict:
    """Clear buffered staging responses for a chat (staging only).

    Returns a dict with keys: status_code, body.
    """
    if not _config_mod.IS_STAGING:
        return {"status_code": 404, "body": None}
    auth_err = _check_staging_auth(request)
    if auth_err:
        return {"status_code": 401, "body": {"error": auth_err}}
    _staging_responses.pop(chat_id, None)
    return {"status_code": 200, "body": {"status": "cleared"}}


async def test_reset_clients(request) -> dict:
    """Disconnect and remove SDK clients matching a prefix (staging only).

    Body JSON: {"prefix": str}  — e.g. "qa_" disconnects all clients
    whose session_key starts with "telegram:qa_".

    Returns a dict with keys: status_code, body.
    """
    if not _config_mod.IS_STAGING:
        return {"status_code": 404, "body": None}
    auth_err = _check_staging_auth(request)
    if auth_err:
        return {"status_code": 401, "body": {"error": auth_err}}

    try:
        body = await request.json()
    except Exception:
        return {"status_code": 400, "body": {"error": "invalid JSON body"}}

    prefix = body.get("prefix", "")
    if not prefix:
        return {"status_code": 400, "body": {"error": "prefix is required"}}

    from bot.agent import client_pool
    if not client_pool:
        return {"status_code": 503, "body": {"error": "client pool not initialized"}}

    # Find matching clients by session_key prefix
    full_prefix = f"telegram:{prefix}"
    matching = [k for k in list(client_pool._clients.keys()) if k.startswith(full_prefix)]

    disconnected = []
    for sk in matching:
        try:
            await client_pool.reset_session(sk)
            disconnected.append(sk)
        except Exception as e:
            log.warning(f"Failed to reset {sk}: {e}")

    log.info(f"[STAGING] Reset {len(disconnected)} clients matching '{prefix}': {disconnected}")
    return {"status_code": 200, "body": {"disconnected": disconnected, "count": len(disconnected)}}


async def test_logs(request) -> dict:
    """Return raw log lines matching a grep pattern (staging only).

    Query params:
        pattern: grep pattern (required)
        lines: max lines to return (default 50, max 500)
        file: log file name (default "bot.log", also "bot.log.1" etc.)

    Returns a dict with keys: status_code, body.
    """
    if not _config_mod.IS_STAGING:
        return {"status_code": 404, "body": None}
    auth_err = _check_staging_auth(request)
    if auth_err:
        return {"status_code": 401, "body": {"error": auth_err}}

    pattern = request.query_params.get("pattern", "")
    if not pattern:
        return {"status_code": 400, "body": {"error": "pattern query param is required"}}

    max_lines = min(int(request.query_params.get("lines", "50")), 500)
    log_file = request.query_params.get("file", "bot.log")
    # Sanitize filename (only allow bot.log variants)
    if not log_file.startswith("bot.log"):
        return {"status_code": 400, "body": {"error": "file must start with 'bot.log'"}}

    import subprocess
    log_path = f"/app/logs/{log_file}"
    try:
        result = subprocess.run(
            ["grep", "-n", pattern, log_path],
            capture_output=True, text=True, timeout=10,
        )
        lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
        # Take last N lines
        lines = lines[-max_lines:]
        return {
            "status_code": 200,
            "body": {
                "pattern": pattern,
                "file": log_file,
                "count": len(lines),
                "lines": lines,
            },
        }
    except subprocess.TimeoutExpired:
        return {"status_code": 500, "body": {"error": "grep timed out"}}
    except FileNotFoundError:
        return {"status_code": 404, "body": {"error": f"log file not found: {log_file}"}}
