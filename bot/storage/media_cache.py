# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Media caching operations -- cache, search, cleanup, descriptions.

Extracted from the original bot/memory.py (May 2026 reorg). All functions are re-exported from bot.storage.memory
for backwards compatibility.

Provides searchable persistent storage for media files:
- Cache media files with metadata (FTS5 indexed)
- Re-fetch evicted files from Telegram via tg_file_id
- Search/list cached media
- Periodic cleanup of expired entries
"""

import logging
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from bot.core.access import (
    AccessContext,
    _access_sql_filter,
)
from bot.storage.db import open_db
from config import ORG_TIMEZONE, MEDIA_CACHE_DIR, MEDIA_RETENTION_DAYS

log = logging.getLogger(__name__)
TZ = ZoneInfo(ORG_TIMEZONE)


def _sanitize_fts_query(query: str) -> str:
    """Escape FTS5 special characters to prevent query syntax errors.

    Wraps each word in double quotes for literal matching.
    """
    words = re.findall(r'[\w]+', query)
    if not words:
        return '""'
    return " ".join(f'"{w}"' for w in words[:10])


def _guess_extension(media_type: str) -> str:
    """Guess file extension from media type."""
    return {
        "photo": ".jpg",
        "document": "",
        "voice": ".ogg",
        "video": ".mp4",
        "video_note": ".mp4",
        "animation": ".mp4",
        "sticker": ".webp",
        "audio": ".mp3",
    }.get(media_type, "")


async def cache_media_file(
    source_path: str,
    media_type: str,
    source: str,
    sender_name: str = "",
    chat_id: str = "",
    original_filename: str = "",
    description: str = "",
    mime_type: str = "",
    tg_file_id: str = "",
) -> dict:
    """Cache a media file: copy from temp to cache dir, index in SQLite.

    Args:
        source_path: path to the temporary downloaded file
        media_type: photo, document, voice, video, etc.
        source: telegram or whatsapp
        sender_name: who sent the media
        chat_id: chat/group ID
        original_filename: original filename from the sender
        description: caption or auto-description
        mime_type: MIME type if known

    Returns:
        dict with cached file info, or None on failure
    """
    src = Path(source_path)
    if not src.exists():
        log.warning(f"Media cache: source file not found: {source_path}")
        return None

    file_size = src.stat().st_size
    if file_size == 0:
        log.warning(f"Media cache: empty file skipped: {source_path}")
        return None

    # Generate a unique cached filename
    now = datetime.now(TZ)
    ts = now.strftime("%Y%m%d_%H%M%S")
    ext = src.suffix or _guess_extension(media_type)
    cached_filename = f"{ts}_{source}_{media_type}{ext}"
    cached_path = MEDIA_CACHE_DIR / cached_filename

    # Copy file to cache directory
    try:
        shutil.copy2(str(src), str(cached_path))
    except Exception as e:
        log.error(f"Media cache: failed to copy {src} -> {cached_path}: {e}")
        return None

    # Calculate expiry
    expires_at = (now + timedelta(days=MEDIA_RETENTION_DAYS)).isoformat()

    # Index in SQLite
    try:
        async with open_db() as db:
            cursor = await db.execute(
                "INSERT INTO media_cache "
                "(filename, original_filename, media_type, source, chat_id, "
                " sender_name, description, file_size, mime_type, cached_at, expires_at, file_path, tg_file_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cached_filename, original_filename or src.name, media_type, source,
                 chat_id, sender_name, description, file_size, mime_type,
                 now.isoformat(), expires_at, str(cached_path), tg_file_id or None),
            )
            media_cache_id = cursor.lastrowid
            await db.commit()
    except Exception as e:
        log.error(f"Media cache: failed to index {cached_filename}: {e}")
        # Clean up the copied file if DB insert fails
        cached_path.unlink(missing_ok=True)
        return None

    log.info(f"Media cached: {cached_filename} (id={media_cache_id}, {file_size} bytes, tg_file_id={'yes' if tg_file_id else 'no'})")
    return {
        "id": media_cache_id,
        "filename": cached_filename,
        "file_path": str(cached_path),
        "media_type": media_type,
        "file_size": file_size,
        "cached_at": now.isoformat(),
        "expires_at": expires_at,
    }


async def ensure_media_file(media_row: dict) -> dict:
    """Ensure a media file exists on disk. Re-fetches from TG if needed.

    Args:
        media_row: dict from media_cache table (must include file_path, tg_file_id, source, id)

    Returns:
        dict with 'status' ('available', 'refetched', 'unavailable') and 'file_path'
    """
    file_path = media_row.get("file_path", "")
    if file_path and Path(file_path).exists():
        return {"status": "available", "file_path": file_path}

    # Try TG re-fetch
    tg_file_id = media_row.get("tg_file_id")
    if not tg_file_id:
        return {"status": "unavailable", "file_path": file_path,
                "reason": "File missing, no TG file_id for re-fetch" if media_row.get("source") == "telegram"
                else "File missing (WhatsApp media cannot be re-fetched)"}

    try:
        from integrations.telegram import download_file_by_id
        local_path = await download_file_by_id(tg_file_id, media_row.get("filename", "refetched"))
        if local_path:
            # Copy to cached path
            cached_path = Path(file_path)
            cached_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_path, str(cached_path))
            Path(local_path).unlink(missing_ok=True)
            log.info(f"Media re-fetched from TG: {cached_path.name} (id={media_row.get('id')})")
            return {"status": "refetched", "file_path": str(cached_path)}
    except Exception as e:
        log.warning(f"TG re-fetch failed for media id={media_row.get('id')}: {e}")

    return {"status": "unavailable", "file_path": file_path,
            "reason": "TG re-fetch failed"}


async def link_media_to_message(source: str, platform_msg_id: str, media_cache_id: int):
    """Link a media_cache entry to its source message in the messages table."""
    try:
        async with open_db() as db:
            await db.execute(
                "UPDATE messages SET media_cache_id = ? "
                "WHERE source = ? AND message_id = ? AND media_cache_id IS NULL",
                (media_cache_id, source, platform_msg_id),
            )
            await db.commit()
            log.debug(f"Linked media_cache_id={media_cache_id} to message {source}:{platform_msg_id}")
    except Exception as e:
        log.warning(f"Failed to link media to message: {e}")


async def search_media_cache(query: str, limit: int = 10,
                              access_ctx: AccessContext | None = None) -> list[dict]:
    """Search cached media files using FTS5.

    Filters by AccessContext for data isolation (media_cache already has chat_id).
    """
    safe_query = _sanitize_fts_query(query)
    access_clause, access_params = _access_sql_filter(access_ctx, "mc.chat_id")
    try:
        async with open_db() as db:

            cursor = await db.execute(
                "SELECT mc.id, mc.filename, mc.original_filename, mc.media_type, mc.source, "
                "mc.sender_name, substr(mc.description, 1, 1000) as description, "
                "mc.file_size, mc.cached_at, mc.file_path, "
                f"mc.tg_file_id, rank "
                "FROM media_cache_fts f "
                "JOIN media_cache mc ON f.rowid = mc.id "
                f"WHERE media_cache_fts MATCH ? {access_clause} "
                "ORDER BY rank LIMIT ?",
                [safe_query] + access_params + [limit],
            )
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"search_media_cache error: {e}")
        return []


async def list_cached_media(media_type: str = "", limit: int = 20,
                            access_ctx: AccessContext | None = None) -> list[dict]:
    """List recently cached media files, optionally filtered by type.

    Filters by AccessContext for data isolation.
    """
    access_clause, access_params = _access_sql_filter(access_ctx, "chat_id")
    try:
        async with open_db() as db:

            if media_type:
                cursor = await db.execute(
                    f"SELECT id, filename, original_filename, media_type, source, sender_name, "
                    f"substr(description, 1, 1000) as description, file_size, cached_at, file_path, tg_file_id "
                    f"FROM media_cache WHERE media_type = ? {access_clause} "
                    f"ORDER BY cached_at DESC LIMIT ?",
                    [media_type] + access_params + [limit],
                )
            else:
                if access_clause:
                    where = "WHERE " + access_clause.lstrip("AND ")
                else:
                    where = ""
                cursor = await db.execute(
                    f"SELECT id, filename, original_filename, media_type, source, sender_name, "
                    f"substr(description, 1, 1000) as description, file_size, cached_at, file_path, tg_file_id "
                    f"FROM media_cache {where} ORDER BY cached_at DESC LIMIT ?",
                    access_params + [limit],
                )
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"list_cached_media error: {e}")
        return []


async def cleanup_expired_media():
    """Delete cached media files that have exceeded the retention period.

    Called periodically from the scheduler loop.
    """
    now = datetime.now(TZ).isoformat()
    deleted_count = 0
    try:
        async with open_db() as db:
            # Find expired entries
            cursor = await db.execute(
                "SELECT id, filename, file_path FROM media_cache WHERE expires_at < ?",
                (now,),
            )
            expired = await cursor.fetchall()

            for row in expired:
                row_id, filename, file_path = row
                # Delete the file
                fpath = Path(file_path)
                if fpath.exists():
                    try:
                        fpath.unlink()
                        deleted_count += 1
                    except Exception as e:
                        log.warning(f"Failed to delete expired media file {filename}: {e}")
                else:
                    deleted_count += 1  # File already gone, still clean up DB

                # Remove FTS entry first (required for content= tables)
                await db.execute(
                    "INSERT INTO media_cache_fts(media_cache_fts, rowid, filename, original_filename, "
                    "description, sender_name, media_type) "
                    "SELECT 'delete', id, filename, original_filename, description, sender_name, media_type "
                    "FROM media_cache WHERE id = ?",
                    (row_id,),
                )

            # Delete from main table
            if expired:
                await db.execute(
                    "DELETE FROM media_cache WHERE expires_at < ?",
                    (now,),
                )
                await db.commit()

        if deleted_count:
            log.info(f"Media cache cleanup: deleted {deleted_count} expired files")
    except Exception as e:
        log.error(f"Media cache cleanup error: {e}")


async def get_media_cache_stats() -> dict:
    """Get statistics about the media cache."""
    try:
        async with open_db() as db:
            cursor = await db.execute("SELECT COUNT(*) FROM media_cache")
            total_files = (await cursor.fetchone())[0]

            cursor = await db.execute("SELECT SUM(file_size) FROM media_cache")
            total_size = (await cursor.fetchone())[0] or 0

            cursor = await db.execute(
                "SELECT media_type, COUNT(*) FROM media_cache GROUP BY media_type"
            )
            by_type = {row[0]: row[1] for row in await cursor.fetchall()}

        return {
            "total_files": total_files,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "by_type": by_type,
            "retention_days": MEDIA_RETENTION_DAYS,
            "cache_dir": str(MEDIA_CACHE_DIR),
        }
    except Exception as e:
        log.error(f"get_media_cache_stats error: {e}")
        return {"error": str(e)}


async def update_media_description(media_cache_id: int, description: str) -> bool:
    """Update the description field for a media_cache entry.

    The FTS5 UPDATE trigger (media_cache_au) automatically keeps the search index in sync.
    Returns True on success, False on failure.
    """
    try:
        async with open_db() as db:
            await db.execute(
                "UPDATE media_cache SET description = ? WHERE id = ?",
                (description, media_cache_id),
            )
            await db.commit()
        log.info(f"Media description updated: id={media_cache_id}, len={len(description)}")
        return True
    except Exception as e:
        log.error(f"update_media_description error (id={media_cache_id}): {e}")
        return False


async def get_undescribed_media(limit: int = 50) -> list[dict]:
    """Get media_cache entries that have no AI description yet.

    Returns entries where description is empty or just the caption (short text).
    Used for startup backfill -- processes media that was cached before descriptions were enabled,
    or where description generation failed.
    """
    try:
        async with open_db() as db:

            cursor = await db.execute(
                "SELECT id, filename, file_path, media_type, description, original_filename "
                "FROM media_cache "
                "WHERE (description = '' OR description IS NULL) "
                "AND media_type IN ('photo', 'image', 'document', 'video', 'voice', 'audio') "
                "ORDER BY cached_at DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"get_undescribed_media error: {e}")
        return []
