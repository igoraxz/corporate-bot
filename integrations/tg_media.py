# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Telegram media handling — download, metadata extraction, file safety.

Extracted from integrations/telegram.py. Pure extraction, no behavior changes.
"""

import logging
from pathlib import Path

from pyrogram.types import Message

from config import TMP_DIR, MAX_MEDIA_SIZE_MB

log = logging.getLogger(__name__)

__all__ = [
    "_VALID_MEDIA_TYPES",
    "_safe_filename",
    "_extract_media_meta",
    "_get_file_id",
    "download_media",
    "_download_pyrogram_media",
    "_download_from_metadata",
    "_download_dict_media",
    "download_file_by_id",
]

# Valid media types for download validation
_VALID_MEDIA_TYPES = {"photo", "document", "animation", "video",
                      "sticker", "voice", "video_note", "audio"}


def _safe_filename(name: str, fallback: str = "file") -> str:
    """Sanitize a filename to prevent path traversal.

    Strips directory components, null bytes, and control chars.
    Returns fallback if result is empty.
    """
    import os
    # Strip path components (os.path.basename handles ../ etc.)
    safe = os.path.basename(name)
    # Remove null bytes and control characters
    safe = "".join(c for c in safe if c.isprintable() and c != "\x00")
    return safe.strip() or fallback


def _extract_media_meta(message: Message) -> dict | None:
    """Extract JSON-serializable media metadata from a Pyrogram Message.

    Returns a dict with file_id, media_type, and type-specific fields,
    or None if no media. This survives PQ serialization (no Python objects).
    """
    msg_id = message.id
    if message.photo:
        return {"message_id": msg_id, "media_type": "photo",
                "file_id": message.photo.file_id,
                "width": message.photo.width, "height": message.photo.height}
    if message.document:
        return {"message_id": msg_id, "media_type": "document",
                "file_id": message.document.file_id,
                "file_name": message.document.file_name or f"{msg_id}_document",
                "mime_type": message.document.mime_type or "",
                "file_size": message.document.file_size or 0}
    if message.animation:
        return {"message_id": msg_id, "media_type": "animation",
                "file_id": message.animation.file_id}
    if message.video:
        return {"message_id": msg_id, "media_type": "video",
                "file_id": message.video.file_id,
                "file_size": message.video.file_size or 0}
    if message.sticker:
        return {"message_id": msg_id, "media_type": "sticker",
                "file_id": message.sticker.file_id,
                "emoji": message.sticker.emoji or ""}
    if message.voice:
        return {"message_id": msg_id, "media_type": "voice",
                "file_id": message.voice.file_id,
                "duration": message.voice.duration or 0}
    if message.video_note:
        return {"message_id": msg_id, "media_type": "video_note",
                "file_id": message.video_note.file_id}
    if message.audio:
        return {"message_id": msg_id, "media_type": "audio",
                "file_id": message.audio.file_id,
                "file_name": message.audio.file_name or f"{msg_id}_audio.mp3",
                "duration": message.audio.duration or 0,
                "title": message.audio.title or "",
                "performer": message.audio.performer or "",
                "mime_type": message.audio.mime_type or "",
                "file_size": message.audio.file_size or 0}
    return None


def _get_file_id(message: Message) -> str:
    """Extract file_id from a Pyrogram message's media."""
    for attr in ("photo", "document", "animation", "video", "sticker",
                 "voice", "video_note", "audio"):
        media = getattr(message, attr, None)
        if media:
            return getattr(media, "file_id", "") or ""
    return ""


async def download_media(message) -> dict | None:
    """Download media from a Pyrogram Message, metadata dict, or legacy Bot API dict.

    Accepts:
    - Pyrogram Message object (live, pre-PQ)
    - Metadata dict from _extract_media_meta() (survives PQ serialization)
    - Legacy Bot API dict (backward compat with media_cache)
    - str (broken serialization fallback — logs warning, returns None)

    Returns dict with: local_path, media_type, filename, file_size, and type-specific metadata.
    """
    if isinstance(message, Message):
        return await _download_pyrogram_media(message)
    if isinstance(message, str):
        log.warning("download_media: got str instead of Message/dict, skipping")
        return None
    if not isinstance(message, dict):
        log.warning(f"download_media: unexpected type {type(message)}, skipping")
        return None
    # Metadata dict from _extract_media_meta — has file_id + media_type at top level
    if message.get("file_id") and message.get("media_type"):
        return await _download_from_metadata(message)
    # Legacy Bot API dict format (photo/document/etc. as nested keys)
    return await _download_dict_media(message)


