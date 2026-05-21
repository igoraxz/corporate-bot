# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Suspicious response detection and notification.

Extracted from bot/agent.py (Phase 0 refactor).
"""

import logging

log = logging.getLogger(__name__)

# --- Suspicious Response Detection ---
# Thresholds for detecting responses that indicate session corruption,
# API anomalies, or caching issues. Debug-only -- no retries, just logging.
SUSPICIOUS_FAST_MULTI_TURN_S = 5.0   # multi-turn (>2) can't complete this fast
SUSPICIOUS_FAST_TOOLS_S = 3.0        # tools ran but near-empty result
SUSPICIOUS_INSTANT_EMPTY_S = 2.0     # zero turns, zero output
SUSPICIOUS_MIN_RESULT_LEN = 100      # below this with tools = suspicious


def _check_suspicious_response(
    elapsed: float,
    num_turns: int,
    tools_used: list[str],
    send_tool_called: bool,
    result_text: str,
    usage: dict | None,
    session_key: str,
    model: str,
    cost: float | None,
) -> dict | None:
    """Check if a query response looks suspicious (session corruption, API anomaly).

    Returns a diagnostic dict if suspicious, None if normal.
    Detection criteria are structurally decisive (AP-12): each rule targets
    a combination that is physically impossible in a healthy response.
    """
    from bot.agent import _total_input_tokens, _context_window_for_model

    result_len = len(result_text) if result_text else 0
    reasons: list[str] = []

    # Rule 1: Impossibly fast multi-turn
    # Each tool-calling turn needs API roundtrip (~1-2s min). 3+ turns in <5s is impossible.
    if elapsed < SUSPICIOUS_FAST_MULTI_TURN_S and num_turns > 2:
        reasons.append(
            f"impossibly_fast_multi_turn: {elapsed:.1f}s with {num_turns} turns"
        )

    # Rule 2: Ghost tools -- tools apparently ran but produced near-nothing
    # and no message was sent. Indicates cached/stale response replay.
    if (elapsed < SUSPICIOUS_FAST_TOOLS_S
            and tools_used
            and not send_tool_called
            and result_len < SUSPICIOUS_MIN_RESULT_LEN):
        reasons.append(
            f"ghost_tools: {elapsed:.1f}s, {len(tools_used)} tools, "
            f"result={result_len} chars, no send"
        )

    # Rule 3: Empty instant -- zero turns, zero output, session didn't engage
    if (elapsed < SUSPICIOUS_INSTANT_EMPTY_S
            and num_turns == 0
            and result_len == 0):
        reasons.append(f"empty_instant: {elapsed:.1f}s, 0 turns, 0 chars")

    if not reasons:
        return None

    # Gather full diagnostic context
    total_ctx = _total_input_tokens(usage) if usage else 0
    ctx_pct = total_ctx * 100 // _context_window_for_model(model) if total_ctx > 0 else 0
    inp = usage.get("input_tokens", 0) if usage else 0
    out = usage.get("output_tokens", 0) if usage else 0
    cache_read = usage.get("cache_read_input_tokens", 0) if usage else 0
    cache_create = usage.get("cache_creation_input_tokens", 0) if usage else 0
    from bot.hooks import compacted_sessions, pending_compaction
    recently_compacted = session_key in compacted_sessions
    parent_compacted = False

    diagnosis = {
        "reasons": reasons,
        "elapsed": round(elapsed, 2),
        "num_turns": num_turns,
        "tools_used": tools_used,
        "send_tool_called": send_tool_called,
        "result_len": result_len,
        "result_preview": (result_text[:200] if result_text else ""),
        "session_key": session_key,
        "is_fork": False,  # deprecated -- kept for compat
        "model": model,
        "cost_usd": round(cost, 4) if cost else None,
        "ctx_tokens": total_ctx,
        "ctx_pct": ctx_pct,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read": cache_read,
        "cache_create": cache_create,
        "recently_compacted": recently_compacted,
        "parent_compacted": parent_compacted,
        "pending_compaction": bool(pending_compaction.get(session_key)),
    }
    return diagnosis


async def _notify_suspicious_response(
    diagnosis: dict, session_key: str, source: str = "", chat_id: str = "",
):
    """Log extensive diagnostics and notify admin about suspicious response."""
    reasons = diagnosis["reasons"]
    reason_str = "; ".join(reasons)

    # Detailed structured log for post-mortem analysis
    log.warning(
        f"SUSPICIOUS_RESPONSE detected for {session_key}: {reason_str} | "
        f"elapsed={diagnosis['elapsed']}s turns={diagnosis['num_turns']} "
        f"tools=[{', '.join(diagnosis['tools_used'])}] "
        f"send_called={diagnosis['send_tool_called']} "
        f"result={diagnosis['result_len']}chars "
        f"ctx={diagnosis['ctx_tokens']//1000}k/{diagnosis['ctx_pct']}% "
        f"inp={diagnosis['input_tokens']} out={diagnosis['output_tokens']} "
        f"cache_read={diagnosis['cache_read']} cache_create={diagnosis['cache_create']} "
        f"cost=${diagnosis['cost_usd'] or 0} model={diagnosis['model']} "
        f"fork={diagnosis['is_fork']} "
        f"compacted={diagnosis['recently_compacted']} "
        f"parent_compacted={diagnosis['parent_compacted']} "
        f"pending_compaction={diagnosis['pending_compaction']}"
    )
    if diagnosis["result_preview"]:
        log.warning(
            f"SUSPICIOUS_RESPONSE result preview: {diagnosis['result_preview']!r}"
        )

    # Build admin-facing notification (only for admin chats)
    try:
        parts = session_key.split(":")
        if len(parts) < 2:
            return
        src, cid = parts[0], parts[1]

        # Only notify in admin/team chats
        lines = [
            "\u26a0\ufe0f <b>Suspicious response detected</b>",
            "",
            f"<b>Reasons:</b> {reason_str}",
            f"<b>Elapsed:</b> {diagnosis['elapsed']}s | <b>Turns:</b> {diagnosis['num_turns']}",
            f"<b>Tools:</b> {', '.join(diagnosis['tools_used']) or 'none'}",
            f"<b>Send called:</b> {diagnosis['send_tool_called']}",
            f"<b>Result:</b> {diagnosis['result_len']} chars",
            f"<b>Context:</b> {diagnosis['ctx_tokens']//1000}k tokens ({diagnosis['ctx_pct']}%)",
            f"<b>Cache:</b> read={diagnosis['cache_read']}, create={diagnosis['cache_create']}",
            f"<b>Cost:</b> ${diagnosis['cost_usd'] or 0}",
            f"<b>Model:</b> {diagnosis['model']}",
            f"<b>Session:</b> {'fork' if diagnosis['is_fork'] else 'main'}",
            f"<b>Recently compacted:</b> {diagnosis['recently_compacted']}",
        ]
        if diagnosis["result_preview"]:
            preview = diagnosis["result_preview"][:150].replace("<", "&lt;").replace(">", "&gt;")
            lines.append(f"<b>Preview:</b> <code>{preview}</code>")
        lines.append("")
        lines.append("<i>Debug info logged. No retry -- investigating root causes.</i>")

        msg = "\n".join(lines)
        if src == "telegram":
            from integrations.telegram import send_message as tg_send
            await tg_send(msg, chat_id=int(cid), parse_mode="HTML")
    except Exception as e:
        log.warning(f"Failed to notify suspicious response for {session_key}: {e}")
