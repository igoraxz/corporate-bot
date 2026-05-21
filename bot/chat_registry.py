# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Unified Chat Registry — single source of truth for all registered chats.

Replaces the legacy 3-registry design (TG_ALLOWED_USERS + TG_ALLOWED_CHATS +
TG_EXTERNAL_CHATS). Every registered chat/topic has a mode (comms style):

    active_plus  — full UX: streaming, placeholders (default for registered)
    active       — bot participates, no streaming (multi-party / 3P chats)
    silent       — listen only, store messages (monitoring)

Permissions are RBAC-managed, not mode-based. The registry only decides:
"does the bot talk here, and with what UX style?"

Root admin stays in .env (ADMIN_USERS) — the registry does NOT handle auth tiers.

Keys: "telegram:<chat_id>" or "telegram:<chat_id>:<thread_id>" for forum topics.
"""

import logging
from pathlib import Path
from typing import Any

from bot.config_store import JsonConfigStore

log = logging.getLogger(__name__)

# Valid modes (comms style only — permissions are RBAC)
VALID_MODES = {"active_plus", "active", "silent"}
DEFAULT_MODE = "active_plus"

_registry: JsonConfigStore | None = None


def init(config_dir: Path) -> None:
    """Initialize the chat registry. Called once from config.py."""
    global _registry
    _registry = JsonConfigStore(
        "chat_registry",
        config_dir / "chat_registry.json",
    )
    log.info(f"Chat registry loaded: {len(_registry)} entries")


def _store() -> JsonConfigStore:
    if _registry is None:
        raise RuntimeError("chat_registry.init() not called yet")
    return _registry


# ============================================================
# CRUD
# ============================================================

import re
# Telegram: source:numeric_id or source:numeric_id:thread_id
# Teams: source:uuid-or-conversation-ref (non-numeric, may contain colons/hyphens)
_KEY_PATTERN = re.compile(r"^telegram:-?\d+(:-?\d+)?$|^teams:[^\s]+$")


def register(chat_key: str, title: str = "", mode: str = DEFAULT_MODE,
             parent: str = "", **extra: Any) -> dict:
    """Register a chat/topic. Overwrites if exists.

    Args:
        chat_key: "telegram:<chat_id>" or "telegram:<chat_id>:<thread_id>"
        title: Display name
        mode: active_plus | active | silent
        parent: Parent chat_key for forum topics (e.g. "telegram:-100123")
        **extra: Additional metadata (custom_instructions, etc.)
    """
    if not _KEY_PATTERN.match(chat_key):
        raise ValueError(f"Invalid chat_key format: {chat_key!r}. Expected: telegram:<id> or telegram:<id>:<thread>")
    if mode not in VALID_MODES:
        raise ValueError(f"Invalid mode: {mode}. Must be one of {VALID_MODES}")
    from datetime import datetime, timezone
    entry: dict[str, Any] = {
        "title": title,
        "mode": mode,
        "added_at": datetime.now(timezone.utc).isoformat(),
    }
    if parent:
        entry["parent"] = parent
    entry.update(extra)
    _store().set(chat_key, entry)
    log.info(f"Chat registered: {chat_key} mode={mode} title={title!r}")
    return entry


def unregister(chat_key: str) -> bool:
    """Remove a chat from the registry. Returns True if it existed."""
    store = _store()
    if chat_key in store:
        store.delete(chat_key)
        log.info(f"Chat unregistered: {chat_key}")
        return True
    return False


def get(chat_key: str) -> dict | None:
    """Get a chat entry. Returns None if not registered."""
    return _store().get(chat_key)


# CB-5: Alias for callers that expect get_entry (clearer than shadowing builtin get)
get_entry = get


def get_mode(chat_key: str) -> str | None:
    """Get the mode for a chat. Returns None if not registered.

    For forum topics, falls back to parent group mode.
    """
    entry = get(chat_key)
    if entry:
        return entry.get("mode", DEFAULT_MODE)
    # Topic fallback: check parent group
    parts = chat_key.split(":")
    if len(parts) == 3:
        parent_key = f"{parts[0]}:{parts[1]}"
        parent_entry = get(parent_key)
        if parent_entry:
            return parent_entry.get("mode", DEFAULT_MODE)
    return None


def set_mode(chat_key: str, mode: str) -> None:
    """Update the mode for an existing chat."""
    if mode not in VALID_MODES:
        raise ValueError(f"Invalid mode: {mode}. Must be one of {VALID_MODES}")
    store = _store()
    entry = store.get(chat_key)
    if entry is None:
        raise KeyError(f"Chat not registered: {chat_key}")
    entry["mode"] = mode
    store.set(chat_key, entry)


def is_registered(chat_key: str) -> bool:
    """Check if a chat is registered (directly or via parent group for topics)."""
    if chat_key in _store():
        return True
    # Topic fallback
    parts = chat_key.split(":")
    if len(parts) == 3:
        parent_key = f"{parts[0]}:{parts[1]}"
        return parent_key in _store()
    return False


def all_entries() -> dict[str, dict]:
    """Return a snapshot of all registered chats."""
    return _store().all()


def update_entry(chat_key: str, **fields) -> dict:
    """Update specific fields on an existing chat entry.

    Preserves all existing fields, only updates those provided.
    Validates write_domain and reference_domains if present.
    """
    store = _store()
    entry = store.get(chat_key)
    if entry is None:
        raise KeyError(f"Chat not registered: {chat_key}")

    # Validate domain fields if provided
    if "write_domain" in fields:
        wd = fields["write_domain"]
        if wd and not _validate_domain_field(wd):
            raise ValueError(f"Invalid write_domain: {wd!r}")
    if "reference_domains" in fields:
        refs = fields["reference_domains"]
        if isinstance(refs, str):
            refs = [r.strip() for r in refs.split(",") if r.strip()]
            fields["reference_domains"] = refs
        for ref in refs:
            if not _validate_domain_field(ref):
                raise ValueError(f"Invalid reference_domain: {ref!r}")
    if "mode" in fields:
        if fields["mode"] not in VALID_MODES:
            raise ValueError(f"Invalid mode: {fields['mode']}")

    entry.update(fields)
    store.set(chat_key, entry)
    log.info(f"Chat updated: {chat_key} fields={list(fields.keys())}")
    return entry


def _validate_domain_field(domain_id: str) -> bool:
    """Validate a domain_id format (alphanumeric + hyphens/underscores)."""
    import re
    return bool(re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", domain_id))


# ============================================================
# AUTH CHECK (single entry point)
# ============================================================

def is_authorized(source: str, chat_id: int | str,
                  user_id: int | str | None = None,
                  thread_id: int | str | None = None) -> bool:
    """Check if a message from this chat/user should be processed.

    Returns True if:
    - The specific topic or chat is in the registry, OR
    - The user is in ADMIN_USERS (.env root admin)

    IMPORTANT: For forum topic messages (thread_id set), the topic key
    source:chat_id:thread_id must be explicitly registered. Parent group
    registration does NOT grant access to all topics — unregistered topics
    are unknown entities with no permissions (full ignore).
    Non-topic messages (thread_id=None) check the group key as before.
    """
    store = _store()
    # Forum topic messages: require EXPLICIT topic registration.
    # Parent group registration does NOT authorize unregistered topics.
    # Admin role does NOT override — topics must be registered to be processed.
    if thread_id is not None:
        topic_key = f"{source}:{chat_id}:{thread_id}"
        if topic_key in store:
            return True
        # Unregistered topic = ignore, regardless of user role.
        # Admin can register topics via manage_chat, but until then: silent drop.
        return False

    # Non-topic messages: group key check
    group_key = f"{source}:{chat_id}"
    if group_key in store:
        return True
    # Fallback: root admin always authorized in non-topic contexts (DMs, unregistered groups)
    if user_id is not None:
        from config import is_admin_user
        if is_admin_user(source, str(user_id)):
            return True
    return False


# ============================================================
# FORUM TOPIC NAME TRACKING (CB-130)
# ============================================================
# Telegram Bot API has no listForumTopics endpoint. We track topic names
# from forum_topic_created and forum_topic_edited service messages.
# This is a NAME CACHE only — topics are NOT auto-registered (activated).
# Admin uses manage_chat to register topics they want the bot active in.

_topic_names: JsonConfigStore | None = None


def _topic_store() -> JsonConfigStore:
    global _topic_names
    if _topic_names is None:
        from config import DATA_DIR
        _topic_names = JsonConfigStore(
            "topic_names",
            DATA_DIR / "config" / "topic_names.json",
        )
    return _topic_names


def track_topic_name(source: str, chat_id: int | str, thread_id: int | str,
                     name: str, *, icon_color: int | None = None) -> None:
    """Record a forum topic's name (from service message). Does NOT register it."""
    key = f"{source}:{chat_id}:{thread_id}"
    store = _topic_store()
    entry = {"name": name, "chat_id": str(chat_id), "thread_id": str(thread_id)}
    if icon_color is not None:
        entry["icon_color"] = icon_color
    store.set(key, entry)
    log.info("Topic name tracked: %s → %s", key, name)


