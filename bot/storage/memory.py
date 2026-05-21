# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Memory system -- messages, facts, media cache.

Provides searchable persistent storage for ALL organization data:
- Messages: every user/assistant message (FTS5 indexed)
- Facts: structured key-value facts in message_facts table (FTS5 indexed, RAG-adjacent)
- Media: cached media files with metadata (FTS5 indexed)

Memory architecture: RAG (semantic search) + Facts (structured k/v) + Messages (FTS5).
Search is done via the memory_search MCP tool which calls search_messages + search_message_facts + rag_search in parallel.
Context continuity is handled by the SDK's native session management and compaction.

Data access isolation:
- AccessContext controls what data each session can see
- Admin DM: full access (all data, no filter)
- Primary (group + DMs): own chat + all primary group chats
- Discovered chat: own chat only (even for admin users)

Fact and media_cache operations are implemented in bot.storage.facts and bot.storage.media_cache
respectively, and re-exported here for backwards compatibility.
"""

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from bot.core.access import (
    AccessContext,
    build_access_context,  # noqa: F401 — re-exported for backwards compat (used by bot.agent)
    _access_sql_filter,
)
from bot.storage.db import open_db
from config import (
    DB_PATH,
    ORG_TIMEZONE,
)

# Re-export all fact operations (backwards compat)
from bot.storage.facts import (  # noqa: F401
    _guess_fact_category,
    derive_subject_from_key,
    _backfill_fact_subjects,
    get_facts_for_prompt,
    save_message_fact,
    expire_message_fact,
    bulk_expire_message_facts,
    get_fact_document,
    search_message_facts,
    get_all_message_facts,
    seed_core_facts,
)

# Re-export all media_cache operations (backwards compat)
from bot.storage.media_cache import (  # noqa: F401
    cache_media_file,
    ensure_media_file,
    link_media_to_message,
    _guess_extension,
    search_media_cache,
    list_cached_media,
    cleanup_expired_media,
    get_media_cache_stats,
    update_media_description,
    get_undescribed_media,
)

log = logging.getLogger(__name__)
TZ = ZoneInfo(ORG_TIMEZONE)


# Re-export for backwards compatibility (external code may do
# ``from bot.storage.memory import AccessContext`` etc.)
# No __all__ — bot.storage.memory re-exports public names from facts + media_cache


# === CONVERSATION DATABASE (SQLite + FTS5) ===

async def init_db() -> None:
    """Initialize the conversation database with FTS5 support for all memory types."""
    async with open_db() as db:
        await db.executescript("""
            -- Messages table (core conversation log)
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,           -- 'telegram' or 'teams'
                user_name TEXT NOT NULL,
                user_id TEXT,
                text TEXT NOT NULL,
                role TEXT NOT NULL,              -- 'user' or 'assistant'
                timestamp TEXT NOT NULL,
                message_id TEXT,                 -- platform-specific message ID
                session_id TEXT,                 -- conversation session ID
                msg_type TEXT DEFAULT 'user_message',  -- user_message, bot_reply, status, placeholder, system
                media_cache_id INTEGER           -- FK to media_cache.id (links message to its media)
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                text,
                user_name,
                content=messages,
                content_rowid=id
            );

            CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, text, user_name)
                VALUES (new.id, new.text, new.user_name);
            END;

            -- Media cache index (searchable media file metadata)
            CREATE TABLE IF NOT EXISTS media_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,          -- stored filename in cache dir
                original_filename TEXT,          -- original name from sender
                media_type TEXT NOT NULL,        -- photo, document, voice, video, etc.
                source TEXT NOT NULL,            -- telegram or teams
                chat_id TEXT,                    -- chat/group where media was sent
                sender_name TEXT,                -- who sent it
                description TEXT DEFAULT '',     -- auto-generated or caption text
                file_size INTEGER DEFAULT 0,
                mime_type TEXT DEFAULT '',
                cached_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,        -- retention-based expiry
                file_path TEXT NOT NULL,         -- full path in media_cache dir
                tg_file_id TEXT                  -- Telegram file_id for re-fetching evicted files
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS media_cache_fts USING fts5(
                filename,
                original_filename,
                description,
                sender_name,
                media_type,
                content=media_cache,
                content_rowid=id
            );

            CREATE TRIGGER IF NOT EXISTS media_cache_ai AFTER INSERT ON media_cache BEGIN
                INSERT INTO media_cache_fts(rowid, filename, original_filename, description, sender_name, media_type)
                VALUES (new.id, new.filename, new.original_filename, new.description, new.sender_name, new.media_type);
            END;

            CREATE TRIGGER IF NOT EXISTS media_cache_au AFTER UPDATE ON media_cache BEGIN
                INSERT INTO media_cache_fts(media_cache_fts, rowid, filename, original_filename, description, sender_name, media_type)
                VALUES ('delete', old.id, old.filename, old.original_filename, old.description, old.sender_name, old.media_type);
                INSERT INTO media_cache_fts(rowid, filename, original_filename, description, sender_name, media_type)
                VALUES (new.id, new.filename, new.original_filename, new.description, new.sender_name, new.media_type);
            END;

            -- Structured key-value facts linked to messages
            CREATE TABLE IF NOT EXISTS message_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fact_key TEXT NOT NULL,
                fact_value TEXT NOT NULL,
                category TEXT DEFAULT 'general',
                source_msg_id INTEGER,              -- message ID that created this fact
                source_chunk_id INTEGER,             -- nearest RAG chunk ID
                doc_pointer TEXT,                     -- file path for linked documents (passport scans, etc.)
                superseded_by INTEGER,                -- ID of newer fact that replaced this one
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expired INTEGER DEFAULT 0            -- soft delete: 1 = superseded/obsolete
            );

            CREATE INDEX IF NOT EXISTS idx_message_facts_key
                ON message_facts(fact_key);
            CREATE INDEX IF NOT EXISTS idx_message_facts_category
                ON message_facts(category);
            CREATE INDEX IF NOT EXISTS idx_message_facts_source
                ON message_facts(source_msg_id);

            CREATE VIRTUAL TABLE IF NOT EXISTS message_facts_fts USING fts5(
                fact_key,
                fact_value,
                category,
                content=message_facts,
                content_rowid=id
            );

            CREATE TRIGGER IF NOT EXISTS message_facts_ai AFTER INSERT ON message_facts BEGIN
                INSERT INTO message_facts_fts(rowid, fact_key, fact_value, category)
                VALUES (new.id, new.fact_key, new.fact_value, new.category);
            END;

            CREATE TRIGGER IF NOT EXISTS message_facts_au AFTER UPDATE ON message_facts BEGIN
                INSERT INTO message_facts_fts(message_facts_fts, rowid, fact_key, fact_value, category)
                VALUES ('delete', old.id, old.fact_key, old.fact_value, old.category);
                INSERT INTO message_facts_fts(rowid, fact_key, fact_value, category)
                VALUES (new.id, new.fact_key, new.fact_value, new.category);
            END;
        """)
        await db.commit()
    log.info(f"DB initialized (messages, message_facts, media_cache): {DB_PATH}")

    # Run lightweight migrations for existing databases (columns added after initial schema).
    # New databases get these columns from CREATE TABLE above; this handles upgrades.
    async with open_db() as db:
        for table, col, col_def in [
            ("messages", "msg_type", "TEXT DEFAULT 'user_message'"),
            ("messages", "media_cache_id", "INTEGER"),
            ("messages", "chat_id", "TEXT DEFAULT ''"),
            ("messages", "message_thread_id", "TEXT DEFAULT ''"),
            ("media_cache", "tg_file_id", "TEXT"),
            ("message_facts", "doc_pointer", "TEXT"),
            ("message_facts", "superseded_by", "INTEGER"),
            ("message_facts", "subject", "TEXT DEFAULT ''"),
            ("message_facts", "source_chat_id", "TEXT DEFAULT ''"),
            ("message_facts", "domain_id", "TEXT DEFAULT ''"),
            ("message_facts", "access_count", "INTEGER DEFAULT 0"),
            ("message_facts", "last_accessed_at", "TEXT DEFAULT ''"),
        ]:
            cursor = await db.execute(f"PRAGMA table_info({table})")
            columns = {row[1] for row in await cursor.fetchall()}
            if col not in columns:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
                log.info(f"Migration: added {col} to {table}")
        await db.commit()

    # Create indexes for data access isolation columns (idempotent)
    async with open_db() as db:
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id)"
        )
        # M14 fix: composite index for link_media_to_message lookups
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_source_msgid ON messages(source, message_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_message_facts_source_chat_id "
            "ON message_facts(source_chat_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_message_facts_domain_id "
            "ON message_facts(domain_id)"
        )
        await db.commit()

    # Ensure media_cache FTS5 UPDATE trigger exists (added after initial schema)
    async with open_db() as db:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name='media_cache_au'"
        )
        if not await cursor.fetchone():
            await db.execute("""
                CREATE TRIGGER IF NOT EXISTS media_cache_au AFTER UPDATE ON media_cache BEGIN
                    INSERT INTO media_cache_fts(media_cache_fts, rowid, filename, original_filename, description, sender_name, media_type)
                    VALUES ('delete', old.id, old.filename, old.original_filename, old.description, old.sender_name, old.media_type);
                    INSERT INTO media_cache_fts(rowid, filename, original_filename, description, sender_name, media_type)
                    VALUES (new.id, new.filename, new.original_filename, new.description, new.sender_name, new.media_type);
                END;
            """)
            await db.commit()
            log.info("Migration: added media_cache_au FTS5 update trigger")

    # Migrate message_facts FTS5 to include subject column (graph triples)
    async with open_db() as db:
        # Check if FTS5 table already has 'subject' column
        cursor = await db.execute(
            "SELECT sql FROM sqlite_master WHERE name='message_facts_fts' AND type='table'"
        )
        row = await cursor.fetchone()
        needs_fts_rebuild = row and row[0] and "subject" not in row[0]
        if needs_fts_rebuild:
            log.info("Migration: rebuilding message_facts_fts to include subject column")
            await db.executescript("""
                DROP TRIGGER IF EXISTS message_facts_ai;
                DROP TRIGGER IF EXISTS message_facts_au;
                DROP TABLE IF EXISTS message_facts_fts;

                CREATE VIRTUAL TABLE message_facts_fts USING fts5(
                    subject, fact_key, fact_value, category,
                    content=message_facts, content_rowid=id
                );

                CREATE TRIGGER message_facts_ai AFTER INSERT ON message_facts BEGIN
                    INSERT INTO message_facts_fts(rowid, subject, fact_key, fact_value, category)
                    VALUES (new.id, new.subject, new.fact_key, new.fact_value, new.category);
                END;

                CREATE TRIGGER message_facts_au AFTER UPDATE ON message_facts BEGIN
                    INSERT INTO message_facts_fts(message_facts_fts, rowid, subject, fact_key, fact_value, category)
                    VALUES ('delete', old.id, old.subject, old.fact_key, old.fact_value, old.category);
                    INSERT INTO message_facts_fts(rowid, subject, fact_key, fact_value, category)
                    VALUES (new.id, new.subject, new.fact_key, new.fact_value, new.category);
                END;
            """)
            # Rebuild FTS index from existing data
            await db.execute(
                "INSERT INTO message_facts_fts(message_facts_fts) VALUES ('rebuild')"
            )
            await db.commit()
            log.info("Migration: message_facts_fts rebuilt with subject column")

    # Backfill subjects for existing facts that have empty subject
    await _backfill_fact_subjects()

    # Initialize usage_log table (cost + token tracking)
    async with open_db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_key TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                model TEXT,
                cost_usd REAL,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                cache_create_tokens INTEGER DEFAULT 0,
                num_turns INTEGER DEFAULT 0,
                duration_s REAL DEFAULT 0,
                query_preview TEXT
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_log_ts ON usage_log(timestamp)"
        )
        # Migration: add oauth_profile column (safe no-op if already exists)
        try:
            await db.execute("ALTER TABLE usage_log ADD COLUMN oauth_profile TEXT DEFAULT 'default'")
            log.info("Migration: added oauth_profile column to usage_log")
        except Exception:
            pass  # Column already exists
        await db.commit()

    # Initialize RAG chunks table
    try:
        from bot.storage.rag import init_rag_tables
        await init_rag_tables()
    except Exception as e:
        log.warning(f"RAG init skipped: {e}")


async def store_usage(
    session_key: str,
    model: str | None = None,
    cost_usd: float | None = None,
    usage: dict | None = None,
    num_turns: int | None = None,
    duration_s: float = 0,
    query_preview: str = "",
    oauth_profile: str = "default",
) -> None:
    """Persist a query's usage metrics to usage_log."""
    from datetime import datetime, timezone
    async with open_db() as db:
        await db.execute(
            "INSERT INTO usage_log "
            "(session_key, timestamp, model, cost_usd, input_tokens, output_tokens, "
            "cache_read_tokens, cache_create_tokens, num_turns, duration_s, query_preview, "
            "oauth_profile) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_key,
                datetime.now(timezone.utc).isoformat(),
                model,
                cost_usd,
                (usage or {}).get("input_tokens", 0),
                (usage or {}).get("output_tokens", 0),
                (usage or {}).get("cache_read_input_tokens", 0),
                (usage or {}).get("cache_creation_input_tokens", 0),
                num_turns or 0,
                duration_s,
                (query_preview or "")[:100],
                oauth_profile or "default",
            ),
        )
        await db.commit()


