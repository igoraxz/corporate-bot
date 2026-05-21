# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Fact/knowledge-graph operations — CRUD, search, formatting.

Extracted from the original bot/memory.py (May 2026 reorg). All functions are re-exported from bot.storage.memory
for backwards compatibility.

Graph triples: (subject, key=predicate, value=object).
FTS5 indexed for full-text search. Versioned with superseded_by chains.
"""

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from bot.core.access import (
    AccessContext,
    NO_ACCESS,
    _access_sql_filter,
)
from bot.storage.db import open_db
from config import ORG_TIMEZONE, ORG_MEMBER_NAMES

log = logging.getLogger(__name__)
TZ = ZoneInfo(ORG_TIMEZONE)


# --- Helpers ---

def _guess_fact_category(key: str) -> str:
    """Heuristically guess a category from a fact key name.

    Soft fallback only -- used when category is not explicitly provided.
    Any category string is valid; this just provides reasonable defaults.
    """
    key_lower = key.lower()
    if any(w in key_lower for w in ("passport", "licence", "license", "birth_cert", "certificate", "document", "nhs_number", "visa_doc")):
        return "documents"
    if any(w in key_lower for w in ("password", "api_key", "token", "credential", "secret")):
        return "credentials"
    if any(w in key_lower for w in ("phone", "email", "contact", "address_of", "whatsapp_of", "telegram_of")):
        return "contact"
    if any(w in key_lower for w in ("training", "course", "certification", "workshop", "class", "exam")):
        return "training"
    if any(w in key_lower for w in ("trip", "flight", "hotel", "travel", "booking", "airport", "itinerary")):
        return "travel"
    if any(w in key_lower for w in ("car_", "vehicle", "mot_", "garage", "parking")):
        return "vehicle"
    if any(w in key_lower for w in ("doctor", "dentist", "hospital", "health", "medical", "allergy")):
        return "health"
    if any(w in key_lower for w in ("payment", "invoice", "mortgage", "tax_", "bank", "budget", "insurance")):
        return "finance"
    if any(w in key_lower for w in ("bot_", "deploy", "config", "mcp", "prompt_")):
        return "bot"
    if any(w in key_lower for w in ("office", "workspace", "infrastructure", "network", "hardware")):
        return "infrastructure"
    if any(w in key_lower for w in ("team", "member", "contact", "department", "onboarding")):
        return "people"
    if any(w in key_lower for w in ("meeting", "standup", "sprint", "review", "demo", "activity")):
        return "activities"
    return "general"


def derive_subject_from_key(key: str) -> str:
    """Extract subject entity from a fact key prefix using organization member names.

    Examples: 'john_uk_passport' -> 'john', 'alice_school' -> 'alice',
    'hotel_search_criteria' -> '' (no entity prefix, returns empty).
    ORG_MEMBER_NAMES is sorted longest-first for greedy matching.
    """
    key_lower = key.lower()
    for name in ORG_MEMBER_NAMES:
        # Match "name_" prefix (e.g. "john_", "grandma_elena_")
        prefix = name + "_"
        if key_lower.startswith(prefix):
            return name
    return ""


async def _backfill_fact_subjects():
    """One-time migration: derive subjects for existing facts with empty subject."""
    async with open_db() as db:
        cursor = await db.execute(
            "SELECT id, fact_key FROM message_facts WHERE (subject IS NULL OR subject = '') AND expired = 0"
        )
        rows = await cursor.fetchall()
        if not rows:
            return
        count = 0
        for row in rows:
            subject = derive_subject_from_key(row[1])
            if subject:
                await db.execute(
                    "UPDATE message_facts SET subject = ? WHERE id = ?",
                    (subject, row[0]),
                )
                count += 1
        await db.commit()
        if count:
            log.info(f"Backfilled subjects for {count}/{len(rows)} facts")


def _sanitize_fts_query(query: str) -> str:
    """Escape FTS5 special characters to prevent query syntax errors.

    Wraps each word in double quotes for literal matching.
    """
    words = re.findall(r'[\w]+', query)
    if not words:
        return '""'
    return " ".join(f'"{w}"' for w in words[:10])


# --- Facts for system prompt ---

async def get_facts_for_prompt(
    access_ctx: AccessContext | None = None,
    categories: list[str] | None = None,
    max_facts: int = 500,
) -> str:
    """Return active facts formatted for system prompt injection.

    Groups facts by subject, then by category within each subject.
    Filters by AccessContext: admin sees all, team sees own+primary groups+legacy,
    external sees own chat only.
    If categories is provided, only facts in those categories are included.
    Domain-scoped facts are included when allowed_domains is set on AccessContext.
    max_facts: maximum pinned facts to inject (default 500 — generous limit).
    Sorted by access_count DESC, updated_at DESC for relevance ranking (CB-98).
    """
    if access_ctx is None:
        access_ctx = NO_ACCESS

    access_clause, access_params = _access_sql_filter(access_ctx, "source_chat_id")

    # Domain-scoped fact visibility:
    # - Chat-local facts (domain_id='') → filtered by access_clause (chat isolation)
    # - Domain facts (domain_id set) → visible if domain_id in allowed_domains,
    #   regardless of which chat created them (cross-chat domain sharing)
    # This requires OR-logic: (chat-local AND chat-filter) OR (domain-match)
    combined_clause = ""
    combined_params: list[str] = []
    if access_ctx.allowed_domains and access_ctx.has_filter:
        # Build: AND ( (domain_id IN (...allowed...)) OR ((domain_id='' OR domain_id IS NULL) AND chat_filter) )
        d_placeholders = ",".join("?" for _ in access_ctx.allowed_domains)
        chat_filter_sql, chat_filter_params = access_clause, list(access_params)
        # Strip leading " AND " from access_clause for embedding in sub-expression
        inner_chat = chat_filter_sql.lstrip(" AND") if chat_filter_sql else "1=1"
        combined_clause = (
            f" AND (domain_id IN ({d_placeholders})"
            f" OR ((domain_id = '' OR domain_id IS NULL) AND ({inner_chat})))"
        )
        combined_params = list(access_ctx.allowed_domains) + chat_filter_params
    elif access_ctx.is_admin and not access_ctx.allowed_chat_ids:
        # Admin DM (global scope): no filter needed (sees everything)
        combined_clause = ""
        combined_params = []
    else:
        # No domain access: only chat-local facts with chat filter
        combined_clause = f"{access_clause} AND (domain_id = '' OR domain_id IS NULL)"
        combined_params = list(access_params)

    # Optional category filter from harness
    cat_clause = ""
    all_params = combined_params
    if categories:
        placeholders = ", ".join("?" for _ in categories)
        cat_clause = f" AND category IN ({placeholders})"
        all_params = combined_params + categories
    async with open_db() as db:
        cursor = await db.execute(
            "SELECT fact_key, fact_value, category, subject, doc_pointer, id "
            f"FROM message_facts WHERE expired = 0 {combined_clause}{cat_clause} "
            "ORDER BY COALESCE(access_count, 0) DESC, updated_at DESC "
            f"LIMIT {int(max_facts)}",
            all_params,
        )
        rows = await cursor.fetchall()

        # CB-98: access_count tracks EXPLICIT searches (memory_search, search_facts),
        # NOT passive prompt injection. Moved to search functions.
    if not rows:
        return "No facts stored yet."

    lines = []
    current_subject = None
    for row in rows:
        key, value, _category, subject, doc_ptr = row[0], row[1], row[2], row[3] or "", row[4]
        # Strip subject prefix from key for cleaner display (case-insensitive)
        key_lower = key.lower()
        display_key = key
        if subject and key_lower.startswith(subject + "_"):
            display_key = key_lower[len(subject) + 1:]

        if subject != current_subject:
            current_subject = subject
            label = subject if subject else "general"
            lines.append(f"\n[{label}]")

        # Truncate long values for prompt compactness
        display_val = value[:200] + "..." if len(value) > 200 else value
        doc = " [doc]" if doc_ptr else ""
        lines.append(f"  {display_key}: {display_val}{doc}")

    total = len(rows)
    _more_note = f" (showing top {max_facts} by relevance — use memory_search for more)" if total >= max_facts else ""
    result = f"Total: {total} facts{_more_note}\n" + "\n".join(lines)
    log.debug(f"Facts for prompt: {total} facts, {len(result)} chars")
    return result


# --- Fact CRUD ---

async def save_message_fact(key: str, value: str, source_msg_id: int | None = None,
                            category: str = "", doc_pointer: str = "",
                            subject: str = "", source_chat_id: str = "",
                            domain_id: str = "") -> int | None:
    """Save a fact (graph triple) linked to its source message. Upserts by key.

    Graph triple: (subject, key=predicate, value=object).
    If subject is not provided, it's auto-derived from the key prefix.
    source_chat_id: chat that created this fact (for data access isolation).
    domain_id: knowledge domain this fact belongs to (model-driven routing).
    If key already exists, the old fact is marked expired with superseded_by
    pointing to the new fact (version chaining). Returns the fact ID, or None on failure.
    """
    if not key or not value:
        log.warning("save_message_fact: empty key or value, skipping")
        return None
    key = key.strip()[:100]
    value = str(value).strip()[:10000]
    # Auto-derive subject from key prefix if not explicitly provided
    if not subject:
        subject = derive_subject_from_key(key)
    subject = subject.strip()[:100].lower()
    # Category: use provided, or guess from key (any string is valid)
    if not category:
        category = _guess_fact_category(key)

    # Reject credential-looking values
    if any(s in value for s in ("sk-ant-", "Bearer ", "sk-", "AKIA")):
        log.warning(f"save_message_fact: rejected credential-like value for key '{key}'")
        return None

    now = datetime.now(TZ).isoformat()

    # Find nearest RAG chunk for source_msg_id
    source_chunk_id = None
    if source_msg_id:
        try:
            async with open_db() as db:
                cursor = await db.execute(
                    "SELECT id FROM rag_chunks WHERE start_msg_id <= ? AND end_msg_id >= ? LIMIT 1",
                    (source_msg_id, source_msg_id),
                )
                row = await cursor.fetchone()
                if row:
                    source_chunk_id = row[0]
        except Exception:
            pass  # rag_chunks table might not exist yet

    # Sanitize doc_pointer
    if doc_pointer:
        doc_pointer = doc_pointer.strip()[:500]

    async with open_db() as db:
        # Check if key already exists (non-expired)
        cursor = await db.execute(
            "SELECT id, fact_value FROM message_facts WHERE fact_key = ? AND expired = 0",
            (key,),
        )
        existing = await cursor.fetchone()

        if existing and existing[1] == value and not doc_pointer:
            # Same value, no doc_pointer change -- just touch timestamp + update subject/chat/domain
            await db.execute(
                "UPDATE message_facts SET source_msg_id = COALESCE(?, source_msg_id), "
                "source_chunk_id = COALESCE(?, source_chunk_id), "
                "subject = CASE WHEN ? != '' THEN ? ELSE subject END, "
                "source_chat_id = CASE WHEN ? != '' THEN ? ELSE source_chat_id END, "
                "domain_id = CASE WHEN ? != '' THEN ? ELSE domain_id END, "
                "updated_at = ? WHERE id = ?",
                (source_msg_id, source_chunk_id, subject, subject,
                 source_chat_id, source_chat_id, domain_id, domain_id,
                 now, existing[0]),
            )
            await db.commit()
            log.info(f"message_fact touched: {key} (unchanged value, subject={subject or 'kept'})")
            return existing[0]
        elif existing:
            # Value changed -- version chain: expire old, insert new with superseded_by link
            cursor = await db.execute(
                "INSERT INTO message_facts (fact_key, fact_value, category, subject, "
                "source_msg_id, source_chunk_id, doc_pointer, source_chat_id, domain_id, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (key, value, category, subject, source_msg_id, source_chunk_id,
                 doc_pointer or None, source_chat_id, domain_id, now, now),
            )
            new_id = cursor.lastrowid
            await db.execute(
                "UPDATE message_facts SET expired = 1, superseded_by = ?, "
                "updated_at = ? WHERE id = ?",
                (new_id, now, existing[0]),
            )
            await db.commit()
            log.info(f"message_fact versioned: [{subject}] {key}={value[:80]} (old={existing[0]} -> new={new_id})")
            return new_id
        else:
            # Insert new fact
            cursor = await db.execute(
                "INSERT INTO message_facts (fact_key, fact_value, category, subject, "
                "source_msg_id, source_chunk_id, doc_pointer, source_chat_id, domain_id, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (key, value, category, subject, source_msg_id, source_chunk_id,
                 doc_pointer or None, source_chat_id, domain_id, now, now),
            )
            await db.commit()
            fact_id = cursor.lastrowid
            log.info(f"message_fact created: [{subject}] {key}={value[:80]} (id={fact_id})")
            return fact_id


async def expire_message_fact(key: str, access_ctx: AccessContext | None = None) -> bool:
    """Soft-delete a fact by marking it expired. Returns True if found and expired.

    When access_ctx is provided and not admin, only facts owned by allowed chats
    can be expired. This prevents external chats from expiring admin/shared facts.
    """
    access_clause, access_params = _access_sql_filter(access_ctx, "source_chat_id")
    async with open_db() as db:
        cursor = await db.execute(
            f"UPDATE message_facts SET expired = 1, updated_at = ? "
            f"WHERE fact_key = ? AND expired = 0 {access_clause}",
            [datetime.now(TZ).isoformat(), key] + access_params,
        )
        await db.commit()
        if cursor.rowcount > 0:
            log.info(f"message_fact expired: {key}")
            return True
        return False


async def bulk_expire_message_facts(keys: list[str],
                                     access_ctx: AccessContext | None = None) -> dict:
    """Expire multiple facts in one transaction. Returns summary.

    When access_ctx is provided and not admin, only facts owned by allowed chats
    can be expired.
    """
    access_clause, access_params = _access_sql_filter(access_ctx, "source_chat_id")
    expired_keys = []
    not_found_keys = []
    async with open_db() as db:
        now = datetime.now(TZ).isoformat()
        for key in keys:
            cursor = await db.execute(
                f"UPDATE message_facts SET expired = 1, updated_at = ? "
                f"WHERE fact_key = ? AND expired = 0 {access_clause}",
                [now, key] + access_params,
            )
            if cursor.rowcount > 0:
                expired_keys.append(key)
            else:
                not_found_keys.append(key)
        await db.commit()
    log.info(f"bulk_expire: {len(expired_keys)} expired, {len(not_found_keys)} not found")
    return {"expired": expired_keys, "not_found": not_found_keys}


async def get_fact_document(fact_key: str, access_ctx: AccessContext | None = None) -> dict | None:
    """Look up a fact by key and return its doc_pointer + value.

    Returns dict with fact_key, fact_value, doc_pointer, category or None if not found.
    Also searches media_cache if no doc_pointer but fact value mentions a document.
    Filters by AccessContext for data isolation.
    """
    access_clause, access_params = _access_sql_filter(access_ctx, "source_chat_id")
    async with open_db() as db:

        cursor = await db.execute(
            f"SELECT id, fact_key, fact_value, category, doc_pointer "
            f"FROM message_facts WHERE fact_key = ? AND expired = 0 {access_clause}",
            [fact_key] + access_params,
        )
        row = await cursor.fetchone()
        if row:
            result = dict(row)
            # If no doc_pointer, try to find in media_cache by keyword
            if not result.get("doc_pointer"):
                mc_cursor = await db.execute(
                    "SELECT file_path FROM media_cache "
                    "WHERE description LIKE ? OR filename LIKE ? LIMIT 1",
                    (f"%{fact_key}%", f"%{fact_key}%"),
                )
                mc_row = await mc_cursor.fetchone()
                if mc_row:
                    result["doc_pointer"] = mc_row[0]
            return result
    return None


# --- Fact search ---

async def search_message_facts(query: str, category: str = "", limit: int = 20,
                                access_ctx: AccessContext | None = None) -> list[dict]:
    """Search message_facts using FTS5. Only returns non-expired facts.

    Filters by AccessContext for data isolation.
    """
    safe_query = _sanitize_fts_query(query)
    if safe_query == '""':
        return []
    access_clause, access_params = _access_sql_filter(access_ctx, "mf.source_chat_id")
    async with open_db() as db:
        if category:
            cursor = await db.execute(
                "SELECT mf.id, mf.fact_key, mf.fact_value, mf.category, mf.subject, "
                "mf.source_msg_id, mf.source_chunk_id, mf.doc_pointer, mf.created_at, mf.updated_at "
                "FROM message_facts_fts f "
                "JOIN message_facts mf ON f.rowid = mf.id "
                f"WHERE message_facts_fts MATCH ? AND mf.category = ? AND mf.expired = 0 {access_clause} "
                "ORDER BY rank LIMIT ?",
                [safe_query, category] + access_params + [limit],
            )
        else:
            cursor = await db.execute(
                "SELECT mf.id, mf.fact_key, mf.fact_value, mf.category, mf.subject, "
                "mf.source_msg_id, mf.source_chunk_id, mf.doc_pointer, mf.created_at, mf.updated_at "
                "FROM message_facts_fts f "
                "JOIN message_facts mf ON f.rowid = mf.id "
                f"WHERE message_facts_fts MATCH ? AND mf.expired = 0 {access_clause} "
                "ORDER BY rank LIMIT ?",
                [safe_query] + access_params + [limit],
            )
        rows = await cursor.fetchall()

    # CB-98: Track access_count on EXPLICIT searches (not passive prompt loading)
    if rows:
        try:
            import time
            _now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _ids = [r[0] for r in rows if r[0]]  # r[0] = mf.id
            if _ids:
                async with open_db() as db:
                    _ph = ",".join("?" for _ in _ids)
                    await db.execute(
                        f"UPDATE message_facts SET access_count = COALESCE(access_count, 0) + 1, "
                        f"last_accessed_at = ? WHERE id IN ({_ph})",
                        [_now] + _ids,
                    )
                    await db.commit()
        except Exception:
            pass  # Non-critical

    return [dict(r) for r in rows]


async def get_all_message_facts(category: str = "", subject: str = "",
                                include_expired: bool = False,
                                access_ctx: AccessContext | None = None) -> list[dict]:
    """Get all facts, optionally filtered by category or subject.

    Filters by AccessContext for data isolation.
    """
    access_clause, access_params = _access_sql_filter(access_ctx, "source_chat_id")
    async with open_db() as db:
        conditions = []
        params: list = []
        if not include_expired:
            conditions.append("expired = 0")
        if category:
            conditions.append("category = ?")
            params.append(category)
        if subject:
            conditions.append("subject = ?")
            params.append(subject.lower())
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        # Append access filter (starts with AND, so needs existing WHERE)
        if access_clause:
            if where:
                where += f" {access_clause}"
            else:
                # Convert "AND ..." to "WHERE ..." by stripping leading AND
                where = "WHERE " + access_clause.lstrip("AND ")
            params.extend(access_params)
        cursor = await db.execute(
            f"SELECT id, fact_key, fact_value, category, subject, source_msg_id, "
            f"source_chunk_id, doc_pointer, superseded_by, created_at, updated_at, expired "
            f"FROM message_facts {where} ORDER BY subject, category, fact_key",
            params,
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def seed_core_facts(facts: list[dict]):
    """Seed initial core facts into message_facts table. Idempotent -- skips existing keys."""
    count = 0
    async with open_db() as db:
        for fact in facts:
            key = fact.get("key", "")
            value = fact.get("value", "")
            category = fact.get("category", "general")
            doc_ptr = fact.get("doc_pointer", None)
            if not key or not value:
                continue
            cursor = await db.execute(
                "SELECT id FROM message_facts WHERE fact_key = ? AND expired = 0", (key,)
            )
            if await cursor.fetchone():
                continue  # Already exists
            now = datetime.now(TZ).isoformat()
            await db.execute(
                "INSERT INTO message_facts (fact_key, fact_value, category, "
                "source_msg_id, source_chunk_id, doc_pointer, created_at, updated_at) "
                "VALUES (?, ?, ?, NULL, NULL, ?, ?, ?)",
                (key[:100], str(value)[:10000], category, doc_ptr, now, now),
            )
            count += 1
        await db.commit()
    if count:
        log.info(f"Seeded {count} core facts into message_facts table")
    return count
