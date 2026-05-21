# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Coding artifact registry — index scripts for cross-session discovery (CB-104).

6-Layer FILES layer: tracks coding artifacts (scripts, configs, generated files)
with AI-generated descriptions for discoverability across sessions.

Usage:
    await register_artifact("/path/to/script.py", domain_id="admin", chat_id="telegram:123456789")
    results = await search_artifacts("deployment script")
"""
import logging
import time

log = logging.getLogger(__name__)


async def init_artifact_tables():
    """Create coding_artifacts table if not exists."""
    from bot.storage.db import open_db

    async with open_db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS coding_artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                description TEXT DEFAULT '',
                language TEXT DEFAULT '',
                domain_id TEXT DEFAULT '',
                chat_id TEXT DEFAULT '',
                commit_hash TEXT DEFAULT '',
                ticket_ref TEXT DEFAULT '',
                file_size INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_artifacts_domain "
            "ON coding_artifacts(domain_id)"
        )
        await db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS artifacts_fts USING fts5(
                file_path, description, language, domain_id,
                content=coding_artifacts, content_rowid=id
            )
        """)
        # FTS5 sync triggers
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS artifacts_ai AFTER INSERT ON coding_artifacts BEGIN
                INSERT INTO artifacts_fts(rowid, file_path, description, language, domain_id)
                VALUES (new.id, new.file_path, new.description, new.language, new.domain_id);
            END
        """)
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS artifacts_au AFTER UPDATE ON coding_artifacts BEGIN
                INSERT INTO artifacts_fts(artifacts_fts, rowid, file_path, description, language, domain_id)
                VALUES ('delete', old.id, old.file_path, old.description, old.language, old.domain_id);
                INSERT INTO artifacts_fts(rowid, file_path, description, language, domain_id)
                VALUES (new.id, new.file_path, new.description, new.language, new.domain_id);
            END
        """)
        await db.commit()
    log.debug("Artifact tables initialized")


async def register_artifact(
    file_path: str,
    description: str = "",
    language: str = "",
    domain_id: str = "",
    chat_id: str = "",
    commit_hash: str = "",
    ticket_ref: str = "",
    file_size: int = 0,
) -> int:
    """Register a coding artifact for cross-session discovery.

    Returns the artifact ID.
    """
    from bot.storage.db import open_db
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Auto-detect language from extension
    if not language and "." in file_path:
        ext = file_path.rsplit(".", 1)[-1].lower()
        _ext_map = {
            "py": "python", "js": "javascript", "ts": "typescript",
            "sh": "bash", "yaml": "yaml", "yml": "yaml",
            "json": "json", "html": "html", "css": "css",
            "md": "markdown", "sql": "sql", "toml": "toml",
        }
        language = _ext_map.get(ext, ext)

    async with open_db() as db:
        cursor = await db.execute(
            "INSERT INTO coding_artifacts "
            "(file_path, description, language, domain_id, chat_id, "
            "commit_hash, ticket_ref, file_size, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (file_path, description, language, domain_id, chat_id,
             commit_hash, ticket_ref, file_size, now, now),
        )
        await db.commit()
        artifact_id = cursor.lastrowid

    log.info("Registered artifact: %s (%s) domain=%s", file_path, language, domain_id)
    return artifact_id


async def search_artifacts(query: str, domain_id: str = "",
                           limit: int = 20) -> list[dict]:
    """Search coding artifacts by description/path/language.

    Uses FTS5 for full-text search.
    """
    from bot.storage.db import open_db

    async with open_db() as db:
        if domain_id:
            cursor = await db.execute(
                "SELECT a.id, a.file_path, a.description, a.language, "
                "a.domain_id, a.chat_id, a.commit_hash, a.ticket_ref, "
                "a.created_at "
                "FROM coding_artifacts a "
                "JOIN artifacts_fts f ON a.id = f.rowid "
                "WHERE artifacts_fts MATCH ? AND a.domain_id = ? "
                "ORDER BY rank LIMIT ?",
                (query, domain_id, limit),
            )
        else:
            cursor = await db.execute(
                "SELECT a.id, a.file_path, a.description, a.language, "
                "a.domain_id, a.chat_id, a.commit_hash, a.ticket_ref, "
                "a.created_at "
                "FROM coding_artifacts a "
                "JOIN artifacts_fts f ON a.id = f.rowid "
                "WHERE artifacts_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (query, limit),
            )
        rows = await cursor.fetchall()

    return [
        {
            "id": r[0], "file_path": r[1], "description": r[2],
            "language": r[3], "domain_id": r[4], "chat_id": r[5],
            "commit_hash": r[6], "ticket_ref": r[7], "created_at": r[8],
        }
        for r in rows
    ]


async def list_artifacts(domain_id: str = "", limit: int = 50) -> list[dict]:
    """List recent coding artifacts, optionally filtered by domain."""
    from bot.storage.db import open_db

    async with open_db() as db:
        if domain_id:
            cursor = await db.execute(
                "SELECT id, file_path, description, language, domain_id, "
                "commit_hash, ticket_ref, created_at "
                "FROM coding_artifacts WHERE domain_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (domain_id, limit),
            )
        else:
            cursor = await db.execute(
                "SELECT id, file_path, description, language, domain_id, "
                "commit_hash, ticket_ref, created_at "
                "FROM coding_artifacts ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()

    return [
        {
            "id": r[0], "file_path": r[1], "description": r[2],
            "language": r[3], "domain_id": r[4],
            "commit_hash": r[5], "ticket_ref": r[6], "created_at": r[7],
        }
        for r in rows
    ]