async def get_usage_summary(hours: int = 24) -> dict:
    """Return aggregated usage stats for the last N hours."""
    hours = max(1, min(int(hours), 8760))  # clamp to 1h-365d
    async with open_db() as db:
        hours_param = f"-{hours}"
        cursor = await db.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0), "
            "COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0), "
            "COALESCE(SUM(cache_read_tokens), 0), COALESCE(SUM(cache_create_tokens), 0), "
            "COALESCE(AVG(cost_usd), 0), COALESCE(AVG(num_turns), 0) "
            "FROM usage_log WHERE timestamp > datetime('now', ? || ' hours')",
            (hours_param,),
        )
        row = await cursor.fetchone()
        # Model breakdown
        cursor2 = await db.execute(
            "SELECT model, COUNT(*), COALESCE(SUM(cost_usd), 0) "
            "FROM usage_log WHERE timestamp > datetime('now', ? || ' hours') "
            "GROUP BY model ORDER BY SUM(cost_usd) DESC",
            (hours_param,),
        )
        models = [{"model": r[0], "queries": r[1], "cost_usd": round(r[2], 4)}
                  for r in await cursor2.fetchall()]
    return {
        "hours": hours,
        "queries": row[0],
        "total_cost_usd": round(row[1], 4),
        "total_input_tokens": row[2],
        "total_output_tokens": row[3],
        "total_cache_read": row[4],
        "total_cache_create": row[5],
        "avg_cost_per_query": round(row[6], 4),
        "avg_turns_per_query": round(row[7], 1),
        "by_model": models,
    }


