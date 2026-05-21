# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Per-user workstation configuration for desktop relay.

Stores employee Mac workstation connection details (host, user, TOTP secret,
SSH key) in a JSON config file, keyed by user_id. TOTP secrets are encrypted
at rest using Fernet (same key as token_vault).

Design:
- Each employee configures their own Mac via `/workstation setup` in DM
- Replaces the old bot-wide DESKTOP_ENABLED/HOST/USER env vars
- Global env vars serve as fallback/default for backward compat
- TOTP secret encrypted at rest, never logged or returned in plain text
"""

import logging
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from bot.config_store import JsonConfigStore

log = logging.getLogger(__name__)

__all__ = [
    "WorkstationConfig",
    "get_workstation",
    "set_workstation",
    "delete_workstation",
    "list_workstations",
    "is_workstation_enabled",
    "get_effective_config",
    "set_authenticated",
    "check_authenticated",
    "get_workstation_for_chat",
    "GLOBAL_DESKTOP_ENABLED",
    "_encrypt_secret",
    "_SAFE_HOSTNAME",
    "_SAFE_USERNAME",
]

# Input validation for Mac host/user — prevent SSH option injection
import re as _re
_SAFE_HOSTNAME = _re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9._-]{0,253}[a-zA-Z0-9])?$")
_SAFE_USERNAME = _re.compile(r"^[a-zA-Z_][a-zA-Z0-9._-]{0,31}$")

# Global env vars — backward compat fallback
GLOBAL_DESKTOP_ENABLED = os.environ.get("DESKTOP_ENABLED", "false").lower() == "true"
_GLOBAL_HOST = os.environ.get("DESKTOP_HOST", "")
_GLOBAL_USER = os.environ.get("DESKTOP_USER", "")
_GLOBAL_SESSION_TIMEOUT = int(os.environ.get("DESKTOP_SESSION_TIMEOUT", "7200"))

# Config store — keyed by user_id (string)
_DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
_store = JsonConfigStore(
    "workstation_configs",
    _DATA_DIR / "workstation_configs.json",
    lazy=True,
)

# Encryption helpers (lazy — only loaded when TOTP secrets are stored)
_fernet = None


def _get_fernet():
    """Get Fernet instance for encrypting TOTP secrets."""
    global _fernet
    if _fernet is not None:
        return _fernet
    try:
        from config import TOKEN_ENCRYPTION_KEY
        if not TOKEN_ENCRYPTION_KEY:
            return None
        from cryptography.fernet import Fernet
        _fernet = Fernet(TOKEN_ENCRYPTION_KEY.encode())
        return _fernet
    except Exception:
        return None


def _encrypt_secret(plaintext: str) -> str:
    """Encrypt TOTP secret. Raises if no encryption key configured."""
    if not plaintext:
        return ""
    f = _get_fernet()
    if not f:
        raise RuntimeError(
            "Cannot store TOTP secret: TOKEN_ENCRYPTION_KEY not configured. "
            "Generate with: python3 -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    return f"ENC:{f.encrypt(plaintext.encode()).decode()}"


def _decrypt_secret(stored: str) -> str:
    """Decrypt TOTP secret. Returns plaintext."""
    if not stored or not stored.startswith("ENC:"):
        return stored
    f = _get_fernet()
    if not f:
        log.warning("workstation_config: Cannot decrypt — TOKEN_ENCRYPTION_KEY not set")
        return ""
    try:
        return f.decrypt(stored[4:].encode()).decode()
    except Exception:
        log.warning("workstation_config: Decryption failed — key may have changed")
        return ""


@dataclass
class WorkstationConfig:
    """Per-user workstation connection configuration."""
    mac_host: str = ""          # Tailscale hostname or IP
    mac_user: str = ""          # Mac OS username
    totp_secret: str = ""       # Encrypted TOTP secret
    ssh_key_name: str = ""      # SSH key filename (in ~/.ssh-config/)
    enabled: bool = True        # User-level enable/disable
    session_timeout: int = 7200  # TOTP auth timeout (seconds)
    authenticated_until: float = 0.0  # Unix timestamp
    created_at: str = ""        # ISO timestamp
    display_name: str = ""      # User's display name (for admin listing)

    @classmethod
    def from_dict(cls, d: dict) -> "WorkstationConfig":
        """Create from stored dict, ignoring unknown keys."""
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict:
        """Convert to dict for storage."""
        return asdict(self)

    def is_authenticated(self) -> bool:
        """Check if TOTP auth is still valid."""
        return time.time() < self.authenticated_until

    def get_totp_secret(self) -> str:
        """Decrypt and return TOTP secret."""
        return _decrypt_secret(self.totp_secret)


# ============================================================
# CRUD Operations
# ============================================================

def get_workstation(user_id: str) -> WorkstationConfig | None:
    """Get workstation config for a user. Returns None if not configured."""
    data = _store.get(str(user_id))
    if data is None:
        return None
    return WorkstationConfig.from_dict(data)


def set_workstation(user_id: str, config: WorkstationConfig) -> None:
    """Store workstation config for a user. Encrypts TOTP secret."""
    _store.set(str(user_id), config.to_dict())
    log.info("workstation_config: stored config for user %s (host=%s)",
             user_id, config.mac_host)


def delete_workstation(user_id: str) -> bool:
    """Delete workstation config. Returns True if existed."""
    if str(user_id) in _store:
        _store.delete(str(user_id))
        log.info("workstation_config: deleted config for user %s", user_id)
        return True
    return False


def list_workstations() -> dict[str, WorkstationConfig]:
    """List all configured workstations. Returns {user_id: config}."""
    result = {}
    for uid, data in _store.items():
        try:
            result[uid] = WorkstationConfig.from_dict(data)
        except Exception:
            log.warning("workstation_config: invalid entry for user %s", uid)
    return result


# ============================================================
# Status & Auth Checks
# ============================================================

def is_workstation_enabled(user_id: str) -> bool:
    """Check if workstation is configured and enabled for a user.

    Falls back to global DESKTOP_ENABLED for unconfigured users.
    """
    ws = get_workstation(str(user_id))
    if ws is not None:
        return ws.enabled and bool(ws.mac_host) and bool(ws.mac_user)
    # Fallback: global env vars (backward compat)
    return GLOBAL_DESKTOP_ENABLED


def get_effective_config(user_id: str) -> tuple[str, str, int]:
    """Get effective (mac_host, mac_user, session_timeout) for a user.

    Returns per-user config if set, else falls back to global env vars.
    """
    ws = get_workstation(str(user_id))
    if ws is not None and ws.mac_host and ws.mac_user:
        return ws.mac_host, ws.mac_user, ws.session_timeout
    # Fallback to global
    return _GLOBAL_HOST, _GLOBAL_USER, _GLOBAL_SESSION_TIMEOUT


def set_authenticated(user_id: str, duration_s: int | None = None) -> None:
    """Mark user's workstation as authenticated (TOTP verified)."""
    ws = get_workstation(str(user_id))
    if ws is None:
        log.warning("workstation_config: set_authenticated for unconfigured user %s", user_id)
        return
    timeout = duration_s or ws.session_timeout
    ws.authenticated_until = time.time() + timeout
    set_workstation(str(user_id), ws)
    log.info("workstation_config: user %s authenticated for %ds", user_id, timeout)


def check_authenticated(user_id: str) -> tuple[bool, float]:
    """Check if user's workstation auth is valid.

    Returns (is_authenticated, seconds_remaining).
    """
    ws = get_workstation(str(user_id))
    if ws is None:
        # Fallback: check global auth state (backward compat)
        from bot.relay.core import _cached_authenticated, _cached_session_remaining
        return _cached_authenticated, _cached_session_remaining
    remaining = max(0.0, ws.authenticated_until - time.time())
    return remaining > 0, remaining


def get_workstation_for_chat(chat_id: str) -> tuple[str | None, WorkstationConfig | None]:
    """Get the workstation config for a relay chat by looking up the registry.

    Returns (user_id, config) or (None, None) if not linked.
    Uses _load_registry() directly (sync) to avoid async/sync mismatch.
    """
    from bot.relay.registry import _load_registry
    registry = _load_registry()
    entry = registry.get(str(chat_id))
    if not entry:
        return None, None
    uid = entry.get("user_id", "")
    if uid:
        ws = get_workstation(uid)
        return uid, ws
    # Legacy entry without user_id — fall back to global
    return None, None
