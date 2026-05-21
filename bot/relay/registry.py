# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Session registry — JSON-backed channel↔project linking for employee workstation control.

Maps workstation channel chat_id → {session_name, project_path, created_at, last_used, ...}
Persisted to JSON on the bot-data volume, survives restarts.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
log = logging.getLogger(__name__)

__all__ = [
    "_load_registry",
    "_save_registry",
    "_update_synced_offset",
    "_validate_project_path",
    "link_session",
    "update_pinned_jsonl",
    "unlink_session",
    "get_linked_session",
    "get_all_links",
    "_touch_last_used",
    "_uuid_from_entry",
    "validate_account_label",
    "_encode_project_path",
    "_decode_project_path",
    "_fetch_jsonl_cwd",
    "list_all_jsonls",
    "list_project_jsonls",
    "_DATA_DIR",
    "_REGISTRY_FILE",
    "_registry_lock",
    "_UUID_RE",
    "_UUID_BARE_RE",
    "_SAFE_PROJECT_PATH",
    "_ACCOUNT_LABEL_RE",
]

# Regex for UUID v4 in JSONL filenames
_UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)

_UUID_BARE_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

_DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
_REGISTRY_FILE = _DATA_DIR / "config" / "relay_sessions.json"
_registry_lock = asyncio.Lock()

# JsonConfigStore for atomic writes, .prev.json sidecar, destructive-write guard,
# and git versioning. Replaces bare _load_registry/_save_registry (incident fix).
# LAZY INIT: store created on first access via _get_store() — NOT at import time.
# This prevents the race condition where import happens before config_migration
# moves the file to data/config/, causing JCS to load empty and overwrite on save.
import threading as _threading
from bot.config_store import JsonConfigStore as _JsonConfigStore

_registry_store: _JsonConfigStore | None = None
_store_init_lock = _threading.Lock()


def _get_store() -> _JsonConfigStore:
    """Lazy-init the registry store. Thread-safe."""
    global _registry_store
    if _registry_store is not None:
        return _registry_store
    with _store_init_lock:
        if _registry_store is not None:
            return _registry_store
        _registry_store = _JsonConfigStore(
            "relay_sessions", _REGISTRY_FILE, git_tracked=True,
        )
        return _registry_store

# Path validation: no ".." traversal, must start with / or ~
_SAFE_PROJECT_PATH = re.compile(r"^[/~][a-zA-Z0-9_./ ~-]*$")

# Strict validation for shell-interpolated paths — no shell metacharacters
_SAFE_PATH = re.compile(r"^[a-zA-Z0-9_./ ~-]+$")

# Strict session name validation
_SAFE_SESSION_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")

# Multi-account OAuth (Apr 18 2026) — relay chats can bind to a labeled
# ~/.relay/oauth_token.<label> on Mac. Strict regex mirrors guard.sh so any
# value that passes here also passes guard.sh (defense in depth).
_ACCOUNT_LABEL_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def _uuid_from_entry(entry: dict) -> str:
    """Extract JSONL uuid from a registry entry's pinned_jsonl_path."""
    path = entry.get("pinned_jsonl_path") or ""
    m = _UUID_RE.search(path)
    return m.group(1) if m else ""


def _load_registry() -> dict[str, dict]:
    """Load session registry from JsonConfigStore. Thread-safe, mtime-aware."""
    return _get_store().all()


def _save_registry(registry: dict[str, dict]) -> None:
    """Persist registry via JsonConfigStore (atomic write + .prev.json + git)."""
    _get_store().replace(registry)


async def _update_synced_offset(chat_id: str, offset: int) -> None:
    """Persist JSONL byte offset for a relay chat (for catch-up sync).

    Must hold _registry_lock to prevent read-modify-write race with other
    registry operations (e.g., link_session, set_relay_mode) that could
    clobber entries added by concurrent callers.
    """
    async with _registry_lock:
        registry = _load_registry()
        key = str(chat_id)
        if key in registry:
            registry[key]["last_synced_offset"] = offset
            _save_registry(registry)
        else:
            log.debug("_update_synced_offset: chat %s not in registry, skipping", chat_id)


def _validate_project_path(path: str) -> str | None:
    """Validate project path — reject traversal and metacharacters."""
    if not path:
        return "project_path is required"
    if ".." in path:
        return "project_path must not contain '..'"
    if not _SAFE_PROJECT_PATH.fullmatch(path):
        return "project_path must start with / or ~ and contain no shell metacharacters"
    return None


def _validate_session_name(name: str) -> str | None:
    """Validate session name — reject shell metacharacters."""
    if not name or not _SAFE_SESSION_NAME.fullmatch(name):
        return f"INVALID_SESSION_NAME: must be alphanumeric/dash/underscore, got '{name}'"
    return None