async def get_top_queries(n: int = 10) -> list[dict]:
    """Return the N most expensive queries from usage_log."""
    async with open_db() as db:
        cursor = await db.execute(
            "SELECT session_key, timestamp, model, cost_usd, num_turns, "
            "duration_s, input_tokens, output_tokens, query_preview "
            "FROM usage_log ORDER BY cost_usd DESC LIMIT ?",
            (n,),
        )
        return [
            {
                "session_key": r[0], "timestamp": r[1], "model": r[2],
                "cost_usd": r[3], "num_turns": r[4], "duration_s": round(r[5], 1),
                "input_tokens": r[6], "output_tokens": r[7], "preview": r[8],
            }
            for r in await cursor.fetchall()
        ]


async def get_usage_by_profile(hours: int = 24,
                               session_key_prefix: str = "") -> list[dict]:
    """Return usage stats grouped by OAuth profile for the last N hours.

    session_key_prefix: if set, only count rows whose session_key starts with
    this prefix (e.g. 'telegram:-100123' for chat-scoped view).
    Empty string = no filter (global view, admin DMs only).
    """
    hours = max(1, min(int(hours), 8760))
    async with open_db() as db:
        hours_param = f"-{hours}"
        if session_key_prefix:
            cursor = await db.execute(
                "SELECT COALESCE(oauth_profile, 'default'), COUNT(*), "
                "COALESCE(SUM(cost_usd), 0), "
                "COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0) "
                "FROM usage_log WHERE timestamp > datetime('now', ? || ' hours') "
                "AND (session_key = ? OR session_key LIKE ? || ':%') "
                "GROUP BY COALESCE(oauth_profile, 'default') ORDER BY SUM(cost_usd) DESC",
                (hours_param, session_key_prefix, session_key_prefix),
            )
        else:
            cursor = await db.execute(
                "SELECT COALESCE(oauth_profile, 'default'), COUNT(*), "
                "COALESCE(SUM(cost_usd), 0), "
                "COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0) "
                "FROM usage_log WHERE timestamp > datetime('now', ? || ' hours') "
                "GROUP BY COALESCE(oauth_profile, 'default') ORDER BY SUM(cost_usd) DESC",
                (hours_param,),
            )
        return [
            {"profile": r[0], "queries": r[1], "cost_usd": round(r[2], 4),
             "input_tokens": r[3], "output_tokens": r[4]}
            for r in await cursor.fetchall()
        ]