async def _download_pyrogram_media(message: Message) -> dict | None:
    """Download media from a Pyrogram Message object."""
    msg_id = message.id
    media_type = None
    filename = None
    meta: dict = {}

    if message.photo:
        media_type = "photo"
        filename = f"{msg_id}_photo.jpg"
        meta["width"] = message.photo.width
        meta["height"] = message.photo.height
    elif message.document:
        media_type = "document"
        filename = _safe_filename(message.document.file_name or "", f"{msg_id}_document")
        meta["mime_type"] = message.document.mime_type or ""
        if message.document.file_size and message.document.file_size > MAX_MEDIA_SIZE_MB * 1024 * 1024:
            log.warning(f"Document too large ({message.document.file_size} bytes)")
            return None
    elif message.animation:
        media_type = "animation"
        filename = f"{msg_id}_animation.mp4"
    elif message.video:
        media_type = "video"
        filename = f"{msg_id}_video.mp4"
        if message.video.file_size and message.video.file_size > MAX_MEDIA_SIZE_MB * 1024 * 1024:
            log.warning(f"Video too large ({message.video.file_size} bytes)")
            return None
    elif message.sticker:
        media_type = "sticker"
        filename = f"{msg_id}_sticker.webp"
        meta["emoji"] = message.sticker.emoji or ""
    elif message.voice:
        media_type = "voice"
        filename = f"{msg_id}_voice.ogg"
        meta["duration"] = message.voice.duration or 0
    elif message.video_note:
        media_type = "video_note"
        filename = f"{msg_id}_videonote.mp4"
    elif message.audio:
        media_type = "audio"
        filename = _safe_filename(message.audio.file_name or "", f"{msg_id}_audio.mp3")
        meta["duration"] = message.audio.duration or 0
        meta["title"] = message.audio.title or ""
        meta["performer"] = message.audio.performer or ""
        meta["mime_type"] = message.audio.mime_type or ""
        if message.audio.file_size and message.audio.file_size > MAX_MEDIA_SIZE_MB * 1024 * 1024:
            log.warning(f"Audio too large ({message.audio.file_size} bytes)")
            return None
    else:
        return None

    local_path = str(TMP_DIR / filename)
    # Verify path doesn't escape TMP_DIR
    if not Path(local_path).resolve().is_relative_to(TMP_DIR.resolve()):
        log.error(f"Path traversal blocked: {local_path}")
        return None
    try:
        await message.download(file_name=local_path)
        lp = Path(local_path)
        if not lp.exists():
            log.warning(f"Download completed but file not found: {local_path}")
            return None
        file_size = lp.stat().st_size
        log.info(f"Downloaded {media_type}: {filename} ({file_size} bytes)")
    except Exception as e:
        log.error(f"Download failed: {e}")
        return None

    return {
        "local_path": local_path,
        "media_type": media_type,
        "filename": filename,
        "file_size": file_size,
        "file_id": _get_file_id(message),
        **meta,
    }


