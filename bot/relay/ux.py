# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Workstation channel UX: catchup rendering, desktop turn forwarding, file handling,
and the main workstation channel message handler.

Provides UX for employee workstation control channels — rendering catchup digests,
forwarding desktop activity, and handling file transfers.
All bot.*, config.*, integrations.* imports are LAZY (inside function bodies)
to avoid circular import issues.
"""

import asyncio
import logging
import re
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

# ── Platform-aware relay transport ──────────────────────────────────────
# Import transport functions for Teams-compatible relay sends.
# Individual call sites in this file still use TG-specific imports for now
# (backward compat). The transport layer is available for new code and
# incremental migration. Use get_relay_source(chat_id) to determine platform.
from bot.relay.transport import (  # noqa: F401
    relay_send_message, relay_edit_message, relay_delete_message,
    relay_send_document, get_relay_source,
)

# ── Relay TG send throttling ──────────────────────────────────────────
# Prevents FloodWait from burst-sending catchup digests across multiple
# relay chats simultaneously. Semaphore limits concurrent catchup senders
# to 1, and inter-message delay spaces out individual sends.
_CATCHUP_SEND_DELAY_S = 0.5  # delay between consecutive TG sends in catchup
_CATCHUP_SEMAPHORE = asyncio.Semaphore(1)  # serialize catchup digests globally


# ── SDK task-notification prettifier ─────────────────────────────────────

_TASK_NOTIFICATION_RE = re.compile(
    r"<task-notification>\s*"
    r"(?:<task-id>(?P<task_id>[^<]*)</task-id>\s*)?"
    r"(?:<tool-use-id>[^<]*</tool-use-id>\s*)?"
    r"(?:<output-file>[^<]*</output-file>\s*)?"
    r"(?:<status>(?P<status>[^<]*)</status>\s*)?"
    r"(?:<summary>(?P<summary>[^<]*)</summary>\s*)?"
    r"(?:<result>(?P<result>[\s\S]*?)</result>\s*)?"
    r"(?:<event>(?P<event>[\s\S]*?)</event>\s*)?"
    r"(?:<usage>[\s\S]*?</usage>\s*)?"
    r"</task-notification>",
    re.DOTALL,
)


def _prettify_task_notifications(text: str) -> str:
    """Replace raw <task-notification> XML with compact readable format."""
    def _repl(m: re.Match) -> str:
        task_id = (m.group("task_id") or "")[:12]
        status = m.group("status") or ""
        summary = m.group("summary") or ""
        result = (m.group("result") or "").strip()
        event = (m.group("event") or "").strip()
        icon = "✅" if status == "completed" else "⏳" if status == "running" else "🔔"
        parts = [f"{icon} {summary}" if summary else f"{icon} Task {task_id}"]
        content = result or event
        if content:
            # Trim to first 500 chars to avoid bloating relay messages
            trimmed = content[:500]
            if len(content) > 500:
                trimmed += "\n…(truncated)"
            parts.append(trimmed)
        return "\n".join(parts)
    return _TASK_NOTIFICATION_RE.sub(_repl, text)


# ── Bot-side crash-recovery dedup for desktop_turn events ────────────────
# At-least-once producer (daemon) + idempotent consumer (bot) pattern.
# If daemon crashes between _emit() and _save_tail_offset(), entries are
# re-delivered on respawn. This dedup drops entries already forwarded to TG
# by checking message.id. See CLAUDE.md "three-layer dedup".

_BOT_DEDUP_CAP = 10_000  # ~83 min at 2 entries/sec — far beyond crash-recovery window


class BoundedDedup:
    """FIFO-evicting dedup set. O(1) add/check/evict."""

    __slots__ = ("_cap", "_set", "_order")

    def __init__(self, cap: int = _BOT_DEDUP_CAP):
        if cap < 1:
            raise ValueError(f"BoundedDedup cap must be >= 1, got {cap}")
        self._cap = cap
        self._set: set[str] = set()
        self._order: deque[str] = deque()

    def is_dup(self, msg_id: str) -> bool:
        """Return True if msg_id was already seen (duplicate)."""
        if msg_id in self._set:
            return True
        self._set.add(msg_id)
        self._order.append(msg_id)
        while len(self._order) > self._cap:
            evicted = self._order.popleft()
            self._set.discard(evicted)
        return False

    def __len__(self) -> int:
        return len(self._set)


# Per-chat dedup instances (ephemeral — lost on bot restart, acceptable).
_desktop_turn_dedup: dict[str, BoundedDedup] = {}


def clear_desktop_turn_dedup(chat_id: str) -> None:
    """Remove dedup state for a chat. Called on relay deletion."""
    _desktop_turn_dedup.pop(str(chat_id), None)


# Visual separator between assistant turns in the relay placeholder. Appears
# whenever Mac Claude finishes a text block, runs a tool, then produces more
# text — gives the user a clean reading rhythm instead of one wall of prose.
_TURN_SEPARATOR = ""


_KNOWN_CLAUDE_SLASHES: frozenset[str] = frozenset()
# Intentionally empty (2026-04-18). Previously listed ~25 Claude CLI slashes
# as "forwarded to Mac daemon", but `claude --print` mode (what the daemon
# subprocess uses) does NOT execute built-in slash commands — they pass
# through as plain user text, and the model responds with a confusing
# "/cost is a built-in, use the Desktop app" explanation. Leaving this set
# empty means ANY /slash in a relay chat that isn't bot-side handled earlier
# falls through to the friendly hint below, telling the user exactly that.


# Track current relay placeholder per chat for mid-message switching
_relay_placeholders: dict[str, int] = {}  # chat_id → placeholder_id

# Mid-task injection queue — items waiting for the currently-running
# relay daemon to exit. Drained in _handle_relay_message's finally block.
# Each item: {"source": str, "text": str, "message_id": str}
_pending_relay_queue: dict[str, list[dict]] = {}

# Relay burst-message debounce (FB-50). Mirrors PQ debounce for team chats.
# When no daemon is running, buffer messages for _RELAY_DEBOUNCE_S before
# dispatching — coalesces rapid-fire bursts into a single daemon query.
_RELAY_DEBOUNCE_S: float = 0.15  # 150ms, same as PQ_DISPATCH_DEBOUNCE_MS
_relay_debounce: dict[str, dict] = {}  # chat_id → {texts, last_msg_id, timer, ...}

# QA case 5 mitigation: bounded retry with exponential backoff for transient
# TG send failures in the desktop catchup paths. Daemon-side at-most-once
# delivery means a failed TG send loses the catchup permanently (offset
# already advanced) — these retries catch transient flakes (rate limit blips,
# brief network errors) without unbounded loops.
_TG_SEND_RETRY_BACKOFFS_S: tuple[float, ...] = (1.0, 3.0, 9.0)


# Realtime Desktop-turn threading — remembers the last TG message_id we
# forwarded for a `user` role turn in a given chat, so the next `assistant`
# turn within the same Desktop exchange can thread under it via
# reply_to_message_id. Entries older than 60s are ignored to avoid threading
# unrelated later turns to a stale user message.
# Dict[chat_id_str, (tg_msg_id: int, ts_mono: float)].
_desktop_last_user_msg_id: dict[str, tuple[int, float]] = {}
_DESKTOP_USER_REPLY_TTL_S = 60.0


# Recent re-pin / Mac-recovery tracking — used by _send_desktop_catchup to
# refine the daemon's "first_run" / "delta" cause into a user-visible header
# (re-pin vs new chat; post-online vs other delta). The match window covers
# the kill→respawn→catchup-emit lag without false positives, and doubles as
# the GC threshold for the dicts (entries older than this are purged).
_CATCHUP_REFINEMENT_TTL_S = 60.0
_recently_repinned_chats: dict[str, float] = {}    # chat_id (str) → ts
_recently_recovered_chats: dict[str, float] = {}   # chat_id (str) → ts
# Smart catchup dedup: track the latest timestamp delivered per chat.
# On repin catchup, entries with ts <= _catchup_last_delivered_ts are skipped.
# ISO 8601 strings (YYYY-MM-DDTHH:MM:SS...) are lexicographically ordered,
# so string comparison is correct for chronological ordering.
# Ephemeral (lost on restart) — acceptable since repin-after-restart is rare
# and worst case is duplicate delivery (not data loss). Bounded by relay
# chat count (~10 max), no GC needed.
_catchup_last_delivered_ts: dict[str, str] = {}    # chat_id (str) → ISO ts


async def _send_with_retry(
    coro_factory: Callable[[], Awaitable[Any]],
    *,
    label: str,
    chat_id: str | int,
    backoffs_s: tuple[float, ...] = _TG_SEND_RETRY_BACKOFFS_S,
) -> bool:
    """Run an awaitable factory with exponential backoff retries.

    `coro_factory` is called fresh for each attempt (coroutines aren't
    reusable). Logs each failure with attempt number; final failure logs
    at warning level. Returns True on success, False after all retries
    exhausted. Never raises.

    Used by relay catchup paths where transient TG send failures (rate
    limit, brief 5xx) would otherwise lose the catchup permanently due
    to daemon-side at-most-once offset advancement.
    """
    attempts = len(backoffs_s) + 1
    for attempt in range(attempts):
        try:
            await coro_factory()
            if attempt > 0:
                log.info("%s send recovered chat=%s on attempt %d/%d",
                         label, chat_id, attempt + 1, attempts)
            return True
        except Exception as e:
            if attempt < attempts - 1:
                wait = backoffs_s[attempt]
                log.info("%s send failed chat=%s attempt %d/%d (%s) — retry in %.1fs",
                         label, chat_id, attempt + 1, attempts, e, wait)
                await asyncio.sleep(wait)
            else:
                log.warning("%s send FAILED chat=%s after %d attempts: %s",
                            label, chat_id, attempts, e)
    return False


def mark_relay_repinned(chat_id: str) -> None:
    """Record a re-pin event so the next catchup is labelled 'repin'."""
    import time as _t
    _recently_repinned_chats[str(chat_id)] = _t.time()


def mark_relay_recovered(chat_id: str) -> None:
    """Record a Mac-online recovery event so the next catchup is labelled
    'post_online'. Called by the watchdog online transition for each relay
    chat whose daemon was force-killed for fresh respawn."""
    import time as _t
    _recently_recovered_chats[str(chat_id)] = _t.time()


async def drain_relay_placeholders() -> None:
    """Delete all active relay placeholders. Called during shutdown BEFORE
    Pyrogram stops, so TG API calls still work. Prevents orphan
    '🧠 Thinking...' messages that survive restarts."""
    if not _relay_placeholders:
        return
    from integrations.telegram import delete_message as _tg_del
    for cid, ph_id in list(_relay_placeholders.items()):
        try:
            await _tg_del(ph_id, chat_id=int(cid))
            log.info("Shutdown: deleted relay placeholder %s in chat %s", ph_id, cid)
        except Exception as e:
            log.debug("Shutdown: relay placeholder %s delete failed: %s", ph_id, e)
    _relay_placeholders.clear()


async def _forward_realtime_desktop_turn(chat_id: str, evt: dict) -> None:
    """Forwarder callback registered with the relay pool (item 2).

    Receives one `desktop_turn` event from the Mac daemon's realtime tailer
    and renders it to TG as a single-entry Desktop-catchup message. Reuses
    the existing `_send_desktop_catchup` layout for visual consistency with
    the initial-attach digest.

    Crash-recovery dedup: if daemon re-emits entries after crash (offset
    regression), entries with the same message_id are silently dropped.
    Entries without message_id pass through (metadata, old daemon format).
    """
    try:
        role = evt.get("role") or "?"
        text = evt.get("text") or ""
        if not text:
            return
        # Bot-side crash-recovery dedup via message_id (idempotent consumer).
        msg_id = evt.get("message_id")
        if msg_id and isinstance(msg_id, str):
            cid_key = str(chat_id)
            dedup = _desktop_turn_dedup.get(cid_key)
            if dedup is None:
                dedup = BoundedDedup()
                _desktop_turn_dedup[cid_key] = dedup
            if dedup.is_dup(msg_id):
                log.debug("desktop_turn dedup drop chat=%s seq=%s msg_id=%s",
                           chat_id, evt.get("seq"), msg_id[:12])
                return
        entry: dict = {"role": role, "text": text}
        if evt.get("files"):
            entry["files"] = evt["files"]
        entries = [entry]
        await _send_desktop_catchup(
            chat_id, entries,
            earlier_count=0, earlier_summary="", is_initial=False)
    except Exception as e:
        log.warning("relay realtime forward failed chat=%s seq=%s: %s",
                    chat_id, evt.get("seq"), e)


async def _send_desktop_catchup(chat_id: str, entries: list[dict],
                                 earlier_count: int, earlier_summary: str,
                                 is_initial: bool = False,
                                 cause: str = "",
                                 tail_truncated: bool = False) -> None:
    """Render + send Desktop.app turns as a standalone TG message.

    Called from _handle_relay_message when daemon emits `type=catchup`.
    See relay-chats skill for architecture.

    Header is selected from `cause` (daemon hint) refined by bot-side recent-
    event tracking (`_recently_repinned_chats` / `_recently_recovered_chats`):
    - new chat / fresh attach    → "📱 New relay chat — last N turns"
    - re-pinned to new JSONL     → "📌 Re-pinned to new session — last N turns"
    - Mac came back online       → "🟢 Mac back online — last N turns from Desktop"
    - Daemon respawn (other)     → "📱 Desktop activity since last sync"

    `cause` is daemon-emitted: "first_run" or "delta". Bot-side state
    distinguishes within first_run (new vs repin) and within delta (post-online
    vs other). All variants are bounded to last 10 query/response pairs.

    TG auto-splits at 4096 chars, so large digests just span multiple messages.
    """
    import time as _time
    from bot.md_to_tg_html import convert_for_tg, escape_text_for_tg, tool_emoji
    n_detail = len(entries)
    # Refine cause using bot-side recent-event tracking (60s window).
    now = _time.time()
    cid_key = str(chat_id)
    repinned_at = _recently_repinned_chats.get(cid_key, 0.0)
    recovered_at = _recently_recovered_chats.get(cid_key, 0.0)
    refined = cause or ("first_run" if is_initial else "delta")
    if refined == "first_run" and (now - repinned_at) < _CATCHUP_REFINEMENT_TTL_S:
        refined = "repin"
    elif refined == "delta" and (now - recovered_at) < _CATCHUP_REFINEMENT_TTL_S:
        refined = "post_online"
    for _d in (_recently_repinned_chats, _recently_recovered_chats):
        for _k in [k for k, v in _d.items() if (now - v) > _CATCHUP_REFINEMENT_TTL_S]:
            _d.pop(_k, None)

    # Smart catchup dedup: on repin, filter out entries already delivered
    # to TG in a prior daemon session for the same chat.
    if refined == "repin" and entries:
        last_ts = _catchup_last_delivered_ts.get(cid_key, "")
        if last_ts:
            pre_count = len(entries)
            entries = [e for e in entries if (e.get("ts") or "") > last_ts]
            dropped = pre_count - len(entries)
            if dropped:
                log.info("catchup dedup chat=%s dropped=%d/%d (ts<=%s)",
                         cid_key, dropped, pre_count, last_ts[:19])
            if not entries:
                from integrations.telegram import send_message as _tg_send_dedup
                await _send_with_retry(
                    lambda: _tg_send_dedup(
                        "📌 <b>Re-pinned</b> — no new turns since last sync.",
                        chat_id=int(chat_id), parse_mode="HTML"),
                    label="catchup dedup empty", chat_id=chat_id)
                return
        n_detail = len(entries)

    log.info("desktop_catchup render chat=%s daemon_cause=%s refined=%s "
             "is_initial=%s entries=%d earlier=%d has_ts=%s has_tools=%s",
             cid_key, cause or "<none>", refined, is_initial, n_detail, earlier_count,
             any(e.get("ts") for e in entries),
             any(e.get("tools") for e in entries))

    # Derive project label from relay session registry (best-effort).
    proj_label = ""
    try:
        from bot.desktop_relay import _load_registry
        reg = _load_registry()
        entry = reg.get(cid_key, {})
        pp = entry.get("project_path", "")
        if pp:
            proj_label = pp.rstrip("/").split("/")[-1]
    except Exception:
        pass

    # Header line by cause.
    proj_suffix = f" · <b>{escape_text_for_tg(proj_label)}</b>" if proj_label else ""
    if refined == "first_run":
        headline = f"📱 <b>New relay chat</b>{proj_suffix}"
    elif refined == "repin":
        headline = f"📌 <b>Re-pinned to new session</b>{proj_suffix}"
    elif refined == "post_online":
        headline = f"🟢 <b>Mac back online</b>{proj_suffix}"
    else:
        headline = f"📱 <b>Desktop activity</b>{proj_suffix}"

    # Pair up entries (user, assistant) for display. An entry's "pair index"
    # is its position in the user-led group. A stray leading assistant or
    # dangling user is treated as a single-turn pair.
    pairs: list[list[dict]] = []
    cur: list[dict] = []
    for e in entries:
        if e.get("role") == "user" and cur:
            pairs.append(cur)
            cur = [e]
        else:
            cur.append(e)
    if cur:
        pairs.append(cur)

    n_pairs = len(pairs)
    parts = [headline]
    sub_bits = []
    if n_pairs:
        sub_bits.append(f"{n_pairs} turn{'s' if n_pairs != 1 else ''}")
    if earlier_count:
        plus = "+" if tail_truncated else ""
        sub_bits.append(f"<i>+{earlier_count}{plus} earlier</i>")
    if sub_bits:
        parts.append(" · ".join(sub_bits))
    parts.append("")

    # Strip relay prelude from any turn. Handles:
    #  - Prefix anchor: `[USER_QUERY]\n` (everything before it is prelude)
    #  - Inline stragglers (assistant context restoration, etc): remove
    #    `[TG_HTML reply mode — no Markdown. ... ]` and `[Write naturally ...]`
    #    blocks wherever they appear.
    import re as _re
    _INLINE_PRELUDE_RE = _re.compile(
        r"\[TG_HTML reply mode[^\]]*\][^\n]*\n?"
        r"|\[Write naturally[^\]]*\][^\n]*\n?"
        r"|\[USER_QUERY\]\n?",
        _re.DOTALL,
    )

    def _strip_relay_prelude(text: str) -> str:
        marker = "[USER_QUERY]\n"
        idx = text.find(marker)
        if idx >= 0:
            text = text[idx + len(marker):]
        # Remove any remaining inline prelude fragments (wrapping context etc).
        return _INLINE_PRELUDE_RE.sub("", text)

    # Send header (headline + subtext) as one TG message, then per-pair
    # messages — user first, merged-assistant reply threaded under it.
    # Topics TOC removed: per-turn threading already gives all the context.
    #
    # NOISE GUARD (Apr 18 2026): skip the header for realtime single-turn
    # deltas. When user types in Desktop Claude.app, each new JSONL turn
    # fires a desktop_turn → _send_desktop_catchup with is_initial=False,
    # earlier_count=0, n_pairs=1. Emitting the "📱 Desktop activity · 1 turn"
    # header + the content = 2 TG messages per turn — pure noise. Show the
    # header ONLY for multi-turn catchups (attach-time digests with N>1
    # pairs, or explicit first_run / repin / post_online causes).
    from integrations.telegram import send_message as _tg_send
    from integrations.telegram import get_flood_wait_remaining

    # Throttle: serialize catchup digests globally to prevent multi-relay
    # bursts from triggering FloodWait. Wait if FloodWait already active.
    async with _CATCHUP_SEMAPHORE:
        flood_remaining = get_flood_wait_remaining()
        if flood_remaining > 0:
            log.info("catchup throttle: waiting %.0fs for FloodWait to clear "
                     "chat=%s", flood_remaining, cid_key)
            await asyncio.sleep(flood_remaining)

        _show_header = (
            is_initial
            or earlier_count
            or n_pairs > 1
            or refined in ("repin", "post_online")
        )
        if _show_header:
            header_body = "\n".join(parts).rstrip()
            await _send_with_retry(
                lambda: _tg_send(header_body, chat_id=int(chat_id), parse_mode="HTML"),
                label="desktop catchup header",
                chat_id=chat_id,
            )
            await asyncio.sleep(_CATCHUP_SEND_DELAY_S)

        # Per-pair messages — user first, then ONE merged assistant message per
        # pair (TG reply-to-user). Merging prevents the same user-quote from
        # repeating above multiple bot messages when Claude emits several
        # text blocks interleaved with tool calls within a single response.
        #
        # Timestamp display rule: show "[Xm ago]" only when it changes from the
        # previous emission. When 10 catchup turns all happened within the same
        # minute, repeating "[51m ago]" on every line is pure noise — show on
        # the first turn, omit on subsequent ones until the bucket changes.
        import time as _time_thread
        for pair in pairs:
            user_turns = [t for t in pair if t.get("role") == "user"]
            asst_turns = [t for t in pair if t.get("role") != "user"]

            _last_user_msg_id: int | None = None
            # Realtime-forwarder calls us with a single-entry pair (user OR
            # assistant alone, NOT the full pair). Seed last_user_msg_id from
            # the module-level cache so an assistant turn in a subsequent call
            # threads under the user turn from the prior call within the same
            # Desktop exchange. TTL guard avoids threading unrelated later
            # turns to a stale user message.
            if not user_turns and asst_turns:
                cached = _desktop_last_user_msg_id.get(str(chat_id))
                if cached:
                    cached_mid, cached_ts = cached
                    if (_time_thread.monotonic() - cached_ts) <= _DESKTOP_USER_REPLY_TTL_S:
                        _last_user_msg_id = cached_mid  # noqa: F841 — kept for future reply threading
                    else:
                        _desktop_last_user_msg_id.pop(str(chat_id), None)

            # Send user message(s) — typically just one per pair.
            for u in user_turns:
                raw_text = u.get("text") or ""
                if not raw_text:
                    continue
                stripped = _strip_relay_prelude(raw_text)
                if not stripped.strip():
                    continue
                body = f"💬 <blockquote>{convert_for_tg(stripped)}</blockquote>".rstrip()
                try:
                    sent = await _tg_send(
                        body, chat_id=int(chat_id),
                        parse_mode="HTML",
                        reply_to_message_id=None,
                    )
                    if isinstance(sent, dict):
                        mid = sent.get("message_id") or sent.get("id")
                        if isinstance(mid, int):
                            _last_user_msg_id = mid
                            # Persist across realtime-forwarder calls so the
                            # assistant turn (next call) can thread under this
                            # user turn. Cache TTL handled on read.
                            _desktop_last_user_msg_id[str(chat_id)] = (
                                mid, _time_thread.monotonic())
                    await asyncio.sleep(_CATCHUP_SEND_DELAY_S)
                except Exception as e:
                    log.warning("desktop catchup user send failed chat=%s: %s",
                                chat_id, e)

            # Merge all assistant turns into ONE message. Concat texts with a
            # blank-line gap so narrative flow is preserved. Union all tool
            # chains — dedup while keeping order.
            if not asst_turns:
                continue
            merged_texts: list[str] = []
            merged_tools: list[str] = []
            merged_files: list[str] = []  # from daemon _extract_files
            _seen_tools: set[str] = set()
            _seen_files: set[str] = set()
            latest_ts: str = ""
            for a in asst_turns:
                raw_text = a.get("text") or ""
                stripped = _strip_relay_prelude(raw_text)
                if stripped.strip():
                    merged_texts.append(stripped.strip())
                for tool_name in (a.get("tools") or []):
                    if tool_name not in _seen_tools:
                        _seen_tools.add(tool_name)
                        merged_tools.append(tool_name)
                for fpath in (a.get("files") or []):
                    if fpath not in _seen_files:
                        _seen_files.add(fpath)
                        merged_files.append(fpath)
                ts_val = a.get("ts") or ""
                if ts_val and ts_val > latest_ts:
                    latest_ts = ts_val
            if not merged_texts:
                continue
            asst_lines = []
            if merged_tools:
                shown = merged_tools[:5]
                more = f" +{len(merged_tools) - 5}" if len(merged_tools) > 5 else ""
                tool_str = " → ".join(
                    f"{tool_emoji(n)} {escape_text_for_tg(n)}" for n in shown
                ) + more
                asst_lines.append(f"<i>{tool_str}</i>")
            # Handle RELAY_FILE tags in catchup text.
            # Always fetch + show 📎 indicator. Reply-to enrichment already
            # strips tags from quoted text (no duplicate fetch from replies).
            _catchup_merged = "\n\n".join(merged_texts)
            _catchup_rf_files: list[str] = []
            _catchup_rf_seen: set[str] = set()
            def _rf_handler(m):
                p = re.search(r"RELAY_FILE:\s*(/[^\]\n]+)", m.group(0))
                if p:
                    import os.path as _osp
                    fpath = p.group(1).strip()
                    if fpath not in _catchup_rf_seen:
                        _catchup_rf_seen.add(fpath)
                        _catchup_rf_files.append(fpath)
                    return f"📎 {_osp.basename(fpath)}"
                return ""
            _catchup_rf_re = re.compile(r"\[[\s\S]*?RELAY_FILE:\s*/[^\]\n]+?\s*\]")
            _catchup_merged = _catchup_rf_re.sub(_rf_handler, _catchup_merged).strip()
            if not _catchup_merged:
                continue
            # Prettify SDK task-notification XML before TG HTML conversion
            _catchup_merged = _prettify_task_notifications(_catchup_merged)
            asst_lines.append(convert_for_tg(_catchup_merged))
            asst_body = "\n".join(asst_lines).rstrip()
            try:
                _asst_sent = await _tg_send(
                    asst_body, chat_id=int(chat_id),
                    parse_mode="HTML",
                )
                # Deliver RELAY_FILE attachments (no threading in catchup —
                # user messages are already shown as 💬 blockquotes)
                for _rf_path in _catchup_rf_files:
                    log.info("Catchup RELAY_FILE fetch: chat=%s path=%s",
                             chat_id, _rf_path)
                    asyncio.create_task(_relay_send_file(
                        "telegram", chat_id, int(chat_id), "", _rf_path))
                # Deliver files from daemon _extract_files (Write tool calls).
                # Skip files already handled via RELAY_FILE tags.
                for _ef_path in merged_files:
                    if _ef_path not in _catchup_rf_seen:
                        log.info("Catchup tool-file proxy: chat=%s path=%s",
                                 chat_id, _ef_path)
                        asyncio.create_task(_relay_send_file(
                            "telegram", chat_id, int(chat_id), "", _ef_path))
                await asyncio.sleep(_CATCHUP_SEND_DELAY_S)
            except Exception as e:
                log.warning("desktop catchup asst send failed chat=%s: %s",
                            chat_id, e)

    # Track the latest delivered timestamp for smart catchup dedup.
    # Scan all entries for the max ts — used by repin dedup on next catchup.
    max_ts = ""
    for e in entries:
        ts_val = e.get("ts") or ""
        if isinstance(ts_val, str) and ts_val > max_ts:
            max_ts = ts_val
    if max_ts:
        _catchup_last_delivered_ts[cid_key] = max_ts


async def _relay_send_file(source: str, chat_id: str, tg_cid: int | str,
                           session_name: str, file_path: str,
                           reply_to_message_id: int | None = None):
    """Fetch a file from Mac via SSH relay-fetch and send to TG relay chat."""
    try:
        from bot.desktop_relay import fetch_file
        import hashlib
        import os
        import re as _re_safe
        # Sanitize filename: strip path traversal, keep only safe chars
        raw_name = os.path.basename(file_path)
        safe_name = _re_safe.sub(r"[^a-zA-Z0-9._-]", "_", raw_name) or "file"
        # Add hash prefix to avoid collisions
        h = hashlib.md5(file_path.encode()).hexdigest()[:8]
        local_path = f"/app/data/tmp/relay_{h}_{safe_name}"

        # Fetch binary file from Mac via relay-fetch (base64 encoded transfer)
        ok, file_bytes = await fetch_file(file_path, timeout=60.0)
        if not ok:
            err_msg = file_bytes.decode("utf-8", errors="replace")
            from integrations.telegram import send_message
            _rf_kwargs: dict = {"chat_id": tg_cid, "parse_mode": "html"}
            if reply_to_message_id:
                _rf_kwargs["reply_to_message_id"] = reply_to_message_id
            await send_message(
                f"\u274c Could not fetch file: <code>{file_path}</code>\n<i>{err_msg}</i>",
                **_rf_kwargs,
            )
            return

        # Ensure tmp dir exists and save binary content
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(file_bytes)

        from integrations.telegram import send_document
        await send_document(local_path, chat_id=tg_cid, caption=raw_name,
                            reply_to_message_id=reply_to_message_id)
        log.info("Relay file sent: %s (%d bytes) to %s", raw_name, len(file_bytes), chat_id)

        # Cleanup
        try:
            os.remove(local_path)
        except Exception:
            log.debug("Relay file cleanup failed: %s", local_path)
    except Exception as e:
        log.warning("Relay file send failed: %s", e)


def _fmt_relay_elapsed(s: float) -> str:
    """Format elapsed seconds as '45s' or '1m 30s'."""
    si = int(s)
    return f"{si}s" if si < 60 else f"{si // 60}m {si % 60}s"


async def _handle_relay_message(source: str, chat_id: str, text: str,
                                session_name: str = "", message_id: str = "",
                                project_path: str = "",
                                pinned_jsonl_path: str = "",
                                is_injection: bool = False):
    """Claude relay path: persistent daemon pool + streaming placeholder UX.

    Lifecycle:
      1. Extract UUID from pinned_jsonl_path (or error out).
      2. Delete any stale placeholder; create fresh "🧠 Thinking..." placeholder.
      3. Iterate stream-json entries from the persistent daemon pool,
         accumulate text from assistant entries, edit placeholder periodically
         (throttled to 1Hz to avoid TG rate limits).
      4. On result entry → send final text + cost footer, delete placeholder.
      5. On daemon_error → show error in placeholder, then exit.
      6. Advance last_synced_offset in registry to avoid double-sync from
         background JSONL mirror.

    Error surface (all yielded via placeholder edit):
      - no_uuid_in_jsonl_path (config error — relay needs re-linking)
      - daemon-level errors (spawn failure, claude binary missing, timeout)
      - claude-level errors (rate limited, not logged in, API error)

    The `session_name` arg is accepted for caller compatibility but unused
    by the persistent daemon.
    """
    import html as _html
    import re as _re
    import time as _time_mod
    from bot.commands import _friendly_daemon_error
    tg_cid = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
    reply_to = int(message_id) if message_id and message_id.isdigit() else None
    from bot.desktop_relay import _active_relay_chats, _relay_tasks

    # === Extract UUID from pinned JSONL path ===
    # Expected format: /Users/youruser/.claude/projects/-Users-username-dev-project/<uuid>.jsonl
    uuid_match = _re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
        pinned_jsonl_path or "",
    )
    if not uuid_match:
        log.info("Relay SDK: no UUID in jsonl path=%s — starting fresh session", pinned_jsonl_path)
        uuid = "NEW"
    else:
        uuid = uuid_match.group(1)

    # Touch last_used so stale-relay cleanup has accurate activity timestamps
    if session_name:
        from bot.desktop_relay import _touch_last_used as _tlu
        asyncio.create_task(_tlu(session_name))

    # === Mac reachability preflight (Apr 18 2026) ===
    # Without this, offline Mac → daemon SSH spawn → ~30s hang → friendly
    # error. Worse, some failure modes leave the placeholder stuck on
    # "Thinking…" forever because the error path didn't fire. Short-circuit
    # here with a cached ping (60s watchdog) so the user gets instant
    # feedback. Also surfaces a 🟢 "back online" toast when the Mac just
    # recovered, so the first post-recovery message has context.
    if source == "telegram":
        try:
            from bot.desktop_relay import is_mac_online as _is_online
            _online = await _is_online(max_age_s=60.0)
        except Exception as _pe:
            log.warning("Relay preflight: is_mac_online failed: %s", _pe)
            _online = True  # fail-open: let daemon path handle genuine errors
        if not _online:
            log.info("Relay preflight: Mac offline — short-circuit chat=%s",
                     chat_id)
            try:
                from integrations.telegram import send_message as _tg_send
                await _tg_send(
                    "\U0001f534 Mac offline \u2014 try again when back.",
                    chat_id=tg_cid,
                    reply_to_message_id=reply_to,
                    parse_mode="html",
                )
            except Exception as _se:
                log.warning("Relay preflight: offline send failed: %s", _se)
            return
        # Online: check if the Mac JUST recovered — emit a back-online toast
        # BEFORE the Thinking placeholder so the chat shows the sequence
        # "🟢 Mac back online" → 📱 catchup → "🧠 Thinking…" → reply.
        import time as _tmod
        _rec_at = _recently_recovered_chats.get(str(chat_id), 0.0)
        _is_post_recovery = bool(_rec_at) and (
            _tmod.time() - _rec_at) < _CATCHUP_REFINEMENT_TTL_S
        if _is_post_recovery:
            # NOTE: do NOT pop _recently_recovered_chats here — the
            # daemon's catchup renderer reads it to pick the "back online"
            # header variant. _send_desktop_catchup handles GC via its
            # own TTL check.
            log.info("Relay preflight: Mac back online toast chat=%s",
                     chat_id)
            try:
                from integrations.telegram import send_message as _tg_send
                await _tg_send(
                    "\U0001f7e2 Mac back online \u2014 processing your message\u2026",
                    chat_id=tg_cid,
                    reply_to_message_id=reply_to,
                    parse_mode="html",
                )
            except Exception as _se:
                log.warning("Relay preflight: toast send failed: %s", _se)

    # === Auth preflight (avoids "🧠 Thinking…" hang when TOTP expired) ===
    # get_cached_status() is zero-latency (no SSH). If _auth_checked is True
    # (a previous status()/authenticate() call populated the cache) and
    # authenticated is False (session expired or known bad), fast-fail here
    # before creating the placeholder. Fail-open when cache is fresh-start
    # unknown (_auth_checked=False) so first-post-restart messages still work.
    # WA relay: no preflight here — no persistent placeholder, daemon errors
    # surface normally via the existing error handler path.
    if source == "telegram":
        try:
            from bot.desktop_relay import get_cached_status as _get_cs
            _cs = _get_cs()
            if _cs.get("_auth_checked") and not _cs.get("authenticated"):
                log.info("Relay preflight: auth expired — short-circuit chat=%s",
                         chat_id)
                try:
                    from bot.commands import _friendly_daemon_error as _fde
                    from integrations.telegram import send_message as _tg_send
                    await _tg_send(
                        _fde("non_json_output:auth_expired"),
                        chat_id=tg_cid,
                        reply_to_message_id=reply_to,
                        parse_mode="html",
                    )
                except Exception as _se:
                    log.warning("Relay preflight: auth-expired send failed: %s", _se)
                return
        except Exception as _pe:
            log.warning("Relay preflight: get_cached_status failed: %s", _pe)

    _active_relay_chats.add(str(chat_id))
    _relay_tasks[str(chat_id)] = asyncio.current_task()

    placeholder_id = None
    _ticker_task: asyncio.Task | None = None  # background placeholder ticker
    _stream_done = False  # signal ticker to stop
    # On Mac-recovery paths we want the post-online catchup digest to land
    # BEFORE the Thinking placeholder so the chat reads as:
    #   🟢 back online  →  📱 catchup  →  🧠 Thinking…  →  reply
    # Setting this flag skips the eager placeholder creation here; the
    # stream loop lazy-creates it just before the first non-catchup entry.
    defer_placeholder = (
        source == "telegram" and not is_injection
        and bool(locals().get("_is_post_recovery") or False)
    )
    try:
        # === Create / reuse placeholder ===
        # On mid-task injection (is_injection=True) the dispatch block
        # has already deleted the prior placeholder and created a fresh
        # "Noted..." placeholder in _relay_placeholders. Reuse that so
        # the user sees one continuous placeholder under the new message.
        # Otherwise we create a fresh "Thinking..." placeholder — unless
        # defer_placeholder is set (recovery path).
        if source == "telegram" and not defer_placeholder:
            from integrations.telegram import send_message, edit_message, delete_message
            existing_ph = _relay_placeholders.get(chat_id)
            if is_injection and existing_ph:
                placeholder_id = existing_ph
            else:
                if existing_ph:
                    _relay_placeholders.pop(chat_id, None)
                    try:
                        await delete_message(existing_ph, chat_id=tg_cid)
                    except Exception:
                        pass
                initial_label = "Noted" if is_injection else "Thinking"
                ph = await send_message(
                    f"\U0001f9e0 {initial_label}...",
                    chat_id=tg_cid,
                    reply_to_message_id=reply_to,
                    parse_mode="html",
                )
                if ph and ph.get("message_id"):
                    placeholder_id = ph["message_id"]
                    _relay_placeholders[chat_id] = placeholder_id
        elif source == "telegram" and defer_placeholder:
            # Need imports for later lazy-creation
            from integrations.telegram import send_message, edit_message, delete_message  # noqa: F401
            log.info("Relay: deferring placeholder for post-recovery catchup chat=%s",
                     chat_id)

        # === Stream from daemon ===
        # NOTE on duplicate prevention: claude --print --output-format stream-json
        # emits THREE types of text-bearing entries per assistant turn:
        #   1. stream_event/content_block_delta/text_delta  (fine-grained, live)
        #   2. partial_assistant_message/delta              (SDK-style partial)
        #   3. assistant/message/content[].type=text        (final complete message)
        # The three are redundant — content #3 = sum of deltas #1 (and #2).
        # Accumulating all three doubled/tripled the user-visible reply.
        # Fix: track per-turn whether we have already captured text via deltas.
        # If yes, skip the final assistant text block. If no (rare: model emits
        # no stream_event deltas, e.g. when --output-format stream-json omits
        # them in some CLI versions), fall back to the assistant block so we
        # never drop text. Reset the flag at each turn boundary (`result`
        # entry or new user/tool_use entry).
        start_ts = _time_mod.monotonic()
        accumulated_text: list[str] = []
        last_edit_ts = start_ts
        _edit_frozen = False  # Set True on FloodWait — skip all subsequent edits
        result_cost: float | None = None
        result_status = ""
        daemon_error_text: str | None = None
        tool_calls = 0
        tool_labels_seen: list[str] = []  # parity with team chat placeholder
        saw_any_text = False
        saw_deltas_this_turn = False
        pending_turn_break = False
        status_word = "Noted" if is_injection else "Thinking"

        # Claude relay: route to the persistent daemon pool. The pool yields
        #   {"type":"stream","entry":{...}}    — Mac Claude stream-json entry
        #   {"type":"result",...}              — final cost/status
        #   {"type":"error",...}               — daemon-level error
        # Unwrap stream entries to the flat stream-json schema the placeholder
        # loop expects; pass result/error through as result / daemon_error.
        from bot.relay.pool import get_pool
        async def _daemon_entries():
            if not chat_id:
                yield {"type": "daemon_error", "error": "no_chat_id"}
                return
            log.info("relay: dispatch chat=%s uuid=%s text_bytes=%d",
                     chat_id, uuid, len(text or ""))
            try:
                client = await get_pool().acquire(
                    str(chat_id), uuid, project_path or "")
                async for msg in client.query(text):
                    mt = msg.get("type")
                    if mt == "stream":
                        inner = msg.get("entry")
                        if isinstance(inner, dict):
                            yield inner
                    elif mt == "result":
                        yield {
                            "type": "result",
                            "subtype": msg.get("subtype", ""),
                            "cost_usd": msg.get("cost_usd"),
                        }
                    elif mt == "error":
                        yield {"type": "daemon_error",
                               "error": msg.get("error") or "pool_error"}
                    elif mt == "catchup":
                        yield {
                            "type": "desktop_catchup",
                            "entries": msg.get("entries") or [],
                            "earlier_count": msg.get("earlier_count") or 0,
                            "earlier_summary": msg.get("earlier_summary") or "",
                            "is_initial": bool(msg.get("is_initial")),
                            "cause": msg.get("cause") or "",
                            "tail_truncated": bool(msg.get("tail_truncated")),
                        }
                    elif mt == "catchup_warning":
                        # QA case 2 fix — daemon couldn't locate the JSONL
                        # for the pinned UUID. Surface the issue to the user.
                        yield {
                            "type": "desktop_catchup_warning",
                            "subtype": msg.get("subtype") or "unknown",
                            "uuid": msg.get("uuid") or "",
                        }
                    elif mt == "new_session":
                        # Fresh session created — pool already updated
                        # registry. Just log it.
                        log.info("Relay new session chat=%s uuid=%s",
                                 chat_id, msg.get("uuid"))
            except Exception as _pe:
                yield {"type": "daemon_error",
                       "error": f"pool_failed:{_pe}"}

        # --- Background ticker for relay placeholder updates ---------------
        # Mirrors primary-chat _tg_ticker (streaming.py). The inline edit
        # code (further below) only fires when a stream entry arrives from
        # the daemon. If the daemon is silent (Mac Claude thinking, running
        # a long bash command), the placeholder freezes. This background
        # task edits the placeholder every ~5s so the user always sees
        # progress: tool chain, elapsed time, or heartbeat.
        async def _relay_ticker():
            nonlocal last_edit_ts, _edit_frozen, placeholder_id
            _TICK_INTERVAL = 3.0   # check every 3s
            _EDIT_COOLDOWN = 5.0   # don't edit more often than 5s
            try:
                while not _stream_done:
                    await asyncio.sleep(_TICK_INTERVAL)
                    if _stream_done or _edit_frozen:
                        break
                    if source != "telegram" or not placeholder_id:
                        continue
                    now = _time_mod.monotonic()
                    since_edit = now - last_edit_ts
                    if since_edit < _EDIT_COOLDOWN:
                        continue
                    # Build status text — same logic as inline edit below
                    elapsed = int(now - start_ts)
                    mm, ss = divmod(elapsed, 60)
                    elapsed_str = f"{mm}m{ss}s" if mm else f"{ss}s"
                    preview = "".join(accumulated_text)[-3500:]
                    if preview:
                        from bot.md_to_tg_html import convert_for_tg as _md2h_t
                        new_text = _md2h_t(preview)
                    elif tool_labels_seen:
                        recent = tool_labels_seen[-5:]
                        tools_line = " \u2192 ".join(
                            _html.escape(t) for t in recent)
                        new_text = (f"\U0001f527 {tools_line}  "
                                    f"({elapsed_str})")
                    else:
                        word = status_word.lower()
                        if elapsed >= 60:
                            new_text = (f"\u23f3 Still {word}... "
                                        f"({elapsed_str})")
                        else:
                            new_text = (f"\U0001f9e0 {status_word}... "
                                        f"({elapsed_str})")
                    try:
                        from integrations.telegram import (
                            edit_message as _tick_edit)
                        await _tick_edit(
                            placeholder_id, new_text,
                            chat_id=tg_cid, parse_mode="html")
                        last_edit_ts = _time_mod.monotonic()
                    except Exception as _te:
                        from pyrogram.errors import FloodWait as _FW_t
                        if isinstance(_te, _FW_t):
                            _edit_frozen = True
                            log.warning("Relay ticker frozen (FloodWait) "
                                        "chat=%s", tg_cid)
                            break
                        # MESSAGE_ID_INVALID = placeholder was deleted
                        # (injection race). Null out to stop further edits.
                        _err_name = type(_te).__name__
                        if "MESSAGE_ID_INVALID" in str(_te) or \
                                _err_name == "MessageIdInvalid":
                            log.info("Relay ticker: placeholder gone "
                                     "(MESSAGE_ID_INVALID) chat=%s",
                                     tg_cid)
                            placeholder_id = None
                            break
                        log.debug("Relay ticker edit failed: %s", _te)
            except asyncio.CancelledError:
                pass
            except Exception as _tex:
                log.debug("Relay ticker unexpected: %s", _tex)

        if source == "telegram":
            _ticker_task = asyncio.create_task(_relay_ticker())

        async for entry in _daemon_entries():
            etype = entry.get("type") if isinstance(entry, dict) else None

            if etype == "daemon_error":
                daemon_error_text = entry.get("error", "unknown_daemon_error")
                break

            if etype == "desktop_catchup_warning":
                # Daemon could not find the JSONL for the pinned UUID.
                # User most likely pinned to a typo'd UUID, or the JSONL was
                # deleted between pin and respawn. Surface clearly so they
                # can re-pin to a valid session.
                _w_uuid = entry.get("uuid") or "?"
                _w_sub = entry.get("subtype") or "unknown"
                if source == "telegram" and chat_id:
                    from integrations.telegram import send_message as _tg_send
                    _warning_text = (
                        f"\u26a0\ufe0f <b>Desktop catchup skipped</b>\n"
                        f"No JSONL on Mac for pinned session "
                        f"<code>{_w_uuid}</code> "
                        f"(<i>{_w_sub}</i>).\n"
                        f"Check <code>/session list</code> and re-pin "
                        f"with <code>/session pin</code> &lt;N&gt;."
                    )
                    await _send_with_retry(
                        lambda: _tg_send(_warning_text,
                                          chat_id=int(chat_id),
                                          parse_mode="HTML"),
                        label="catchup_warning",
                        chat_id=chat_id,
                    )
                continue

            if etype == "desktop_catchup":
                # Desktop.app turns from the shared JSONL — forward as a
                # standalone TG message BEFORE the query response streams.
                # `cause` ("first_run" / "delta") plus bot-side recent-event
                # tracking selects the user-visible header (new chat / repin /
                # post-online / generic delta). See relay-chats skill.
                _ce = entry.get("entries") or []
                _ec = entry.get("earlier_count") or 0
                _es = entry.get("earlier_summary") or ""
                _ii = bool(entry.get("is_initial"))
                _cause = entry.get("cause") or ""
                _tt = bool(entry.get("tail_truncated"))
                if (_ce or _ec) and source == "telegram" and chat_id:
                    try:
                        await _send_desktop_catchup(
                            chat_id, _ce, _ec, _es,
                            is_initial=_ii, cause=_cause,
                            tail_truncated=_tt,
                        )
                    except Exception as _err:
                        log.warning("relay catchup send failed: %s", _err)
                continue

            # Lazy-create placeholder on first non-catchup entry when we
            # deferred on recovery. This ensures the catchup digest lands
            # BEFORE the Thinking placeholder visually.
            if (defer_placeholder and placeholder_id is None
                    and etype not in (None, "desktop_catchup",
                                       "desktop_catchup_warning")):
                try:
                    from integrations.telegram import send_message as _lz_send
                    _ph = await _lz_send(
                        "\U0001f9e0 Thinking...",
                        chat_id=tg_cid,
                        reply_to_message_id=reply_to,
                        parse_mode="html",
                    )
                    if _ph and _ph.get("message_id"):
                        placeholder_id = _ph["message_id"]
                        _relay_placeholders[chat_id] = placeholder_id
                        log.info("Relay: lazy-created placeholder post-catchup "
                                 "chat=%s id=%s", chat_id, placeholder_id)
                except Exception as _lze:
                    log.warning("Relay lazy placeholder create failed: %s", _lze)
                defer_placeholder = False  # one-shot

            if etype == "assistant":
                msg = entry.get("message") or {}
                for block in msg.get("content") or []:
                    btype = block.get("type") if isinstance(block, dict) else None
                    if btype == "text":
                        # Skip if deltas for this turn already captured the text
                        if not saw_deltas_this_turn:
                            txt = block.get("text") or ""
                            if txt:
                                if pending_turn_break and accumulated_text:
                                    accumulated_text.append(_TURN_SEPARATOR)
                                pending_turn_break = False
                                accumulated_text.append(txt)
                                saw_any_text = True
                    elif btype == "tool_use":
                        tool_calls += 1
                        # Track tool name for placeholder parity with primary
                        # chat (🔧 bash → read → edit …). Cap growth so the
                        # list doesn't balloon on long multi-tool turns.
                        _tname = (block.get("name") or "").strip() or "tool"
                        tool_labels_seen.append(_tname)
                        if len(tool_labels_seen) > 50:
                            tool_labels_seen[:] = tool_labels_seen[-50:]
                        # Mark that the next text block starts a new turn so
                        # we inject a visual separator between them.
                        pending_turn_break = True
                # Turn boundary: reset delta flag so the next turn can
                # independently decide whether deltas or assistant wins.
                saw_deltas_this_turn = False

            elif etype == "partial_assistant_message":
                delta = entry.get("delta") or {}
                if delta.get("type") == "text_delta":
                    txt = delta.get("text") or ""
                    if txt:
                        if pending_turn_break and accumulated_text:
                            accumulated_text.append(_TURN_SEPARATOR)
                            pending_turn_break = False
                        accumulated_text.append(txt)
                        saw_any_text = True
                        saw_deltas_this_turn = True

            elif etype == "stream_event":
                # Claude CLI emits stream_event with content_block_delta for
                # token-by-token text streaming — these are the primary text
                # source. The subsequent `assistant` entry is redundant and
                # skipped above when saw_deltas_this_turn is True.
                evt = entry.get("event") or {}
                if evt.get("type") == "content_block_delta":
                    delta = evt.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        txt = delta.get("text") or ""
                        if txt:
                            if pending_turn_break and accumulated_text:
                                accumulated_text.append(_TURN_SEPARATOR)
                                pending_turn_break = False
                            accumulated_text.append(txt)
                            saw_any_text = True
                            saw_deltas_this_turn = True

            elif etype == "result":
                result_status = entry.get("subtype") or ""
                cost = entry.get("cost_usd")
                if isinstance(cost, (int, float)):
                    result_cost = float(cost)

            # Throttled placeholder update — mirrors primary-chat _tg_ticker
            # pattern for visual parity: 🧠 Thinking / 🔧 tool → tool / text.
            # Edit at 3 Hz while streaming, 5 s while pre-text (tool phase).
            # Widened from 1s to 3s (Apr 2026) to stay under TG global rate
            # limit when multiple relay chats stream concurrently.
            now = _time_mod.monotonic()
            if source == "telegram" and placeholder_id and not _edit_frozen:
                preview = "".join(accumulated_text)[-3500:]
                since_edit = now - last_edit_ts
                should_edit = False
                if preview and since_edit > 3.0:
                    should_edit = True
                elif not preview and since_edit >= 5.0:
                    should_edit = True  # tool-chain / thinking refresh
                if should_edit:
                    elapsed = int(now - start_ts)
                    mm, ss = divmod(elapsed, 60)
                    elapsed_str = f"{mm}m{ss}s" if mm else f"{ss}s"
                    if preview:
                        # Content is streaming — the content IS the placeholder.
                        # Mac Claude writes native markdown; convert to TG HTML.
                        # Stateless conversion on each edit: converter tolerates
                        # unclosed fences/emphasis (closes at EOF), so truncating
                        # mid-token is safe. See bot/md_to_tg_html.py.
                        from bot.md_to_tg_html import convert_for_tg as _md2h
                        new_text = _md2h(preview)
                    elif tool_labels_seen:
                        # Tool-chain phase: show last 5 tools joined by →
                        # (matches bot/main.py:_build_tg_status primary-chat UX).
                        recent = tool_labels_seen[-5:]
                        tools_line = " → ".join(_html.escape(t) for t in recent)
                        new_text = (f"\U0001f527 {tools_line}  "
                                    f"({elapsed_str})")
                    else:
                        # Pre-tool thinking phase (or heartbeat during silence)
                        word = "Noted" if is_injection else "Thinking"
                        if elapsed >= 60:
                            new_text = (f"\u23f3 Still {word.lower()}... "
                                        f"({elapsed_str})")
                        else:
                            new_text = f"\U0001f9e0 {word}... ({elapsed_str})"
                    try:
                        from integrations.telegram import edit_message as _edit
                        await _edit(
                            placeholder_id,
                            new_text,
                            chat_id=tg_cid,
                            parse_mode="html",
                        )
                        last_edit_ts = now
                    except Exception as _edit_exc:
                        # If FloodWait hit (even after retries in _retry_on_flood),
                        # freeze all further edits for this relay response. The
                        # result entry will still be consumed and the final reply
                        # sent as a fresh message — edits are cosmetic only.
                        from pyrogram.errors import FloodWait as _FW
                        if isinstance(_edit_exc, _FW):
                            _edit_frozen = True
                            log.warning(
                                f"Relay placeholder edit frozen (FloodWait) "
                                f"chat={tg_cid}")
                        elif ("MESSAGE_ID_INVALID" in str(_edit_exc)
                              or type(_edit_exc).__name__ == "MessageIdInvalid"):
                            # Placeholder was deleted (injection race condition).
                            # Null out to stop further edits from both inline
                            # and background ticker.
                            log.info(
                                "Relay placeholder gone (MESSAGE_ID_INVALID) "
                                "chat=%s — stopping edits", tg_cid)
                            placeholder_id = None
                        else:
                            log.debug(
                                f"Relay placeholder edit failed: {_edit_exc}")

        # Stop background ticker before finalization
        _stream_done = True
        if _ticker_task and not _ticker_task.done():
            _ticker_task.cancel()
            try:
                await _ticker_task
            except (asyncio.CancelledError, Exception):
                pass

        # === Finalize (primary-chat parity, Apr 18 2026) ===
        # Primary chat: placeholder is a STATUS surface during streaming, then
        # ALWAYS deleted. The actual reply is a fresh telegram_send_message
        # threaded under the user's original. Relay used to mutate placeholder
        # in-place via edit_message — that diverged from the bot's UX. This
        # block now mirrors the primary-chat lifecycle on every exit path:
        # daemon_error / final_text / empty-text → drop placeholder + send fresh.
        if source == "telegram":
            from integrations.telegram import send_message, delete_message
            final_text = "".join(accumulated_text).strip()

            # Diagnostic: finalize entry trace (Apr 18 2026, stuck-placeholder
            # investigation). Captures loop-exit state so we can diagnose
            # divergence between daemon result and bot-side placeholder state.
            log.info(
                "Relay finalize entry chat=%s daemon_err=%s result_status=%s "
                "text_len=%d tools=%d ph_id=%s",
                chat_id, bool(daemon_error_text), result_status or "",
                len(final_text), tool_calls, placeholder_id)

            async def _drop_placeholder():
                """Idempotent: delete the streaming placeholder + clear chat
                map entry. Safe to call from any branch / multiple times."""
                nonlocal placeholder_id
                if placeholder_id is None:
                    return
                if _relay_placeholders.get(chat_id) == placeholder_id:
                    _relay_placeholders.pop(chat_id, None)
                _ph_id = placeholder_id
                try:
                    await delete_message(placeholder_id, chat_id=tg_cid)
                    log.info("Relay placeholder deleted chat=%s id=%s",
                             chat_id, _ph_id)
                except Exception as _de:
                    log.debug("Relay placeholder delete failed: %s", _de)
                placeholder_id = None

            if daemon_error_text:
                log.info("Relay finalize branch=daemon_error chat=%s",
                         chat_id)
                err_msg = _friendly_daemon_error(daemon_error_text)
                # If the error was auth-related, update the local cache so
                # the auth preflight can fast-fail on the very next message
                # instead of hanging again for the daemon-ready timeout.
                try:
                    from bot.commands import _is_daemon_auth_error as _idae
                    from bot.desktop_relay import _mark_auth_expired as _mae
                    if _idae(daemon_error_text):
                        _mae()
                        log.info("Relay: auth cache invalidated after daemon auth "
                                 "error chat=%s", chat_id)
                except Exception as _ce:
                    log.warning("Relay: cache invalidation failed: %s", _ce)
                await _drop_placeholder()
                try:
                    _sent = await send_message(
                        err_msg, chat_id=tg_cid,
                        reply_to_message_id=reply_to,
                        parse_mode="html")
                    log.info("Relay reply sent chat=%s branch=daemon_error "
                             "msg_id=%s", chat_id,
                             (_sent or {}).get("message_id"))
                except Exception as _e:
                    log.warning("Relay SDK: error send failed: %s", _e)
            elif result_status == "merged_into_prior_qid":
                # Daemon reader detected that this qid's user frame was
                # absorbed by the CLI into the PRIOR qid's result (multi-
                # turn merge). The prior qid already delivered the merged
                # reply. Silently drop this qid's placeholder — emitting
                # a second reply here would re-send the merged content.
                # Empirical basis: fact `relay_injection_semantics_verified`.
                log.info("Relay finalize branch=merged_into_prior_qid chat=%s "
                         "— dropping placeholder silently", chat_id)
                await _drop_placeholder()
            elif final_text:
                # Extract any [RELAY_FILE: /path] tags Mac Claude emitted.
                # Tag pattern: `[RELAY_FILE: /abs/path]` (whitespace-tolerant).
                # Remove tags from the visible text; each file is fetched +
                # sent as a separate TG document after the text send.
                _relay_files: list[str] = []
                _relay_files_seen: set[str] = set()
                # Tolerant pattern: model sometimes wraps RELAY_FILE in
                # extra formatting (━━━, newlines, brackets). Match any
                # block starting with [ that contains RELAY_FILE: /path ].
                _relay_file_re = re.compile(
                    r"\[[\s\S]*?RELAY_FILE:\s*(/[^\]\n]+?)\s*\]")
                def _extract_relay_file(m):
                    import os.path as _osp
                    p = m.group(1).strip()
                    if p and p.startswith("/") and p not in _relay_files_seen:
                        _relay_files_seen.add(p)
                        _relay_files.append(p)
                    # Show 📎 filename instead of removing entirely
                    return f"📎 {_osp.basename(p)}" if p else ""
                final_text = _relay_file_re.sub(_extract_relay_file,
                                                 final_text).strip()
                footer = ""
                if result_cost is not None:
                    footer = (f"\n\n<i>cost ${result_cost:.4f}, "
                              f"tools: {tool_calls}</i>")
                # Mac Claude writes native markdown — convert to TG HTML so
                # **bold**, code fences, links render properly.
                from bot.md_to_tg_html import convert_for_tg as _md2h_final
                final_body = _md2h_final(final_text) + footer
                # Primary-chat parity: ALWAYS drop placeholder + send fresh
                # threaded under user's original message. telegram.py auto-
                # split handles oversized bodies (>4096 chars) — no manual
                # length gate needed; multi-message split is transparent.
                log.info("Relay finalize branch=final_text chat=%s body_len=%d "
                         "files=%d", chat_id, len(final_body),
                         len(_relay_files))
                await _drop_placeholder()
                try:
                    _sent = await send_message(
                        final_body,
                        chat_id=tg_cid,
                        reply_to_message_id=reply_to,
                        parse_mode="html",
                    )
                    log.info("Relay reply sent chat=%s branch=final_text "
                             "msg_id=%s len=%d", chat_id,
                             (_sent or {}).get("message_id"),
                             len(final_body))
                except Exception as _e:
                    log.warning("Relay SDK: final send failed: %s", _e)
                # Fetch + send files, threaded to same user msg as the text.
                for _fp in _relay_files:
                    log.info("Relay RELAY_FILE detected: %s", _fp)
                    asyncio.create_task(_relay_send_file(
                        source, chat_id, tg_cid, "", _fp,
                        reply_to_message_id=reply_to))
            else:
                # No text + no daemon_error → unusual (e.g. tools-only turn,
                # CLI emitted no assistant text). Mirror primary-chat warning
                # path: drop placeholder + send fresh "task finished without
                # reply" message threaded under user's original.
                log.info("Relay finalize branch=empty_text chat=%s "
                         "result_status=%s tools=%d",
                         chat_id, result_status or "", tool_calls)
                elapsed = int(_time_mod.monotonic() - start_ts)
                mm, ss = divmod(elapsed, 60)
                elapsed_str = f"{mm}m{ss}s" if mm else f"{ss}s"
                warn_parts = [
                    f"\u26a0\ufe0f Task finished without reply ({elapsed_str})"
                ]
                if tool_calls:
                    warn_parts.append(f"tools: {tool_calls}")
                if result_status:
                    warn_parts.append(f"result: {_html.escape(result_status)}")
                warn_msg = ", ".join(warn_parts) + "."
                await _drop_placeholder()
                try:
                    _sent = await send_message(
                        warn_msg, chat_id=tg_cid,
                        reply_to_message_id=reply_to,
                        parse_mode="html")
                    log.info("Relay reply sent chat=%s branch=empty_text "
                             "msg_id=%s", chat_id,
                             (_sent or {}).get("message_id"))
                except Exception as _e:
                    log.warning("Relay SDK: warn send failed: %s", _e)

    except Exception as e:
        # Ensure ticker is stopped on exception path
        _stream_done = True
        if _ticker_task and not _ticker_task.done():
            _ticker_task.cancel()
        log.error("Relay SDK handler error: %s", e, exc_info=True)
        if source == "telegram":
            # Parity with primary-chat salvage: delete the orphaned placeholder
            # BEFORE sending the error message, so the user doesn't see a
            # stuck "Thinking..." + an error message side-by-side.
            if placeholder_id:
                try:
                    from integrations.telegram import delete_message as _dm
                    await _dm(placeholder_id, chat_id=tg_cid)
                    log.info("Relay exc-path: deleted orphan placeholder %s",
                             placeholder_id)
                except Exception as _de:
                    log.debug("Relay exc-path placeholder delete failed: %s", _de)
                if _relay_placeholders.get(chat_id) == placeholder_id:
                    _relay_placeholders.pop(chat_id, None)
                placeholder_id = None
            try:
                from integrations.telegram import send_message
                await send_message(
                    f"\u26a0\ufe0f Relay handler error: <code>{_html.escape(str(e)[:300])}</code>",
                    chat_id=tg_cid,
                    reply_to_message_id=reply_to,
                    parse_mode="html",
                )
            except Exception:
                pass
    finally:
        # Defensive: ensure ticker stopped on all exit paths
        _stream_done = True
        if _ticker_task and not _ticker_task.done():
            _ticker_task.cancel()
        _active_relay_chats.discard(str(chat_id))
        # Only clear _relay_tasks if this task is still the tracked one.
        # Concurrent mid-task injection may have overwritten the ref with
        # a later task; popping blindly would remove that tracking and
        # break detection for future injections.
        try:
            if _relay_tasks.get(str(chat_id)) is asyncio.current_task():
                _relay_tasks.pop(str(chat_id), None)
        except Exception:
            _relay_tasks.pop(str(chat_id), None)
        # Parity with primary-chat finally: last-resort cleanup for any
        # placeholder that escaped all branches above (defensive). Normal
        # paths null out placeholder_id after edit/delete; if it's still set
        # here, something went wrong and we must not leave it orphaned.
        if source == "telegram" and placeholder_id:
            try:
                from integrations.telegram import delete_message as _dm
                await _dm(placeholder_id, chat_id=tg_cid)
                log.warning("Relay finally: deleted orphan placeholder %s "
                            "(escaped finalize branches)", placeholder_id)
            except Exception as _de:
                log.debug("Relay finally placeholder delete failed: %s", _de)
        if _relay_placeholders.get(chat_id) == placeholder_id:
            _relay_placeholders.pop(chat_id, None)
        # Drain mid-task queue: if new messages arrived while this daemon
        # was running, dispatch the next one now. Uses the existing
        # "Noted..." placeholder already placed by the dispatch block.
        queued = _pending_relay_queue.get(str(chat_id))
        if queued:
            next_item = queued.pop(0)
            if not queued:
                _pending_relay_queue.pop(str(chat_id), None)
            try:
                from bot.desktop_relay import get_relay_mode
                entry = get_relay_mode(chat_id) or {}
                asyncio.create_task(
                    _handle_relay_message(
                        next_item["source"], chat_id, next_item["text"],
                        entry.get("session_name", ""),
                        message_id=next_item["message_id"],
                        project_path=entry.get("project_path", ""),
                        pinned_jsonl_path=entry.get("pinned_jsonl_path", ""),
                        is_injection=True,
                    )
                )
                log.info(f"Relay queue drain: spawned next daemon for chat={chat_id}")
            except Exception as e:
                log.error(f"Relay queue drain failed for chat={chat_id}: {e}",
                          exc_info=True)
