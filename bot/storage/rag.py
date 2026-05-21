# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""RAG — Per-chat chunk-based semantic search over conversations.

Architecture:
- Per-chat isolation: chunks are built per chat_id, never mixing conversations
- Size-aware chunking: character budget (default 2000 chars) per chunk, max 7 msgs
- Long messages (>budget) auto-split into overlapping sub-chunks (200 char overlap)
- Consecutive chunks overlap by 2 messages for context continuity
- No truncation: every character of every message is embedded and searchable
- Gemini gemini-embedding-001 embeddings (3072-dim, stored as float32 blobs)
- Cosine similarity search with AccessContext filtering at SQL level
- Only skips msg_type in ('status', 'placeholder', 'system')
- Keeps everything else: short messages, emoji, "ok", media descriptors
- Post-search: agent can fetch full conversation context via msg ID range
"""

import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import heapq

import numpy as np

from bot.storage.db import open_db
from config import (
    GEMINI_API_KEY, ORG_TIMEZONE,
    RAG_CHUNK_SIZE,
    RAG_MAX_CHUNK_CHARS, RAG_CHUNK_OVERLAP_MSGS, RAG_LONG_MSG_OVERLAP_CHARS,
    RAG_EMBEDDING_MODEL, RAG_EMBEDDING_DIM, RAG_EMBEDDING_BATCH_SIZE,
    RAG_SEARCH_BATCH_SIZE,
)
from itertools import groupby

log = logging.getLogger(__name__)
TZ = ZoneInfo(ORG_TIMEZONE)
_backfill_lock = asyncio.Lock()

# === CONFIG (imported from config.py — single source of truth) ===
EMBEDDING_MODEL = RAG_EMBEDDING_MODEL
EMBEDDING_DIM = RAG_EMBEDDING_DIM
BATCH_SIZE = RAG_EMBEDDING_BATCH_SIZE
CHUNK_SIZE = RAG_CHUNK_SIZE          # max messages per chunk (cap)
MAX_CHUNK_CHARS = RAG_MAX_CHUNK_CHARS          # character budget per chunk
OVERLAP_MSGS = RAG_CHUNK_OVERLAP_MSGS          # overlap msgs between chunks
LONG_MSG_OVERLAP = RAG_LONG_MSG_OVERLAP_CHARS  # overlap chars for split long msgs
SKIP_MSG_TYPES = {"status", "placeholder", "system"}


# === EMBEDDING GENERATION ===

def _get_genai_client():
    """Lazy-init Gemini client."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not configured")
    from google import genai
    return genai.Client(api_key=GEMINI_API_KEY)


def _vec_to_blob(vec: np.ndarray) -> bytes:
    return vec.astype(np.float32).tobytes()


def _blob_to_vec(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


async def embed_texts(texts: list[str]) -> list[np.ndarray]:
    """Embed a batch of texts via Gemini API. Returns list of embedding vectors."""
    if not texts:
        return []
    client = _get_genai_client()
    all_embeddings = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i:i + BATCH_SIZE]
        try:
            result = await asyncio.to_thread(
                client.models.embed_content,
                model=EMBEDDING_MODEL,
                contents=batch,
            )
            for emb in result.embeddings:
                all_embeddings.append(np.array(emb.values, dtype=np.float32))
        except Exception as e:
            log.error(f"Embedding API error (batch {i // BATCH_SIZE}): {e}")
            for _ in batch:
                all_embeddings.append(np.zeros(EMBEDDING_DIM, dtype=np.float32))
    return all_embeddings


# === TABLE INIT ===

