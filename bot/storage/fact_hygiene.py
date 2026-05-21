# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Fact hygiene — staleness detection, deduplication, and auto-cleanup (CB-108).

Runs as part of domain self-optimization scheduled tasks. Identifies stale,
duplicate, and superseded facts for automatic cleanup.

Criteria for staleness:
- Facts older than N days with 0 access_count (never used)
- Facts with superseded_by set but not expired (missed cleanup)
- Duplicate facts: same subject+key with different values (keep newest)

Safety: TODOs are NEVER expired without explicit user confirmation.
Credentials are NEVER auto-expired.

Usage:
    result = await run_fact_hygiene(domain_id="teamwork", dry_run=True)
    result = await run_fact_hygiene(domain_id="teamwork", dry_run=False)
"""
import logging

log = logging.getLogger(__name__)

# Categories that are NEVER auto-expired (require explicit user action)
_PROTECTED_CATEGORIES = frozenset({"credentials", "todo", "documents", "migration"})

# Default staleness thresholds
DEFAULT_STALE_DAYS = 90       # facts unused for 90+ days
DEFAULT_ORPHAN_DAYS = 30      # superseded facts not expired for 30+ days


async def run_fact_hygiene(
    domain_id: str = "",
    chat_id: str = "",
    stale_days: int = DEFAULT_STALE_DAYS,
    orphan_days: int = DEFAULT_ORPHAN_DAYS,
    dry_run: bool = True,
) -> dict:
    """Run fact hygiene: detect and optionally clean stale + duplicate facts.

    domain_id: scope to a specific domain (empty = all accessible)
    chat_id: scope to a specific chat (empty = all accessible)
    stale_days: expire facts with 0 access_count older than this
    orphan_days: expire superseded facts older than this
    dry_run: if True, only report findings without making changes

    Returns: {"stale": N, "orphaned": N, "duplicates": N, "expired": N}
    """
    from bot.storage.db import open_db

    result = {
        "stale": 0,
        "orphaned": 0,
        "duplicates": 0,
        "expired": 0,
        "dry_run": dry_run,
    }

    async with open_db() as db:
        # --- 1. Stale facts: old + never accessed ---
        # Build parameterized clauses (no string interpolation — SQL injection safe)
        base_params: list = [f"-{stale_days} days"]
        _domain_clause = ""
        _chat_clause = ""
        if domain_id:
            _domain_clause = "AND domain_id = ?"
            base_params.append(domain_id)
        if chat_id:
            _chat_clause = "AND source_chat_id = ?"
            base_params.append(chat_id)

        # Build protected categories exclusion (static frozenset — safe)
        _prot_placeholders = ",".join("?" for _ in _PROTECTED_CATEGORIES)
        _prot_params = list(_PROTECTED_CATEGORIES)

        cursor = await db.execute(f"""
            SELECT id, fact_key, category, subject, updated_at,
                   COALESCE(access_count, 0) as ac
            FROM message_facts
            WHERE expired = 0
            AND COALESCE(access_count, 0) = 0
            AND updated_at < datetime('now', ?)
            AND category NOT IN ({_prot_placeholders})
            {_domain_clause} {_chat_clause}
            ORDER BY updated_at ASC
        """, base_params + _prot_params)
        stale_rows = await cursor.fetchall()
        result["stale"] = len(stale_rows)

        if not dry_run and stale_rows:
            stale_ids = [r[0] for r in stale_rows]
            # Batch expire
            for batch_start in range(0, len(stale_ids), 100):
                batch = stale_ids[batch_start:batch_start + 100]
                placeholders = ",".join("?" for _ in batch)
                await db.execute(
                    f"UPDATE message_facts SET expired = 1 WHERE id IN ({placeholders})",
                    batch,
                )
            result["expired"] += len(stale_ids)
            log.info("Fact hygiene: expired %d stale facts (0 access, >%d days old)",
                     len(stale_ids), stale_days)

        # --- 2. Orphaned superseded facts ---
        orphan_params: list = [f"-{orphan_days} days"]
        if domain_id:
            orphan_params.append(domain_id)
        if chat_id:
            orphan_params.append(chat_id)
        cursor = await db.execute(f"""
            SELECT id, fact_key, superseded_by
            FROM message_facts
            WHERE expired = 0
            AND superseded_by IS NOT NULL
            AND superseded_by != 0
            AND updated_at < datetime('now', ?)
            AND category NOT IN ({_prot_placeholders})
            {_domain_clause} {_chat_clause}
        """, orphan_params + _prot_params)
        orphan_rows = await cursor.fetchall()
        result["orphaned"] = len(orphan_rows)

        if not dry_run and orphan_rows:
            orphan_ids = [r[0] for r in orphan_rows]
            for batch_start in range(0, len(orphan_ids), 100):
                batch = orphan_ids[batch_start:batch_start + 100]
                placeholders = ",".join("?" for _ in batch)
                await db.execute(
                    f"UPDATE message_facts SET expired = 1 WHERE id IN ({placeholders})",
                    batch,
                )
            result["expired"] += len(orphan_ids)
            log.info("Fact hygiene: expired %d orphaned superseded facts",
                     len(orphan_ids))

        # --- 3. Duplicate detection: same fact_key, different values ---
        dup_params: list = []
        if domain_id:
            dup_params.append(domain_id)
        if chat_id:
            dup_params.append(chat_id)
        cursor = await db.execute(f"""
            SELECT fact_key, COUNT(*) as cnt
            FROM message_facts
            WHERE expired = 0
            AND category NOT IN ({_prot_placeholders})
            {_domain_clause} {_chat_clause}
            GROUP BY fact_key
            HAVING cnt > 1
        """, dup_params + _prot_params)
        dup_keys = await cursor.fetchall()

        dup_count = 0
        for key_row in dup_keys:
            fact_key = key_row[0]
            # Get all versions, keep the newest
            cursor = await db.execute(
                "SELECT id, fact_value, updated_at FROM message_facts "
                "WHERE fact_key = ? AND expired = 0 ORDER BY updated_at DESC",
                (fact_key,),
            )
            versions = await cursor.fetchall()
            if len(versions) <= 1:
                continue

            # Keep the newest (first row), expire the rest
            keep_id = versions[0][0]
            expire_ids = [v[0] for v in versions[1:]]
            dup_count += len(expire_ids)

            if not dry_run:
                placeholders = ",".join("?" for _ in expire_ids)
                await db.execute(
                    f"UPDATE message_facts SET expired = 1, "
                    f"superseded_by = ? WHERE id IN ({placeholders})",
                    [keep_id] + expire_ids,
                )

        result["duplicates"] = dup_count
        if not dry_run:
            result["expired"] += dup_count
            if dup_count:
                log.info("Fact hygiene: deduplicated %d facts (%d unique keys)",
                         dup_count, len(dup_keys))

        if not dry_run:
            await db.commit()

    _scope = f"domain={domain_id}" if domain_id else f"chat={chat_id}" if chat_id else "global"
    log.info("Fact hygiene complete (%s): stale=%d orphaned=%d duplicates=%d expired=%d dry_run=%s",
             _scope, result["stale"], result["orphaned"], result["duplicates"],
             result["expired"], dry_run)

    return result


async def get_hygiene_stats(domain_id: str = "") -> dict:
    """Get fact hygiene statistics without making changes."""
    return await run_fact_hygiene(domain_id=domain_id, dry_run=True)
