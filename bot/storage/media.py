# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Media description orchestration — background tasks for auto-describing cached media.

Extracted from main.py to avoid circular imports (mcp_tools.py needs to call
generate_media_description, but main.py imports mcp_tools.py).
"""

import asyncio
import logging

from config import MEDIA_DESCRIPTION_ENABLED, MEDIA_DESCRIBE_MAX_CONCURRENT

log = logging.getLogger(__name__)

# Semaphore to limit concurrent Gemini description calls (prevents rate limit errors
# when many media files arrive at once, e.g. 50 forwarded photos)
_describe_semaphore = asyncio.Semaphore(MEDIA_DESCRIBE_MAX_CONCURRENT)


async def generate_media_description(media_cache_id: int, file_path: str, filename: str):
    """Background task: generate AI description for a cached media file.

    Uses Gemini Flash to analyze any supported media type (images, PDFs, video,
    audio, text, office docs) and stores the description in media_cache.description
    (FTS5 index auto-updated via trigger).

    Rate-limited by semaphore to max MEDIA_DESCRIBE_MAX_CONCURRENT concurrent calls.
    """
    if not MEDIA_DESCRIPTION_ENABLED:
        return
    async with _describe_semaphore:
        try:
            from integrations.gemini import describe_media
            from bot.storage.memory import update_media_description
            result = await describe_media(file_path)
            if result.get("description"):
                await update_media_description(media_cache_id, result["description"])
                log.info(f"Auto-described media id={media_cache_id} ({filename}): {len(result['description'])} chars")
            elif result.get("error"):
                log.warning(f"Media description failed for id={media_cache_id} ({filename}): {result['error']}")
        except Exception as e:
            log.error(f"Media description error for id={media_cache_id}: {e}")
