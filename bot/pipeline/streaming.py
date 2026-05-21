# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""TG placeholder/ticker logic.

Handles real-time streaming UI: thinking indicators, tool-chain display,
and zombie detection.
"""

import asyncio
import logging
import time

from config import TASK_STALE_TIMEOUT_S

from bot.pipeline.task_manager import ActiveTask, task_manager

log = logging.getLogger(__name__)


# === TG STREAMING (real-time thinking + tools + text in one placeholder) ===

def _kill_placeholder(task: ActiveTask):
    """Mark placeholder as dead and clear all refs — no further edit/delete attempts."""
    task.tg_placeholder_alive = False
    task.tg_placeholder_id = None
    task.tg_last_status_text = ""


def _cancel_if_pending(t: asyncio.Task | None) -> None:
    """Cancel an asyncio Task if it exists and isn't already done. No-op otherwise.

    Used for eager-cancelling the deferred "🧠 Thinking..." placeholder creator
    (_ph_deferred) from multiple sites in handle_telegram_message without
    duplicating the None+done guard. CancelledError is swallowed in the
    deferred task's own except block; caller does not need to await.
    """
    if t is not None and not t.done():
        t.cancel()


async def _tg_safe_edit(task: ActiveTask, new_text: str, chat_id: int | str = None,
                        parse_mode: str | None = None):
    """Edit a TG placeholder message, handling errors gracefully.

    Distinguishes permanent errors (message deleted/not found) from transient
    ones (rate limits, timeouts). Only kills placeholder on permanent errors.
    """
    if not task.tg_placeholder_id or not task.tg_placeholder_alive:
        return
    if not new_text or not new_text.strip():
        log.debug("Skipping empty placeholder edit — would delete the message")
        return
    if new_text == task.tg_last_status_text:
        return
    task.tg_last_status_text = new_text
    try:
        from integrations.telegram import edit_message
        cid = chat_id or int(task.chat_id)
        await edit_message(task.tg_placeholder_id, new_text, chat_id=cid,
                           parse_mode=parse_mode)
    except RuntimeError:
        # Permanent error — message was deleted or not found
        log.info(f"Placeholder {task.tg_placeholder_id} permanently dead")
        _kill_placeholder(task)
    except Exception as e:
        # Transient error (rate limit, timeout) — keep alive for retry
        log.debug(f"Placeholder edit transient error (will retry): {e}")


def _animated_dots(task: ActiveTask) -> str:
    """Return animated dots based on elapsed time."""
    n = int(task.elapsed_seconds()) % 4
    return "." * (n + 1)


def _build_tg_status(task: ActiveTask) -> str:
    """Build a combined TG placeholder showing thinking + tools + streamed text."""
    elapsed = task.elapsed()
    dots = _animated_dots(task)

    if task.phase == "streaming":
        return task.streaming_text[:4096]
    elif task.phase == "failed":
        return f"⚠️ Error: {task.error[:200]}"

    # Check if compaction is in progress for this task's session
    from bot.hooks import pending_compaction
    from bot.chat_key import build_chat_key_from_task
    session_key = build_chat_key_from_task(task)
    if not task.use_main_session:
        session_key += f":task-{task.task_id}"
    compact_info = pending_compaction.get(session_key)
    if compact_info and compact_info.get("started_at"):
        approx_tokens = compact_info.get("approx_tokens", 0)
        from bot.agent import _context_window_for_model
        from config import get_model_for_chat
        ctx_model = get_model_for_chat(str(task.chat_id))
        ctx_window = _context_window_for_model(ctx_model)
        pct = approx_tokens * 100 // ctx_window if approx_tokens > 0 else 0
        tok_info = f" ({approx_tokens // 1000}k, {pct}%)" if approx_tokens >= 1000 else ""
        return f"\U0001f9f9 Compacting context{tok_info}{dots} ({elapsed})"

    # Show tools chain if we have any
    if task.tool_labels_seen:
        recent = task.tool_labels_seen[-5:]
        tools_line = " → ".join(recent)
        return f"🔧 {tools_line}{dots}  ({elapsed})"

    if task.phase == "thinking":
        return f"🧠 Thinking{dots} ({elapsed})"

    return f"🧠 Processing{dots} ({elapsed})"


async def _check_sdk_liveness(task: ActiveTask) -> str:
    """Check if the SDK subprocess behind a task is still alive.

    Returns a human-readable reason string for the zombie state.
    Uses SDK transport internals as a deterministic probe (not heuristic).
    """
    from bot.agent import client_pool
    from bot.chat_key import build_chat_key_from_task
    # Derive session_key from task metadata (matches agent.py logic)
    session_key = build_chat_key_from_task(task)
    if not task.use_main_session:
        session_key += f":task-{task.task_id}"
    mc = client_pool.get_client(session_key)
    if not mc or not mc.client:
        return "SDK client not found"
    try:
        transport = getattr(mc.client, "_transport", None)
        if transport is None:
            return "no SDK transport"
        proc = getattr(transport, "_process", None)
        if proc and proc.returncode is not None:
            return f"SDK process exited (code {proc.returncode})"
        is_ready = getattr(transport, "is_ready", None)
        if is_ready and not is_ready():
            return "SDK transport not ready"
    except Exception as e:
        return f"liveness check error: {e}"
    return f"no SDK activity for {int(time.time() - task.last_sdk_activity)}s"


def _get_task_timeout(task: ActiveTask) -> int:
    """Get query_timeout_s from the task's chat harness (default: TASK_STALE_TIMEOUT_S).

    Uses build_chat_key_from_task() to include thread_id for forum topics,
    so the topic's harness is resolved — not the parent group's harness.
    """
    from bot.chat_key import build_chat_key_from_task
    chat_key = build_chat_key_from_task(task)
    try:
        from bot.harness import get_harness_for_chat, resolve_field
        h = get_harness_for_chat(chat_key)
        val = resolve_field(h, "query_timeout_s", chat_key)
        if val is not None:
            return max(0, int(val))
    except Exception as e:
        log.debug("Harness timeout lookup failed for %s: %s", chat_key, e)
    return TASK_STALE_TIMEOUT_S


async def _tg_ticker(task: ActiveTask):
    """Background ticker: update TG placeholder with current status.

    Runs every 1s but only edits every 5s to avoid TG rate limits.
    During streaming phase, on_stream_chunk handles edits directly,
    but if no update for 60s, ticker sends a heartbeat to prevent
    the appearance of a stuck process.
    Exits only when task is done/failed/cancelled.
    """
    tick = 0
    last_edit_time = time.time()
    while not task.cancel_event.is_set() and task.phase not in ("done", "failed"):
        await asyncio.sleep(1)
        if task.phase in ("done", "failed"):
            break
        tick += 1
        if not task.tg_placeholder_alive:
            continue

        now = time.time()

        # --- Zombie session detection ---
        # If SDK hasn't produced any activity for query_timeout_s (harness-configurable),
        # the session is likely dead (e.g. post-compaction zombie). Kill the task.
        zombie_timeout = _get_task_timeout(task)
        idle = now - task.last_sdk_activity
        if zombie_timeout > 0 and idle > zombie_timeout:
            reason = await _check_sdk_liveness(task)
            idle_min = int(idle) // 60
            log.warning(f"Zombie detected for task {task.task_id}: {reason} (idle {idle_min}m)")
            zombie_msg = f"⚠️ Task stuck — no SDK activity for {idle_min}min ({reason}). Cancelling."
            await _tg_safe_edit(task, zombie_msg)
            await task_manager.fail_task(task.task_id, f"zombie: {reason}")
            task.cancel_event.set()
            if task._asyncio_task and not task._asyncio_task.done():
                task._asyncio_task.cancel()
            break

        # During streaming, on_stream_chunk handles edits — but send heartbeat
        # if no update has happened for 60+ seconds (prevents appearing stuck)
        if task.phase == "streaming":
            if now - last_edit_time >= 60:
                elapsed = task.elapsed()
                heartbeat = f"⏳ Still working... ({elapsed})"
                await _tg_safe_edit(task, heartbeat)
                last_edit_time = now
            continue

        # Only edit every 5s to avoid Telegram rate limits
        if tick % 5 != 0:
            continue
        status = _build_tg_status(task)
        await _tg_safe_edit(task, status)
        last_edit_time = now