def validate_account_label(label: str) -> bool:
    """Strict label validator; shared source of truth with guard.sh regex."""
    return bool(label) and bool(_ACCOUNT_LABEL_RE.fullmatch(label))


async def link_session(
    chat_id: str, session_name: str, project_path: str,
    pinned_jsonl_path: str = "",
    source: str = "telegram",
    user_id: str = "",
) -> dict:
    """Link a chat to a relay session with a project path.

    Args:
        chat_id: TG/Teams chat ID (as string)
        session_name: tmux session name (relay_ prefix added if missing)
        project_path: Mac filesystem path to project
        pinned_jsonl_path: JSONL file path to pin for this relay chat.
            Once set, relay always reads from this file — never explicitly registers.
        user_id: CB-140: owner's user ID (e.g. "telegram:123456789") — links
            this relay session to a per-user WorkstationConfig for SSH target resolution.

    Returns dict with: success, entry, error
    """
    if not session_name.startswith("relay_"):
        session_name = f"relay_{session_name}"
    err = _validate_session_name(session_name)
    if err:
        return {"success": False, "error": err}
    err = _validate_project_path(project_path)
    if err:
        return {"success": False, "error": err}

    now = datetime.now(timezone.utc).isoformat()
    entry = {
        "session_name": session_name,
        "project_path": project_path,
        "source": source,  # "telegram" or "teams" — used by transport layer
        "created_at": now,
        "last_used": now,
    }
    if user_id:
        entry["user_id"] = user_id  # CB-140: links to WorkstationConfig
    if pinned_jsonl_path:
        # Validate using _SAFE_PATH (consistent with registry path checks)
        if _SAFE_PATH.fullmatch(pinned_jsonl_path.replace("~", "/tmp")):
            entry["pinned_jsonl_path"] = pinned_jsonl_path
        else:
            log.warning("link_session: rejected unsafe pinned_jsonl_path: %s", pinned_jsonl_path)

    async with _registry_lock:
        registry = _load_registry()
        registry[str(chat_id)] = entry
        _save_registry(registry)

    log.info("Session linked: chat_id=%s session=%s path=%s jsonl=%s",
             chat_id, session_name, project_path, pinned_jsonl_path or "(discover)")
    return {"success": True, "entry": entry}


async def update_pinned_jsonl(chat_id: str, jsonl_path: str) -> None:
    """Update the pinned JSONL path in the registry when user switches session.

    Called when user confirms a session switch via /relay-follow.
    Resets last_synced_offset to 0 so catch-up starts fresh from the new file.
    """
    # Validate using _SAFE_PATH (consistent with registry path checks)
    if not jsonl_path or not _SAFE_PATH.fullmatch(jsonl_path.replace("~", "/tmp")):
        log.warning("update_pinned_jsonl: rejected unsafe path for chat %s: %s", chat_id, jsonl_path)
        return
    async with _registry_lock:
        registry = _load_registry()
        key = str(chat_id)
        if key in registry:
            registry[key]["pinned_jsonl_path"] = jsonl_path
            registry[key]["last_synced_offset"] = 0
            _save_registry(registry)
            log.info("Pinned JSONL updated: chat=%s path=%s", chat_id, jsonl_path)
        else:
            log.warning("update_pinned_jsonl: chat %s not in registry", chat_id)


async def unlink_session(chat_id: str) -> dict:
    """Remove a chat's linked session.

    Returns dict with: success, removed, error
    """
    async with _registry_lock:
        registry = _load_registry()
        removed = registry.pop(str(chat_id), None)
        if removed:
            _save_registry(registry)

    if removed:
        # Clean up pane streaming state
        log.info("Session unlinked: chat_id=%s was=%s", chat_id, removed.get("session_name"))
        return {"success": True, "removed": removed}
    return {"success": False, "error": f"No linked session for chat_id={chat_id}"}


async def get_linked_session(chat_id: str) -> dict | None:
    """Look up the linked session for a chat. Returns entry or None.

    Used by model for auto-resolving session from chat context (Phase 3).
    No lock needed: _save_registry uses atomic rename, so reads always get
    a complete file (old or new). Lock is only for read-modify-write sequences.
    """
    registry = _load_registry()
    return registry.get(str(chat_id))


async def get_all_links() -> dict[str, dict]:
    """Return all chat→session links. See get_linked_session re: no lock."""
    return _load_registry()


async def _touch_last_used(session_name: str) -> None:
    """Update last_used for a linked session (by session_name). Best-effort."""
    try:
        async with _registry_lock:
            registry = _load_registry()
            for entry in registry.values():
                if entry.get("session_name") == session_name:
                    entry["last_used"] = datetime.now(timezone.utc).isoformat()
                    _save_registry(registry)
                    break
    except Exception:
        pass  # non-critical — don't fail the operation


