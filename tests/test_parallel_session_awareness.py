# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Tests for parallel session file awareness (session_state.py)."""

from bot.core.session_state import (
    _get_or_create_state,
    _session_state,
    check_repo_conflicts,
    cleanup_session_state,
    record_repo_file,
)


def _reset():
    """Clear all session state between tests."""
    _session_state.clear()


def test_record_repo_file_creates_set():
    _reset()
    record_repo_file("session_A", "/host-repo/bot/hooks.py")
    state = _get_or_create_state("session_A")
    assert state["modified_repo_files"] == {"/host-repo/bot/hooks.py"}


def test_record_repo_file_accumulates():
    _reset()
    record_repo_file("session_A", "/host-repo/bot/hooks.py")
    record_repo_file("session_A", "/host-repo/bot/agent.py")
    state = _get_or_create_state("session_A")
    assert state["modified_repo_files"] == {
        "/host-repo/bot/hooks.py",
        "/host-repo/bot/agent.py",
    }


def test_record_repo_file_deduplicates():
    _reset()
    record_repo_file("session_A", "/host-repo/bot/hooks.py")
    record_repo_file("session_A", "/host-repo/bot/hooks.py")
    state = _get_or_create_state("session_A")
    assert len(state["modified_repo_files"]) == 1


def test_check_repo_conflicts_no_conflict():
    _reset()
    record_repo_file("session_A", "/host-repo/bot/hooks.py")
    record_repo_file("session_B", "/host-repo/bot/agent.py")
    conflicts = check_repo_conflicts("session_A", "/host-repo/bot/hooks.py")
    assert conflicts == []


def test_check_repo_conflicts_with_conflict():
    _reset()
    record_repo_file("session_A", "/host-repo/bot/hooks.py")
    record_repo_file("session_B", "/host-repo/bot/hooks.py")
    conflicts = check_repo_conflicts("session_A", "/host-repo/bot/hooks.py")
    assert conflicts == ["session_B"]


def test_check_repo_conflicts_excludes_self():
    _reset()
    record_repo_file("session_A", "/host-repo/bot/hooks.py")
    # Self should never appear in conflicts
    conflicts = check_repo_conflicts("session_A", "/host-repo/bot/hooks.py")
    assert "session_A" not in conflicts


def test_check_repo_conflicts_multiple_conflicting_sessions():
    _reset()
    record_repo_file("session_A", "/host-repo/bot/hooks.py")
    record_repo_file("session_B", "/host-repo/bot/hooks.py")
    record_repo_file("session_C", "/host-repo/bot/hooks.py")
    conflicts = check_repo_conflicts("session_A", "/host-repo/bot/hooks.py")
    assert set(conflicts) == {"session_B", "session_C"}


def test_cleanup_removes_tracked_files():
    _reset()
    record_repo_file("session_A", "/host-repo/bot/hooks.py")
    cleanup_session_state("session_A")
    # After cleanup, session_A's state is gone — no conflicts for B
    record_repo_file("session_B", "/host-repo/bot/hooks.py")
    conflicts = check_repo_conflicts("session_B", "/host-repo/bot/hooks.py")
    assert conflicts == []


def test_check_conflicts_empty_state():
    _reset()
    # No sessions have modified anything
    conflicts = check_repo_conflicts("session_A", "/host-repo/bot/hooks.py")
    assert conflicts == []