# === MESSAGE STORAGE & SEARCH ===

async def store_message(source: str, user_name: str, text: str, role: str,
                        user_id: str = "", message_id: str = "", session_id: str = "",
                        msg_type: str = "", chat_id: str = "",
                        message_thread_id: str = "") -> int:
    """Store a message in the conversation database.

    msg_type values: user_message, bot_reply, status, placeholder, system.
    If not provided, defaults based on role (user->user_message, assistant->bot_reply).
    chat_id: the chat/group this message belongs to (for data access isolation).
    message_thread_id: forum topic thread ID (for per-topic message filtering).
    """
    if not msg_type:
        msg_type = "bot_reply" if role == "assistant" else "user_message"
    async with open_db() as db:
        cursor = await db.execute(
            "INSERT INTO messages (source, user_name, user_id, text, role, timestamp, "
            "message_id, session_id, msg_type, chat_id, message_thread_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source, user_name, user_id, text, role,
             datetime.now(TZ).isoformat(), message_id, session_id, msg_type,
             chat_id or "", str(message_thread_id) if message_thread_id else "")
        )
        db_id = cursor.lastrowid
        await db.commit()
    return db_id


async def get_recent_messages(limit: int = 20,
                              access_ctx: AccessContext | None = None,
                              chat_id: str = "") -> list[dict]:
    """Get the most recent messages for conversation context.

    If chat_id is provided, filters to that single chat (ignoring access_ctx).
    Otherwise filters by AccessContext for data isolation.
    """
    if chat_id:
        # Single-chat mode: only messages from this specific chat
        where = "WHERE chat_id = ?"
        params: list = [str(chat_id)]
    else:
        access_clause, access_params = _access_sql_filter(access_ctx, "chat_id")
        params = list(access_params)
        # When there's a filter, we need WHERE; access_clause starts with AND
        if access_clause:
            where = "WHERE " + access_clause.lstrip("AND ")
        else:
            where = ""
    async with open_db() as db:

        cursor = await db.execute(
            f"SELECT source, user_name, text, role, timestamp, chat_id, id"
            f" FROM messages "
            f"{where} ORDER BY id DESC LIMIT ?",
            params + [limit],
        )
        rows = await cursor.fetchall()
    # Reverse to chronological order
    return [dict(r) for r in reversed(rows)]


