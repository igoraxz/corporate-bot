# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Corporate audit logging — per-user, per-team activity tracking.

Records all significant bot interactions for compliance:
- WHO (user_id, user_email) accessed WHAT (domain, tool, data)
- WHEN (timestamp) and WHERE (conversation, channel)
- Result summary (success/error, tokens used, cost)

Storage: SQLite audit_log table (same DB, partitioned by month for rotation).
Retention: configurable (default 90 days for detailed, 365 days for summary).
Export: CSV/JSON for compliance team review.

GDPR note: audit logs contain PII (user IDs, emails). Deletion requests
must purge both user_tokens AND audit_log entries for the user.
"""

import logging
import time
from typing import Any

from bot.storage.db import open_db

log = logging.getLogger(__name__)


# ============================================================
# Schema
# ============================================================

async def init_audit_tables():
    """Create audit logging tables."""
    async with open_db() as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                user_id TEXT NOT NULL,
                user_email TEXT NOT NULL DEFAULT '',
                user_name TEXT NOT NULL DEFAULT '',
                conversation_id TEXT NOT NULL DEFAULT '',
                conversation_type TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                domains_accessed TEXT NOT NULL DEFAULT '',
                tools_used TEXT NOT NULL DEFAULT '',
                tokens_used INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'ok',
                error TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_audit_timestamp
                ON audit_log(timestamp);
            CREATE INDEX IF NOT EXISTS idx_audit_user
                ON audit_log(user_id);
            CREATE INDEX IF NOT EXISTS idx_audit_conversation
                ON audit_log(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_audit_action
                ON audit_log(action);
        """)
        await db.commit()
    log.info("Audit logging tables initialized")


# ============================================================
# Logging Operations
# ============================================================

async def log_event(
    user_id: str,
    action: str,
    *,
    user_email: str = "",
    user_name: str = "",
    conversation_id: str = "",
    conversation_type: str = "",
    details: str = "",
    domains_accessed: list[str] | None = None,
    tools_used: list[str] | None = None,
    tokens_used: int = 0,
    cost_usd: float = 0.0,
    status: str = "ok",
    error: str = "",
) -> None:
    """Record an audit event.

    Args:
        user_id: Azure AD Object ID
        action: Event type (message, search, tool_use, auth, admin_action)
        details: Human-readable description
        domains_accessed: Knowledge domains queried
        tools_used: MCP tools invoked
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    async with open_db() as db:
        await db.execute("""
            INSERT INTO audit_log (timestamp, user_id, user_email, user_name,
                                   conversation_id, conversation_type,
                                   action, details, domains_accessed, tools_used,
                                   tokens_used, cost_usd, status, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            now, user_id, user_email, user_name,
            conversation_id, conversation_type,
            action, details,
            ",".join(domains_accessed or []),
            ",".join(tools_used or []),
            tokens_used, cost_usd, status, error,
        ))
        await db.commit()


# ============================================================
# Structured Admin Action Logger (CB-50)
# ============================================================

# Dedicated logger for admin actions — structured JSON for easy parsing
_audit_log = logging.getLogger("audit")


async def log_admin_action(
    action: str,
    actor: str,
    target: str,
    result: str,
    details: dict | None = None,
) -> None:
    """Log a structured admin action to both the audit logger and the DB.

    Args:
        action: Action type (e.g. "domain.register", "harness.update",
                "chat.delete", "oauth.switch")
        actor: User ID or email of the person performing the action.
        target: What was affected (domain_id, chat_id, harness label, etc.)
        result: Outcome — "success", "denied", "error"
        details: Optional additional context dict.
    """
    import json as _json

    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "action": action,
        "actor": actor,
        "target": target,
        "result": result,
    }
    if details:
        entry["details"] = details

    # Structured log line (machine-parseable)
    _audit_log.info(_json.dumps(entry))

    # Also persist to the audit_log DB table for querying/export
    try:
        detail_str = _json.dumps(details) if details else ""
        await log_event(
            user_id=actor,
            action=f"admin:{action}",
            details=f"{target} -> {result}" + (f" | {detail_str}" if detail_str else ""),
            status="ok" if result == "success" else result,
        )
    except Exception as e:
        log.warning("Failed to persist admin action to DB: %s", e)