def get_topic_name(source: str, chat_id: int | str, thread_id: int | str) -> str | None:
    """Get a tracked topic name (None if not seen yet)."""
    key = f"{source}:{chat_id}:{thread_id}"
    entry = _topic_store().get(key)
    return entry.get("name") if entry else None


def list_topic_names(source: str, chat_id: int | str) -> dict[str, str]:
    """List all known topic names for a group. Returns {thread_id: name}."""
    prefix = f"{source}:{chat_id}:"
    result = {}
    for key, entry in _topic_store().all().items():
        if key.startswith(prefix):
            tid = key[len(prefix):]
            result[tid] = entry.get("name", "")
    return result


# ============================================================
# BOOT MIGRATION — populate from legacy registries
# ============================================================

def migrate_from_legacy() -> int:
    """Populate the registry from legacy TG_ALLOWED_USERS + TG_EXTERNAL_CHATS.

    Called once at startup. Only adds entries that don't already exist
    (idempotent — safe to run on every boot).

    Returns the number of new entries added.
    """
    store = _store()
    added = 0

    # 1. Migrate TG_ALLOWED_USERS (admin DMs → active_plus)
    try:
        from config import TG_ALLOWED_USERS
        for uid, name in TG_ALLOWED_USERS.items():
            key = f"telegram:{uid}"
            if key not in store:
                register(key, title=name, mode="active_plus")
                added += 1
    except Exception as e:
        log.warning(f"Legacy migration: TG_ALLOWED_USERS failed: {e}")

    # 2. Migrate TG_CHAT_ID (notification channel → active_plus)
    try:
        from config import TG_CHAT_ID
        if TG_CHAT_ID:
            key = f"telegram:{TG_CHAT_ID}"
            if key not in store:
                register(key, title="Notification Channel", mode="active_plus")
                added += 1
    except Exception as e:
        log.warning(f"Legacy migration: TG_CHAT_ID failed: {e}")

    # 3. Migrate TG_EXTERNAL_CHATS (keep existing modes)
    try:
        from config import TG_EXTERNAL_CHATS
        for cid in TG_EXTERNAL_CHATS:
            info = TG_EXTERNAL_CHATS.get(cid, {})
            key = f"telegram:{cid}"
            if key not in store:
                mode = info.get("mode", "silent")
                if mode not in VALID_MODES:
                    mode = "silent"  # Unknown mode → default to silent
                register(
                    key,
                    title=info.get("title", str(cid)),
                    mode=mode,
                    custom_instructions=info.get("custom_instructions", ""),
                )
                added += 1
    except Exception as e:
        log.warning(f"Legacy migration: TG_EXTERNAL_CHATS failed: {e}")

    if added:
        log.info(f"Chat registry: migrated {added} entries from legacy registries")
    return added