async def _download_from_metadata(meta: dict) -> dict | None:
    """Download media using extracted metadata dict (from _extract_media_meta).

    This is the PQ-safe path: metadata is all JSON-serializable scalars.
    Uses file_id to re-download from Telegram.
    """
    file_id = meta["file_id"]
    media_type = meta["media_type"]
    msg_id = meta.get("message_id", 0)

    if media_type not in _VALID_MEDIA_TYPES:
        log.warning(f"download_from_metadata: invalid media_type={media_type!r}")
        return None

    # Build filename from metadata
    ext_map = {"photo": "jpg", "document": "", "animation": "mp4", "video": "mp4",
               "sticker": "webp", "voice": "ogg", "video_note": "mp4", "audio": "mp3"}
    if media_type == "document":
        filename = _safe_filename(meta.get("file_name", ""), f"{msg_id}_document")
    elif media_type == "audio":
        filename = _safe_filename(meta.get("file_name", ""), f"{msg_id}_audio.mp3")
    else:
        ext = ext_map.get(media_type, "bin")
        filename = f"{msg_id}_{media_type}.{ext}"

    # Check file size limits
    file_size = meta.get("file_size", 0)
    if file_size and file_size > MAX_MEDIA_SIZE_MB * 1024 * 1024:
        log.warning(f"Media too large ({file_size} bytes), skipping")
        return None

    local_path = await download_file_by_id(file_id, filename)
    if not local_path:
        return None
    actual_size = Path(local_path).stat().st_size

    # Build result with all available metadata
    result = {
        "local_path": local_path,
        "media_type": media_type,
        "filename": filename,
        "file_size": actual_size,
        "file_id": file_id,
    }
    # Carry over type-specific metadata
    for key in ("width", "height", "mime_type", "duration", "emoji",
                "title", "performer"):
        if key in meta:
            result[key] = meta[key]
    return result


async def _download_dict_media(message: dict) -> dict | None:
    """Download media from a raw dict (legacy Bot API format).

    Handles media_cache entries stored before the userbot migration.
    """
    msg_id = message.get("message_id", 0)
    file_id = None
    media_type = None
    filename = None

    photo = message.get("photo")
    document = message.get("document")
    sticker = message.get("sticker")
    voice = message.get("voice")
    video_note = message.get("video_note")
    video = message.get("video")
    animation = message.get("animation")

    if photo:
        best = photo[-1] if isinstance(photo, list) else photo
        file_id = best.get("file_id") if isinstance(best, dict) else None
        media_type = "photo"
        filename = f"{msg_id}_photo.jpg"
    elif document:
        file_id = document.get("file_id")
        media_type = "document"
        filename = _safe_filename(document.get("file_name", ""), f"{msg_id}_document")
    elif animation:
        file_id = animation.get("file_id")
        media_type = "animation"
        filename = f"{msg_id}_animation.mp4"
    elif video:
        file_id = video.get("file_id")
        media_type = "video"
        filename = f"{msg_id}_video.mp4"
    elif sticker:
        file_id = sticker.get("file_id")
        media_type = "sticker"
        filename = f"{msg_id}_sticker.webp"
    elif voice:
        file_id = voice.get("file_id")
        media_type = "voice"
        filename = f"{msg_id}_voice.ogg"
    elif video_note:
        file_id = video_note.get("file_id")
        media_type = "video_note"
        filename = f"{msg_id}_videonote.mp4"
    elif message.get("audio"):
        audio = message["audio"]
        file_id = audio.get("file_id")
        media_type = "audio"
        filename = _safe_filename(audio.get("file_name", ""), f"{msg_id}_audio.mp3")
    else:
        return None

    if not file_id:
        return None

    local_path = await download_file_by_id(file_id, filename)
    if not local_path:
        return None
    file_size = Path(local_path).stat().st_size
    return {
        "local_path": local_path,
        "media_type": media_type,
        "filename": filename,
        "file_size": file_size,
        "file_id": file_id,
    }


async def download_file_by_id(file_id: str, filename: str = "refetched") -> str | None:
    """Re-download a file from Telegram by file_id.

    Uses Pyrogram's native file download.
    Returns local path on success, None on failure.
    """
    # Lazy import to avoid circular dependency (telegram.py imports from tg_media.py)
    from integrations.telegram import get_client

    client = await get_client()
    filename = _safe_filename(filename, "refetched")
    local_path = str(TMP_DIR / filename)
    # Verify path doesn't escape TMP_DIR
    if not Path(local_path).resolve().is_relative_to(TMP_DIR.resolve()):
        log.error(f"Path traversal blocked: {local_path}")
        return None
    try:
        downloaded = await client.download_media(file_id, file_name=local_path)
        if not downloaded:
            log.warning(f"TG re-fetch returned nothing for file_id: {file_id[:20]}...")
            return None
        file_size = Path(local_path).stat().st_size
        log.info(f"TG re-fetch successful: {filename} ({file_size} bytes)")
        return local_path
    except Exception as e:
        log.error(f"TG re-fetch failed: {e}")
        return None