# ============================================================
# Query / Reporting
# ============================================================

async def get_audit_summary(
    hours: int = 24,
    user_id: str = "",
    conversation_id: str = "",
) -> dict:
    """Get audit summary for a time period.

    Returns: {total_events, users_active, domains_queried, tools_used, cost_total}
    """
    import datetime
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(hours=hours)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    filters = ["timestamp > ?"]
    params: list[Any] = [cutoff]

    if user_id:
        filters.append("user_id = ?")
        params.append(user_id)
    if conversation_id:
        filters.append("conversation_id = ?")
        params.append(conversation_id)

    where = " AND ".join(filters)

    async with open_db() as db:
        cursor = await db.execute(f"""
            SELECT
                COUNT(*) as total_events,
                COUNT(DISTINCT user_id) as users_active,
                SUM(tokens_used) as total_tokens,
                SUM(cost_usd) as total_cost
            FROM audit_log WHERE {where}
        """, params)
        row = await cursor.fetchone()

        # Top domains
        cursor2 = await db.execute(f"""
            SELECT domains_accessed, COUNT(*) as cnt
            FROM audit_log WHERE {where} AND domains_accessed != ''
            GROUP BY domains_accessed ORDER BY cnt DESC LIMIT 10
        """, params)
        domain_rows = await cursor2.fetchall()

        # Top tools
        cursor3 = await db.execute(f"""
            SELECT tools_used, COUNT(*) as cnt
            FROM audit_log WHERE {where} AND tools_used != ''
            GROUP BY tools_used ORDER BY cnt DESC LIMIT 10
        """, params)
        tool_rows = await cursor3.fetchall()

    return {
        "period_hours": hours,
        "total_events": row["total_events"] if row else 0,
        "users_active": row["users_active"] if row else 0,
        "total_tokens": row["total_tokens"] or 0 if row else 0,
        "total_cost_usd": round(row["total_cost"] or 0, 2) if row else 0,
        "top_domains": [{"domain": r["domains_accessed"], "count": r["cnt"]} for r in domain_rows],
        "top_tools": [{"tool": r["tools_used"], "count": r["cnt"]} for r in tool_rows],
    }


async def get_user_activity(user_id: str, limit: int = 50) -> list[dict]:
    """Get recent activity for a specific user (compliance review)."""
    async with open_db() as db:
        cursor = await db.execute("""
            SELECT timestamp, action, details, domains_accessed, tools_used,
                   conversation_id, status, error
            FROM audit_log WHERE user_id = ?
            ORDER BY timestamp DESC LIMIT ?
        """, (user_id, limit))
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def export_audit_csv(hours: int = 24) -> str:
    """Export audit log as CSV string (for compliance download)."""
    import datetime
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(hours=hours)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    async with open_db() as db:
        cursor = await db.execute("""
            SELECT timestamp, user_id, user_email, user_name,
                   conversation_id, conversation_type,
                   action, details, domains_accessed, tools_used,
                   tokens_used, cost_usd, status, error
            FROM audit_log WHERE timestamp > ?
            ORDER BY timestamp ASC
        """, (cutoff,))
        rows = await cursor.fetchall()

    if not rows:
        return "No events in period.\n"

    import csv
    import io

    output = io.StringIO()
    headers = list(rows[0].keys())
    writer = csv.writer(output, quoting=csv.QUOTE_ALL)
    writer.writerow(headers)
    for row in rows:
        values = []
        for h in headers:
            val = str(row[h])[:200]
            # CSV injection protection: prefix dangerous leading chars
            if val and val[0] in ("=", "+", "-", "@", "\t", "\r"):
                val = "'" + val
            values.append(val)
        writer.writerow(values)

    return output.getvalue()


# ============================================================
# Cleanup (retention policy)
# ============================================================

