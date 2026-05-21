# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Message triage routing: PQ dispatch, external chat throttling, triage_and_enqueue.

All bot.*, config.*, integrations.* imports are LAZY (inside function bodies)
to avoid circular import issues.
"""

import asyncio
import html as _html
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)


# === DISCOVERED CHAT THROTTLE (active group chats, cold mode) ===
# When bot hasn't spoken in an external group chat recently ("cold mode"),
# accumulate messages and invoke SDK at most once per _EC_COLD_SECS.
# When bot recently spoke ("hot mode", within _EC_HOT_SECS), dispatch immediately.
# DMs (1:1 chats) always dispatch immediately — throttling is for groups only.

_EC_HOT_SECS = 300    # 5 min — if bot sent within this window, dispatch immediately
_EC_COLD_SECS = 150   # 2.5 min — in cold mode, batch messages and dispatch after this delay

_ec_bot_send_ts: dict[str, float] = {}         # chat_id -> monotonic ts of last bot send
_ec_throttle_handles: dict[str, asyncio.TimerHandle] = {}  # chat_id -> pending timer
_ec_throttle_pending: dict[str, dict] = {}     # chat_id -> latest pending parsed msg


def update_ec_bot_send_time(chat_id: str | int) -> None:
    """Record that the bot just sent a message to a external chat.

    Called from mcp_tools after successful telegram_send_message.
    """
    _ec_bot_send_ts[str(chat_id)] = time.monotonic()


def _is_ec_hot(chat_id: str) -> bool:
    """True if bot recently sent to this chat (within hot window)."""
    last = _ec_bot_send_ts.get(chat_id, 0)
    return (time.monotonic() - last) < _EC_HOT_SECS


def _is_active_ec_group(source: str, chat_id: str) -> bool:
    """True if chat_id is an active (NOT active_plus) external GROUP chat.

    active_plus chats dispatch immediately (no cold throttle) — they have full
    primary-chat UX with placeholders, so delays would be confusing.
    Only 'active' mode groups get cold/hot throttling.
    """
    import bot.chat_registry as _cr
    if source == "telegram":
        try:
            cid = int(chat_id)
        except (ValueError, TypeError):
            return False
        entry = _cr.get(f"telegram:{cid}")
        if not entry:
            return False
        # TG group IDs are negative; positive = DM
        return entry.get("mode") == "active" and cid < 0
    return False


def _schedule_ec_throttle(chat_id: str, parsed: dict) -> None:
    """Store latest message and (re)schedule a delayed dispatch."""
    _ec_throttle_pending[chat_id] = parsed  # keep only the latest

    # Cancel existing timer (debounce)
    existing = _ec_throttle_handles.pop(chat_id, None)
    if existing:
        existing.cancel()

    loop = asyncio.get_event_loop()
    handle = loop.call_later(
        _EC_COLD_SECS,
        lambda cid=chat_id: asyncio.create_task(_flush_ec_throttle(cid)),
    )
    _ec_throttle_handles[chat_id] = handle


async def _flush_ec_throttle(chat_id: str) -> None:
    """Dispatch the most recent throttled message for a external chat."""
    _ec_throttle_handles.pop(chat_id, None)
    parsed = _ec_throttle_pending.pop(chat_id, None)
    if not parsed:
        return
    log.info(f"EC throttle flush: dispatching queued msg for chat={chat_id}")
    try:
        import main as _main_mod
        pq = _main_mod.persistent_queue
        if pq:
            await pq.enqueue(parsed)
    except Exception as e:
        log.error(f"EC throttle flush error: {e}", exc_info=True)


async def _dispatch_task(parsed: dict) -> None:
    """Dispatch a message to the main session for this chat.

    Called by PersistentQueue worker. Always routes through the main persistent
    session (one per chat, sequential processing). No fork sessions.
    """
    from config import get_model_primary
    from bot.pipeline.task_manager import task_manager
    from bot.pipeline.handlers import handle_telegram_message, handle_teams_message
    import main as _main_mod

    source = parsed.get("source") or ""  # CB-133: no platform assumption
    # Corporate mode: chat_id must be explicitly provided in the parsed message.
    # No fallback to a "primary group" — every message has explicit routing.
    chat_id = str(parsed.get("chat_id") or parsed.get("chat_jid") or "")
    user_name = parsed.get("user_name", "?")
    user_id = str(parsed.get("user_id", ""))
    text = parsed.get("text", "")

    # Register task (for UI tracking, cancel, etc.)
    task = task_manager.register_task(source, chat_id, user_name, user_id, text)
    task.use_main_session = True
    # Store original TG message ID + forum topic thread ID for reply routing
    if source == "telegram":
        msg_id = parsed.get("message_id", "")
        if msg_id:
            try:
                task.tg_user_message_id = int(msg_id)
            except (ValueError, TypeError):
                pass
        from bot.chat_key import normalize_thread_id as _norm_tid
        _tid_task = _norm_tid(parsed.get("message_thread_id"))
        if _tid_task is not None:
            task.tg_message_thread_id = _tid_task
    parsed["model"] = get_model_primary()

    # Forum topic routing: thread_id flows through parsed["message_thread_id"]
    # to handlers and agent.py which include it in session_key.

    log.info(f"Task {task.task_id}: routing through main session for chat {chat_id}")

    if source == "teams":
        handler = handle_teams_message
    else:
        handler = handle_telegram_message

    # Extract PQ row metadata for completion tracking
    pq_row_id = parsed.pop("_pq_row_id", None)
    pq_catchup_ids = parsed.pop("_pq_catchup_ids", None)

    persistent_queue = _main_mod.persistent_queue

    async def _run():
        try:
            await handler(parsed, task)
            await task_manager.complete_task(task.task_id)
            # Mark PQ row(s) as done
            if persistent_queue and pq_row_id is not None:
                await persistent_queue.complete(pq_row_id, catchup_ids=pq_catchup_ids)
        except asyncio.CancelledError:
            # Task was cancelled (by user or zombie detector) — release TaskManager slot
            await task_manager.fail_task(task.task_id, "Cancelled")
            # Mark PQ row as failed so it won't replay
            if persistent_queue and pq_row_id is not None:
                await persistent_queue.fail(pq_row_id, "Cancelled by user", catchup_ids=pq_catchup_ids)
        except Exception as e:
            await task_manager.fail_task(task.task_id, str(e))
            # Mark PQ row(s) as failed
            if persistent_queue and pq_row_id is not None:
                await persistent_queue.fail(pq_row_id, str(e)[:500], catchup_ids=pq_catchup_ids)
        finally:
            pass  # Main sessions are persistent — no cleanup needed

    task._asyncio_task = asyncio.create_task(_run())


async def _try_lightweight_command(
    cmd_text: str, source: str, chat_id: str, user_id: str,
    parsed: dict, _topic_tid: int | None,
) -> bool:
    """CB-142: Handle lightweight slash commands inline (no SDK session needed).

    Returns True if the command was handled (caller should return).
    Returns False if the message should continue to relay/enqueue processing.

    Extracted from triage_and_enqueue() for maintainability.
    All 30+ slash commands are matched here via regex.
    """
    from bot.commands import (
        _DROP_CALL_RE, _STATUS_RE,
        _CHATS_RE, _USAGE_RE, _CLAUDE_ACCOUNTS_RE, _ACCOUNTS_RE,
        _MAC_SETUP_RE, _MAC_STATUS_RE, _MAC_LOCK_RE,
        _DAEMONS_RE, _DAEMON_REAP_RE, _DAEMON_KILL_RE,
        _CREATE_RELAY_RE, _DELETE_RELAY_RE,
        _DELETE_CHAT_RE, _CLAUDE_AUTH_RE, _M365_AUTH_RE, _GOOGLE_AUTH_RE, _GITHUB_AUTH_RE, _REVOKE_RE,
        _MAC_REAUTH_RE, _MAC_ACCOUNTS_RE, _MAC_SET_ACCOUNT_RE,
        _MAC_OAUTH_CODE_RE, _pending_mac_reauth,
        _ROLLBACK_RE, _PERMISSIONS_RE, _HARNESSES_RE, _SET_HARNESS_RE, _SWITCH_OAUTH_RE,
        _HELP_RE, _SESSION_RE, _MAC_SESSIONS_RE, _ONBOARDING_RE,
        _lightweight_status,
        _lightweight_chats,
        _lightweight_usage, _lightweight_claude_accounts, _lightweight_accounts, _lightweight_mac_setup, _lightweight_mac_status, _lightweight_mac_lock,
        _lightweight_daemons, _lightweight_daemon_reap,
        _lightweight_daemon_kill, _lightweight_create_relay,
        _lightweight_delete_relay, _lightweight_delete_chat,
        _lightweight_claude_auth, _lightweight_m365_auth, _lightweight_google_auth, _lightweight_github_auth, _lightweight_revoke,
        _pending_google_auth, _handle_google_auth_code,
        _lightweight_rollback, _lightweight_permissions,
        _lightweight_harnesses, _lightweight_set_harness,
        _lightweight_switch_oauth,
        _lightweight_help, _lightweight_onboarding, _lightweight_mac_sessions,
        _lightweight_session, _handle_mac_reauth_code,
    )
    from bot.relay.core import DESKTOP_ENABLED

    # Handle drop call
    if _DROP_CALL_RE.match(cmd_text):
        from integrations.phone import get_active_calls, signal_drop_call
        if get_active_calls():
            log.info(f"Drop call command from {parsed.get('user_name')}")
            signal_drop_call()
            return True

    # Handle /status (no SDK needed)
    if _STATUS_RE.match(cmd_text):
        log.info(f"Status requested by {parsed.get('user_name')}")
        await _lightweight_status(source, chat_id, user_id, message_thread_id=_topic_tid)
        return True

    # Handle /chats
    if _CHATS_RE.match(cmd_text):
        await _lightweight_chats(source, chat_id, user_id, message_thread_id=_topic_tid)
        return True

    # Handle /usage [N]
    _usage_m = _USAGE_RE.match(cmd_text)
    if _usage_m:
        days = int(_usage_m.group(1)) if _usage_m.group(1) else 7
        await _lightweight_usage(source, chat_id, user_id, days=days, message_thread_id=_topic_tid)
        return True

    # Handle /accounts
    if _ACCOUNTS_RE.match(cmd_text):
        await _lightweight_accounts(source, chat_id, user_id, message_thread_id=_topic_tid)
        return True

    if _CLAUDE_ACCOUNTS_RE.match(cmd_text):
        log.info(f"/claude-accounts requested by {parsed.get('user_name')}")
        await _lightweight_claude_accounts(source, chat_id, user_id, message_thread_id=_topic_tid)
        return True

    # Handle /mac-setup
    _mac_setup_m = _MAC_SETUP_RE.match(cmd_text)
    if _mac_setup_m:
        log.info(f"Mac setup requested by {parsed.get('user_name')}")
        await _lightweight_mac_setup(source, chat_id, user_id,
                                     args=_mac_setup_m.group(1) or "",
                                     message_thread_id=_topic_tid)
        return True

    # Handle /mac status
    if _MAC_STATUS_RE.match(cmd_text):
        log.info(f"Mac status requested by {parsed.get('user_name')}")
        await _lightweight_mac_status(source, chat_id, user_id, message_thread_id=_topic_tid)
        return True

    if _MAC_LOCK_RE.match(cmd_text):
        log.info(f"Mac lock requested by {parsed.get('user_name')}")
        await _lightweight_mac_lock(source, chat_id, user_id, message_thread_id=_topic_tid)
        return True

    # Daemon management
    if _DAEMONS_RE.match(cmd_text):
        await _lightweight_daemons(source, chat_id, user_id, message_thread_id=_topic_tid)
        return True

    if _DAEMON_REAP_RE.match(cmd_text):
        await _lightweight_daemon_reap(source, chat_id, user_id, message_thread_id=_topic_tid)
        return True

    _kill_m = _DAEMON_KILL_RE.match(cmd_text)
    if _kill_m:
        await _lightweight_daemon_kill(source, chat_id, user_id,
                                       idx=int(_kill_m.group(1)), message_thread_id=_topic_tid)
        return True

    # Relay management
    _create_m = _CREATE_RELAY_RE.match(cmd_text)
    if _create_m:
        await _lightweight_create_relay(source, chat_id, user_id,
                                        idx=_create_m.group(1), message_thread_id=_topic_tid)
        return True

    _delete_m = _DELETE_RELAY_RE.match(cmd_text)
    if _delete_m:
        idx = int(_delete_m.group(1)) if _delete_m.group(1) else None
        await _lightweight_delete_relay(source, chat_id, user_id,
                                        idx=idx, message_thread_id=_topic_tid)
        return True

    # Chat/auth management
    _delchat_m = _DELETE_CHAT_RE.match(cmd_text)
    if _delchat_m:
        target = _delchat_m.group(1) or ""
        await _lightweight_delete_chat(source, chat_id, user_id,
                                        target=target.strip(), message_thread_id=_topic_tid)
        return True

    _clauth_m = _CLAUDE_AUTH_RE.match(cmd_text)
    if _clauth_m:
        label = _clauth_m.group(1) or ""
        await _lightweight_claude_auth(source, chat_id, user_id,
                                        label=label.strip(), message_thread_id=_topic_tid)
        return True

    _m365_m = _M365_AUTH_RE.match(cmd_text)
    if _m365_m:
        cancel = bool(_m365_m.group(1))
        await _lightweight_m365_auth(source, chat_id, user_id,
                                      cancel=cancel, message_thread_id=_topic_tid)
        return True

    _gauth_m = _GOOGLE_AUTH_RE.match(cmd_text)
    if _gauth_m:
        cancel_or_code = _gauth_m.group(1) or ""
        if cancel_or_code.lower() == "cancel":
            await _lightweight_google_auth(source, chat_id, user_id,
                                            cancel=True, message_thread_id=_topic_tid)
        elif _pending_google_auth.get(f"{source}:{chat_id}"):
            await _handle_google_auth_code(source, chat_id, user_id,
                                            code=cancel_or_code, message_thread_id=_topic_tid)
        else:
            await _lightweight_google_auth(source, chat_id, user_id,
                                            message_thread_id=_topic_tid)
        return True

    _gh_m = _GITHUB_AUTH_RE.match(cmd_text)
    if _gh_m:
        cancel = _gh_m.group(1) == "cancel" if _gh_m.group(1) else False
        await _lightweight_github_auth(source, chat_id, user_id,
                                        cancel=cancel, message_thread_id=_topic_tid)
        return True

    _revoke_m = _REVOKE_RE.match(cmd_text)
    if _revoke_m:
        cred_type = _revoke_m.group(1) or ""
        label = _revoke_m.group(2) or ""
        await _lightweight_revoke(source, chat_id, user_id,
                                   cred_type=cred_type, label=label.strip(),
                                   message_thread_id=_topic_tid)
        return True

    # Mac OAuth reauth
    _reauth_m = _MAC_REAUTH_RE.match(cmd_text)
    if _reauth_m:
        if DESKTOP_ENABLED:
            from bot.commands import _handle_mac_reauth
            await _handle_mac_reauth(source, chat_id, user_id,
                                      label=(_reauth_m.group(1) or "").strip(),
                                      message_thread_id=_topic_tid)
        return True

    if _MAC_ACCOUNTS_RE.match(cmd_text):
        if DESKTOP_ENABLED:
            from bot.commands import _lightweight_mac_accounts
            await _lightweight_mac_accounts(source, chat_id, user_id, message_thread_id=_topic_tid)
        return True

    _set_acc_m = _MAC_SET_ACCOUNT_RE.match(cmd_text)
    if _set_acc_m:
        if DESKTOP_ENABLED:
            from bot.commands import _lightweight_mac_set_account
            await _lightweight_mac_set_account(source, chat_id, user_id,
                                                label=(_set_acc_m.group(1) or "").strip(),
                                                message_thread_id=_topic_tid)
        return True

    # OAuth code interception
    if _pending_mac_reauth.get(f"{source}:{chat_id}"):
        _ocode_m = _MAC_OAUTH_CODE_RE.match(cmd_text)
        if _ocode_m:
            await _handle_mac_reauth_code(source, chat_id, user_id,
                                           code=_ocode_m.group(1), message_thread_id=_topic_tid)
            return True

    # Session management
    if _SESSION_RE.match(cmd_text):
        if DESKTOP_ENABLED:
            await _lightweight_session(source, chat_id, user_id,
                                        args=cmd_text.split(None, 1)[1] if len(cmd_text.split()) > 1 else "",
                                        message_thread_id=_topic_tid)
            return True

    if _MAC_SESSIONS_RE.match(cmd_text):
        if DESKTOP_ENABLED:
            asyncio.create_task(
                _lightweight_mac_sessions(source, chat_id, user_id, message_thread_id=_topic_tid)
            )
            return True

    # Help & onboarding
    if _HELP_RE.match(cmd_text):
        log.info(f"Help requested by {parsed.get('user_name')}")
        await _lightweight_help(source, chat_id, user_id, message_thread_id=_topic_tid)
        return True

    if _ONBOARDING_RE.match(cmd_text):
        await _lightweight_onboarding(source, chat_id, user_id, message_thread_id=_topic_tid)
        return True

    # Admin management (harnesses, permissions, switch-oauth, rollback)
    _harness_m = _HARNESSES_RE.match(cmd_text)
    if _harness_m:
        await _lightweight_harnesses(source, chat_id, user_id,
                                      specific=(_harness_m.group(1) or "").strip(),
                                      message_thread_id=_topic_tid)
        return True

    _set_h_m = _SET_HARNESS_RE.match(cmd_text)
    if _set_h_m:
        await _lightweight_set_harness(source, chat_id, user_id,
                                        args=(_set_h_m.group(1) or "").strip(),
                                        message_thread_id=_topic_tid)
        return True

    _perm_m = _PERMISSIONS_RE.match(cmd_text)
    if _perm_m:
        await _lightweight_permissions(source, chat_id, user_id,
                                        target=(_perm_m.group(1) or "").strip(),
                                        message_thread_id=_topic_tid)
        return True

    _sw_m = _SWITCH_OAUTH_RE.match(cmd_text)
    if _sw_m:
        await _lightweight_switch_oauth(source, chat_id, user_id,
                                         label=(_sw_m.group(1) or "").strip(),
                                         message_thread_id=_topic_tid)
        return True

    if _ROLLBACK_RE.match(cmd_text):
        await _lightweight_rollback(source, chat_id, user_id, message_thread_id=_topic_tid)
        return True

    return False  # Not a lightweight command — continue processing


async def triage_and_enqueue(parsed: dict) -> None:
    """Triage an incoming message: handle commands inline, enqueue the rest.

    This replaces the old message_processor's while-loop. Called directly by
    TG handler and WA polling loop — no intermediate asyncio.Queue.

    Structure (CB-142: refactored):
      1. Setup & imports (~50 lines)
      2. Cancel handling (~70 lines) — /cancel, cancel detection, PQ cancel
      3. Lightweight command dispatch → _try_lightweight_command() (extracted)
      4. Relay mode forwarding (~200 lines) — workstation relay message handling
      5. Dedup & throttle (~50 lines) — external chat rate limiting
      6. Enqueue for SDK processing (~100 lines) — PQ enqueue + context injection
      7. Late commands (~60 lines) — /reset, /diagnose, /continue, /knowledge
      8. DM auto-onboarding (~30 lines) — first-time user detection
    """
    import main as _main_mod
    persistent_queue = _main_mod.persistent_queue

    if persistent_queue is None:
        log.warning("[PQ] Queue not initialized yet, dropping message: "
                    f"{parsed.get('text', '')[:60]}")
        return

    from config import (
        REPLY_QUOTE_MAX_CHARS,
    )
    from bot.pipeline.task_manager import task_manager
    from bot.pipeline.streaming import _kill_placeholder
    from bot.commands import (
        _CANCEL_RE, _ROLLBACK_RE, _PERMISSIONS_RE, _HARNESSES_RE, _SET_HARNESS_RE, _SWITCH_OAUTH_RE,
        _RESET_RE, _DIAGNOSE_RE, _CONTINUE_RE, _KNOWLEDGE_RE,
        _is_cancel_message, _send_to_platform_simple, _request_thread_id,
        _lightweight_knowledge,
        _lightweight_rollback, _lightweight_permissions,
        _lightweight_harnesses, _lightweight_set_harness,
        _lightweight_switch_oauth,
        _handle_reset_session,
        run_self_diagnose,
    )
    from bot.relay.ux import (
        _KNOWN_CLAUDE_SLASHES, _relay_placeholders,
        _pending_relay_queue, _handle_relay_message,
        _relay_debounce, _RELAY_DEBOUNCE_S,
    )

    text = parsed.get("text", "").strip()
    # For command detection use the original (pre-wrapper) text.  Active_plus
    # external chats have parsed["text"] prefixed with metadata by telegram.py;
    # all ^-anchored slash-command regexes fail on that prefix.  raw_text holds
    # the user's actual message; it falls back to text for non-external chats.
    cmd_text = parsed.get("raw_text", text).strip()
    # Strip @bot_username suffix from TG commands in groups (e.g. /chats@your_bot → /chats)
    if cmd_text.startswith("/"):
        first_word = cmd_text.split()[0] if cmd_text.split() else cmd_text
        if "@" in first_word:
            rest = cmd_text[len(first_word):]
            cmd_text = first_word.split("@")[0] + rest
    source = parsed.get("source") or ""  # CB-133: no platform assumption
    # Corporate mode: chat_id must be explicitly provided in parsed message.
    # No fallback to "primary group" — every message has explicit routing.
    chat_id = str(parsed.get("chat_id") or parsed.get("chat_jid") or "")
    message_id = str(parsed.get("message_id", ""))
    user_id = str(parsed.get("user_id", ""))
    # Forum topic thread_id — propagated to all lightweight send calls via:
    # 1. Explicit message_thread_id=_topic_tid on direct send_message calls
    # 2. ContextVar fallback in _send_to_platform_simple() for lightweight commands
    from bot.chat_key import normalize_thread_id
    _raw_tid = parsed.get("message_thread_id")
    _topic_tid: int | None = normalize_thread_id(_raw_tid)
    _request_thread_id.set(_topic_tid)

    # Silent mode: store message but do NOT process or respond.
    # "silent" means listen-only — the bot observes but stays quiet.
    try:
        from bot.chat_registry import get_mode as _get_chat_mode
        _chat_key = f"{source}:{chat_id}:{_topic_tid}" if _topic_tid else f"{source}:{chat_id}"
        _mode = _get_chat_mode(_chat_key)
        if _mode == "silent":
            # Store for memory/search but skip all processing
            try:
                from bot.storage.memory import store_message
                await store_message(
                    source=source, user_name=parsed.get("user_name", ""),
                    user_id=user_id, text=text, role="user_message",
                    message_id=message_id, chat_id=chat_id,
                    message_thread_id=str(_topic_tid) if _topic_tid else "",
                )
            except Exception:
                pass
            log.debug("Silent mode: stored but not processing msg from %s:%s", source, chat_id)
            return
    except RuntimeError:
        pass  # Registry not initialized yet (startup) — continue normally
    except Exception as _silent_err:
        log.warning("Silent mode check failed for %s:%s: %s", source, chat_id, _silent_err)

    # Dedup: reject messages already in the queue (Pyrogram re-delivers on reconnect)
    # CB-144: Use composite key (chat_id:thread_id) matching task_queue storage
    _dedup_chat_id = f"{chat_id}:{_topic_tid}" if _topic_tid is not None else chat_id
    if message_id and message_id != "0":
        from bot.storage.db import open_db as _open_db
        async with _open_db() as db:
            existing = await db.execute_fetchall(
                "SELECT id FROM task_queue "
                "WHERE source=? AND chat_id=? AND message_id=? AND status != 'cancelled' "
                "LIMIT 1",
                (source, _dedup_chat_id, message_id),
            )
            if existing:
                log.info(f"Dedup: dropping re-delivered msg {message_id} from {source}:{chat_id}")
                return

    try:
        # Stale messages from registered chats — enqueue as catch-up
        _is_mode_active = False
        try:
            from bot.chat_registry import get_mode as _get_reg_mode2
            _reg_mode = _get_reg_mode2(f"{source}:{chat_id}")
            _is_mode_active = _reg_mode in ("active", "active_plus")
        except Exception:
            _is_mode_active = parsed.get("is_external_chat", False)  # legacy fallback
        if parsed.get("is_stale") and _is_mode_active:
            log.info(f"Stale chat msg \u2192 PQ catch-up: chat={chat_id}")
            await persistent_queue.enqueue(parsed, is_catchup=True)
            return

        # Active GROUP chat throttling (DMs always immediate)
        if (_is_mode_active and not parsed.get("is_stale")
                and _is_active_ec_group(source, chat_id)):
            if not _is_ec_hot(chat_id):
                # Cold mode: store message for context, schedule delayed dispatch
                log.info(f"DC throttle (cold): chat={chat_id}, "
                         f"queuing for \u2264{_EC_COLD_SECS}s dispatch")
                try:
                    from bot.storage.memory import store_message
                    await store_message(
                        source,
                        parsed.get("user_name", "?"),
                        parsed.get("text", ""),
                        "user",
                        str(parsed.get("user_id", "")),
                        str(parsed.get("message_id", "")),
                        chat_id=str(chat_id),
                    )
                except Exception as e:
                    log.debug(f"DC throttle store failed: {e}")
                _schedule_ec_throttle(chat_id, parsed)
                return
            # Hot mode: fall through to normal dispatch
            log.debug(f"DC throttle (hot): chat={chat_id}, dispatching immediately")

        # Handle reply-to-placeholder cancel (TG only)
        reply_to = parsed.get("reply_to")
        if reply_to and reply_to.get("is_bot") and task_manager.active_count > 0:
            reply_msg_id = reply_to.get("message_id", 0)
            for t in task_manager.get_active_tasks():
                if t.tg_placeholder_id == reply_msg_id and _CANCEL_RE.search(cmd_text):
                    log.info(f"Cancel by reply to placeholder: task {t.task_id}")
                    t.cancel_event.set()
                    if t._asyncio_task:
                        t._asyncio_task.cancel()
                    await task_manager.fail_task(t.task_id, "Cancelled by user")
                    await _send_to_platform_simple(source, chat_id,
                        f"\U0001f6d1 Cancelled: {t.text[:60]}",
                        message_thread_id=_topic_tid)
                    return

        # Handle cancel
        if _is_cancel_message(cmd_text) and task_manager.active_count > 0:
            target = task_manager.find_cancel_target(cmd_text, chat_id=str(chat_id))
            if target:
                log.info(f"Cancelling task {target.task_id}: {target.text[:60]}")
                target.cancel_event.set()
                if target._asyncio_task:
                    target._asyncio_task.cancel()
                await task_manager.fail_task(target.task_id, "Cancelled by user")
                msg = f"\U0001f6d1 Cancelled: {target.text[:60]}"
                # Also cancel pending PQ rows for this chat/topic
                _cancel_key = f"{chat_id}:{_topic_tid}" if _topic_tid is not None else chat_id
                cleared = await persistent_queue.cancel_for_chat(_cancel_key)
                if cleared:
                    msg += f"\n\U0001f4cb Cleared {cleared} queued message(s)"
                await _send_to_platform_simple(source, chat_id, msg, message_thread_id=_topic_tid)
                # If the cancel message has additional instructions, process them
                remaining = cmd_text.lower()
                for word in ["cancel", "stop", "this task", "that task",
                             "the task", "and ", "then "]:
                    remaining = remaining.replace(word, "").strip()
                if remaining and len(remaining) > 5:
                    parsed["text"] = remaining
                    await persistent_queue.enqueue(parsed)
                return
            # Check if only PQ pending rows exist (no active tasks matched)
            pq_pending = await persistent_queue.get_pending_count()
            if pq_pending > 0:
                _cancel_key2 = f"{chat_id}:{_topic_tid}" if _topic_tid is not None else chat_id
                cleared = await persistent_queue.cancel_for_chat(_cancel_key2)
                if cleared:
                    await _send_to_platform_simple(source, chat_id,
                        f"\U0001f6d1 Cleared {cleared} queued message(s)",
                        message_thread_id=_topic_tid)
                    return

        # CB-142: Lightweight command dispatch — 30+ slash commands extracted
        # to _try_lightweight_command() for maintainability. Returns True if handled.
        if await _try_lightweight_command(cmd_text, source, chat_id, user_id, parsed, _topic_tid):
            return

        # Handle employee workstation mode — forward all messages to Mac via SDK daemon.
        # /login, /cost, /clear, etc. pass through to the daemon (see _KNOWN_CLAUDE_SLASHES).
        # Unknown slash commands would be silently dropped by Claude CLI — intercept
        # them locally with a hint so the UX is not confusing.
        # Check relay mode regardless of text presence — media-only messages
        # (no caption) must also be pushed to Mac and dispatched to daemon.
        from bot.desktop_relay import get_relay_mode, is_enabled, _relay_tasks
        if is_enabled():
            relay_entry = get_relay_mode(chat_id)
        else:
            relay_entry = None
        if text or (relay_entry and parsed.get("has_media")):
            if not text:
                text = ""  # Will be populated by media push below
            if relay_entry:
                # Parity with primary chats: enrich text with replied-to
                # context so the Mac daemon sees what the user was pointing
                # at. Primary handle_telegram_message does this at ~line 1025
                # but relay chats bypass that handler — enrichment happens
                # here instead, just before daemon dispatch. Placed AFTER
                # the cancel logic (which uses raw text) and AFTER the
                # reply-to-placeholder check so those still match as expected.
                _rt = parsed.get("reply_to")
                if _rt and _rt.get("text"):
                    _label = ("bot's previous response"
                              if _rt.get("is_bot") else "earlier message")
                    _rt_text = _rt["text"]
                    if len(_rt_text) > REPLY_QUOTE_MAX_CHARS:
                        _rt_text = _rt_text[:REPLY_QUOTE_MAX_CHARS] + "\u2026[truncated]"
                    text = (f'[Replying to {_label}: '
                            f'"{_rt_text}"]\n{text}')
                # Media push — download the attached file (own msg or reply-to
                # target) and upload to Mac's relay inbox so the daemon can
                # Read()/describe it natively. Falls back to a placeholder
                # label if the push fails so the conversation still flows.
                _media_raw = None
                _media_role = None  # "Attached" (own) or "Reply-to" (replied-to)
                if parsed.get("has_media") and parsed.get("raw"):
                    _media_raw = parsed["raw"]
                    _media_role = "Attached"
                elif _rt and _rt.get("has_media") and _rt.get("raw"):
                    _media_raw = _rt["raw"]
                    _media_role = "Replying to earlier"
                if _media_raw and source == "telegram":
                    try:
                        from integrations.telegram import (
                            download_media as _tg_dl)
                        from bot.desktop_relay import push_file as _mac_push
                        _info = await _tg_dl(_media_raw)
                        if _info and _info.get("local_path"):
                            _mtype = _info.get("media_type", "file")
                            _local = _info["local_path"]
                            # === Primary-parity preprocessing ===
                            # Mirror handle_telegram_message (lines ~1090-1135):
                            # voice/audio ALWAYS gets Gemini transcription,
                            # large non-voice files get Gemini description.
                            # The resulting text is injected alongside the
                            # Mac path so the daemon has immediate context
                            # without needing to Read() a huge file.
                            _extra = ""
                            if _mtype in ("voice", "audio"):
                                try:
                                    from integrations.gemini import (
                                        transcribe_voice)
                                    from config import (
                                        VOICE_TRANSCRIBE_TIMEOUT_S)
                                    _tr = await asyncio.wait_for(
                                        transcribe_voice(_local),
                                        timeout=VOICE_TRANSCRIBE_TIMEOUT_S,
                                    )
                                    if _tr.get("transcript"):
                                        _extra = (f'\nVoice transcript: '
                                                  f'"{_tr["transcript"]}"')
                                        log.info(
                                            "Relay voice transcribed: "
                                            f"{len(_tr['transcript'])} chars")
                                    elif _tr.get("error"):
                                        log.warning(
                                            "Relay voice transcribe failed: "
                                            f"{_tr['error']}")
                                except Exception as _te:
                                    log.warning(
                                        f"Relay voice transcribe error: {_te}")
                            else:
                                try:
                                    _fsize = _info.get("file_size") or 0
                                    if not _fsize:
                                        try:
                                            _fsize = Path(_local).stat().st_size
                                        except OSError:
                                            _fsize = 0
                                    from config import (
                                        MEDIA_INLINE_READ_MAX_KB,
                                        MEDIA_DESCRIBE_TIMEOUT_S)
                                    if _fsize > MEDIA_INLINE_READ_MAX_KB * 1024:
                                        from integrations.gemini import (
                                            describe_media)
                                        _dr = await asyncio.wait_for(
                                            describe_media(_local),
                                            timeout=MEDIA_DESCRIBE_TIMEOUT_S,
                                        )
                                        if _dr.get("description"):
                                            _extra = (f'\n[AI description: '
                                                      f'{_dr["description"]}]')
                                            log.info(
                                                "Relay large-file described: "
                                                f"{len(_dr['description'])} chars, "
                                                f"{_fsize/1024:.0f}KB")
                                except Exception as _de:
                                    log.warning(
                                        f"Relay media describe error: {_de}")
                            # Push to Mac inbox (existing path injection)
                            _ok, _res = await _mac_push(_local)
                            if _ok:
                                _line = f"[{_media_role} {_mtype}: {_res}]"
                            else:
                                _line = (f"[{_media_role} {_mtype} "
                                         f"\u2014 push failed: {_res}]")
                                log.warning(
                                    f"Relay media push failed: {_res}")
                            text = f"{_line}{_extra}\n{text}".rstrip()
                        else:
                            log.warning(
                                "Relay media: download returned no path")
                    except Exception as _me:
                        log.warning(
                            f"Relay media handling error: {_me}",
                            exc_info=True)
                elif _rt and _rt.get("message_id") and _rt.get("has_media"):
                    # Reply-to media but no raw reference to download from
                    # (message older than re-fetch horizon). Keep the old
                    # placeholder so the daemon at least sees the intent.
                    text = f"[Replying to earlier media message]\n{text}"
                # Forward context — mirrors primary-chat handler.
                # Wraps user's own text (or empty) with origin + SYSTEM hint
                # so Mac Claude acknowledges rather than blindly actioning.
                _fwd = parsed.get("forward") or (
                    {"sender_name": "someone"} if parsed.get("is_forwarded") else None
                )
                if _fwd:
                    _name = _fwd.get("sender_name", "someone") or "someone"
                    text = (
                        f"[Forwarded message from {_name}]\n{text}\n\n"
                        f"[SYSTEM: This is a forwarded message. The user did "
                        f"not add their own instructions. Briefly acknowledge "
                        f"the content and ask what they'd like you to do with it.]"
                    )
                if relay_entry and _is_cancel_message(cmd_text):
                    # Relay cancel keyword: kills the Mac daemon (terminates the
                    # in-flight `claude` subprocess), drains any queued mid-task
                    # injections, deletes the placeholder, sends confirmation.
                    # Mirrors primary-chat cancel UX. Next relay message respawns
                    # the daemon (~1s cold-start cost) — acceptable tradeoff
                    # given no per-query cancel exists in the pool today.
                    from bot.desktop_relay import kill_daemon as _kill_daemon
                    _chat_s = str(chat_id)
                    prior = _relay_tasks.get(_chat_s)
                    queued_n = len(_pending_relay_queue.get(_chat_s) or [])
                    # Drain queue + debounce buffer so `finally`-drain in the
                    # prior task doesn't immediately respawn on cancelled msg.
                    _pending_relay_queue.pop(_chat_s, None)
                    _db_cancel = _relay_debounce.pop(_chat_s, None)
                    if _db_cancel and _db_cancel.get("timer"):
                        _db_cancel["timer"].cancel()
                    # Cancel the asyncio coroutine driving _handle_relay_message
                    if prior and not prior.done():
                        try:
                            prior.cancel()
                        except Exception as _exc:
                            log.debug("Suppressed: %s", _exc)
                    try:
                        _kres = await _kill_daemon(_chat_s)
                    except Exception as _ke:
                        log.warning(f"Relay cancel: kill_daemon failed "
                                    f"chat={_chat_s}: {_ke}")
                        _kres = {"ok": False}
                    # Release pool + eager respawn so daemon is ready for next msg
                    try:
                        from bot.relay.pool import get_pool as _gp_cancel
                        await _gp_cancel().release(_chat_s)
                    except Exception as _exc:
                        log.debug("Suppressed: %s", _exc)
                    if _kres.get("ok"):
                        try:
                            from bot.desktop_relay import eager_respawn_daemon as _erd
                            asyncio.create_task(_erd(_chat_s))
                        except Exception as _exc:
                            log.debug("Suppressed: %s", _exc)
                    _ph = _relay_placeholders.pop(_chat_s, None)
                    if source == "telegram" and _ph:
                        try:
                            from integrations.telegram import edit_message as _em
                            await _em(_ph,
                                      "\u274c Cancelled.",
                                      chat_id=int(chat_id),
                                      parse_mode="html")
                        except Exception:
                            try:
                                from integrations.telegram import delete_message as _dm
                                await _dm(_ph, chat_id=int(chat_id))
                            except Exception as _exc:
                                log.debug("Suppressed: %s", _exc)
                            await _send_to_platform_simple(
                                source, chat_id, "\u274c Cancelled.",
                                parse_mode="HTML",
                                message_thread_id=_topic_tid)
                    else:
                        await _send_to_platform_simple(
                            source, chat_id, "\u274c Cancelled.",
                            parse_mode="HTML",
                            message_thread_id=_topic_tid)
                    log.info(
                        f"Relay cancel: chat={_chat_s} "
                        f"prior_alive={bool(prior and not prior.done())} "
                        f"queue_drained={queued_n} "
                        f"daemon_kill_ok={_kres.get('ok', False)}"
                    )
                    return
                if relay_entry and text.startswith("/"):
                    # Peel the first token (command word, no args) and lowercase.
                    _first = text.split(None, 1)[0].split("@", 1)[0].lower()
                    if _first not in _KNOWN_CLAUDE_SLASHES:
                        asyncio.create_task(_send_to_platform_simple(
                            source, chat_id,
                            f"\U0001f6ab <code>{_html.escape(_first)}</code> isn't a bot-side command. "
                            f"Bot-side slashes: <code>/session</code>, "
                            f"<code>/session list</code>, <code>/menu</code>, "
                            f"<code>/auth</code>, <code>/mac</code>, <code>/mac-reauth</code>, "
                            f"<code>/mac-accounts</code>, <code>/mac-set-account</code>, "
                            f"<code>/lock</code>, <code>/status</code>, <code>/cancel</code>.\n\n"
                            f"Claude CLI built-in slashes (<code>/cost</code>, <code>/clear</code>, "
                            f"<code>/compact</code>, <code>/model</code>, etc.) don't execute via "
                            f"<code>claude --print</code> \u2014 they're interactive-REPL only. "
                            f"Type them on Desktop Claude.app directly.",
                            parse_mode="HTML",
                            message_thread_id=_topic_tid))
                        return
                if relay_entry:
                    session_name = relay_entry.get("session_name", "")
                    project_path = relay_entry.get("project_path", "")
                    pinned_jsonl = relay_entry.get("pinned_jsonl_path", "")
                    chat_s = str(chat_id)
                    # Relay mid-task behaviour — stdin-open injection (Apr 17
                    # 2026, see fact `relay_cli_stdin_reentrant`). If a prior
                    # _handle_relay_message task is running, spawn a CONCURRENT
                    # task that writes the new user frame onto the SAME live
                    # claude subprocess's stdin. Daemon-side FIFO correlation
                    # routes stream entries back to the correct qid. Both
                    # tasks' replies stream to the user; the new message gets
                    # its own "Noted..." placeholder under it.
                    prior = _relay_tasks.get(chat_s)
                    is_injection_flag = bool(prior and not prior.done())
                    if is_injection_flag:
                        # Swap placeholder: delete old, create "Noted..." under new msg.
                        # The new task's _handle_relay_message will pick up this
                        # "Noted..." placeholder via _relay_placeholders lookup.
                        if source == "telegram":
                            old_ph = _relay_placeholders.pop(chat_s, None)
                            if old_ph:
                                try:
                                    from integrations.telegram import delete_message as _del
                                    await _del(old_ph, chat_id=int(chat_id))
                                except Exception as _exc:
                                    log.debug("Suppressed: %s", _exc)
                            try:
                                from integrations.telegram import send_message as _send
                                _rt = int(message_id) if message_id and message_id.isdigit() else None
                                ph = await _send(
                                    "\U0001f9e0 Noted...",
                                    chat_id=int(chat_id),
                                    reply_to_message_id=_rt,
                                    parse_mode="html",
                                    message_thread_id=_topic_tid,
                                )
                                if ph and ph.get("message_id"):
                                    _relay_placeholders[chat_s] = ph["message_id"]
                            except Exception as _exc:
                                log.debug("Suppressed: %s", _exc)
                        log.info(f"Relay mid-task inject: spawning concurrent task "
                                 f"chat={chat_id} (prior still running)")
                        _relay_new_task = asyncio.create_task(
                            _handle_relay_message(source, chat_id, text, session_name,
                                                  message_id=message_id,
                                                  project_path=project_path,
                                                  pinned_jsonl_path=pinned_jsonl,
                                                  is_injection=is_injection_flag)
                        )
                        _relay_tasks[chat_s] = _relay_new_task
                        return

                    # --- Burst debounce (FB-50) ---
                    # No daemon running — buffer messages for 150ms to coalesce
                    # rapid-fire bursts into a single daemon query. Mirrors PQ
                    # dispatch debounce for primary chats.
                    existing_db = _relay_debounce.get(chat_s)
                    if existing_db:
                        # Already debouncing — merge into buffer
                        existing_db["texts"].append(text)
                        existing_db["last_msg_id"] = message_id
                        log.info("Relay debounce: merged msg into buffer "
                                 "chat=%s total=%d", chat_id, len(existing_db["texts"]))
                        return

                    # Start new debounce timer
                    db_entry: dict = {
                        "texts": [text],
                        "last_msg_id": message_id,
                        "source": source,
                        "session_name": session_name,
                        "project_path": project_path,
                        "pinned_jsonl": pinned_jsonl,
                        "chat_id": chat_id,
                    }

                    async def _fire_relay_debounce(_cs: str = chat_s) -> None:
                        await asyncio.sleep(_RELAY_DEBOUNCE_S)
                        data = _relay_debounce.pop(_cs, None)
                        if not data:
                            return
                        coalesced = "\n\n".join(data["texts"])
                        _cid = data["chat_id"]
                        log.info("Relay debounce fired: chat=%s msgs=%d",
                                 _cid, len(data["texts"]))
                        task = asyncio.create_task(
                            _handle_relay_message(
                                data["source"], _cid, coalesced,
                                data["session_name"],
                                message_id=data["last_msg_id"],
                                project_path=data["project_path"],
                                pinned_jsonl_path=data["pinned_jsonl"])
                        )
                        _relay_tasks[_cs] = task

                    db_entry["timer"] = asyncio.create_task(
                        _fire_relay_debounce()
                    )
                    _relay_debounce[chat_s] = db_entry
                    return


        # Handle admin session reset command (no SDK, instant response)
        if _RESET_RE.match(cmd_text):
            await _handle_reset_session(source, chat_id, user_id, cmd_text, message_thread_id=_topic_tid)
            return

        # Handle self-diagnose command (RBAC-gated)
        if _DIAGNOSE_RE.match(cmd_text):
            from bot.commands import _check_command_access, _deny_command
            if not await _check_command_access(source, chat_id, user_id, "diagnose"):
                await _deny_command(source, chat_id, "diagnose", message_thread_id=_topic_tid)
                return
            log.info(f"Self-diagnose requested by {parsed.get('user_name')}")
            asyncio.create_task(run_self_diagnose(source, chat_id, message_thread_id=_topic_tid))
            return

        # Handle /knowledge (lightweight — knowledge domains view)
        if _KNOWLEDGE_RE.match(cmd_text):
            log.info(f"/knowledge requested by {parsed.get('user_name')}")
            await _lightweight_knowledge(source, chat_id, user_id, message_thread_id=_topic_tid)
            return

        # Handle /permissions [target] command (admin, lightweight)
        m_perm = _PERMISSIONS_RE.match(cmd_text)
        if m_perm:
            log.info(f"/permissions requested by {parsed.get('user_name')}")
            await _lightweight_permissions(source, chat_id, user_id,
                                           target=m_perm.group(1) or "",
                                           message_thread_id=_topic_tid)
            return

        # Handle rollback command (admin, lightweight — no SDK)
        m_rb = _ROLLBACK_RE.match(cmd_text)
        if m_rb:
            log.info(f"Rollback requested by {parsed.get('user_name')} confirm={bool(m_rb.group(1))}")
            await _lightweight_rollback(source, chat_id, user_id, confirm=bool(m_rb.group(1)), message_thread_id=_topic_tid)
            return

        # Handle harness commands
        m_h = _HARNESSES_RE.match(cmd_text)
        if m_h:
            await _lightweight_harnesses(source, chat_id, user_id, specific=m_h.group(1), message_thread_id=_topic_tid)
            return
        m_sh = _SET_HARNESS_RE.match(cmd_text)
        if m_sh:
            if not m_sh.group(1):
                await _send_to_platform_simple(source, chat_id,
                    "Usage: <code>/set-harness &lt;label&gt;</code>\n"
                    "Example: <code>/set-harness teamwork</code>\n\n"
                    "Use /harnesses to see available profiles.",
                    parse_mode="HTML", message_thread_id=_topic_tid)
                return
            await _lightweight_set_harness(source, chat_id, user_id, label=m_sh.group(1), message_thread_id=_topic_tid)
            return
        m_so = _SWITCH_OAUTH_RE.match(cmd_text)
        if m_so:
            if not m_so.group(1):
                await _send_to_platform_simple(source, chat_id,
                    "Usage: <code>/claude-set-oauth &lt;label&gt;</code>\n"
                    "Example: <code>/claude-set-oauth main</code>\n\n"
                    "Use /claude-accounts to see available profiles.",
                    parse_mode="HTML", message_thread_id=_topic_tid)
                return
            await _lightweight_switch_oauth(
                source, chat_id, user_id,
                label=m_so.group(1),
                all_chats=m_so.group(2) or "",
                message_thread_id=_topic_tid,
            )
            return

        # Handle "continue" — no-op when tasks are active
        if _CONTINUE_RE.match(cmd_text) and task_manager.active_count > 0:
            log.info("Continue command while task active — no-op")
            await _send_to_platform_simple(source, chat_id, "\U0001f44d Still working on it.", message_thread_id=_topic_tid)
            return

        # Mark user activity for reply-threading interleave detection
        from bot.hooks import mark_chat_activity
        mark_chat_activity(chat_id, "user")

        # Pre-enrich text with reply-to / forward wrappers so both the
        # in-flight inject path AND the pending-merge path see the same
        # model-visible context that handle_telegram_message builds for
        # freshly-enqueued rows (lines ~1035-1091). Text-only enrichment
        # here — media attachments are carried via parsed["raw"] and
        # re-downloaded by the dispatcher; their caption goes through
        # parsed["text"] which we wrap below.
        enriched_text = text
        _reply_ctx = parsed.get("reply_to")
        if _reply_ctx and _reply_ctx.get("text"):
            _rlabel = ("bot's previous response"
                       if _reply_ctx.get("is_bot") else "earlier message")
            _rq = _reply_ctx["text"]
            if len(_rq) > REPLY_QUOTE_MAX_CHARS:
                _rq = _rq[:REPLY_QUOTE_MAX_CHARS] + "\u2026[truncated]"
            enriched_text = f'[Replying to {_rlabel}: "{_rq}"]\n{enriched_text}'
        _fwd_ctx = parsed.get("forward")
        if _fwd_ctx:
            _fname = _fwd_ctx.get("sender_name", "someone") or "someone"
            enriched_text = (
                f"[Forwarded message from {_fname}]\n{enriched_text}\n\n"
                f"[SYSTEM: This is a forwarded message. The user did not add "
                f"their own instructions. Briefly acknowledge the content and "
                f"ask what they'd like you to do with it.]"
            )

        # v5 decision order — PENDING-MERGE WINS OVER INJECT:
        # 1) if a pending PQ row exists for this chat -> merge (text+media)
        # 2) else if text-only + in-flight SDK -> inject into running query
        # 3) else -> enqueue new row
        #
        # Why merge-first: once ANY msg is queued, every subsequent burst msg
        # coalesces into it. T (if running) answers its original scope; R1
        # answers everything that came after, as ONE task with coherent
        # context. UX invariant: each reply threads to the LATEST user msg it
        # included (see handle_telegram_message threading fix below).
        # Forum topic routing: include thread_id in session_key for per-topic sessions
        thread_id = parsed.get("message_thread_id")
        if thread_id is not None:
            session_key = f"{source}:{chat_id}:{thread_id}"
        else:
            session_key = f"{source}:{chat_id}"
        has_media = parsed.get("has_media") or parsed.get("media_info")

        # Step 1: try pending-merge (text OR media)
        if enriched_text or has_media:
            merged_row = await persistent_queue.merge_into_pending(
                parsed, enriched_text
            )
            if merged_row is not None:
                user = parsed.get("user_name", "?")
                log.info(
                    f"Merge OK: {user}'s msg {message_id} coalesced "
                    f"into pending row={merged_row} chat={chat_id}"
                )
                try:
                    from bot.storage.memory import store_message
                    await store_message(
                        source, user, text, "user",
                        user_id, message_id, chat_id=chat_id,
                    )
                except Exception as _se:
                    log.debug(f"Merge store_message failed: {_se}")
                return

        # Step 2: try in-flight inject (text-only, no pending row existed)
        # Skip injection if a deferred session reset or budget disconnect is
        # pending — the query is about to finish and the session will be wiped,
        # so the injected text would be silently lost.
        from bot.agent import client_pool, has_pending_teardown
        if (not has_media and enriched_text
                and client_pool.is_processing(session_key)
                and not has_pending_teardown(session_key)):
            user_name = parsed.get("user_name", "?")
            _platform_tags = {"telegram": "[Telegram]", "teams": "[Teams]"}
            platform_tag = _platform_tags.get(source, f"[{source}]")
            inject_prompt = (
                f"NEW MESSAGE {platform_tag} from {user_name} "
                f"(user ID: {user_id}, message ID: {message_id}):\n"
                f"{enriched_text}\n\n"
                f"The user sent this WHILE you were processing the previous request. "
                f"Incorporate this into your current work."
            )
            injected = await client_pool.inject_message(session_key, inject_prompt)
            if injected:
                log.info(f"Mid-task inject OK for {session_key}: {text[:60]}")
                # Store message for context (but don't enqueue as separate task)
                try:
                    from bot.storage.memory import store_message
                    await store_message(source, user_name, text, "user", user_id,
                                       message_id, chat_id=chat_id)
                except Exception as e:
                    log.debug(f"Injection store_message failed: {e}")
                # Re-create streaming placeholder under the injected message
                # so the user sees visual confirmation it was picked up
                if source == "telegram":
                    try:
                        active_task = None
                        for t in task_manager.get_active_tasks():
                            if t.chat_id == chat_id:
                                active_task = t
                                break
                        if active_task and active_task.tg_placeholder_id:
                            from integrations.telegram import send_message, delete_message
                            # Delete old placeholder — processing switched, not forked
                            old_ph_id = active_task.tg_placeholder_id
                            _kill_placeholder(active_task)
                            try:
                                await delete_message(old_ph_id, chat_id=int(chat_id))
                            except Exception as _exc:
                                log.debug("Suppressed: %s", _exc)
                            # Create new placeholder as reply to the injected message
                            tg_cid = int(chat_id)
                            reply_to = int(message_id) if message_id and message_id.isdigit() else None
                            ph = await send_message(
                                "\U0001f9e0 Noted...",
                                chat_id=tg_cid,
                                reply_to_message_id=reply_to,
                                parse_mode="html",
                                message_thread_id=_topic_tid,
                            )
                            if ph and ph.get("message_id"):
                                active_task.tg_placeholder_id = ph["message_id"]
                                active_task.tg_placeholder_alive = True
                                active_task.tg_last_status_text = ""
                                active_task.tg_last_edit = time.time()
                                log.info(f"Placeholder re-created under msg {message_id} "
                                         f"\u2192 {ph['message_id']}")
                    except Exception as e:
                        log.debug(f"Placeholder re-creation failed: {e}")
                return
            # Injection failed — fall through to normal enqueue
            log.warning(f"Mid-task inject failed for {session_key}, falling back to enqueue")

        # Onboarding detection — check if this is a new employee's first message
        # Runs before enqueue so the welcome message arrives before normal processing.
        # Idempotent: returns False if user already has an onboarding record.
        try:
            from bot.onboarding.detection import check_and_start_onboarding
            _onboarded = await check_and_start_onboarding(
                source=source,
                user_id=user_id,
                display_name=parsed.get("user_name", ""),
                chat_id=chat_id,
            )
            if _onboarded:
                log.info("Onboarding started for %s:%s — consuming first message", source, user_id)
                return  # Don't enqueue — onboarding welcome is the response
        except Exception as _ob_err:
            log.warning("Onboarding detection failed (non-blocking): %s", _ob_err)

        # Onboarding resume — intercept /skip, wizard commands, and block SDK
        # inference while auth steps are pending (no OAuth = no SDK session).
        try:
            from bot.onboarding.detection import check_onboarding_resume
            if await check_onboarding_resume(
                source=source, user_id=user_id, chat_id=chat_id,
                message_text=text,
            ):
                log.info("Onboarding wizard consumed message for %s:%s", source, user_id)
                return
        except Exception as _ob_err:
            log.debug("Onboarding resume check failed (non-blocking): %s", _ob_err)

        # Step 3: Enqueue into PQ — worker handles capacity gating and dispatch
        user = parsed.get("user_name", "?")
        log.info(f"Enqueuing task for {user}: {text[:60]}")
        _row_id, evicted = await persistent_queue.enqueue(parsed)

        # Notify user if older messages were evicted due to queue overflow
        # Single short message — no spam, no per-message listing
        if evicted:
            await _send_to_platform_simple(source, chat_id,
                f"\u26a0\ufe0f Queue full \u2014 {len(evicted)} older queued message(s) dropped.",
                        message_thread_id=_topic_tid)

    except Exception as e:
        log.error(f"Triage error: {e}", exc_info=True)
        try:
            await _send_to_platform_simple(source, chat_id,
                f"\u26a0\ufe0f Message processing failed: {str(e)[:200]}",
                        message_thread_id=_topic_tid)
        except Exception as e2:
            log.debug(f"Error notification failed: {e2}")
