# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Unit tests for BoundedDedup — FIFO-evicting dedup set used for
crash-recovery dedup of desktop_turn events in relay pipeline."""

import pytest

from bot.relay.ux import BoundedDedup


def test_basic_dedup():
    """First add returns False, second add of same id returns True."""
    d = BoundedDedup(cap=100)
    assert d.is_dup("msg-1") is False
    assert d.is_dup("msg-1") is True
    assert d.is_dup("msg-2") is False
    assert d.is_dup("msg-2") is True


def test_distinct_ids_not_deduped():
    """Different ids are not considered duplicates."""
    d = BoundedDedup(cap=100)
    for i in range(50):
        assert d.is_dup(f"msg-{i}") is False
    assert len(d) == 50


def test_eviction_at_cap():
    """When cap is reached, oldest entries are evicted."""
    cap = 5
    d = BoundedDedup(cap=cap)
    # Fill to cap
    for i in range(cap):
        d.is_dup(f"msg-{i}")
    assert len(d) == cap
    # Add one more — should evict msg-0 (oldest)
    assert d.is_dup("msg-new") is False
    assert len(d) == cap
    # msg-0 was evicted — no longer detected as dup
    assert d.is_dup("msg-0") is False
    # Adding msg-0 back evicted msg-1 (new oldest). Now set is:
    # msg-2, msg-3, msg-4, msg-new, msg-0
    assert d.is_dup("msg-2") is True  # still there
    assert d.is_dup("msg-4") is True  # still there


def test_eviction_preserves_newest():
    """Eviction removes oldest, keeps newest entries."""
    d = BoundedDedup(cap=3)
    d.is_dup("a")
    d.is_dup("b")
    d.is_dup("c")
    # Cap is 3, all present
    assert d.is_dup("a") is True
    assert d.is_dup("b") is True
    assert d.is_dup("c") is True
    # Add "d" — evicts "a"
    d.is_dup("d")
    assert d.is_dup("a") is False  # evicted
    assert d.is_dup("d") is True   # still there


def test_empty_dedup():
    """Empty dedup set has length 0."""
    d = BoundedDedup()
    assert len(d) == 0


def test_large_cap():
    """Works correctly with the production cap size."""
    d = BoundedDedup(cap=10_000)
    for i in range(10_000):
        assert d.is_dup(f"id-{i}") is False
    assert len(d) == 10_000
    # All still in set
    assert d.is_dup("id-0") is True
    assert d.is_dup("id-9999") is True
    # Add one more — evicts id-0
    d.is_dup("overflow")
    assert len(d) == 10_000
    assert d.is_dup("id-0") is False  # evicted