def _encode_project_path(project_path: str) -> str:
    """Encode a project path the way Claude Code stores it on disk.

    Example: /Users/youruser/dev/tao-diet → -Users-youruser-dev-myproject

    Returns empty string for empty input.
    """
    if not project_path:
        return ""
    return "-" + project_path.lstrip("/").replace("/", "-")


def _decode_project_path(encoded: str) -> str:
    """Inverse of _encode_project_path.

    Example: -Users-youruser-dev-myproject → /Users/youruser/dev/tao-diet

    Best-effort: ambiguity remains if the original path contained dashes
    (Claude Code encoding is lossy). Callers should treat the result as
    display-friendly, not an authoritative path.
    """
    if not encoded:
        return ""
    # Strip one leading dash then replace remaining dashes with slashes.
    body = encoded[1:] if encoded.startswith("-") else encoded
    return "/" + body.replace("-", "/")


async def _fetch_jsonl_cwd(fpath: str) -> str | None:
    """Read the first 16KB of a Mac JSONL and extract `.cwd` for a precise
    project path. Claude CLI's first JSONL entry carries the session's
    working directory — the authoritative source. Replaces the lossy
    `_decode_project_path` best-guess (which mis-splits hyphenated project
    names like `fiducia-data-works`).

    Uses `relay-read-jsonl-header <fpath>` guard.sh command (FB-32).
    Returns raw text of first 16KB — guard.sh validates path is under
    ~/.claude/projects/*.jsonl.

    Returns None on any failure (SSH error, missing cwd, parse error).
    Callers should fallback to the best-guess decoder.
    """
    from bot.relay.core import DESKTOP_ENABLED  # noqa: PLC0415
    from bot.relay.ssh import execute  # noqa: PLC0415

    if not DESKTOP_ENABLED or not fpath:
        return None
    # Belt-and-braces validation; fpath comes from our own ls parsing but
    # we're feeding it to guard.sh which enforces whitelist, so extra
    # shell-metachar rejection here just cuts pointless SSH round-trips.
    if any(c in fpath for c in ";|&$`<>"):
        log.warning("fetch_jsonl_cwd: blocked metachar in fpath: %s", fpath)
        return None
    ok, output = await execute(f"relay-read-jsonl-header {fpath}", timeout=15.0)
    if not ok or not output:
        log.warning("fetch_jsonl_cwd: relay-read-jsonl-header failed for %s: %s",
                     fpath, (output or "")[:200])
        return None
    # Scan ALL lines in the chunk for a `cwd` field. Claude Desktop.app
    # puts cwd on a later JSONL entry, not always the first line.
    # Claude CLI puts it on line 1 as {"type":"system","subtype":"init","cwd":"..."}.
    for line in output.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        cwd = obj.get("cwd")
        if isinstance(cwd, str) and cwd.startswith("/"):
            return cwd
    log.warning("fetch_jsonl_cwd: no cwd found in %s", fpath)
    return None