async def init_rag_tables():
    """Create rag_chunks table if it doesn't exist, with chat_id for per-chat isolation."""
    async with open_db() as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS rag_chunks (
                chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                chat_id TEXT NOT NULL DEFAULT '',
                start_msg_id INTEGER NOT NULL,
                end_msg_id INTEGER NOT NULL,
                senders TEXT NOT NULL,
                start_ts TEXT NOT NULL,
                end_ts TEXT NOT NULL,
                chunk_text TEXT NOT NULL,
                embedding BLOB NOT NULL,
                model TEXT NOT NULL DEFAULT 'gemini-embedding-001',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rag_chunks_msg_range
                ON rag_chunks(start_msg_id, end_msg_id);
        """)
        # Migration: add chat_id column if missing (existing installs).
        # NOTE: idx_rag_chunks_chat_id is created HERE (not in executescript above)
        # because executescript runs before the migration — if the table exists
        # with the old schema, CREATE INDEX on chat_id would fail before the
        # ALTER TABLE adds the column.
        try:
            await db.execute("SELECT chat_id FROM rag_chunks LIMIT 0")
        except Exception:  # OperationalError if column missing
            await db.execute("ALTER TABLE rag_chunks ADD COLUMN chat_id TEXT NOT NULL DEFAULT ''")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_rag_chunks_chat_id ON rag_chunks(chat_id)")
        await db.commit()
    log.info("RAG v4 rag_chunks table initialized (per-chat)")


# === CHUNKING ===

def _format_message_for_chunk(row: dict) -> str:
    """Format a single message row into chunk text line."""
    source = row.get("source", "")
    user = row.get("user_name", "")
    text = row.get("text", "")
    ts = row.get("timestamp", "")[:16]  # trim seconds
    prefix = f"[{ts} {source}]"
    role = row.get("role", "")
    if role == "assistant":
        return f"{prefix} Bot: {text}"
    return f"{prefix} {user}: {text}"


async def _fetch_eligible_messages(after_msg_id: int = 0) -> list[dict]:
    """Fetch all messages eligible for chunking (skipping status/placeholder/system).

    Returns messages ordered by chat_id then id, so groupby(chat_id) yields
    per-chat message streams for isolated chunk building.
    """
    async with open_db() as db:

        cursor = await db.execute(
            "SELECT id, source, user_name, text, role, timestamp, msg_type, "
            "COALESCE(chat_id, '') AS chat_id "
            "FROM messages WHERE id > ? "
            "AND (msg_type IS NULL OR msg_type NOT IN ('status', 'placeholder', 'system')) "
            "ORDER BY chat_id, id",
            (after_msg_id,),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


def _split_long_message(msg: dict, formatted_line: str) -> list[dict]:
    """Split a single message that exceeds MAX_CHUNK_CHARS into overlapping sub-chunks.

    Each sub-chunk contains a portion of the message text with LONG_MSG_OVERLAP chars
    of overlap between consecutive sub-chunks, ensuring no content is lost.
    """
    text = formatted_line
    sub_chunks = []
    pos = 0

    while pos < len(text):
        end = min(pos + MAX_CHUNK_CHARS, len(text))
        chunk_text = text[pos:end]

        sub_chunks.append({
            "source": msg.get("source", ""),
            "chat_id": msg.get("chat_id", "") or "",
            "start_msg_id": msg["id"],
            "end_msg_id": msg["id"],
            "senders": msg.get("user_name", ""),
            "start_ts": msg.get("timestamp", ""),
            "end_ts": msg.get("timestamp", ""),
            "chunk_text": chunk_text,
        })

        if end >= len(text):
            break
        pos = end - LONG_MSG_OVERLAP  # overlap for continuity

    return sub_chunks


def _build_chunks(messages: list[dict]) -> list[dict]:
    """Build size-aware chunks with character budget and message overlap.

    Instead of fixed N-message windows, each chunk grows until it hits
    MAX_CHUNK_CHARS or CHUNK_SIZE messages. Long messages that exceed
    MAX_CHUNK_CHARS on their own are split into overlapping sub-chunks.
    Consecutive chunks overlap by OVERLAP_MSGS messages for context continuity.

    Returns list of chunk dicts with:
        source, start_msg_id, end_msg_id, senders, start_ts, end_ts, chunk_text
    """
    chunks = []
    n = len(messages)
    if n == 0:
        return chunks

    i = 0
    while i < n:
        chunk_lines = []
        chunk_msgs = []
        total_chars = 0
        j = i

        while j < n and len(chunk_msgs) < CHUNK_SIZE:
            line = _format_message_for_chunk(messages[j])
            line_len = len(line)

            # Single message exceeds budget — handle specially
            if line_len > MAX_CHUNK_CHARS:
                # Flush any accumulated content first
                if chunk_lines:
                    break
                # Split the long message into sub-chunks
                long_chunks = _split_long_message(messages[j], line)
                chunks.extend(long_chunks)
                j += 1
                i = j
                # Reset accumulators — the long msg was handled
                chunk_lines = []
                chunk_msgs = []
                total_chars = 0
                continue

            # Would this message exceed the budget?
            needed = line_len + (1 if chunk_lines else 0)  # +1 for \n separator
            if total_chars + needed > MAX_CHUNK_CHARS and chunk_lines:
                break  # stop here, this message goes into the next chunk

            chunk_lines.append(line)
            chunk_msgs.append(messages[j])
            total_chars += needed
            j += 1

        if chunk_lines:
            chunk_text = "\n".join(chunk_lines)
            senders = sorted(set(
                m.get("user_name", "") for m in chunk_msgs if m.get("user_name")
            ))
            sources = sorted(set(
                m.get("source", "") for m in chunk_msgs if m.get("source")
            ))

            # chat_id: all messages in the chunk should be from same chat
            # (caller groups by chat_id before calling _build_chunks)
            chunk_chat_id = chunk_msgs[0].get("chat_id", "") or ""

            chunks.append({
                "source": ",".join(sources),
                "chat_id": chunk_chat_id,
                "start_msg_id": chunk_msgs[0]["id"],
                "end_msg_id": chunk_msgs[-1]["id"],
                "senders": ",".join(senders),
                "start_ts": chunk_msgs[0].get("timestamp", ""),
                "end_ts": chunk_msgs[-1].get("timestamp", ""),
                "chunk_text": chunk_text,
            })

            # Advance with overlap — keep last OVERLAP_MSGS for context continuity
            overlap = min(OVERLAP_MSGS, len(chunk_msgs) - 1)
            advance = len(chunk_msgs) - overlap
            i += max(advance, 1)  # always advance at least 1
        elif j <= i:
            i += 1  # safety: always advance to avoid infinite loop

    return chunks


# === BACKFILL ===

async def backfill_chunks(progress_callback=None) -> dict:
    """Build and embed all chunks from scratch using atomic table swap.

    Creates a new table (rag_chunks_new), populates it, then atomically swaps
    it in place of the old rag_chunks table. Old data remains searchable during
    the rebuild; if the rebuild fails, old data is preserved.
    Guarded by _backfill_lock to prevent concurrent backfills.
    Returns stats dict.
    """
    if _backfill_lock.locked():
        return {"error": "Backfill already in progress"}
    if not GEMINI_API_KEY:
        return {"error": "GEMINI_API_KEY not configured"}

    async with _backfill_lock:
        return await _backfill_chunks_inner(progress_callback)


async def _backfill_chunks_inner(progress_callback=None) -> dict:
    """Inner backfill logic, called under _backfill_lock."""
    await init_rag_tables()

    t0 = time.time()

    # Fetch all eligible messages
    messages = await _fetch_eligible_messages(after_msg_id=0)
    total_msgs = len(messages)
    log.info(f"RAG backfill: {total_msgs} eligible messages")

    if total_msgs == 0:
        return {"total_messages": 0, "chunks_created": 0, "time_sec": 0}

    # Build chunks per chat (group by chat_id for isolation)
    chunks = []
    chat_count = 0
    for chat_id, chat_msgs_iter in groupby(messages, key=lambda m: m.get("chat_id", "") or ""):
        chat_msgs = list(chat_msgs_iter)
        chat_chunks = _build_chunks(chat_msgs)
        chunks.extend(chat_chunks)
        chat_count += 1
    total_chunks = len(chunks)
    # Log chunking stats for observability
    avg_chars = sum(len(c["chunk_text"]) for c in chunks) / max(total_chunks, 1)
    long_msg_chunks = sum(1 for c in chunks if c["start_msg_id"] == c["end_msg_id"]
                          and len(c["chunk_text"]) > MAX_CHUNK_CHARS // 2)
    log.info(f"RAG backfill: {total_chunks} chunks to embed across {chat_count} chats "
             f"(avg_chars={avg_chars:.0f}, long_msg_sub_chunks={long_msg_chunks}, "
             f"budget={MAX_CHUNK_CHARS}, overlap_msgs={OVERLAP_MSGS})")

    if total_chunks == 0:
        return {"total_messages": total_msgs, "chunks_created": 0, "time_sec": 0}

    # Create staging table for atomic swap (drop stale staging table from previous failed run)
    async with open_db() as db:
        await db.execute("DROP TABLE IF EXISTS rag_chunks_new")
        await db.executescript("""
            CREATE TABLE rag_chunks_new (
                chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                chat_id TEXT NOT NULL DEFAULT '',
                start_msg_id INTEGER NOT NULL,
                end_msg_id INTEGER NOT NULL,
                senders TEXT NOT NULL,
                start_ts TEXT NOT NULL,
                end_ts TEXT NOT NULL,
                chunk_text TEXT NOT NULL,
                embedding BLOB NOT NULL,
                model TEXT NOT NULL DEFAULT 'gemini-embedding-001',
                created_at TEXT NOT NULL
            );
        """)
        await db.commit()
    log.info("RAG backfill: staging table rag_chunks_new created")

    # Embed in batches and insert into staging table
    embedded = 0
    errors = 0
    now = datetime.now(TZ).isoformat()

    for batch_start in range(0, total_chunks, BATCH_SIZE):
        batch = chunks[batch_start:batch_start + BATCH_SIZE]
        texts = [c["chunk_text"] for c in batch]

        try:
            embeddings = await embed_texts(texts)

            async with open_db() as db:
                for chunk, emb in zip(batch, embeddings):
                    if np.allclose(emb, 0):
                        errors += 1
                        continue
                    await db.execute(
                        "INSERT INTO rag_chunks_new "
                        "(source, chat_id, start_msg_id, end_msg_id, senders, start_ts, end_ts, "
                        "chunk_text, embedding, model, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (chunk["source"], chunk.get("chat_id", ""),
                         chunk["start_msg_id"], chunk["end_msg_id"],
                         chunk["senders"], chunk["start_ts"], chunk["end_ts"],
                         chunk["chunk_text"], _vec_to_blob(emb), EMBEDDING_MODEL, now),
                    )
                    embedded += 1
                await db.commit()

            if progress_callback:
                await progress_callback(batch_start + len(batch), total_chunks, embedded)

            # Rate limit: be gentle with Gemini API
            if batch_start + BATCH_SIZE < total_chunks:
                await asyncio.sleep(0.5)

        except Exception as e:
            log.error(f"RAG backfill batch error at {batch_start}: {e}", exc_info=True)
            errors += len(batch)

    # Atomic swap: rename old → backup, promote new → live, drop backup
    if embedded > 0:
        try:
            async with open_db() as db:
                # Transactional DDL: SQLite supports DDL in transactions
                await db.execute("BEGIN")
                await db.execute("ALTER TABLE rag_chunks RENAME TO rag_chunks_old")
                await db.execute("ALTER TABLE rag_chunks_new RENAME TO rag_chunks")
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_rag_chunks_msg_range "
                    "ON rag_chunks(start_msg_id, end_msg_id)"
                )
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_rag_chunks_chat_id "
                    "ON rag_chunks(chat_id)"
                )
                await db.commit()
                # Drop backup outside transaction (non-critical)
                await db.execute("DROP TABLE IF EXISTS rag_chunks_old")
                await db.commit()
            log.info("RAG backfill: atomic swap completed successfully")
        except Exception as e:
            log.error(f"RAG backfill: swap failed, old data preserved: {e}", exc_info=True)
            # Attempt recovery: restore original table if it was renamed
            try:
                async with open_db() as db:
                    # If rag_chunks was renamed to _old but _new wasn't renamed back
                    await db.execute(
                        "ALTER TABLE rag_chunks_old RENAME TO rag_chunks"
                    )
                    await db.commit()
            except Exception:
                pass  # original table may still exist
            try:
                async with open_db() as db:
                    await db.execute("DROP TABLE IF EXISTS rag_chunks_new")
                    await db.commit()
            except Exception:
                pass
            return {"error": f"Swap failed: {e}", "chunks_embedded": embedded}
    else:
        # Nothing embedded — clean up staging table
        async with open_db() as db:
            await db.execute("DROP TABLE IF EXISTS rag_chunks_new")
            await db.commit()
        log.warning("RAG backfill: no chunks embedded, swap skipped")

    elapsed = round(time.time() - t0, 1)
    stats = {
        "total_messages": total_msgs,
        "chunks_created": embedded,
        "chunks_failed": errors,
        "time_sec": elapsed,
    }
    log.info(f"RAG backfill complete: {stats}")
    return stats


# === INCREMENTAL UPDATE ===

async def update_chunks_incremental():
    """Add chunks for new messages since the last chunk, per-chat.

    Called periodically or after new messages arrive.
    Finds the last chunked message ID and builds new chunks per chat.
    """
    if not GEMINI_API_KEY:
        return

    await init_rag_tables()

    # Find the highest end_msg_id in existing chunks
    async with open_db() as db:
        cursor = await db.execute("SELECT MAX(end_msg_id) FROM rag_chunks")
        row = await cursor.fetchone()
        last_chunked = row[0] if row and row[0] else 0

    # We need some overlap for context continuity — go back by CHUNK_SIZE msgs
    overlap_start = max(0, last_chunked - CHUNK_SIZE + 1)
    messages = await _fetch_eligible_messages(after_msg_id=overlap_start)

    if len(messages) < CHUNK_SIZE:
        return  # not enough new messages for a full chunk

    # Build chunks per chat (group by chat_id for isolation)
    all_chunks = []
    for _chat_id, chat_msgs_iter in groupby(messages, key=lambda m: m.get("chat_id", "") or ""):
        chat_msgs = list(chat_msgs_iter)
        chat_chunks = _build_chunks(chat_msgs)
        all_chunks.extend(chat_chunks)

    if not all_chunks:
        return

    # Filter out chunks that overlap with existing ones
    new_chunks = [c for c in all_chunks if c["end_msg_id"] > last_chunked]
    if not new_chunks:
        return

    # Embed and store
    texts = [c["chunk_text"] for c in new_chunks]
    try:
        embeddings = await embed_texts(texts)
    except Exception as e:
        log.error(f"RAG incremental embed failed: {e}")
        return

    now = datetime.now(TZ).isoformat()
    async with open_db() as db:
        for chunk, emb in zip(new_chunks, embeddings):
            if np.allclose(emb, 0):
                continue
            await db.execute(
                "INSERT INTO rag_chunks "
                "(source, chat_id, start_msg_id, end_msg_id, senders, start_ts, end_ts, "
                "chunk_text, embedding, model, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (chunk["source"], chunk.get("chat_id", ""),
                 chunk["start_msg_id"], chunk["end_msg_id"],
                 chunk["senders"], chunk["start_ts"], chunk["end_ts"],
                 chunk["chunk_text"], _vec_to_blob(emb), EMBEDDING_MODEL, now),
            )
        await db.commit()

    log.info(f"RAG incremental: added {len(new_chunks)} new chunks")


# === SEARCH ===

async def rag_search(query: str, top_k: int = 5, access_ctx=None) -> list[dict]:
    """Semantic search over chunk embeddings with per-chat access filtering.

    access_ctx: optional AccessContext from bot.storage.memory. When provided, only chunks
    from allowed chats are searched. When None, all chunks are searched (admin default).

    Returns list of dicts:
        chunk_text, similarity, source, senders, start_ts, end_ts,
        start_msg_id, end_msg_id
    Sorted by similarity descending.
    """
    if not GEMINI_API_KEY:
        return []

    # Embed query
    try:
        embeddings = await embed_texts([query])
        if not embeddings or np.allclose(embeddings[0], 0):
            return []
        query_vec = embeddings[0]
    except Exception as e:
        log.error(f"RAG search embed failed: {e}")
        return []

    # Build access filter SQL clause for chat_id (per-chat isolation)
    access_clause = ""
    access_params: list[str] = []
    if access_ctx and access_ctx.has_filter:
        from bot.storage.memory import _access_sql_filter  # late import to avoid circular
        access_clause, access_params = _access_sql_filter(access_ctx, "chat_id")

    # Paginated search: load embeddings in batches to bound memory usage.
    # At 10K batch (default): ~117 MB per batch. Uses min-heap to track top-K across batches.
    t_search = time.time()
    query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-10)
    heap: list[tuple[float, int, dict]] = []  # (similarity, counter, chunk_data)
    counter = 0  # tiebreaker for heap (dicts aren't comparable)
    total_searched = 0
    MIN_SIMILARITY = 0.25

    async with open_db() as db:
        offset = 0
        while True:
            cursor = await db.execute(
                "SELECT chunk_id, source, chat_id, start_msg_id, end_msg_id, senders, "
                f"start_ts, end_ts, chunk_text, embedding FROM rag_chunks "
                f"WHERE 1=1 {access_clause} "
                f"ORDER BY chunk_id LIMIT ? OFFSET ?",
                access_params + [RAG_SEARCH_BATCH_SIZE, offset],
            )
            rows = await cursor.fetchall()
            if not rows:
                break

            # Build matrix for this batch
            batch_data = []
            emb_list = []
            for row in rows:
                try:
                    vec = _blob_to_vec(row["embedding"])
                    if len(vec) == EMBEDDING_DIM:
                        emb_list.append(vec)
                        batch_data.append(dict(row))
                except Exception:
                    continue

            if emb_list:
                matrix = np.stack(emb_list)
                norms = np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-10
                similarities = (matrix / norms) @ query_norm

                # Merge into top-K heap
                for i, sim in enumerate(similarities):
                    sim_f = float(sim)
                    if sim_f < MIN_SIMILARITY:
                        continue
                    counter += 1
                    if len(heap) < top_k:
                        heapq.heappush(heap, (sim_f, counter, batch_data[i]))
                    elif sim_f > heap[0][0]:
                        heapq.heapreplace(heap, (sim_f, counter, batch_data[i]))

                total_searched += len(emb_list)

            offset += RAG_SEARCH_BATCH_SIZE
            if len(rows) < RAG_SEARCH_BATCH_SIZE:
                break  # last batch

    elapsed = time.time() - t_search
    log.info(f"RAG search: {total_searched} chunks in {elapsed:.2f}s, "
             f"top_k={top_k}, batches={offset // RAG_SEARCH_BATCH_SIZE + 1}"
             f"{', filtered by access_ctx' if access_clause else ''}")

    # Sort results by similarity descending
    results = []
    for sim_f, _, cd in sorted(heap, key=lambda x: -x[0]):
        results.append({
            "chunk_id": cd["chunk_id"],
            "chunk_text": cd["chunk_text"],
            "similarity": round(sim_f, 4),
            "source": cd["source"],
            "senders": cd["senders"],
            "start_ts": cd["start_ts"],
            "end_ts": cd["end_ts"],
            "start_msg_id": cd["start_msg_id"],
            "end_msg_id": cd["end_msg_id"],
        })

    return results


# === STATS ===

async def get_rag_stats() -> dict:
    """Get RAG chunk coverage stats, including per-chat breakdown."""
    async with open_db() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM messages")
        total_messages = (await cursor.fetchone())[0]

        try:
            cursor = await db.execute("SELECT COUNT(*) FROM rag_chunks")
            total_chunks = (await cursor.fetchone())[0]
            cursor = await db.execute("SELECT MIN(start_msg_id), MAX(end_msg_id) FROM rag_chunks")
            row = await cursor.fetchone()
            min_id, max_id = row if row else (None, None)
            # Per-chat breakdown
            cursor = await db.execute(
                "SELECT chat_id, COUNT(*) as cnt FROM rag_chunks GROUP BY chat_id ORDER BY cnt DESC"
            )
            per_chat = {r["chat_id"] or "(legacy)": r["cnt"] for r in await cursor.fetchall()}
        except Exception:
            total_chunks = 0
            min_id, max_id = None, None
            per_chat = {}

    return {
        "total_messages": total_messages,
        "total_chunks": total_chunks,
        "msg_range": f"{min_id}-{max_id}" if min_id else "none",
        "per_chat": per_chat,
    }
