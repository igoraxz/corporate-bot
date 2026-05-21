# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Browser Pool — unified tracking of Camoufox tabs and Playwright instances.

Provides global budget enforcement, per-chat tab ownership, LRU eviction,
and 3-tier immunity model for both browser backends.

3-Tier Model (symmetric for both browsers):
  TIER 1 — Admin/Promoted: IMMUNE from eviction
  TIER 2 — Coding (project_dir set): LRU-evictable
  TIER 3 — Discovered: LRU-evictable (browser tools OFF by default)

Budget per browser: 6 immune slots + 6 LRU slots = 12 max.
Configurable via BROWSER_POOL_IMMUNE_SLOTS / BROWSER_POOL_LRU_SLOTS env vars.

Camoufox: tracks individual tabs (shared browser, multiple tabs).
  Eviction closes a single tab via camofox-browser REST API.
Playwright: Tier 1 uses shared SSE server (immune, always-on).
  Tier 2/3 use per-chat stdio instances (eviction kills the MCP subprocess).
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Budget configuration
IMMUNE_SLOTS = int(os.environ.get("BROWSER_POOL_IMMUNE_SLOTS", "6"))
LRU_SLOTS = int(os.environ.get("BROWSER_POOL_LRU_SLOTS", "6"))
MAX_SLOTS = IMMUNE_SLOTS + LRU_SLOTS

# Tier constants
TIER_IMMUNE = "immune"   # team/admin/promoted
TIER_LRU = "lru"         # coding + external


@dataclass
class BrowserEntry:
    """A tracked browser resource (Camoufox tab or Playwright instance)."""

    entry_id: str           # tab_id (Camoufox) or session_key (Playwright)
    session_key: str        # owning chat's session_key
    browser: str            # "camofox" | "playwright"
    tier: str               # TIER_IMMUNE | TIER_LRU
    url: str = ""           # last known URL
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)


class BrowserPool:
    """Global browser resource pool with LRU eviction and tier-based immunity.

    Thread-safe via asyncio.Lock. All public methods are async.
    In-memory only — browser resources don't survive container restarts.
    """

    def __init__(self) -> None:
        self._entries: dict[str, BrowserEntry] = {}
        self._lock = asyncio.Lock()

    # ── Registration ─────────────────────────────────────────────

    async def register(
        self,
        entry_id: str,
        session_key: str,
        browser: str,
        tier: str,
        url: str = "",
    ) -> BrowserEntry:
        """Register a new browser resource (tab or instance).

        Args:
            entry_id: Unique ID (Camoufox tab_id or Playwright session_key).
            session_key: Owning chat's session key.
            browser: "camofox" or "playwright".
            tier: TIER_IMMUNE or TIER_LRU.
            url: Initial URL.

        Returns:
            The created BrowserEntry.
        """
        async with self._lock:
            entry = BrowserEntry(
                entry_id=entry_id,
                session_key=session_key,
                browser=browser,
                tier=tier,
                url=url,
            )
            self._entries[entry_id] = entry
            log.info(
                "BrowserPool: registered %s %s (tier=%s, session=%s, url=%s) "
                "[total=%d]",
                browser, entry_id[:12], tier, session_key[:30], url[:60],
                len(self._entries),
            )
            return entry

    async def deregister(self, entry_id: str) -> BrowserEntry | None:
        """Remove a browser resource from the pool.

        Returns the removed entry, or None if not found.
        """
        async with self._lock:
            entry = self._entries.pop(entry_id, None)
            if entry:
                log.info(
                    "BrowserPool: deregistered %s %s (session=%s) [total=%d]",
                    entry.browser, entry_id[:12], entry.session_key[:30],
                    len(self._entries),
                )
            return entry

    # ── Activity tracking ────────────────────────────────────────

    async def touch(self, entry_id: str, url: str = "") -> bool:
        """Update last_used_at timestamp for a browser resource.

        Returns True if the entry exists, False otherwise.
        """
        async with self._lock:
            entry = self._entries.get(entry_id)
            if entry:
                entry.last_used_at = time.time()
                if url:
                    entry.url = url
                return True
            return False

    # ── Ownership ────────────────────────────────────────────────

    def owns(self, entry_id: str, session_key: str) -> bool:
        """Check if a session owns a browser resource. Lock-free read."""
        entry = self._entries.get(entry_id)
        return entry is not None and entry.session_key == session_key

    def get_entry(self, entry_id: str) -> BrowserEntry | None:
        """Get a browser entry by ID. Lock-free read."""
        return self._entries.get(entry_id)

    # ── Budget & Eviction ────────────────────────────────────────

    def count(self, browser: str, tier: str | None = None) -> int:
        """Count entries for a browser type, optionally filtered by tier."""
        return sum(
            1 for e in self._entries.values()
            if e.browser == browser and (tier is None or e.tier == tier)
        )

    def is_at_capacity(self, browser: str, tier: str) -> bool:
        """Check if the pool is at capacity for the given browser and tier.

        Immune entries have IMMUNE_SLOTS budget (hard cap — even immune chats
        are blocked beyond this to prevent unbounded resource growth).
        LRU entries have LRU_SLOTS budget.
        """
        if tier == TIER_IMMUNE:
            return self.count(browser, TIER_IMMUNE) >= IMMUNE_SLOTS
        return self.count(browser, TIER_LRU) >= LRU_SLOTS

    async def evict_oldest(self, browser: str) -> BrowserEntry | None:
        """Evict the oldest LRU entry for a browser type.

        Atomic find-and-pop under a single lock acquisition to avoid TOCTOU races.
        Only removes from the pool — caller must handle the actual
        resource cleanup (REST API call for Camoufox, subprocess kill for Playwright).

        Returns the evicted entry, or None if nothing to evict.
        """
        async with self._lock:
            candidates = [
                e for e in self._entries.values()
                if e.browser == browser and e.tier == TIER_LRU
            ]
            if not candidates:
                log.warning("BrowserPool: no LRU candidates for eviction (browser=%s)", browser)
                return None
            victim = min(candidates, key=lambda e: e.last_used_at)
            entry = self._entries.pop(victim.entry_id, None)

        if entry:
            age = time.time() - entry.last_used_at
            log.info(
                "BrowserPool: evicted %s %s (session=%s, idle=%.0fs, url=%s)",
                entry.browser, entry.entry_id[:12], entry.session_key[:30],
                age, entry.url[:60],
            )
        return entry

    # ── Cleanup ──────────────────────────────────────────────────

    async def cleanup_session(self, session_key: str) -> list[BrowserEntry]:
        """Remove all entries for a session (on SDK session reset/cleanup).

        Returns list of removed entries for caller to handle resource cleanup.
        """
        async with self._lock:
            to_remove = [
                eid for eid, e in self._entries.items()
                if e.session_key == session_key
            ]
            removed = []
            for eid in to_remove:
                entry = self._entries.pop(eid)
                removed.append(entry)

        if removed:
            log.info(
                "BrowserPool: cleaned up %d entries for session %s",
                len(removed), session_key[:30],
            )
        return removed

    # ── Query helpers ────────────────────────────────────────────

    def get_entries_for_session(
        self, session_key: str, browser: str | None = None,
    ) -> list[BrowserEntry]:
        """Get all entries for a session, optionally filtered by browser type."""
        return [
            e for e in self._entries.values()
            if e.session_key == session_key
            and (browser is None or e.browser == browser)
        ]

    def get_stats(self) -> dict:
        """Get pool statistics for /status and monitoring."""
        stats: dict = {
            "total": len(self._entries),
            "camofox": {"immune": 0, "lru": 0},
            "playwright": {"immune": 0, "lru": 0},
            "budget": {
                "immune_slots": IMMUNE_SLOTS,
                "lru_slots": LRU_SLOTS,
                "max_per_browser": MAX_SLOTS,
            },
        }
        for e in self._entries.values():
            if e.browser in stats:
                stats[e.browser][e.tier] = stats[e.browser].get(e.tier, 0) + 1
        return stats


