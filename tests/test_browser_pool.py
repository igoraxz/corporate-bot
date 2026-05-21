# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Unit tests for bot/browser_pool.py — BrowserPool with 3-tier immunity model."""

import asyncio
import pytest
import time

from bot.browser_pool import (
    BrowserPool, BrowserEntry, TIER_IMMUNE, TIER_LRU,
    detect_tier, IMMUNE_SLOTS, LRU_SLOTS,
)


@pytest.fixture
def pool():
    """Fresh BrowserPool for each test."""
    return BrowserPool()


def run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Registration ─────────────────────────────────────────────────


class TestRegistration:
    def test_register_and_get(self, pool):
        entry = run(pool.register("tab-1", "tg:chat1", "camofox", TIER_IMMUNE, "https://amex.com"))
        assert entry.entry_id == "tab-1"
        assert entry.session_key == "tg:chat1"
        assert entry.browser == "camofox"
        assert entry.tier == TIER_IMMUNE
        assert entry.url == "https://amex.com"
        assert pool.get_entry("tab-1") is entry

    def test_register_multiple(self, pool):
        run(pool.register("tab-1", "tg:chat1", "camofox", TIER_IMMUNE))
        run(pool.register("tab-2", "tg:chat1", "camofox", TIER_IMMUNE))
        run(pool.register("pw-1", "tg:chat2", "playwright", TIER_LRU))
        assert pool.count("camofox") == 2
        assert pool.count("playwright") == 1

    def test_deregister(self, pool):
        run(pool.register("tab-1", "tg:chat1", "camofox", TIER_LRU))
        removed = run(pool.deregister("tab-1"))
        assert removed is not None
        assert removed.entry_id == "tab-1"
        assert pool.get_entry("tab-1") is None

    def test_deregister_nonexistent(self, pool):
        removed = run(pool.deregister("nonexistent"))
        assert removed is None


# ── Activity tracking ────────────────────────────────────────────


class TestTouch:
    def test_touch_updates_timestamp(self, pool):
        run(pool.register("tab-1", "tg:chat1", "camofox", TIER_LRU))
        old_ts = pool.get_entry("tab-1").last_used_at
        time.sleep(0.01)
        result = run(pool.touch("tab-1"))
        assert result is True
        assert pool.get_entry("tab-1").last_used_at > old_ts

    def test_touch_updates_url(self, pool):
        run(pool.register("tab-1", "tg:chat1", "camofox", TIER_LRU, "https://old.com"))
        run(pool.touch("tab-1", url="https://new.com"))
        assert pool.get_entry("tab-1").url == "https://new.com"

    def test_touch_nonexistent(self, pool):
        result = run(pool.touch("nonexistent"))
        assert result is False


# ── Ownership ────────────────────────────────────────────────────


class TestOwnership:
    def test_owns_correct_session(self, pool):
        run(pool.register("tab-1", "tg:chat1", "camofox", TIER_LRU))
        assert pool.owns("tab-1", "tg:chat1") is True

    def test_owns_wrong_session(self, pool):
        run(pool.register("tab-1", "tg:chat1", "camofox", TIER_LRU))
        assert pool.owns("tab-1", "tg:chat2") is False

    def test_owns_nonexistent(self, pool):
        assert pool.owns("nonexistent", "tg:chat1") is False


# ── Budget & Capacity ────────────────────────────────────────────


class TestCapacity:
    def test_immune_capacity(self, pool):
        for i in range(IMMUNE_SLOTS):
            run(pool.register(f"tab-{i}", "tg:chat1", "camofox", TIER_IMMUNE))
        assert pool.is_at_capacity("camofox", TIER_IMMUNE) is True

    def test_lru_capacity(self, pool):
        for i in range(LRU_SLOTS):
            run(pool.register(f"tab-{i}", f"tg:chat{i}", "camofox", TIER_LRU))
        assert pool.is_at_capacity("camofox", TIER_LRU) is True

    def test_not_at_capacity(self, pool):
        run(pool.register("tab-1", "tg:chat1", "camofox", TIER_LRU))
        assert pool.is_at_capacity("camofox", TIER_LRU) is False

    def test_capacity_per_browser(self, pool):
        """Camofox and Playwright have separate budgets."""
        for i in range(LRU_SLOTS):
            run(pool.register(f"cfx-{i}", f"tg:chat{i}", "camofox", TIER_LRU))
        assert pool.is_at_capacity("camofox", TIER_LRU) is True
        assert pool.is_at_capacity("playwright", TIER_LRU) is False

    def test_count_by_tier(self, pool):
        run(pool.register("tab-1", "tg:chat1", "camofox", TIER_IMMUNE))
        run(pool.register("tab-2", "tg:chat2", "camofox", TIER_LRU))
        run(pool.register("tab-3", "tg:chat3", "camofox", TIER_LRU))
        assert pool.count("camofox", TIER_IMMUNE) == 1
        assert pool.count("camofox", TIER_LRU) == 2
        assert pool.count("camofox") == 3


# ── LRU Eviction ─────────────────────────────────────────────────