async def cleanup_old_events(retention_days: int = 0) -> int:
    """Delete audit events older than retention_days.

    Called by scheduled task (e.g. daily).
    Uses AUDIT_RETENTION_DAYS from config if retention_days=0.
    """
    if retention_days <= 0:
        import os
        retention_days = int(os.environ.get("AUDIT_RETENTION_DAYS", "90"))
    import datetime
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=retention_days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    async with open_db() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM audit_log WHERE timestamp < ?", (cutoff,)
        )
        row = await cursor.fetchone()
        count = row["cnt"] if row else 0

        if count > 0:
            await db.execute("DELETE FROM audit_log WHERE timestamp < ?", (cutoff,))
            await db.commit()
            log.info("Audit cleanup: removed %d events older than %d days", count, retention_days)

    return count


# ============================================================
# GDPR: Per-user data deletion
# ============================================================

async def delete_user_data(user_id: str) -> dict:
    """Delete ALL data for a specific user (GDPR right to erasure).

    Removes:
    - audit_log entries (by user_id)
    - user_tokens (by user_id)
    - messages (by user_id)
    - message_facts (by source_chat_id matching user's DM chat_id — best effort)
    - media_cache (by sender_name matching user_id — best effort)
    - credential_store (user-scoped credentials owned by user)

    Limitations:
    - RAG chunks may contain user text but are not user-keyed; they are
      periodically rebuilt from messages, so they will be purged on next
      backfill after the source messages are deleted.
    - media_cache deletion matches on sender_name which may not capture
      all entries if the sender_name was stored differently (e.g. display
      name vs user_id). This is best-effort.
    - message_facts deletion uses source_chat_id; facts created in group
      chats (not the user's DM) are not deleted because they may contain
      contributions from multiple users.

    Returns summary of what was deleted.
    """
    from bot.auth.token_vault import delete_token

    async with open_db() as db:
        # Count and delete audit entries
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM audit_log WHERE user_id = ?", (user_id,)
        )
        audit_count = (await cursor.fetchone())["cnt"]
        await db.execute("DELETE FROM audit_log WHERE user_id = ?", (user_id,))

        # Count and delete messages by user_id
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM messages WHERE user_id = ?", (user_id,)
        )
        messages_count = (await cursor.fetchone())["cnt"]
        await db.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))

        # Delete message_facts where source_chat_id matches the user's DM
        # chat_id. In Teams, DM conversation IDs typically contain the user's
        # AAD object ID, so we match on that. This is best-effort.
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM message_facts WHERE source_chat_id LIKE ?",
            (f"%{user_id}%",),
        )
        facts_count = (await cursor.fetchone())["cnt"]
        await db.execute(
            "DELETE FROM message_facts WHERE source_chat_id LIKE ?",
            (f"%{user_id}%",),
        )

        # Delete media_cache entries where sender matches user_id (best effort)
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM media_cache WHERE sender_name = ?",
            (user_id,),
        )
        media_count = (await cursor.fetchone())["cnt"]
        await db.execute(
            "DELETE FROM media_cache WHERE sender_name = ?", (user_id,),
        )

        # Delete user-scoped credentials from credential_store
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM credential_store "
            "WHERE scope = 'user' AND owner = ?",
            (user_id,),
        )
        creds_count = (await cursor.fetchone())["cnt"]
        await db.execute(
            "DELETE FROM credential_store WHERE scope = 'user' AND owner = ?",
            (user_id,),
        )

        await db.commit()

    # Delete token from vault
    await delete_token(user_id)

    log.info(
        "GDPR deletion for user %s: %d audit, %d messages, %d facts, "
        "%d media, %d credentials + token",
        user_id[:8], audit_count, messages_count, facts_count,
        media_count, creds_count,
    )
    return {
        "user_id": user_id,
        "audit_events_deleted": audit_count,
        "messages_deleted": messages_count,
        "facts_deleted": facts_count,
        "media_deleted": media_count,
        "credentials_deleted": creds_count,
        "token_deleted": True,
        "status": "complete",
        "limitations": (
            "RAG chunks are not user-keyed and will be purged on next "
            "backfill. media_cache deletion is best-effort by sender_name. "
            "Facts from group chats are not deleted."
        ),
    }
