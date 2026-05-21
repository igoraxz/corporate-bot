# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Per-chat override management: model, OAuth profile, and effort level.

Extracted from config.py (Phase 0 reorganization). All public symbols are
re-exported by config.py so existing ``from config import ...`` continues
to work unchanged.

Dependencies on config.py are imported lazily (inside functions) or via
module-level late-binding helpers to avoid circular imports.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bot.config_store import JsonConfigStore

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Late-binding helpers — resolved once on first call, cached thereafter.
# This avoids importing config at module level (circular).
# ---------------------------------------------------------------------------

_cfg_cache: dict = {}

# Names that are mutable at runtime and must NOT be cached.
_MUTABLE_ATTRS = frozenset({"_model_primary"})


def _cfg(name: str):
    """Lazily fetch a config attribute.

    Immutable constants (DATA_DIR, ALLOWED_MODELS, ...) are cached after
    first access.  Mutable globals (_model_primary) are always read live
    from the config module so callers see runtime changes.
    """
    if name in _MUTABLE_ATTRS:
        import config as _c
        return getattr(_c, name)
    if name not in _cfg_cache:
        import config as _c
        _cfg_cache[name] = getattr(_c, name)
    return _cfg_cache[name]


# ===================================================================
# CHAT KEY NORMALIZATION
# ===================================================================
# MCP tools use session keys like "telegram:12345" while lightweight
# commands historically used "tg:12345". Normalize to "telegram:" for
# consistent lookups across all code paths.


def _normalize_chat_key(key: str) -> str:
    """Normalize tg: → telegram: for consistent key lookups."""
    if key.startswith("tg:"):
        return "telegram:" + key[3:]
    return key


# ===================================================================
# MODEL OVERRIDES — per-chat model selection (backed by JsonConfigStore)
# ===================================================================

_model_store: JsonConfigStore | None = None  # created in init()


def _topic_fallback_get(store: "JsonConfigStore", key: str, default=None):
    """Lookup with topic → group fallback for composite keys (source:chat:thread)."""
    result = store.get(key)
    if result is None:
        parts = key.split(":")
        if len(parts) == 3:
            parent_key = f"{parts[0]}:{parts[1]}"
            result = store.get(parent_key)
    return result if result is not None else default


def get_model_for_chat(chat_key: str) -> str:
    """Get model for a specific chat (override or global default).

    Falls back from topic to group for forum topics.
    """
    if _model_store is None:
        return _cfg("_model_primary")
    return _topic_fallback_get(_model_store, _normalize_chat_key(chat_key), _cfg("_model_primary"))


def get_model_override_only(chat_key: str) -> str | None:
    """Return per-chat model override only. Returns None if no override is set.

    Use this in agent.py instead of get_model_for_chat so that _build_options
    can properly fall through to the harness model when no explicit override exists.
    Resolution chain: per-chat override → harness model → global default.
    Falls back from topic to group for forum topics.
    """
    if _model_store is None:
        return None
    return _topic_fallback_get(_model_store, _normalize_chat_key(chat_key))


def set_model_for_chat(chat_key: str, model: str) -> str:
    """Set model override for a specific chat. Returns the new model."""
    if _model_store is None:
        raise RuntimeError("config_overrides.init() not called yet")
    allowed = _cfg("ALLOWED_MODELS")
    if model not in allowed:
        raise ValueError(f"Unknown model: {model}. Allowed: {', '.join(sorted(allowed))}")
    _model_store.set(_normalize_chat_key(chat_key), model)
    return model


def clear_model_for_chat(chat_key: str) -> None:
    """Remove per-chat override, reverting to global default."""
    if _model_store is None:
        return
    _model_store.delete(_normalize_chat_key(chat_key))


def get_chat_model_overrides() -> dict[str, str]:
    """Get all per-chat overrides (for status display)."""
    if _model_store is None:
        return {}
    return _model_store.all()


# ===================================================================
# OAUTH PROFILE OVERRIDES — per-chat OAuth profile selection
# ===================================================================

_OAUTH_LABEL_RE_STR = r"^[a-z0-9_-]{1,32}$"
_OAUTH_LABEL_RE = re.compile(_OAUTH_LABEL_RE_STR)