class TestEviction:
    def test_evict_oldest_lru(self, pool):
        run(pool.register("tab-old", "tg:chat1", "camofox", TIER_LRU))
        time.sleep(0.01)
        run(pool.register("tab-new", "tg:chat2", "camofox", TIER_LRU))
        evicted = run(pool.evict_oldest("camofox"))
        assert evicted is not None
        assert evicted.entry_id == "tab-old"
        assert pool.get_entry("tab-old") is None
        assert pool.get_entry("tab-new") is not None

    def test_evict_respects_immunity(self, pool):
        """Immune entries are never eviction candidates."""
        run(pool.register("tab-immune", "tg:chat1", "camofox", TIER_IMMUNE))
        run(pool.register("tab-lru", "tg:chat2", "camofox", TIER_LRU))
        evicted = run(pool.evict_oldest("camofox"))
        assert evicted.entry_id == "tab-lru"
        # Immune tab survives
        assert pool.get_entry("tab-immune") is not None

    def test_evict_no_lru_candidates(self, pool):
        """Only immune entries → nothing to evict."""
        run(pool.register("tab-immune", "tg:chat1", "camofox", TIER_IMMUNE))
        evicted = run(pool.evict_oldest("camofox"))
        assert evicted is None

    def test_evict_empty_pool(self, pool):
        evicted = run(pool.evict_oldest("camofox"))
        assert evicted is None

    def test_evict_respects_touch(self, pool):
        """Touch updates last_used_at, so touched entry survives."""
        run(pool.register("tab-old", "tg:chat1", "camofox", TIER_LRU))
        time.sleep(0.01)
        run(pool.register("tab-new", "tg:chat2", "camofox", TIER_LRU))
        # Touch the old one — now it's "newest"
        time.sleep(0.01)
        run(pool.touch("tab-old"))
        evicted = run(pool.evict_oldest("camofox"))
        assert evicted.entry_id == "tab-new"

    def test_evict_cross_browser_isolated(self, pool):
        """Evicting camofox doesn't touch playwright entries."""
        run(pool.register("cfx-1", "tg:chat1", "camofox", TIER_LRU))
        run(pool.register("pw-1", "tg:chat2", "playwright", TIER_LRU))
        evicted = run(pool.evict_oldest("camofox"))
        assert evicted.entry_id == "cfx-1"
        assert pool.get_entry("pw-1") is not None


# ── Session Cleanup ──────────────────────────────────────────────


class TestCleanup:
    def test_cleanup_removes_all_for_session(self, pool):
        run(pool.register("tab-1", "tg:chat1", "camofox", TIER_LRU))
        run(pool.register("tab-2", "tg:chat1", "camofox", TIER_IMMUNE))
        run(pool.register("tab-3", "tg:chat2", "camofox", TIER_LRU))
        removed = run(pool.cleanup_session("tg:chat1"))
        assert len(removed) == 2
        assert pool.get_entry("tab-1") is None
        assert pool.get_entry("tab-2") is None
        assert pool.get_entry("tab-3") is not None

    def test_cleanup_empty_session(self, pool):
        removed = run(pool.cleanup_session("nonexistent"))
        assert len(removed) == 0


# ── Query helpers ────────────────────────────────────────────────


class TestQueryHelpers:
    def test_get_entries_for_session(self, pool):
        run(pool.register("tab-1", "tg:chat1", "camofox", TIER_LRU))
        run(pool.register("pw-1", "tg:chat1", "playwright", TIER_LRU))
        run(pool.register("tab-2", "tg:chat2", "camofox", TIER_LRU))
        entries = pool.get_entries_for_session("tg:chat1")
        assert len(entries) == 2
        entries_cfx = pool.get_entries_for_session("tg:chat1", browser="camofox")
        assert len(entries_cfx) == 1

    def test_get_stats(self, pool):
        run(pool.register("tab-1", "tg:chat1", "camofox", TIER_IMMUNE))
        run(pool.register("tab-2", "tg:chat2", "camofox", TIER_LRU))
        run(pool.register("pw-1", "tg:chat3", "playwright", TIER_LRU))
        stats = pool.get_stats()
        assert stats["total"] == 3
        assert stats["camofox"]["immune"] == 1
        assert stats["camofox"]["lru"] == 1
        assert stats["playwright"]["lru"] == 1
        assert stats["budget"]["immune_slots"] == IMMUNE_SLOTS
        assert stats["budget"]["lru_slots"] == LRU_SLOTS


# ── Tier Detection ───────────────────────────────────────────────


class TestTierDetection:
    def test_team_chat_is_immune(self):
        state = {"is_admin": False, "is_external_chat": False}
        assert detect_tier(state) == TIER_IMMUNE

    def test_admin_chat_is_immune(self):
        state = {"is_admin": True, "is_external_chat": False}
        assert detect_tier(state) == TIER_IMMUNE

    def test_external_chat_is_lru(self):
        state = {"is_admin": False, "is_external_chat": True}
        assert detect_tier(state) == TIER_LRU

    def test_coding_sandbox_is_lru(self):
        state = {"is_admin": False, "is_external_chat": True,
                 "sandbox_dir": "/app/data/harness-projects/coding"}
        assert detect_tier(state) == TIER_LRU
