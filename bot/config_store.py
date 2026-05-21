# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Unified JSON config file management with caching, locking, and atomic writes.

Replaces the duplicated load/save/cache/lock boilerplate across config_overrides,
harness, scheduler, config (external chats), hooks (tool overrides), and relay registry.

All stores get: atomic writes (tmp + rename), threading.Lock on mutations,
mtime-based external edit detection (throttled to once per second).

Usage:
    store = JsonConfigStore("model_overrides", DATA_DIR / "model_overrides.json")
    store.get("telegram:123", default="claude-sonnet-4-6")
    store.set("telegram:123", "claude-opus-4-6")
    store.delete("telegram:123")
    store.all()  # snapshot dict
"""

import json
import logging
import shutil
import threading
import time
import traceback
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_SENTINEL = object()


class JsonConfigStore:
    """Thread-safe, atomic, mtime-aware JSON dict store.

    Designed for bot config files: small (<1000 entries), infrequent writes,
    human-readable JSON on disk. NOT for high-frequency message storage.

    Key type: str (default) or int. Int keys are stored as strings in JSON
    and converted back on load via key_type parameter.

    Thread safety: threading.Lock guards all mutations. Safe from both sync
    and async contexts (no awaits held while locked). Read-only operations
    (get, all, __contains__) are lock-free for performance — they read from
    the in-memory dict which is atomically replaced on reload.
    """

    def __init__(
        self,
        name: str,
        path: Path,
        *,
        key_type: type = str,
        mtime_interval_s: float = 1.0,
        lazy: bool = False,
        git_tracked: bool = False,
    ):
        self._name = name
        self._path = Path(path)
        self._key_type = key_type
        self._mtime_interval_s = mtime_interval_s
        self._git_tracked = git_tracked
        self._data: dict = {}
        self._mtime: float = 0
        self._last_stat_time: float = 0
        self._lock = threading.Lock()
        self._loaded = False
        if not lazy:
            self._load()
            self._loaded = True

    # ------------------------------------------------------------------
    # Public read API (lock-free, uses atomic dict reference)
    # ------------------------------------------------------------------

    def get(self, key: Any, default: Any = None) -> Any:
        """Get a value by key. Returns default if missing."""
        self._auto_load()
        self._maybe_reload()
        return self._data.get(self._coerce_key(key), default)

    def all(self) -> dict:
        """Return a shallow copy of all entries."""
        self._auto_load()
        self._maybe_reload()
        return dict(self._data)

    def __contains__(self, key: Any) -> bool:
        self._auto_load()
        self._maybe_reload()
        return self._coerce_key(key) in self._data

    def __len__(self) -> int:
        self._auto_load()
        self._maybe_reload()
        return len(self._data)

    def __repr__(self) -> str:
        return f"<JsonConfigStore '{self._name}' path={self._path} entries={len(self._data)}>"

    # ------------------------------------------------------------------
    # Dict-like API (enables drop-in replacement for plain dicts)
    # ------------------------------------------------------------------

    def __getitem__(self, key: Any) -> Any:
        self._auto_load()
        self._maybe_reload()
        ck = self._coerce_key(key)
        try:
            return self._data[ck]
        except KeyError:
            raise KeyError(key) from None

    def __setitem__(self, key: Any, value: Any) -> None:
        self.set(key, value)

    def __delitem__(self, key: Any) -> None:
        if not self.delete(key):
            raise KeyError(key)

    def __iter__(self):
        self._auto_load()
        self._maybe_reload()
        return iter(dict(self._data))

    def items(self):
        """Return (key, value) pairs (snapshot — safe to iterate while mutating)."""
        return self.all().items()

    def keys(self):
        """Return keys (snapshot)."""
        return self.all().keys()

    def values(self):
        """Return values (snapshot)."""
        return self.all().values()

    def pop(self, key: Any, *default):
        """Remove and return value for key. Raises KeyError if missing and no default."""
        with self._lock:
            self._auto_load_locked()
            ck = self._coerce_key(key)
            if ck not in self._data:
                if default:
                    return default[0]
                raise KeyError(key)
            result = self._data.pop(ck)
            self._save()
            return result

    # ------------------------------------------------------------------
    # Public write API (locked, atomic save)
    # ------------------------------------------------------------------

    def set(self, key: Any, value: Any) -> None:
        """Set a single key-value pair. Saves to disk atomically."""
        with self._lock:
            self._auto_load_locked()
            self._data[self._coerce_key(key)] = value
            self._save()

    def delete(self, key: Any) -> bool:
        """Delete a key. Returns True if the key existed."""
        with self._lock:
            self._auto_load_locked()
            removed = self._data.pop(self._coerce_key(key), _SENTINEL) is not _SENTINEL
            if removed:
                self._save()
            return removed

    def update(self, items: dict) -> None:
        """Batch-set multiple entries. Single atomic save."""
        with self._lock:
            self._auto_load_locked()
            for k, v in items.items():
                self._data[self._coerce_key(k)] = v
            self._save()

    def clear(self) -> None:
        """Remove all entries and save."""
        with self._lock:
            self._data = {}
            self._save()

    def replace(self, data: dict) -> None:
        """Replace all entries atomically. Single lock + single save."""
        with self._lock:
            self._data = {self._coerce_key(k): v for k, v in data.items()}
            self._save()

    # ------------------------------------------------------------------
    # Explicit reload (for callers that want to force a disk check)
    # ------------------------------------------------------------------

    def reload_if_changed(self) -> bool:
        """Check disk mtime and reload if changed. Returns True if reloaded."""
        try:
            current_mtime = self._path.stat().st_mtime
            self._last_stat_time = time.monotonic()  # update after successful stat
            if current_mtime != self._mtime:
                with self._lock:
                    # Double-check under lock
                    if self._path.stat().st_mtime != self._mtime:
                        log.info("%s: file changed on disk, reloading", self._name)
                        self._load()
                        return True
        except OSError:
            pass  # don't update _last_stat_time — retry on next call
        return False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _coerce_key(self, key: Any) -> Any:
        """Convert key to the store's key type."""
        if self._key_type is str:
            return str(key)
        return self._key_type(key)

    def _auto_load(self) -> None:
        """Lazy init: load from disk on first access."""
        if not self._loaded:
            with self._lock:
                if not self._loaded:
                    self._load()
                    self._loaded = True

    def _auto_load_locked(self) -> None:
        """Lazy init when caller already holds the lock."""
        if not self._loaded:
            self._load()
            self._loaded = True

    def _maybe_reload(self) -> None:
        """Throttled mtime check — stat() at most once per interval."""
        now = time.monotonic()
        if (now - self._last_stat_time) < self._mtime_interval_s:
            return
        try:
            current_mtime = self._path.stat().st_mtime
            self._last_stat_time = now  # update AFTER successful stat
            if current_mtime != self._mtime:
                with self._lock:
                    # Double-check under lock
                    try:
                        if self._path.stat().st_mtime != self._mtime:
                            log.info("%s: file changed on disk, reloading", self._name)
                            self._load()
                    except OSError:
                        pass
        except OSError:
            pass  # don't update _last_stat_time — retry on next call

    def _load(self) -> None:
        """Load data from disk. Caller must hold lock (or be in __init__)."""
        if not self._path.exists():
            self._data = {}
            self._prev_count = 0
            self._mtime = 0
            return
        try:
            raw = json.loads(self._path.read_text())
            if not isinstance(raw, dict):
                log.warning("%s: expected dict, got %s — using empty", self._name, type(raw).__name__)
                self._data = {}
                return
            data = {}
            for k, v in raw.items():
                try:
                    ck = self._coerce_key(k)
                except (ValueError, TypeError):
                    log.warning("%s: dropping entry with invalid key: %s", self._name, k)
                    continue
                data[ck] = v
            self._data = data
            self._prev_count = len(data)
            self._mtime = self._path.stat().st_mtime
            if data:
                log.debug("Loaded %d entries from %s", len(data), self._name)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to load %s from %s: %s", self._name, self._path, e)
            self._data = {}
            self._prev_count = 0

    def _save(self) -> None:
        """Atomic write via tmp + rename. Caller must hold lock.

        Safety features:
        - .prev.json sidecar: copies current file before overwriting (instant rollback)
        - Destructive-write guard: warns when going from N>0 entries to 0
        - Entry count audit log on every write
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            out = {str(k): v for k, v in self._data.items()}
            new_count = len(out)

            # --- .prev.json sidecar: copy current state before overwriting ---
            if self._path.exists():
                prev_path = self._path.with_suffix(".prev.json")
                try:
                    shutil.copy2(str(self._path), str(prev_path))
                except OSError as cp_err:
                    log.warning("%s: failed to create .prev.json backup: %s",
                                self._name, cp_err)

            # --- Destructive-write guard ---
            old_count = self._prev_count if hasattr(self, "_prev_count") else -1
            if old_count > 0 and new_count == 0:
                log.warning("DESTRUCTIVE WRITE: %s going from %d entries to 0 — "
                            ".prev.json sidecar preserved at %s\nCaller:\n%s",
                            self._name, old_count, self._path.with_suffix(".prev.json"),
                            "".join(traceback.format_stack(limit=15)))
            # --- Atomic write ---
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(out, indent=2, ensure_ascii=False))
            tmp.replace(self._path)
            self._mtime = self._path.stat().st_mtime

            # --- Audit log ---
            if old_count >= 0 and old_count != new_count:
                log.info("%s: %d → %d entries", self._name, old_count, new_count)
            self._prev_count = new_count

            # --- Git commit (outside the lock would be ideal but we're already locked) ---
            if self._git_tracked:
                try:
                    from bot.config_git import git_commit_file
                    git_commit_file(
                        self._path.parent, self._path.name,
                        self._name, entry_count=new_count,
                    )
                except Exception as git_err:
                    log.debug("Git commit skipped for %s: %s", self._name, git_err)
        except OSError as e:
            log.warning("Failed to save %s to %s: %s", self._name, self._path, e)
            # Clean up orphaned tmp file
            try:
                tmp = self._path.with_suffix(".tmp")
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