def _oauth_default_label_file() -> Path:
    """Path to the persisted default OAuth label."""
    return _cfg("CONFIG_DIR") / "oauth_default_label.json"


_default_label_cache: str | None = None  # populated by init(), updated by set_default_oauth_label()


def _load_default_label_from_disk() -> str:
    """Read persisted default label from disk, fall back to env var."""
    try:
        f = _oauth_default_label_file()
        if f.exists():
            data = json.loads(f.read_text())
            label = data.get("label", "")
            if label and validate_oauth_label(label):
                return label
    except Exception:
        pass
    return _cfg("OAUTH_DEFAULT_LABEL")


def get_default_oauth_label() -> str:
    """Get the current default OAuth label (cached in memory, hot-path safe)."""
    if _default_label_cache is not None:
        return _default_label_cache
    # Fallback: cache not yet populated (pre-init); read from disk
    return _load_default_label_from_disk()


def set_default_oauth_label(label: str) -> str:
    """Set the default OAuth label.  Persists to disk + updates cache.  Returns the label."""
    global _default_label_cache
    if not validate_oauth_label(label):
        raise ValueError(f"Invalid label: {label}. Must match {_OAUTH_LABEL_RE_STR}")
    path = get_oauth_profile_path(label)
    if not path.exists():
        raise ValueError(f"Profile '{label}' not found. Add it first via manage_oauth.")
    # Atomic write — tmp+rename to prevent corruption on crash
    _target = _oauth_default_label_file()
    _tmp = _target.with_suffix(".tmp")
    _tmp.write_text(json.dumps({"label": label}))
    _tmp.rename(_target)
    _default_label_cache = label
    # Clean up chat overrides pointing to the new default (now redundant).
    # Use per-key delete() for atomic safety — no read-all/replace-all race.
    if _oauth_store is not None:
        stale_keys = [k for k, v in _oauth_store.items() if v == label]
        for k in stale_keys:
            _oauth_store.delete(k)
        if stale_keys:
            log.info("Cleared %d redundant OAuth override(s) after setting default to '%s'", len(stale_keys), label)
    log.info("OAuth default label set to '%s'", label)
    return label


def is_default_oauth(label: str) -> bool:
    """Check if a label is the current default (or the literal word 'default')."""
    return label in ("default", get_default_oauth_label())


def validate_oauth_label(label: str) -> bool:
    """Check if an OAuth profile label is valid."""
    return bool(_OAUTH_LABEL_RE.match(label))


_oauth_store: JsonConfigStore | None = None  # created in init()


def get_oauth_for_chat(chat_key: str) -> str:
    """Get OAuth profile label for a chat.  Returns the default label if no override.

    Falls back from topic to group for forum topics.
    """
    if _oauth_store is None:
        return get_default_oauth_label()
    return _topic_fallback_get(_oauth_store, _normalize_chat_key(chat_key), get_default_oauth_label())


def set_oauth_for_chat(chat_key: str, label: str) -> str:
    """Set OAuth profile override for a chat. Returns the label."""
    if _oauth_store is None:
        raise RuntimeError("config_overrides.init() not called yet")
    nkey = _normalize_chat_key(chat_key)
    if not is_default_oauth(label) and not validate_oauth_label(label):
        raise ValueError(f"Invalid OAuth label: {label}. Must match {_OAUTH_LABEL_RE_STR}")
    if is_default_oauth(label):
        _oauth_store.delete(nkey)
    else:
        # Verify profile exists
        if not get_oauth_profile_path(label).exists():
            raise ValueError(f"OAuth profile '{label}' not found. Use manage_oauth to add it first.")
        _oauth_store.set(nkey, label)
    return label


def clear_oauth_for_chat(chat_key: str) -> None:
    """Remove per-chat OAuth override, reverting to default."""
    if _oauth_store is None:
        return
    _oauth_store.delete(_normalize_chat_key(chat_key))


def get_chat_oauth_overrides() -> dict[str, str]:
    """Get all per-chat OAuth overrides (for status display)."""
    if _oauth_store is None:
        return {}
    return _oauth_store.all()


def get_oauth_profile_path(label: str) -> Path:
    """Get the file path for an OAuth profile."""
    return _cfg("OAUTH_PROFILES_DIR") / f"{label}.json"