def _sanitize_fts_query(query: str) -> str:
    """Escape FTS5 special characters to prevent query syntax errors.

    Wraps each word in double quotes for literal matching.
    """
    # Remove FTS5 operators and special chars, keep alphanumeric + spaces
    words = re.findall(r'[\w]+', query)
    if not words:
        return '""'
    # Quote each word for exact matching (avoids AND/OR/NOT interpretation)
    return " ".join(f'"{w}"' for w in words[:10])  # limit to 10 words


async def search_messages(query: str, limit: int = 10,
                          access_ctx: AccessContext | None = None) -> list[dict]:
    """Search past messages using FTS5 (BM25 ranking).

    Filters by AccessContext for data isolation.
    """
    safe_query = _sanitize_fts_query(query)
    if safe_query == '""':
        return []
    access_clause, access_params = _access_sql_filter(access_ctx, "m.chat_id")
    async with open_db() as db:

        cursor = await db.execute(
            "SELECT m.id, m.source, m.user_name, m.text, m.role, m.timestamp, "
            "rank FROM messages_fts f "
            "JOIN messages m ON f.rowid = m.id "
            f"WHERE messages_fts MATCH ? {access_clause} "
            "ORDER BY rank LIMIT ?",
            [safe_query] + access_params + [limit],
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_messages_around(message_id: int, before: int = 5, after: int = 5,
                              access_ctx: AccessContext | None = None) -> list[dict]:
    """Get messages surrounding a specific message ID for full context.

    Filters by AccessContext for data isolation.
    """
    access_clause, access_params = _access_sql_filter(access_ctx, "chat_id")
    async with open_db() as db:

        cursor = await db.execute(
            "SELECT id, source, user_name, text, role, timestamp FROM messages "
            f"WHERE id BETWEEN ? AND ? {access_clause} ORDER BY id",
            [message_id - before, message_id + after] + access_params,
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_messages_by_range(
    start_id: int | None = None,
    end_id: int | None = None,
    msg_ids: list[int] | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    access_ctx: AccessContext | None = None,
) -> dict:
    """Get full messages + linked facts by ID range, specific IDs, or time range.

    Exactly one query mode should be used:
    - start_id + end_id: contiguous ID range
    - msg_ids: list of specific message IDs
    - from_date + to_date: ISO date/datetime range (e.g. '2026-03-01', '2026-03-07T18:00')

    Returns messages with their full text and all linked facts from message_facts.
    Filters by AccessContext for data isolation.
    """
    msg_access_clause, msg_access_params = _access_sql_filter(access_ctx, "chat_id")
    fact_access_clause, fact_access_params = _access_sql_filter(access_ctx, "source_chat_id")
    async with open_db() as db:


        if msg_ids:
            # Specific message IDs
            placeholders = ",".join("?" for _ in msg_ids)
            cursor = await db.execute(
                f"SELECT id, source, user_name, text, role, timestamp FROM messages "
                f"WHERE id IN ({placeholders}) {msg_access_clause} ORDER BY id",
                list(msg_ids) + msg_access_params
            )
            msg_rows = await cursor.fetchall()
            messages = [dict(r) for r in msg_rows]

            cursor = await db.execute(
                f"SELECT id, fact_key, fact_value, category, source_msg_id, doc_pointer, "
                f"superseded_by, expired, created_at FROM message_facts "
                f"WHERE source_msg_id IN ({placeholders}) {fact_access_clause} ORDER BY source_msg_id, id",
                list(msg_ids) + fact_access_params
            )
        elif from_date and to_date:
            # Time range
            cursor = await db.execute(
                f"SELECT id, source, user_name, text, role, timestamp FROM messages "
                f"WHERE timestamp >= ? AND timestamp <= ? {msg_access_clause} ORDER BY id",
                [from_date, to_date] + msg_access_params
            )
            msg_rows = await cursor.fetchall()
            messages = [dict(r) for r in msg_rows]

            # Get IDs for fact lookup
            found_ids = [m["id"] for m in messages]
            if found_ids:
                placeholders = ",".join("?" for _ in found_ids)
                cursor = await db.execute(
                    f"SELECT id, fact_key, fact_value, category, source_msg_id, doc_pointer, "
                    f"superseded_by, expired, created_at FROM message_facts "
                    f"WHERE source_msg_id IN ({placeholders}) {fact_access_clause} ORDER BY source_msg_id, id",
                    found_ids + fact_access_params
                )
            else:
                return {"messages": [], "facts": []}
        else:
            # ID range (original behavior)
            cursor = await db.execute(
                f"SELECT id, source, user_name, text, role, timestamp FROM messages "
                f"WHERE id BETWEEN ? AND ? {msg_access_clause} ORDER BY id",
                [start_id, end_id] + msg_access_params
            )
            msg_rows = await cursor.fetchall()
            messages = [dict(r) for r in msg_rows]

            cursor = await db.execute(
                f"SELECT id, fact_key, fact_value, category, source_msg_id, doc_pointer, "
                f"superseded_by, expired, created_at FROM message_facts "
                f"WHERE source_msg_id BETWEEN ? AND ? {fact_access_clause} ORDER BY source_msg_id, id",
                [start_id, end_id] + fact_access_params
            )

        fact_rows = await cursor.fetchall()
        facts = [dict(r) for r in fact_rows]

    return {"messages": messages, "facts": facts}




def format_recent_context(messages: list[dict], origin_chat_id: str = "") -> str:
    """Format recent messages as readable conversation context.

    Last CONTEXT_FULL_TAIL messages are shown in full (untruncated).
    Earlier messages are truncated to CONTEXT_MSG_TRUNCATE chars.
    When messages span multiple chats, a [chat:ID] label is added.
    """
    from config import CONTEXT_MSG_TRUNCATE, CONTEXT_FULL_TAIL
    if not messages:
        return ""
    lines = []
    total = len(messages)
    full_start = max(0, total - CONTEXT_FULL_TAIL)
    truncated_count = 0
    first_id = messages[0].get("id", "?")
    last_id = messages[-1].get("id", "?")
    # Detect if messages span multiple chats — add label if so
    chat_ids = {str(m.get("chat_id", "")) for m in messages if m.get("chat_id")}
    multi_chat = len(chat_ids) > 1
    for i, msg in enumerate(messages):
        prefix = "\U0001f464" if msg["role"] == "user" else "\U0001f916"
        src = "[TG]" if msg["source"] == "telegram" else "[WA]"
        ts = msg.get("timestamp", "")[:16].split("T")[-1] if msg.get("timestamp") else "?"
        # Add chat label when messages span multiple chats
        chat_label = ""
        if multi_chat:
            msg_chat = str(msg.get("chat_id", ""))
            if msg_chat and origin_chat_id and msg_chat == origin_chat_id:
                chat_label = " [this chat]"
            elif msg_chat:
                chat_label = f" [chat:{msg_chat}]"
        text = msg["text"]
        if i < full_start and len(text) > CONTEXT_MSG_TRUNCATE:
            # Truncate at last newline before limit to avoid breaking mid-line/mid-code-block
            cut = text[:CONTEXT_MSG_TRUNCATE].rfind("\n")
            if cut < CONTEXT_MSG_TRUNCATE // 2:
                cut = CONTEXT_MSG_TRUNCATE  # no good newline found, hard cut
            text = text[:cut] + "\n... [truncated]"
            truncated_count += 1
        lines.append(f"[{ts}] {src}{chat_label} {prefix} {msg['user_name']}: {text}")
    result = "\n".join(lines)
    if truncated_count:
        result += (
            f"\n\n--- {truncated_count} message(s) truncated. "
            f"Use get_messages_by_range(start_id={first_id}, end_id={last_id}) "
            f"for full text of any message. ---"
        )
    return result
