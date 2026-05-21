# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Language detection utilities for incoming messages.

Extracted from bot/agent.py (Phase 0 refactor).
"""

import logging
import re

log = logging.getLogger(__name__)

# Language code -> human-readable name (for language directive in volatile header)
LANG_NAMES = {"en": "English", "ru": "Russian", "es": "Spanish", "fr": "French",
              "de": "German", "it": "Italian", "pt": "Portuguese", "zh-cn": "Chinese",
              "ja": "Japanese", "ko": "Korean", "ar": "Arabic", "tr": "Turkish"}

# Built lazily on first use (depends on PARENT_NAMES from config)
_silence_patterns_compiled: list | None = None


def _detect_language(text: str) -> tuple[str, float]:
    """Detect language of text using langdetect. Returns (ISO 639-1 code, confidence 0-1).
    Returns ("", 0.0) if detection fails or text is empty."""
    if not text or not text.strip():
        return ("", 0.0)
    # Strip common bot-injected prefixes before detection
    clean = re.sub(r"^\[Replying to .*?\]\s*", "", text, flags=re.DOTALL)
    if not clean.strip():
        return ("", 0.0)
    try:
        from langdetect import detect_langs, DetectorFactory
        DetectorFactory.seed = 0
        results = detect_langs(clean[:500])
        if not results:
            return ("", 0.0)
        top = results[0]
        return (top.lang, top.prob)
    except Exception:
        return ("", 0.0)


async def _detect_language_with_fallback(
    text: str,
    user_id: str | int,
    chat_id: str | int,
    source: str,
    short_threshold: int = 25,
    confidence_threshold: float = 0.85,
) -> str:
    """Detect language with fallback to recent user messages when current text is ambiguous.

    If the current message is too short or detection confidence is low, pulls the last 10
    messages from the same user in the same chat that are themselves long enough to carry
    meaningful language signal (>= short_threshold chars). If no qualifying recent messages
    exist, returns "" to trigger the English default in agent.py.
    Returns ISO 639-1 code, or "" if detection is inconclusive.
    """
    lang, conf = _detect_language(text)
    clean_len = len(re.sub(r"^\[Replying to .*?\]\s*", "", text, flags=re.DOTALL).strip())

    if lang and conf >= confidence_threshold and clean_len >= short_threshold:
        # High-confidence detection from current message -- use it directly
        return lang

    # Low confidence or short text -- build fallback corpus from recent messages by this user.
    # Only include messages that are long enough to carry real language signal (>= short_threshold).
    # Without this filter, short ambiguous words like "test" (valid in EN/FR/DE/ET) get repeated
    # in the fallback corpus and langdetect misclassifies them (e.g. "test test test..." → French).
    try:
        from bot.storage.memory import open_db
        uid = str(user_id) if user_id else ""
        cid = str(chat_id) if chat_id else ""
        if uid:
            async with open_db() as db:
                rows = await db.execute_fetchall(
                    "SELECT text FROM messages "
                    "WHERE user_id = ? AND chat_id = ? AND source = ? AND role = 'user' "
                    "AND text IS NOT NULL AND length(text) >= ? "
                    "ORDER BY id DESC LIMIT 10",
                    (uid, cid, source, short_threshold),
                )
            if rows:
                recent_texts = [r[0] for r in rows if r[0]]
                fallback_corpus = " ".join(reversed(recent_texts)) + " " + text
                lang2, conf2 = _detect_language(fallback_corpus)
                log.debug(
                    "Language detection: text=%r orig=(%s,%.2f) fallback=(%s,%.2f) recent_msgs=%d",
                    text[:40], lang, conf, lang2, conf2, len(recent_texts),
                )
                if lang2 and conf2 >= confidence_threshold:
                    return lang2
    except Exception as e:
        log.debug("Language fallback DB query failed: %s", e)

    # No reliable signal from either direct or fallback detection — return ""
    # which triggers the English default in agent.py.
    log.debug(
        "Language detection inconclusive: text=%r orig=(%s,%.2f), defaulting to English",
        text[:40], lang, conf,
    )
    return ""


def _get_silence_patterns() -> list:
    """Get compiled silence patterns (built once, cached)."""
    global _silence_patterns_compiled
    if _silence_patterns_compiled is None:
        from config import PARENT_NAMES
        _name_alt = "|".join(n.lower() for n in PARENT_NAMES) if PARENT_NAMES else "them"
        _silence_patterns_compiled = [
            rf"directed at (?:her|him|them|{_name_alt})",
            rf"tagging.*(?:{_name_alt}|not (?:at|for) the bot)",
            r"addressed to (?:her|him|them|each other)",
            r"not (?:at|for|directed at) the bot",
            rf"message is (?:for|to|between) (?:{_name_alt})",
            r"(?:don.t|shouldn.t|should not|won.t|will not) (?:respond|reply|intervene)",
            r"no (?:action|response|reply) (?:needed|required|necessary)",
        ]
    return _silence_patterns_compiled