def list_oauth_profiles() -> list[dict]:
    """List all available OAuth profiles with metadata.

    All profiles are labeled files in OAUTH_PROFILES_DIR.  One is flagged as default.
    """
    default_label = get_default_oauth_label()
    profiles_dir = _cfg("OAUTH_PROFILES_DIR")
    profiles: list[dict] = []
    if profiles_dir.exists():
        for f in sorted(profiles_dir.glob("*.json")):
            label = f.stem
            if not validate_oauth_label(label):
                continue
            entry: dict[str, Any] = {
                "label": label,
                "source": str(f),
                "exists": True,
                "is_default": label == default_label,
            }
            try:
                data = json.loads(f.read_text())
                token = data.get("accessToken", "")
                entry["token_prefix"] = token[:8] + "..." if len(token) > 8 else "(empty)"
                entry["added_at"] = data.get("addedAt", "")
            except Exception:
                entry["error"] = "parse_failed"
            profiles.append(entry)
    # Sort: default first, then alphabetical
    profiles.sort(key=lambda p: (not p.get("is_default", False), p["label"]))
    return profiles


def add_oauth_profile(label: str, token: str) -> dict:
    """Add or update an OAuth profile. Returns profile metadata."""
    if not validate_oauth_label(label):
        raise ValueError(f"Invalid label: {label}. Must match {_OAUTH_LABEL_RE_STR}")
    if label == "default":
        raise ValueError("'default' is a reserved keyword and cannot be used as a profile label.")
    if not token or not token.startswith("sk-ant-"):
        raise ValueError("Token must start with 'sk-ant-'")
    now = datetime.now(timezone.utc)
    profile_data = {
        "accessToken": token,
        "expiresAt": (now + timedelta(days=365)).isoformat(),
        "label": label,
        "addedAt": now.isoformat(),
    }
    path = get_oauth_profile_path(label)
    # M3 fix: atomic write (tmp+rename) to prevent corruption on crash
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(profile_data, indent=2))
    try:
        os.chmod(str(tmp), 0o600)  # M4 audit: restrict token file permissions
    except OSError:
        pass
    tmp.rename(path)
    log.info("OAuth profile '%s' saved to %s", label, path)
    # Auto-set as default if this is the first profile (fresh deployment)
    profiles_dir = path.parent
    existing = [f for f in profiles_dir.glob("*.json") if f.name != f"{label}.json"]
    if not existing:
        set_default_oauth_label(label)
        log.info("First OAuth profile — auto-set '%s' as default", label)
    # Register in credential_store for RBAC tracking (metadata only — token stays in file).
    # Fire-and-forget: we're in a sync function but credential registration is async.
    # Use create_task if event loop is running, skip if not (startup context).
    try:
        import asyncio
        from bot.auth.credentials import register_credential_metadata
        loop = asyncio.get_running_loop()
        async def _register_cred():
            try:
                await register_credential_metadata(
                    cred_id=f"claude:{label}",
                    cred_type="claude-sdk",
                    account=label,
                    scope="system",
                    file_path=str(path),
                    extra_metadata={"token_prefix": token[:8], "registered_via": "add_oauth_profile"},
                )
            except Exception as _e:
                log.warning("Failed to register Claude OAuth '%s' in credential_store: %s", label, _e)
        loop.create_task(_register_cred())
    except RuntimeError:
        pass  # no running event loop (startup context) — skip registration
    except Exception as _cred_err:
        log.warning("Claude OAuth credential registration failed: %s", _cred_err)

    return {"label": label, "path": str(path), "token_prefix": token[:12] + "..."}


def remove_oauth_profile(label: str) -> bool:
    """Remove an OAuth profile. Returns True if removed. Cannot remove the current default."""
    if is_default_oauth(label):
        raise ValueError(f"Cannot remove the default profile '{get_default_oauth_label()}'. Change default first.")
    path = get_oauth_profile_path(label)
    if path.exists():
        path.unlink()
        # Remove any chat overrides pointing to this profile.
        # Use per-key delete() for atomic safety — no read-all/replace-all race.
        stale_keys: list[str] = []
        if _oauth_store is not None:
            stale_keys = [k for k, v in _oauth_store.items() if v == label]
            for k in stale_keys:
                _oauth_store.delete(k)
        log.info("OAuth profile '%s' removed (cleared %d chat override(s))", label, len(stale_keys))
        return True
    return False


