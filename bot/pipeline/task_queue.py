# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Persistence-backed task queue — crash-safe, infinite, restart-recoverable.

Replaces the in-memory asyncio.Queue + collections.deque overflow with a SQLite
table (`task_queue`). Tasks survive restarts: pending rows are re-dispatched on
startup. No message is ever lost due to crash or OOM.

Design:
- Single writer (enqueue), single reader (worker_loop) — no contention.
- Event-driven: asyncio.Event signals new work; zero polling.
- Triage happens BEFORE insert (cancel/diagnose/drop handled inline).
- Catch-up: stale external-chat messages stored with is_catchup=1,
  aggregated after a 5s debounce into a single synthetic task.
- GC: completed/failed rows older than 24h are purged on startup.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Callable, Awaitable

from bot.storage.db import open_db
from config import ORG_TIMEZONE

from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
TZ = ZoneInfo(ORG_TIMEZONE)

# Max messages to aggregate in a single catch-up batch (DoS protection)
MAX_CATCHUP_MESSAGES = 100

# --- Schema ---

TASK_QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending, processing, done, failed, cancelled
    source TEXT NOT NULL,                     -- telegram, teams
    chat_id TEXT NOT NULL,                    -- target chat for reply
    user_name TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',      -- platform message ID (for dedup)
    text TEXT NOT NULL DEFAULT '',
    parsed_json TEXT NOT NULL DEFAULT '{}',   -- full parsed dict, JSON-encoded
    is_catchup INTEGER NOT NULL DEFAULT 0,    -- 1 = stale external-chat batch
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_tq_status ON task_queue(status);
CREATE INDEX IF NOT EXISTS idx_tq_catchup ON task_queue(is_catchup, status, chat_id);
"""


class PersistentQueue:
    """SQLite-backed task queue with event-driven dispatch."""

    def __init__(
        self,
        dispatch_fn: Callable[[dict], Awaitable[None]],
        can_accept_fn: Callable[[], bool],
        catchup_delay: float = 5.0,
        max_pending: int = 50,
        excluded_chats_fn: Callable[[], set[str]] | None = None,
    ):
        """
        Args:
            dispatch_fn: async callable that runs a single task (receives parsed dict).
            can_accept_fn: returns True when a global worker slot is free.
            catchup_delay: seconds to wait before aggregating catch-up rows.
            max_pending: max pending rows before oldest are evicted (0 = unlimited).
            excluded_chats_fn: returns set of chat_ids at their per-chat limit.
        """
        self._dispatch = dispatch_fn
        self._can_accept = can_accept_fn
        self._catchup_delay = catchup_delay
        self._max_pending = max_pending
        self._excluded_chats = excluded_chats_fn
        self._wake = asyncio.Event()
        self._catchup_timers: dict[str, asyncio.TimerHandle] = {}  # chat_id -> scheduled handle
        self._running = False
        # v5 burst-coalesce debounce: hold dispatch of a freshly-enqueued
        # (or freshly-merged) row for this many ms so rapid-fire burst members
        # can merge into it. Every new merge bumps updated_at, extending the
        # window. Config: PQ_DISPATCH_DEBOUNCE_MS env var, default 150.
        # Set to 0 to disable (pre-v5 behavior).
        try:
            self._dispatch_debounce_ms = int(
                os.environ.get("PQ_DISPATCH_DEBOUNCE_MS", "150")
            )
        except (TypeError, ValueError):
            self._dispatch_debounce_ms = 150
        if self._dispatch_debounce_ms < 0:
            self._dispatch_debounce_ms = 0
        # Track debounce-wake timers so we don't pile them up on repeated polls
        self._debounce_timer: asyncio.TimerHandle | None = None

    # --- Schema init ---

    async def init_schema(self):
        """Create the task_queue table if it doesn't exist."""
        async with open_db() as db:
            await db.executescript(TASK_QUEUE_SCHEMA)
            # Migration: add message_id column if missing (tables created before dedup).
            # The dedup index is NOT in TASK_QUEUE_SCHEMA because executescript() would
            # fail on existing tables where message_id doesn't exist yet (CREATE TABLE
            # IF NOT EXISTS is skipped, but CREATE INDEX references the missing column).
            cursor = await db.execute("PRAGMA table_info(task_queue)")
            columns = {row[1] for row in await cursor.fetchall()}
            if "message_id" not in columns:
                await db.execute("ALTER TABLE task_queue ADD COLUMN message_id TEXT NOT NULL DEFAULT ''")
                log.info("[PQ] migrated: added message_id column")
            # Always ensure dedup index exists (safe: IF NOT EXISTS)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tq_dedup "
                "ON task_queue(source, chat_id, message_id)"
            )
            await db.commit()
        log.info("[PQ] task_queue schema ready")

    # --- Enqueue ---

    async def enqueue(self, parsed: dict, *, is_catchup: bool = False) -> tuple[int, list[str]]:
        """Insert a task row and signal the worker.

        Returns (row_id, evicted_texts) where evicted_texts is a list of
        truncated message texts that were evicted due to queue overflow.
        """
        source = parsed.get("source") or ""  # CB-133: no platform assumption
        chat_id = str(
            parsed.get("chat_id")
            or parsed.get("chat_jid")
            or ""
        )
        # CB-144: Include thread_id in queue key so topics process independently
        _tid = parsed.get("message_thread_id")
        if _tid is not None:
            chat_id = f"{chat_id}:{_tid}"
        user_name = parsed.get("user_name", "")
        user_id = str(parsed.get("user_id", ""))
        message_id = str(parsed.get("message_id", ""))
        text = parsed.get("text", "")
        now = datetime.now(TZ).isoformat()
        evicted_texts: list[str] = []

        async with open_db() as db:
            # Evict oldest pending rows if queue is at capacity
            if self._max_pending > 0:
                rows = await db.execute_fetchall(
                    "SELECT COUNT(*) as cnt FROM task_queue WHERE status='pending'"
                )
                pending = rows[0]["cnt"] if rows else 0
                if pending >= self._max_pending:
                    # Cancel oldest pending rows to make room (FIFO eviction)
                    overflow = pending - self._max_pending + 1
                    evict_rows = await db.execute_fetchall(
                        "SELECT id, text FROM task_queue "
                        "WHERE status='pending' ORDER BY id LIMIT ?",
                        (overflow,),
                    )
                    if evict_rows:
                        evict_ids = [r["id"] for r in evict_rows]
                        ph = ",".join("?" * len(evict_ids))
                        await db.execute(
                            f"UPDATE task_queue SET status='cancelled', "
                            f"error='evicted: queue overflow', updated_at=? "
                            f"WHERE id IN ({ph})",
                            [now] + evict_ids,
                        )
                        evicted_texts = [r["text"][:60] for r in evict_rows]
                        log.warning(f"[PQ] queue overflow ({pending}/{self._max_pending}), "
                                    f"evicted {len(evict_ids)} oldest: {evicted_texts}")

            cur = await db.execute(
                "INSERT INTO task_queue "
                "(status, source, chat_id, user_name, user_id, message_id, text, parsed_json, "
                " is_catchup, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "pending", source, chat_id, user_name, user_id, message_id,
                    text[:500], json.dumps(parsed, default=str),
                    1 if is_catchup else 0, now, now,
                ),
            )
            await db.commit()
            row_id = cur.lastrowid

        log.info(f"[PQ] enqueued id={row_id} catchup={is_catchup} "
                 f"chat={chat_id} {user_name}: {text[:60]}")

        if is_catchup:
            self._schedule_catchup_signal(chat_id)
        else:
            self._wake.set()

        return row_id, evicted_texts

    # --- Mid-burst merge ---

    # Soft caps for merged rows. Exceeding either bails to a fresh enqueue.
    _MERGE_MAX_TEXT_CHARS = 50_000
    _MERGE_MAX_MEDIA_ITEMS = 10

    async def merge_into_pending(
        self, parsed: dict, enriched_text: str
    ) -> int | None:
        """Merge a new message into the latest pending row for this chat.

        Used to coalesce rapid-fire bursts (e.g. multi-forwards) that arrive
        BEFORE the first has dispatched. Appends:
          * `enriched_text` → parsed_json["text"] (newline-joined)
          * media raw metadata → parsed_json["merged_media"] (list)
          * new message_id → parsed_json["merged_message_ids"] (list)

        Atomic SELECT+UPDATE with `status='pending'` guard. If the row was
        dispatched between SELECT and UPDATE (rowcount=0), returns None so
        the caller can fall through to enqueue a fresh row.

        Soft caps: _MERGE_MAX_TEXT_CHARS / _MERGE_MAX_MEDIA_ITEMS. Exceeding
        either returns None.

        Returns merged row_id on success, None otherwise.
        """
        source = parsed.get("source") or ""  # CB-133: no platform assumption
        chat_id = str(
            parsed.get("chat_id") or parsed.get("chat_jid") or ""
        )
        # CB-144: Include thread_id in queue key for per-topic isolation
        _tid = parsed.get("message_thread_id")
        if _tid is not None:
            chat_id = f"{chat_id}:{_tid}"
        message_id = str(parsed.get("message_id", ""))
        has_media = bool(parsed.get("has_media") or parsed.get("media_info"))
        now = datetime.now(TZ).isoformat()

        async with open_db() as db:
            rows = await db.execute_fetchall(
                "SELECT id, parsed_json FROM task_queue "
                "WHERE source=? AND chat_id=? AND status='pending' "
                "AND is_catchup=0 ORDER BY id DESC LIMIT 1",
                (source, chat_id),
            )
            if not rows:
                return None
            row_id = rows[0]["id"]
            try:
                pj = json.loads(rows[0]["parsed_json"])
            except Exception:
                log.warning(f"[PQ] merge: invalid parsed_json on row={row_id}")
                return None

            existing_text = pj.get("text", "") or ""
            combined_text = (
                (existing_text + "\n\n" + enriched_text).strip()
                if existing_text
                else enriched_text
            )
            if len(combined_text) > self._MERGE_MAX_TEXT_CHARS:
                log.info(
                    f"[PQ] merge skipped (text>{self._MERGE_MAX_TEXT_CHARS}): "
                    f"row={row_id} chat={chat_id}"
                )
                return None

            merged_media = list(pj.get("merged_media") or [])
            if has_media and parsed.get("raw"):
                if len(merged_media) >= self._MERGE_MAX_MEDIA_ITEMS:
                    log.info(
                        f"[PQ] merge skipped (media>{self._MERGE_MAX_MEDIA_ITEMS}): "
                        f"row={row_id} chat={chat_id}"
                    )
                    return None
                merged_media.append({
                    "raw": parsed["raw"],
                    "message_id": message_id,
                    "has_media": True,
                    "caption": enriched_text,
                    "forward": parsed.get("forward"),
                    "reply_to": parsed.get("reply_to"),
                })

            merged_ids = list(pj.get("merged_message_ids") or [])
            if message_id and message_id not in merged_ids:
                merged_ids.append(message_id)

            pj["text"] = combined_text
            pj["merged_media"] = merged_media
            pj["merged_message_ids"] = merged_ids

            cur = await db.execute(
                "UPDATE task_queue SET text=?, parsed_json=?, updated_at=? "
                "WHERE id=? AND status='pending'",
                (
                    combined_text[:500],
                    json.dumps(pj, default=str),
                    now,
                    row_id,
                ),
            )
            await db.commit()
            if cur.rowcount == 0:
                # Row was picked up by worker between SELECT and UPDATE.
                log.info(
                    f"[PQ] merge lost race (dispatched): row={row_id} "
                    f"chat={chat_id} msg={message_id}"
                )
                return None

        log.info(
            f"[PQ] merged msg={message_id} into row={row_id} "
            f"chat={chat_id} text_len={len(combined_text)} "
            f"media_n={len(merged_media)}"
        )
        return row_id

    # --- Burst dispatch debounce ---

    def _schedule_debounce_wake(self, delay_ms: float) -> None:
        """Wake the worker after delay_ms so a debounced row can be re-checked.

        Coalesces multiple calls: if a timer is already scheduled for later
        than or equal to delay_ms, keep it; otherwise cancel + reschedule.
        Fires only once per scheduled wake (loop.call_later auto-clears).
        """
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        delay_s = max(0.01, delay_ms / 1000)
        # If an existing timer will fire sooner or equal, reuse it.
        existing = self._debounce_timer
        if existing is not None and not existing.cancelled():
            remaining = existing.when() - loop.time()
            if remaining <= delay_s:
                return
            existing.cancel()

        def _wake_and_clear() -> None:
            self._debounce_timer = None
            self._wake.set()

        self._debounce_timer = loop.call_later(delay_s, _wake_and_clear)

    # --- Catch-up debounce ---

    def _schedule_catchup_signal(self, chat_id: str):
        """Schedule a wake signal after catchup_delay seconds (debounced per chat)."""
        existing = self._catchup_timers.pop(chat_id, None)
        if existing is not None:
            existing.cancel()

        loop = asyncio.get_event_loop()
        handle = loop.call_later(
            self._catchup_delay,
            lambda: self._wake.set(),
        )
        self._catchup_timers[chat_id] = handle

    # --- Worker loop ---

    async def worker_loop(self):
        """Main dispatch loop — runs forever, wakes on signal."""
        self._running = True
        log.info("[PQ] worker_loop started")
        while self._running:
            try:
                # Wait for signal (new enqueue or catchup timer)
                await self._wake.wait()
                self._wake.clear()

                # Drain all claimable rows
                while self._running:
                    if not self._can_accept():
                        # No free global slot — stop draining, wait for next signal
                        break

                    # Get chats that are at their per-chat limit
                    try:
                        excluded = self._excluded_chats() if self._excluded_chats else set()
                    except Exception:
                        log.warning("[PQ] excluded_chats_fn raised, using empty set", exc_info=True)
                        excluded = set()

                    row = await self._claim_next(excluded)
                    if row is None:
                        break  # nothing pending

                    row_id = row["id"]
                    is_catchup = row["is_catchup"]

                    if is_catchup:
                        parsed = await self._aggregate_catchup(row["chat_id"])
                        if parsed is None:
                            # All rows corrupt or empty — complete ALL catchup rows for this chat
                            await self._complete_all_catchup(row["chat_id"])
                            continue
                    else:
                        try:
                            parsed = json.loads(row["parsed_json"])
                        except (json.JSONDecodeError, TypeError):
                            log.error(f"[PQ] bad JSON in row {row_id}, marking failed")
                            await self._fail(row_id, "invalid parsed_json")
                            continue

                    # Attach row_id so dispatch can mark complete/failed
                    parsed["_pq_row_id"] = row_id

                    try:
                        await self._dispatch(parsed)
                    except Exception as e:
                        error_msg = str(e)[:500]
                        log.error(f"[PQ] dispatch error for row {row_id}: {error_msg}", exc_info=True)
                        await self._fail(row_id, error_msg)

            except asyncio.CancelledError:
                log.info("[PQ] worker_loop cancelled")
                break
            except Exception as e:
                log.error(f"[PQ] worker_loop error: {e}", exc_info=True)
                await asyncio.sleep(1)  # prevent tight error loop

    # --- Claim / complete / fail ---

    async def _claim_next(self, excluded_chats: set[str] | None = None) -> dict | None:
        """Claim the oldest pending non-catchup row, or oldest ripe catchup row.

        Returns a dict-like Row or None.
        Catchup rows are only claimed if no pending timer exists for their chat.
        Rows from excluded_chats (at per-chat limit) are skipped.
        """
        excluded = excluded_chats or set()
        async with open_db() as db:
            # Priority 1: non-catchup pending
            if excluded:
                placeholders = ",".join("?" for _ in excluded)
                row = await db.execute_fetchall(
                    f"SELECT * FROM task_queue WHERE status='pending' AND is_catchup=0 "
                    f"AND chat_id NOT IN ({placeholders}) "
                    f"ORDER BY id LIMIT 1",
                    tuple(excluded),
                )
            else:
                row = await db.execute_fetchall(
                    "SELECT * FROM task_queue WHERE status='pending' AND is_catchup=0 "
                    "ORDER BY id LIMIT 1"
                )
            if row:
                # v5 burst-coalesce debounce: defer claim if the row was last
                # touched (INSERT or MERGE) within the debounce window. Every
                # merge_into_pending call bumps updated_at, so the window
                # extends as long as burst members keep arriving. Once the
                # chat goes quiet for DEBOUNCE_MS, claim + dispatch.
                if self._dispatch_debounce_ms > 0:
                    try:
                        row_ts = datetime.fromisoformat(row[0]["updated_at"])
                        now_ts = datetime.now(TZ)
                        elapsed_ms = (now_ts - row_ts).total_seconds() * 1000
                        if elapsed_ms < self._dispatch_debounce_ms:
                            remaining_ms = (
                                self._dispatch_debounce_ms - elapsed_ms
                            )
                            self._schedule_debounce_wake(remaining_ms)
                            log.debug(
                                f"[PQ] debounce: row={row[0]['id']} fresh "
                                f"({elapsed_ms:.0f}ms), defer {remaining_ms:.0f}ms"
                            )
                            return None
                    except Exception as _de:
                        log.debug(f"[PQ] debounce parse failed: {_de}")
                await db.execute(
                    "UPDATE task_queue SET status='processing', updated_at=? WHERE id=?",
                    (datetime.now(TZ).isoformat(), row[0]["id"]),
                )
                await db.commit()
                return row[0]

            # Priority 2: catchup pending (only if no debounce timer is active for the chat)
            rows = await db.execute_fetchall(
                "SELECT DISTINCT chat_id FROM task_queue "
                "WHERE status='pending' AND is_catchup=1"
            )
            for r in rows:
                cid = r["chat_id"]
                if cid in self._catchup_timers:
                    continue  # still collecting — wait for debounce
                if cid in excluded:
                    continue  # chat at per-chat limit
                # Claim the first row for this chat (will aggregate all others)
                first = await db.execute_fetchall(
                    "SELECT * FROM task_queue "
                    "WHERE status='pending' AND is_catchup=1 AND chat_id=? "
                    "ORDER BY id LIMIT 1",
                    (cid,),
                )
                if first:
                    await db.execute(
                        "UPDATE task_queue SET status='processing', updated_at=? WHERE id=?",
                        (datetime.now(TZ).isoformat(), first[0]["id"]),
                    )
                    await db.commit()
                    return first[0]

            return None

    async def _aggregate_catchup(self, chat_id: str) -> dict | None:
        """Aggregate all pending catchup rows for a chat into one synthetic message.

        Claims all remaining pending catchup rows for this chat and marks them processing.
        Returns a single synthetic parsed dict, or None if no messages found.
        """
        async with open_db() as db:
            rows = await db.execute_fetchall(
                "SELECT id, parsed_json FROM task_queue "
                "WHERE status='pending' AND is_catchup=1 AND chat_id=? "
                "ORDER BY id",
                (chat_id,),
            )
            # Also include the already-claimed row (status=processing)
            processing_rows = await db.execute_fetchall(
                "SELECT id, parsed_json FROM task_queue "
                "WHERE status='processing' AND is_catchup=1 AND chat_id=? "
                "ORDER BY id",
                (chat_id,),
            )
            all_rows = list(processing_rows) + list(rows)

            if not all_rows:
                return None

            # Mark remaining pending rows as processing
            pending_ids = [r["id"] for r in rows]
            if pending_ids:
                placeholders = ",".join("?" * len(pending_ids))
                now = datetime.now(TZ).isoformat()
                await db.execute(
                    f"UPDATE task_queue SET status='processing', updated_at=? "
                    f"WHERE id IN ({placeholders})",
                    [now] + pending_ids,
                )
                await db.commit()

        # Parse all messages
        messages = []
        all_ids = []
        source = ""  # CB-133: extract from parsed rows, no platform assumption
        ec_title = f"Chat {chat_id}"
        skipped = 0

        for r in all_rows:
            all_ids.append(r["id"])
            try:
                p = json.loads(r["parsed_json"])
            except (json.JSONDecodeError, TypeError):
                skipped += 1
                continue
            source = p.get("source", source)
            ec_title = p.get("external_chat_title", ec_title)
            sender = p.get("user_name", "?")
            text = p.get("text", "")
            msg_id = p.get("message_id", 0)
            messages.append({"sender": sender, "text": text[:200], "msg_id": msg_id})

        if skipped:
            log.warning(f"[PQ] {skipped}/{len(all_rows)} catch-up rows had invalid JSON "
                        f"for chat {chat_id}")

        if not messages:
            return None

        # Cap aggregation to prevent DoS via massive catch-up batches
        total_msgs = len(messages)
        if total_msgs > MAX_CATCHUP_MESSAGES:
            messages = messages[:MAX_CATCHUP_MESSAGES]

        # Build summary (include message_id for reply-threading)
        lines = []
        for m in messages:
            mid = m.get("msg_id", 0)
            mid_tag = f" [msg_id={mid}]" if mid else ""
            lines.append(f"  {m['sender']}{mid_tag}: {m['text']}")
        if total_msgs > MAX_CATCHUP_MESSAGES:
            lines.append(f"  [... and {total_msgs - MAX_CATCHUP_MESSAGES} more messages]")
        summary = "\n".join(lines)

        log.info(f"[PQ] catch-up aggregated {len(messages)} msg(s) for {ec_title}")

        # Build synthetic parsed dict
        synthetic = {
            "source": source,
            "is_bot_message": False,
            "is_external_chat": True,
            "is_external_user": True,
            "user_id": 0,
            "user_name": "System",
            "text": (
                f"[External chat: {ec_title}] "
                f"[CATCH-UP: {len(messages)} missed message(s) during restart]\n"
                f"{summary}\n\n"
                # CB-133: Use platform-appropriate send tool
                f"[SYSTEM: These messages were sent while the bot was restarting. "
                f"Read ALL messages chronologically to understand context. Reply via "
                f"{'teams_send_message' if source == 'teams' else 'telegram_send_message'} with chat_id="
                f"{chat_id}. "
                f"PRIORITY: A user's LAST message supersedes their earlier ones — respond to their "
                f"final position, not intermediate messages they corrected or updated. "
                f"REPLY DECISION: Only reply if someone is addressing the bot, asking a question "
                f"only you can answer, or the conversation needs your input. If participants are "
                f"talking to each other, stay silent — do NOT interject. "
                f"When replying to specific people, use reply_to_message_id=<msg_id> to thread "
                f"your reply to their message. "
                f"Be warm and friendly. Do NOT mention the restart. Do NOT over-respond — "
                f"one reply per topic, keep it short.]"
            ),
            "message_id": 0,
            "chat_id": chat_id,
            "has_media": False,
            "location": None,
            "contact": None,
            "reply_to": None,
            "forward": None,
            "raw": {},
            "external_chat_title": ec_title,
            "_pq_catchup_ids": all_ids,  # all row IDs to mark complete
        }
        return synthetic

    async def complete(self, row_id: int | None, *, catchup_ids: list[int] | None = None):
        """Mark a task row (and any catchup siblings) as done."""
        ids_to_mark = []
        if row_id is not None:
            ids_to_mark.append(row_id)
        if catchup_ids:
            ids_to_mark.extend(catchup_ids)
        if not ids_to_mark:
            return
        async with open_db() as db:
            placeholders = ",".join("?" * len(ids_to_mark))
            await db.execute(
                f"UPDATE task_queue SET status='done', updated_at=? "
                f"WHERE id IN ({placeholders})",
                [datetime.now(TZ).isoformat()] + ids_to_mark,
            )
            await db.commit()

    async def fail(self, row_id: int | None, error: str, *, catchup_ids: list[int] | None = None):
        """Mark a task row (and any catchup siblings) as failed."""
        ids_to_mark = []
        if row_id is not None:
            ids_to_mark.append(row_id)
        if catchup_ids:
            ids_to_mark.extend(catchup_ids)
        if not ids_to_mark:
            return
        async with open_db() as db:
            placeholders = ",".join("?" * len(ids_to_mark))
            await db.execute(
                f"UPDATE task_queue SET status='failed', error=?, updated_at=? "
                f"WHERE id IN ({placeholders})",
                [error[:500], datetime.now(TZ).isoformat()] + ids_to_mark,
            )
            await db.commit()

    async def _complete(self, row_id: int):
        """Internal shortcut for single-row completion."""
        await self.complete(row_id)

    async def _fail(self, row_id: int, error: str):
        """Internal shortcut for single-row failure."""
        await self.fail(row_id, error)

    async def _complete_all_catchup(self, chat_id: str):
        """Mark ALL catchup rows (pending + processing) for a chat as done.

        Used when _aggregate_catchup returns None (all rows had invalid JSON).
        Prevents rows from getting stuck in 'processing' forever.
        """
        async with open_db() as db:
            cur = await db.execute(
                "UPDATE task_queue SET status='done', updated_at=? "
                "WHERE is_catchup=1 AND chat_id=? AND status IN ('pending','processing')",
                (datetime.now(TZ).isoformat(), chat_id),
            )
            await db.commit()
            if cur.rowcount:
                log.warning(f"[PQ] completed {cur.rowcount} empty/corrupt catchup row(s) "
                            f"for chat {chat_id}")

    # --- Cancel ---

    async def cancel_for_chat(self, chat_id: str) -> int:
        """Cancel all pending tasks for a specific chat. Returns count cancelled."""
        async with open_db() as db:
            cur = await db.execute(
                "UPDATE task_queue SET status='cancelled', updated_at=? "
                "WHERE status='pending' AND chat_id=?",
                (datetime.now(TZ).isoformat(), chat_id),
            )
            await db.commit()
            count = cur.rowcount
        if count:
            log.info(f"[PQ] cancelled {count} pending task(s) for chat {chat_id}")
        return count

    async def get_pending_count(self) -> int:
        """Count of pending (not yet claimed) rows."""
        async with open_db() as db:
            rows = await db.execute_fetchall(
                "SELECT COUNT(*) as cnt FROM task_queue WHERE status='pending'"
            )
            return rows[0]["cnt"] if rows else 0

    # --- Startup recovery ---

    async def startup_recovery(self):
        """On startup: handle 'processing' rows interrupted by shutdown/deploy.

        If the bot was restarted by a deploy (recent deploy_result.json), mark
        processing rows as 'done' — they triggered the deploy and must not re-run.
        Otherwise, reset them to 'pending' for retry (crash recovery).
        """
        deploy_triggered = self._was_deploy_restart()
        now_iso = datetime.now(TZ).isoformat()

        async with open_db() as db:
            if deploy_triggered:
                cur = await db.execute(
                    "UPDATE task_queue SET status='done', updated_at=? "
                    "WHERE status='processing'",
                    (now_iso,),
                )
                await db.commit()
                if cur.rowcount:
                    log.info(f"[PQ] post-deploy: marked {cur.rowcount} processing task(s) as done "
                             f"(interrupted by deploy, not re-queued)")
            else:
                cur = await db.execute(
                    "UPDATE task_queue SET status='pending', updated_at=? "
                    "WHERE status='processing'",
                    (now_iso,),
                )
                await db.commit()
                if cur.rowcount:
                    log.info(f"[PQ] crash recovery: reset {cur.rowcount} interrupted task(s) → pending")
                    self._wake.set()

    @staticmethod
    def _was_deploy_restart() -> bool:
        """Check if this startup follows a deploy (marker file left by deploy_bot)."""
        from pathlib import Path
        marker = Path("/app/data/deploy_pending")
        if marker.exists():
            try:
                action = marker.read_text().strip()
                marker.unlink()
                log.info(f"[PQ] deploy marker found (action={action}), removing")
                return True
            except Exception as e:
                log.warning(f"[PQ] deploy marker read/delete failed: {e}")
                return True  # marker exists but unreadable — still treat as deploy
        return False

    async def gc_old_rows(self, hours: int = 24):
        """Delete completed/failed/cancelled rows older than `hours`."""
        cutoff = (datetime.now(TZ) - timedelta(hours=hours)).isoformat()
        async with open_db() as db:
            cur = await db.execute(
                "DELETE FROM task_queue WHERE status IN ('done','failed','cancelled') "
                "AND updated_at < ?",
                (cutoff,),
            )
            await db.commit()
            if cur.rowcount:
                log.info(f"[PQ] GC purged {cur.rowcount} old rows (>{hours}h)")

    # --- Shutdown ---

    def stop(self):
        """Signal the worker loop to stop and clean up timers."""
        self._running = False
        for handle in self._catchup_timers.values():
            handle.cancel()
        self._catchup_timers.clear()
        self._wake.set()

    # --- Signal (external wake-up) ---

    def signal(self):
        """Wake the worker loop (e.g. after a task completes and a slot opens)."""
        self._wake.set()