# ── Singleton ────────────────────────────────────────────────────

_pool: BrowserPool | None = None


def get_pool() -> BrowserPool:
    """Get the global BrowserPool singleton (lazy init)."""
    global _pool
    if _pool is None:
        _pool = BrowserPool()
        log.info(
            "BrowserPool initialized (immune=%d, lru=%d, max=%d per browser)",
            IMMUNE_SLOTS, LRU_SLOTS, MAX_SLOTS,
        )
    return _pool


# ── Tier detection helpers ───────────────────────────────────────

def detect_tier(state: dict) -> str:
    """Detect the browser tier for a session based on hook state.

    Args:
        state: The session state dict from hooks.py (_get_state_for_hook).

    Returns:
        TIER_IMMUNE or TIER_LRU.
    """
    # Admin and non-external chats (primary + promoted) are immune
    _is_admin = state.get("is_admin", False)
    is_external = state.get("is_external_chat", False)
    has_sandbox = bool(state.get("sandbox_dir"))

    # Tier 1: team/admin/promoted → immune
    if not is_external:
        return TIER_IMMUNE

    # Tier 2: coding (external with sandbox) → LRU
    if has_sandbox:
        return TIER_LRU

    # Tier 3: plain external → LRU
    return TIER_LRU


# ── Camoufox eviction helper ────────────────────────────────────

async def close_camofox_tab(tab_id: str) -> bool:
    """Close a Camoufox tab via the browser server REST API.

    This bypasses the MCP layer — used for cross-chat eviction when
    we need to close a tab owned by a different chat's MCP subprocess.

    Returns True if the tab was successfully closed.
    """
    import httpx
    import re

    # Validate tab_id to prevent URL path injection (tab_id is normally a UUID)
    if not tab_id or not re.match(r'^[a-zA-Z0-9_-]+$', tab_id):
        log.warning("BrowserPool: invalid tab_id for close: %r", tab_id[:50] if tab_id else "")
        return False

    camofox_url = os.environ.get("CAMOFOX_BROWSER_URL", "http://camofox-browser:9377")
    from config import CAMOFOX_API_KEY
    api_key = CAMOFOX_API_KEY

    url = f"{camofox_url}/tabs/{tab_id}"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.delete(url, headers=headers)
            if resp.status_code in (200, 204, 404):
                log.info("BrowserPool: closed Camoufox tab %s (status=%d)", tab_id[:12], resp.status_code)
                return True
            log.warning("BrowserPool: failed to close Camoufox tab %s (status=%d)", tab_id[:12], resp.status_code)
            return False
    except Exception as e:
        log.warning("BrowserPool: error closing Camoufox tab %s: %s", tab_id[:12], e)
        return False