async def list_all_jsonls(limit: int = 30) -> list[dict]:
    """List recent JSONL sessions across ALL Mac Claude projects.

    Enumerates every project directory under ~/.claude/projects and returns
    the most-recently-modified JSONL files across all of them, sorted
    newest-first. Used by the TG DM "show Mac sessions" flow so the admin can
    pick which project/session to link a relay chat to.

    Guarantees at least the newest JSONL per project_dir is included,
    with remaining slots filled by additional JSONLs (any project) in
    mtime order. When unique projects exceed ``limit``, all projects
    are still included (limit is a soft target, not a hard cap).

    Returns list of dicts, each with:
      - uuid: JSONL stem (Claude session UUID)
      - project_dir: encoded dir name ("-Users-youruser-dev-myproject")
      - project_path: decoded path ("/Users/youruser/dev/tao-diet") — best-effort
      - size: file size in bytes
      - mtime_str: "Apr 16 20:10" style mtime from ls output

    Returns [] on SSH failure or empty projects dir.
    """
    from bot.relay.ssh import execute  # noqa: PLC0415

    # Single SSH call — guard.sh relay-list-jsonls runs ls internally (FB-32).
    cmd = "relay-list-jsonls"
    ok, output = await execute(cmd, timeout=20.0)
    if not ok:
        log.info("list_all_jsonls: ssh failed: %s", output[:200])
        return []
    # Parse ALL lines first, then deduplicate per project_dir (keep newest).
    # Without dedup, projects with many JSONLs (e.g. tg-claude-bot with 200+)
    # push other projects past the limit — tao5-validator was at position #76
    # and invisible with limit=30.
    all_items: list[dict] = []
    for line in output.splitlines():
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        fpath = parts[8]
        if not fpath.endswith(".jsonl"):
            continue
        # fpath looks like /Users/youruser/.claude/projects/<encoded>/<uuid>.jsonl
        # Extract the encoded project dir and UUID stem.
        try:
            segments = fpath.split("/")
            fname = segments[-1]
            project_dir = segments[-2]
        except IndexError:
            continue
        stem = fname[: -len(".jsonl")]
        if not _UUID_BARE_RE.fullmatch(stem):
            continue
        try:
            size = int(parts[4])
        except (ValueError, IndexError):
            continue
        mtime_str = " ".join(parts[5:8])
        all_items.append({
            "uuid": stem,
            "project_dir": project_dir,
            # project_path filled below via precise cwd lookup
            "project_path": _decode_project_path(project_dir),
            "fpath": fpath,  # needed for cwd fetch, stripped before return
            "size": size,
            "mtime_str": mtime_str,
        })

    # Two-pass selection to guarantee every project appears while still
    # showing multiple JSONLs per project within the limit.
    # Pass 1: tag the newest JSONL per project_dir as "must-include".
    # Pass 2: walk all items in mtime order, include must-haves and fill
    #   remaining slots with extras — preserves global mtime ordering.
    # Input is already sorted by mtime desc (ls -lt), so first seen wins.
    seen_projects: set[str] = set()
    must_include: set[str] = set()  # uuids of newest-per-project
    for it in all_items:
        pd = it["project_dir"]
        if pd not in seen_projects:
            seen_projects.add(pd)
            must_include.add(it["uuid"])

    items: list[dict] = []
    extra_budget = max(0, limit - len(must_include))
    extras_used = 0
    for it in all_items:
        if it["uuid"] in must_include:
            items.append(it)
        elif extras_used < extra_budget:
            items.append(it)
            extras_used += 1

    # === Precise project_path via JSONL cwd (Apr 18 2026) ===
    # The naive _decode_project_path wrongly splits hyphenated project
    # names (e.g. `fiducia-data-works` → `/fiducia/data/works`). Claude
    # CLI's first JSONL entry carries the authoritative `cwd`. Fetch it
    # once per unique project_dir (cached), parallelized. Fallback to
    # the lossy decoder on any failure so the listing never breaks.
    unique_dirs: dict[str, str] = {}  # project_dir → sample fpath (newest)
    for it in items:
        if it["project_dir"] not in unique_dirs:
            unique_dirs[it["project_dir"]] = it["fpath"]
    resolved: dict[str, str | None] = {}
    if unique_dirs:
        # Run sequentially — parallel SSH connections overwhelm the link
        # and cause silent timeouts. One SSH per project_dir is fine.
        for pd, fp in unique_dirs.items():
            try:
                res = await _fetch_jsonl_cwd(fp)
            except Exception as exc:
                log.warning("fetch_jsonl_cwd exception for %s: %s", pd, exc)
                res = None
            resolved[pd] = res if res else None
    for it in items:
        precise = resolved.get(it["project_dir"])
        if precise:
            it["project_path"] = precise
        # Strip the scratch field before returning
        it.pop("fpath", None)
    return items


async def list_project_jsonls(project_path: str, limit: int = 20) -> list[dict]:
    """List recent JSONL session files in a Mac Claude project directory.

    Uses SSH `ls -lt ~/.claude/projects/<encoded>/` via guard.sh whitelist.
    Returns list of dicts sorted newest-first, each with:
      - uuid: the JSONL stem (Claude session UUID)
      - size: file size in bytes
      - mtime_str: "Apr 16 20:10" style mtime from ls output
    Returns [] on SSH failure or empty project dir.
    """
    from bot.relay.ssh import execute  # noqa: PLC0415

    encoded = _encode_project_path(project_path)
    if not encoded:
        return []
    # Validate encoded to avoid shell metacharacter surprises
    if not _SAFE_PATH.fullmatch(encoded):
        log.warning("list_project_jsonls: unsafe encoded=%s", encoded)
        return []
    # guard.sh relay-list-jsonls with encoded dir arg (FB-32)
    cmd = f"relay-list-jsonls {encoded}"
    ok, output = await execute(cmd, timeout=15.0)
    if not ok:
        log.info("list_project_jsonls: ssh failed: %s", output[:200])
        return []
    items: list[dict] = []
    for line in output.splitlines():
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        fname = parts[8]
        if not fname.endswith(".jsonl"):
            continue
        stem = fname[: -len(".jsonl")]
        if not _UUID_BARE_RE.fullmatch(stem):
            continue
        try:
            size = int(parts[4])
        except (ValueError, IndexError):
            continue
        mtime_str = " ".join(parts[5:8])
        items.append({"uuid": stem, "size": size, "mtime_str": mtime_str})
        if len(items) >= limit:
            break
    return items
