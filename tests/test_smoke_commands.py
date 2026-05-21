# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Smoke tests for slash command handlers in bot/commands.py.

These tests verify that each lightweight command handler is callable without
crashing -- catching import ordering bugs, typos, missing lazy imports, etc.
They do NOT test business logic, only that the function executes without
raising an unhandled exception.

Run:
    python -m pytest tests/test_smoke_commands.py -v
"""

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add repo root to path (same pattern as existing tests)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Module-level patches: the commands module does `from config import ...`
# inside function bodies (lazy imports). We need to patch the config module
# attributes BEFORE those imports resolve. Also mock heavy integrations.
#
# IMPORTANT: import bot.desktop_relay at module level so that
# patch("bot.desktop_relay", ...) can find the attribute. The commands
# use `from bot import desktop_relay` (function-scoped lazy import),
# which resolves the real module unless we patch at this level.
# ---------------------------------------------------------------------------
import bot.desktop_relay  # noqa: F401 — needed for mock.patch to find the attr

# Ensure config module is importable; patch its attributes so the lazy
# `from config import is_admin_user` inside each handler resolves correctly.
# We do NOT replace sys.modules["config"] -- we patch attributes on the real module.

@pytest.fixture(autouse=True)
def _patch_config():
    """Patch config module attributes used by command handlers."""
    import config as _cfg

    # Save originals and patch
    patches = {}
    defaults = {
        "IS_STAGING": False,
        "IS_PRODUCTION": True,
        "TG_CHAT_ID": -100123,
        "TG_ADMIN_CHAT_IDS": [999999],
        "BOT_STARTED": datetime.now(timezone.utc),
        "ORG_TIMEZONE": "UTC",
        "GIT_COMMIT": "abc1234567890",
        "MODEL_PRIMARY": "claude-sonnet-4-6",
        "ALLOWED_MODELS": {"claude-sonnet-4-6", "claude-opus-4-6"},
        "LOG_DIR": Path("/tmp/test-logs"),
        "DATA_DIR": Path("/tmp/test-data"),
    }
    for attr, val in defaults.items():
        patches[attr] = getattr(_cfg, attr, None)
        setattr(_cfg, attr, val)

    # Ensure functions exist
    if not hasattr(_cfg, "is_admin_user"):
        _cfg.is_admin_user = lambda source, user_id: str(user_id) == "999999"
    if not hasattr(_cfg, "get_model_for_chat"):
        _cfg.get_model_for_chat = lambda k: "claude-sonnet-4-6"
    if not hasattr(_cfg, "get_oauth_for_chat"):
        _cfg.get_oauth_for_chat = lambda k: ""
    if not hasattr(_cfg, "get_chat_model_overrides"):
        _cfg.get_chat_model_overrides = lambda: {}
    if not hasattr(_cfg, "get_chat_oauth_overrides"):
        _cfg.get_chat_oauth_overrides = lambda: {}
    if not hasattr(_cfg, "get_model_primary"):
        _cfg.get_model_primary = lambda: "claude-sonnet-4-6"
    if not hasattr(_cfg, "get_default_oauth_label"):
        _cfg.get_default_oauth_label = lambda: "default"
    if not hasattr(_cfg, "list_oauth_profiles"):
        _cfg.list_oauth_profiles = lambda: [{"label": "default", "exists": True, "is_default": True, "token_prefix": "sk-ant-xxx"}]
    if not hasattr(_cfg, "save_external_chats"):
        _cfg.save_external_chats = lambda: None
    if not hasattr(_cfg, "set_oauth_for_chat"):
        _cfg.set_oauth_for_chat = lambda k, v: None
    if not hasattr(_cfg, "promote_chat_to_admin_level"):
        _cfg.promote_chat_to_admin_level = lambda cid: None
    # Backward compat alias
    if not hasattr(_cfg, "promote_chat_to_primary"):
        _cfg.promote_chat_to_primary = _cfg.promote_chat_to_admin_level

    yield

    for attr, val in patches.items():
        if val is None:
            try:
                delattr(_cfg, attr)
            except AttributeError:
                pass
        else:
            setattr(_cfg, attr, val)


@pytest.fixture(autouse=True)
def _patch_send(monkeypatch):
    """Mock _send_to_platform_simple so no real TG/WA calls happen."""
    mock_send = AsyncMock()
    monkeypatch.setattr("bot.commands._send_to_platform_simple", mock_send)
    return mock_send


@pytest.fixture()
def mock_send(_patch_send):
    """Expose the mock_send for assertions."""
    return _patch_send


@pytest.fixture()
def admin_user():
    """Patch is_admin_user to return True."""
    import config as _cfg
    orig = _cfg.is_admin_user
    _cfg.is_admin_user = lambda source, user_id: True
    yield
    _cfg.is_admin_user = orig


@pytest.fixture()
def non_admin_user():
    """Patch is_admin_user to return False."""
    import config as _cfg
    orig = _cfg.is_admin_user
    _cfg.is_admin_user = lambda source, user_id: False
    yield
    _cfg.is_admin_user = orig


def _mock_relay(enabled=False):
    """Build a MagicMock for bot.desktop_relay."""
    relay = MagicMock()
    relay.is_enabled = MagicMock(return_value=enabled)
    relay.get_relay_mode = MagicMock(return_value=None)
    relay.get_all_links = AsyncMock(return_value={})
    relay.list_daemons = AsyncMock(return_value={"ok": True, "daemons": []})
    relay.list_all_jsonls = AsyncMock(return_value=[])
    relay.list_project_jsonls = AsyncMock(return_value=[])
    relay.authenticate = AsyncMock(return_value={"authenticated": True, "ttl_seconds": 7200})
    relay.is_mac_online = AsyncMock(return_value=True)
    relay.status = AsyncMock(return_value={
        "online": True, "latency_ms": 42, "authenticated": True,
        "session_remaining": 3600,
    })
    relay.lock = AsyncMock(return_value={"locked": True})
    relay.reap_orphans = AsyncMock(return_value={"ok": True, "reaped_count": 0})
    relay.kill_daemon = AsyncMock(return_value={"ok": True, "killed_pid": 1234})
    relay.update_pinned_jsonl = AsyncMock(return_value={"success": True})
    relay.eager_respawn_daemon = AsyncMock(return_value={"ok": True})
    return relay


def _mock_pool():
    pool = MagicMock()
    pool.get_status = MagicMock(return_value={})
    pool.reset_session = AsyncMock()
    pool.get_session_id = AsyncMock(return_value=None)
    pool.schedule_session_reset = MagicMock()
    return pool


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestCommandSmoke:
    """Smoke tests: verify each command handler is callable without crashing."""

    # ---- /help ----

    async def test_help_non_admin(self, mock_send, non_admin_user):
        relay = _mock_relay()
        with patch("bot.desktop_relay", relay):
            from bot.commands import _lightweight_help
            await _lightweight_help("telegram", "12345", "99999")
        assert mock_send.called

    async def test_help_admin(self, mock_send, admin_user):
        relay = _mock_relay()
        with patch("bot.desktop_relay", relay):
            from bot.commands import _lightweight_help
            await _lightweight_help("telegram", "12345", "999999")
        assert mock_send.called

    async def test_help_relay_admin(self, mock_send, admin_user):
        relay = _mock_relay(enabled=True)
        relay.get_relay_mode.return_value = {"session_name": "test"}
        with patch("bot.desktop_relay", relay):
            from bot.commands import _lightweight_help
            await _lightweight_help("telegram", "-100999", "999999")
        assert mock_send.called

    # ---- /status ----

    async def test_status(self, mock_send, admin_user):
        pool = _mock_pool()
        tm = MagicMock()
        tm.get_active_tasks = MagicMock(return_value=[])
        pq = MagicMock()
        pq.get_pending_count = AsyncMock(return_value=0)
        with patch("bot.agent.client_pool", pool), \
             patch("bot.agent.get_rate_limit_state", return_value={}), \
             patch("bot.harness.get_chat_assignments", return_value={}), \
             patch("bot.harness.get_chat_overrides", return_value={}), \
             patch("bot.harness.get_chat_harness_label", return_value=""), \
             patch("main.task_manager", tm, create=True), \
             patch("main.persistent_queue", pq, create=True):
            from bot.commands import _lightweight_status
            await _lightweight_status("telegram", "12345")
        assert mock_send.called

    # ---- /chats ----

    async def test_chats_non_admin(self, mock_send, non_admin_user):
        # CB-139: /chats is now accessible to non-admin (shows own chat only)
        with patch("bot.commands._refresh_external_titles", AsyncMock()), \
             patch("bot.commands._get_ec_display_list", AsyncMock(return_value=([], set()))), \
             patch("bot.harness.get_chat_harness_label", return_value=""), \
             patch("bot.harness.get_chat_overrides", return_value={}), \
             patch("bot.harness.get_harness", return_value=None), \
             patch("bot.desktop_relay", _mock_relay()):
            from bot.commands import _lightweight_chats
            await _lightweight_chats("telegram", "12345", "99999")
        assert mock_send.called

    async def test_chats_admin(self, mock_send, admin_user):
        with patch("bot.commands._refresh_external_titles", AsyncMock()), \
             patch("bot.commands._get_ec_display_list", AsyncMock(return_value=([], set()))), \
             patch("bot.harness.get_chat_harness_label", return_value=""), \
             patch("bot.harness.get_chat_overrides", return_value={}), \
             patch("bot.harness.get_harness", return_value=None), \
             patch("bot.desktop_relay", _mock_relay()):
            from bot.commands import _lightweight_chats
            await _lightweight_chats("telegram", "999999", "999999")
        assert mock_send.called

    # ---- /usage ----

    async def test_usage_multi_timeframe(self, mock_send, admin_user):
        with patch("bot.storage.memory.get_usage_by_profile", AsyncMock(return_value=[])):
            from bot.commands import _lightweight_usage
            await _lightweight_usage("telegram", "999999", "999999", hours=0)
        assert mock_send.called

    async def test_usage_single_timeframe(self, mock_send, admin_user):
        with patch("bot.storage.memory.get_usage_by_profile", AsyncMock(return_value=[
            {"profile": "default", "cost_usd": 1.23, "queries": 5},
        ])):
            from bot.commands import _lightweight_usage
            await _lightweight_usage("telegram", "999999", "999999", hours=24)
        assert mock_send.called

    async def test_usage_non_admin(self, mock_send, non_admin_user):
        from bot.commands import _lightweight_usage
        await _lightweight_usage("telegram", "12345", "99999")
        assert mock_send.called

    # ---- /claude-accounts ----

    async def test_claude_accounts(self, mock_send, admin_user):
        from bot.commands import _lightweight_claude_accounts
        await _lightweight_claude_accounts("telegram", "999999", "999999")
        assert mock_send.called

    async def test_claude_accounts_non_admin(self, mock_send, non_admin_user):
        from bot.commands import _lightweight_claude_accounts
        await _lightweight_claude_accounts("telegram", "12345", "99999")
        assert mock_send.called

    # ---- /relays (daemons) ----

    async def test_daemons_disabled(self, mock_send, admin_user):
        relay = _mock_relay(enabled=False)
        with patch("bot.desktop_relay", relay):
            from bot.commands import _lightweight_daemons
            await _lightweight_daemons("telegram", "999999", "999999")
        assert mock_send.called

    async def test_daemons_enabled(self, mock_send, admin_user):
        relay = _mock_relay(enabled=True)
        with patch("bot.desktop_relay", relay), \
             patch("bot.relay.pool.get_pool", return_value=MagicMock(
                 health_all=MagicMock(return_value=[]))):
            from bot.commands import _lightweight_daemons
            await _lightweight_daemons("telegram", "999999", "999999")
        assert mock_send.called

    # ---- /mac ----

    async def test_mac_status_disabled(self, mock_send, admin_user):
        relay = _mock_relay(enabled=False)
        with patch("bot.desktop_relay", relay):
            from bot.commands import _lightweight_mac_status
            await _lightweight_mac_status("telegram", "999999", "999999")
        assert mock_send.called

    async def test_mac_status_enabled(self, mock_send, admin_user):
        relay = _mock_relay(enabled=True)
        with patch("bot.desktop_relay", relay):
            from bot.commands import _lightweight_mac_status
            await _lightweight_mac_status("telegram", "999999", "999999")
        assert mock_send.called

    async def test_mac_status_non_admin(self, mock_send, non_admin_user):
        from bot.commands import _lightweight_mac_status
        await _lightweight_mac_status("telegram", "12345", "99999")
        assert mock_send.called

    # ---- /lock ----

    async def test_mac_lock(self, mock_send, admin_user):
        relay = _mock_relay(enabled=True)
        with patch("bot.desktop_relay", relay):
            from bot.commands import _lightweight_mac_lock
            await _lightweight_mac_lock("telegram", "999999", "999999")
        assert mock_send.called

    async def test_mac_lock_non_admin(self, mock_send, non_admin_user):
        from bot.commands import _lightweight_mac_lock
        await _lightweight_mac_lock("telegram", "12345", "99999")
        assert mock_send.called

    async def test_mac_lock_disabled(self, mock_send, admin_user):
        relay = _mock_relay(enabled=False)
        with patch("bot.desktop_relay", relay):
            from bot.commands import _lightweight_mac_lock
            await _lightweight_mac_lock("telegram", "999999", "999999")
        assert mock_send.called

    # ---- /mac-sessions ----

    async def test_mac_sessions(self, mock_send, admin_user):
        relay = _mock_relay(enabled=True)
        relay.list_all_jsonls = AsyncMock(return_value=[])
        with patch("bot.desktop_relay", relay):
            from bot.commands import _lightweight_mac_sessions
            await _lightweight_mac_sessions("telegram", "999999", "999999")
        assert mock_send.called

    async def test_mac_sessions_disabled(self, mock_send, admin_user):
        relay = _mock_relay(enabled=False)
        with patch("bot.desktop_relay", relay):
            from bot.commands import _lightweight_mac_sessions
            await _lightweight_mac_sessions("telegram", "999999", "999999")
        assert mock_send.called

    async def test_mac_sessions_non_admin(self, mock_send, non_admin_user):
        from bot.commands import _lightweight_mac_sessions
        await _lightweight_mac_sessions("telegram", "12345", "99999")
        assert mock_send.called

    # ---- /session ----

    async def test_session_non_relay(self, mock_send, admin_user):
        relay = _mock_relay(enabled=True)
        relay.list_all_jsonls = AsyncMock(return_value=[])
        with patch("bot.desktop_relay", relay):
            from bot.commands import _lightweight_session
            await _lightweight_session("telegram", "999999", "999999", "")
        assert mock_send.called

    async def test_session_relay_info(self, mock_send, admin_user):
        relay = _mock_relay(enabled=True)
        relay.get_relay_mode.return_value = {
            "session_name": "test_session",
            "project_path": "/Users/test/project",
            "pinned_jsonl_path": "path/to/aaaabbbb-cccc-dddd-eeee-ffffgggghhhh.jsonl",
        }
        with patch("bot.desktop_relay", relay):
            from bot.commands import _lightweight_session
            await _lightweight_session("telegram", "-100999", "999999", "")
        assert mock_send.called

    async def test_session_relay_list(self, mock_send, admin_user):
        relay = _mock_relay(enabled=True)
        relay.get_relay_mode.return_value = {
            "session_name": "test_session",
            "project_path": "/Users/test/project",
            "pinned_jsonl_path": "path/to/aaaabbbb-cccc-dddd-eeee-ffffgggghhhh.jsonl",
        }
        relay.list_project_jsonls = AsyncMock(return_value=[])
        with patch("bot.desktop_relay", relay):
            from bot.commands import _lightweight_session
            await _lightweight_session("telegram", "-100999", "999999", "list")
        assert mock_send.called

    async def test_session_non_admin(self, mock_send, non_admin_user):
        relay = _mock_relay(enabled=True)
        with patch("bot.desktop_relay", relay):
            from bot.commands import _lightweight_session
            await _lightweight_session("telegram", "12345", "99999", "")
        assert mock_send.called

    async def test_session_disabled(self, mock_send, admin_user):
        relay = _mock_relay(enabled=False)
        with patch("bot.desktop_relay", relay):
            from bot.commands import _lightweight_session
            await _lightweight_session("telegram", "999999", "999999", "")
        assert mock_send.called

    # ---- /delete-chat ----

    async def test_delete_chat_out_of_range(self, mock_send, admin_user):
        with patch("bot.commands._get_ec_display_list", AsyncMock(return_value=([], set()))):
            from bot.commands import _lightweight_delete_chat
            await _lightweight_delete_chat("telegram", "999999", "999999", 1)
        assert mock_send.called

    async def test_delete_chat_non_admin(self, mock_send, non_admin_user):
        from bot.commands import _lightweight_delete_chat
        await _lightweight_delete_chat("telegram", "12345", "99999", 1)
        assert mock_send.called

    # ---- /rollback ----

    async def test_rollback_no_marker(self, mock_send, admin_user):
        with patch("pathlib.Path.exists", return_value=False):
            from bot.commands import _lightweight_rollback
            await _lightweight_rollback("telegram", "999999", "999999", confirm=False)
        assert mock_send.called

    async def test_rollback_non_admin(self, mock_send, non_admin_user):
        from bot.commands import _lightweight_rollback
        await _lightweight_rollback("telegram", "12345", "99999", confirm=False)
        assert mock_send.called

    # ---- /harnesses ----

    async def test_harnesses_list(self, mock_send, admin_user):
        with patch("bot.harness.list_harnesses", return_value={}), \
             patch("bot.harness.get_harness", return_value=None), \
             patch("bot.harness.format_harness", return_value="default: sonnet"), \
             patch("bot.harness.get_chat_assignments", return_value={}):
            from bot.commands import _lightweight_harnesses
            await _lightweight_harnesses("telegram", "999999", "999999")
        assert mock_send.called

    async def test_harnesses_specific_not_found(self, mock_send, admin_user):
        with patch("bot.harness.list_harnesses", return_value={}), \
             patch("bot.harness.get_harness", return_value=None), \
             patch("bot.harness.format_harness", return_value=""), \
             patch("bot.harness.get_chat_assignments", return_value={}):
            from bot.commands import _lightweight_harnesses
            await _lightweight_harnesses("telegram", "999999", "999999", specific="unknown")
        assert mock_send.called

    async def test_harnesses_non_admin(self, mock_send, non_admin_user):
        from bot.commands import _lightweight_harnesses
        await _lightweight_harnesses("telegram", "12345", "99999")
        assert mock_send.called

    # ---- /set-harness ----

    async def test_set_harness_not_found(self, mock_send, admin_user):
        with patch("bot.harness.set_chat_harness", MagicMock()), \
             patch("bot.harness.get_harness", return_value=None):
            from bot.commands import _lightweight_set_harness
            await _lightweight_set_harness("telegram", "999999", "999999", "nonexistent")
        assert mock_send.called

    async def test_set_harness_valid(self, mock_send, admin_user):
        harness_obj = MagicMock(label="coding")
        with patch("bot.harness.set_chat_harness", MagicMock()), \
             patch("bot.harness.get_harness", return_value=harness_obj), \
             patch("bot.harness.format_harness", return_value="coding: opus"), \
             patch("bot.agent.client_pool", _mock_pool()):
            from bot.commands import _lightweight_set_harness
            await _lightweight_set_harness("telegram", "999999", "999999", "coding")
        assert mock_send.called

    async def test_set_harness_non_admin(self, mock_send, non_admin_user):
        from bot.commands import _lightweight_set_harness
        await _lightweight_set_harness("telegram", "12345", "99999", "default")
        assert mock_send.called

    # ---- /claude-set-oauth (legacy /switch-oauth) ----

    async def test_switch_oauth(self, mock_send, admin_user):
        with patch("config_overrides.is_default_oauth", return_value=False), \
             patch("bot.agent.client_pool", _mock_pool()):
            from bot.commands import _lightweight_switch_oauth
            await _lightweight_switch_oauth("telegram", "999999", "999999", "personal")
        assert mock_send.called

    async def test_switch_oauth_all(self, mock_send, admin_user):
        with patch("config_overrides.is_default_oauth", return_value=False), \
             patch("bot.agent.client_pool", _mock_pool()):
            from bot.commands import _lightweight_switch_oauth
            await _lightweight_switch_oauth("telegram", "999999", "999999", "personal", all_chats=True)
        assert mock_send.called

    async def test_switch_oauth_non_admin(self, mock_send, non_admin_user):
        from bot.commands import _lightweight_switch_oauth
        await _lightweight_switch_oauth("telegram", "12345", "99999", "personal")
        assert mock_send.called

    # ---- /auth ----

    async def test_auth_non_admin(self, mock_send, non_admin_user):
        from bot.commands import _lightweight_auth
        await _lightweight_auth("telegram", "12345", "99999", "123456")
        assert mock_send.called

    async def test_auth_disabled(self, mock_send, admin_user):
        relay = _mock_relay(enabled=False)
        with patch("bot.desktop_relay", relay):
            from bot.commands import _lightweight_auth
            await _lightweight_auth("telegram", "999999", "999999", "123456")
        assert mock_send.called

    async def test_auth_success(self, mock_send, admin_user):
        relay = _mock_relay(enabled=True)
        with patch("bot.desktop_relay", relay):
            from bot.commands import _lightweight_auth
            await _lightweight_auth("telegram", "999999", "999999", "123456")
        assert mock_send.called

    # ---- /claude-auth ----

    async def test_claude_auth_help(self, mock_send, admin_user):
        from bot.commands import _lightweight_claude_auth
        await _lightweight_claude_auth("telegram", "999999", "999999", label="")
        assert mock_send.called

    async def test_claude_auth_cancel_no_pending(self, mock_send, admin_user):
        from bot.commands import _lightweight_claude_auth
        await _lightweight_claude_auth("telegram", "999999", "999999", label="cancel")
        assert mock_send.called

    async def test_claude_auth_non_admin(self, mock_send, non_admin_user):
        from bot.commands import _lightweight_claude_auth
        await _lightweight_claude_auth("telegram", "12345", "99999", label="test")
        assert mock_send.called

    # ---- /reset ----

    async def test_reset_session(self, mock_send, admin_user):
        pool = _mock_pool()
        with patch("bot.agent.client_pool", pool):
            from bot.commands import _handle_reset_session
            await _handle_reset_session("telegram", "999999", "999999", "/reset")
        assert mock_send.called

    async def test_reset_session_target(self, mock_send, admin_user):
        pool = _mock_pool()
        with patch("bot.agent.client_pool", pool):
            from bot.commands import _handle_reset_session
            await _handle_reset_session("telegram", "999999", "999999", "/reset telegram:99999")
        assert mock_send.called

    async def test_reset_session_non_admin(self, mock_send, non_admin_user):
        from bot.commands import _handle_reset_session
        await _handle_reset_session("telegram", "12345", "99999", "/reset")
        assert mock_send.called

    # ---- /create-relay ----

    async def test_create_relay_disabled(self, mock_send, admin_user):
        relay = _mock_relay(enabled=False)
        with patch("bot.desktop_relay", relay):
            from bot.commands import _lightweight_create_relay
            await _lightweight_create_relay("telegram", "999999", "999999", 1)
        assert mock_send.called

    async def test_create_relay_no_sessions(self, mock_send, admin_user):
        relay = _mock_relay(enabled=True)
        relay.list_all_jsonls = AsyncMock(return_value=[])
        with patch("bot.desktop_relay", relay):
            from bot.commands import _lightweight_create_relay
            await _lightweight_create_relay("telegram", "999999", "999999", 1)
        assert mock_send.called

    async def test_create_relay_non_admin(self, mock_send, non_admin_user):
        from bot.commands import _lightweight_create_relay
        await _lightweight_create_relay("telegram", "12345", "99999", 1)
        assert mock_send.called

    # ---- /delete-relay ----

    async def test_delete_relay_disabled(self, mock_send, admin_user):
        relay = _mock_relay(enabled=False)
        with patch("bot.desktop_relay", relay):
            from bot.commands import _lightweight_delete_relay
            await _lightweight_delete_relay("telegram", "999999", "999999", 1)
        assert mock_send.called

    async def test_delete_relay_out_of_range(self, mock_send, admin_user):
        relay = _mock_relay(enabled=True)
        relay.get_all_links = AsyncMock(return_value={})
        with patch("bot.desktop_relay", relay):
            from bot.commands import _lightweight_delete_relay
            await _lightweight_delete_relay("telegram", "999999", "999999", 1)
        assert mock_send.called

    async def test_delete_relay_non_admin(self, mock_send, non_admin_user):
        from bot.commands import _lightweight_delete_relay
        await _lightweight_delete_relay("telegram", "12345", "99999", 1)
        assert mock_send.called

    # ---- /relay-reap ----

    async def test_daemon_reap(self, mock_send, admin_user):
        relay = _mock_relay(enabled=True)
        with patch("bot.desktop_relay", relay):
            from bot.commands import _lightweight_daemon_reap
            await _lightweight_daemon_reap("telegram", "999999", "999999")
        assert mock_send.called

    async def test_daemon_reap_non_admin(self, mock_send, non_admin_user):
        from bot.commands import _lightweight_daemon_reap
        await _lightweight_daemon_reap("telegram", "12345", "99999")
        assert mock_send.called

    async def test_daemon_reap_disabled(self, mock_send, admin_user):
        relay = _mock_relay(enabled=False)
        with patch("bot.desktop_relay", relay):
            from bot.commands import _lightweight_daemon_reap
            await _lightweight_daemon_reap("telegram", "999999", "999999")
        assert mock_send.called

    # ---- /relay-reset ----

    async def test_daemon_kill_disabled(self, mock_send, admin_user):
        relay = _mock_relay(enabled=False)
        with patch("bot.desktop_relay", relay):
            from bot.commands import _lightweight_daemon_kill
            await _lightweight_daemon_kill("telegram", "999999", "999999", "1")
        assert mock_send.called

    async def test_daemon_kill_non_admin(self, mock_send, non_admin_user):
        from bot.commands import _lightweight_daemon_kill
        await _lightweight_daemon_kill("telegram", "12345", "99999", "1")
        assert mock_send.called