def get_oauth_token(label: str) -> str | None:
    """Read the access token from an OAuth profile.  Works for ALL labels (including default)."""
    # Resolve "default" keyword to the actual label
    effective = get_default_oauth_label() if label == "default" else label
    path = get_oauth_profile_path(effective)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return data.get("accessToken")
    except Exception as e:
        log.warning("Failed to read OAuth profile '%s': %s", effective, e)
        return None


# ===================================================================
# EFFORT LEVEL OVERRIDES
# ===================================================================

ALLOWED_EFFORTS: set[str] = {"low", "medium", "high", "max"}
_effort_level: str = os.environ.get("EFFORT_LEVEL", "medium")


def get_effort_level() -> str:
    """Get the current effort level (runtime-switchable)."""
    return _effort_level


def set_effort_level(level: str) -> str:
    """Switch the effort level at runtime. Returns the new level."""
    global _effort_level
    level = level.lower().strip()
    if level not in ALLOWED_EFFORTS:
        raise ValueError(f"Unknown effort: {level}. Allowed: {', '.join(sorted(ALLOWED_EFFORTS))}")
    _effort_level = level
    return level


# ===================================================================
# MODULE INITIALIZATION
# ===================================================================

def init() -> None:
    """Load persisted overrides from disk via JsonConfigStore.

    Called once from config.py after all config-level constants are set.
    Separated from module import to avoid circular-import issues (the
    load helpers need DATA_DIR / ALLOWED_MODELS / OAUTH_DEFAULT_LABEL
    which live in config.py).
    """
    global _model_store, _oauth_store, _default_label_cache

    config_dir = _cfg("CONFIG_DIR")

    # --- Model overrides store ---
    _model_store = JsonConfigStore("model_overrides", config_dir / "model_overrides.json")
    # Validate: drop entries with invalid models, normalize tg: keys
    _cleanup_model_store()

    # --- OAuth overrides store ---
    _oauth_store = JsonConfigStore("oauth_overrides", config_dir / "oauth_overrides.json")

    # Populate default label cache from disk (avoids file I/O on every query)
    _default_label_cache = _load_default_label_from_disk()

    # Validate: drop entries with invalid labels, normalize tg: keys,
    # remove entries pointing to the current default (redundant)
    _cleanup_oauth_store()


def _cleanup_model_store() -> None:
    """Post-load cleanup: normalize keys and drop invalid model entries.

    Runs at startup (single-threaded). Uses per-key set/delete for
    consistency with the no-replace-all pattern.
    """
    data = _model_store.all()
    if not data:
        return
    allowed = _cfg("ALLOWED_MODELS")
    dropped = 0
    normalized = 0
    for k, v in list(data.items()):
        nk = _normalize_chat_key(k)
        if v not in allowed:
            _model_store.delete(k)
            dropped += 1
        elif nk != k:
            _model_store.delete(k)
            _model_store.set(nk, v)
            normalized += 1
    if dropped:
        log.warning("Dropped %d invalid model override(s)", dropped)
    if normalized:
        log.info("Normalized %d model override key(s)", normalized)


def _cleanup_oauth_store() -> None:
    """Post-load cleanup: normalize keys, drop invalid labels, remove default-pointing entries.

    Runs at startup (single-threaded). Uses per-key set/delete for
    consistency with the no-replace-all pattern.
    """
    data = _oauth_store.all()
    if not data:
        return
    default_label = _default_label_cache or get_default_oauth_label()
    dropped = 0
    normalized = 0
    redundant = 0
    for k, v in list(data.items()):
        nk = _normalize_chat_key(k)
        if not isinstance(v, str) or not (is_default_oauth(v) or validate_oauth_label(v)):
            _oauth_store.delete(k)
            dropped += 1
        elif v == default_label:
            _oauth_store.delete(k)
            redundant += 1
        elif nk != k:
            _oauth_store.delete(k)
            _oauth_store.set(nk, v)
            normalized += 1
    if dropped:
        log.warning("Dropped %d invalid OAuth override(s)", dropped)
    if normalized:
        log.info("Normalized %d OAuth override key(s)", normalized)
    if redundant:
        log.info("Boot cleanup: removed %d redundant OAuth overrides (pointed to default '%s')", redundant, default_label)
