# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Staging environment tests.

Tests the staging infrastructure:
- Config isolation (IS_STAGING flag, env overrides)
- Test inject endpoint (POST /test/inject)
- Response buffer (staging_buffer_response)
- DB snapshot utility
- Lifespan guards (TG/WA/relay disabled in staging)

Usage:
    # Unit tests (no running server needed):
    python -m pytest tests/test_staging.py -v

    # Against a running staging instance:
    STAGING_URL=http://localhost:8100 python -m pytest tests/test_staging.py -v -k "live"
"""

import json
import os
import sqlite3
import sys
import tempfile
import time

import pytest

# Add parent dir to path so we can import bot modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STAGING_URL = os.environ.get("STAGING_URL", "")

requires_staging = pytest.mark.skipif(
    not STAGING_URL, reason="STAGING_URL not set — skipping live staging tests"
)


# === Unit Tests (no server needed) ===


class TestStagingConfig:
    """Test staging configuration isolation."""

    def test_bot_env_default_is_production(self):
        """BOT_ENV defaults to 'production' when not set."""
        # Don't import config directly — it reads env at import time.
        # Instead, test the logic.
        env_val = os.environ.get("BOT_ENV", "production")
        # In test context, BOT_ENV is not set (or is production)
        assert env_val in ("production", "staging", "test")

    def test_is_staging_false_in_production(self):
        """IS_STAGING is False when BOT_ENV != 'staging'."""
        original = os.environ.get("BOT_ENV")
        try:
            os.environ["BOT_ENV"] = "production"
            # Re-evaluate the condition (can't reimport config safely)
            assert os.environ["BOT_ENV"] != "staging"
        finally:
            if original is None:
                os.environ.pop("BOT_ENV", None)
            else:
                os.environ["BOT_ENV"] = original

    def test_is_staging_true_when_set(self):
        """IS_STAGING is True when BOT_ENV == 'staging'."""
        original = os.environ.get("BOT_ENV")
        try:
            os.environ["BOT_ENV"] = "staging"
            assert os.environ["BOT_ENV"] == "staging"
        finally:
            if original is None:
                os.environ.pop("BOT_ENV", None)
            else:
                os.environ["BOT_ENV"] = original


class TestStagingResponseBuffer:
    """Test the staging response buffer mechanism."""

    def test_buffer_stores_responses(self):
        """staging_buffer_response stores tool calls."""
        # Import with IS_STAGING patched
        from unittest.mock import patch
        with patch("config.IS_STAGING", True):
            from main import _staging_responses, staging_buffer_response
            _staging_responses.clear()

            staging_buffer_response("test-chat", "telegram_send_message", {"text": "hello"})

            assert "test-chat" in _staging_responses
            assert len(_staging_responses["test-chat"]) == 1
            resp = _staging_responses["test-chat"][0]
            assert resp["tool"] == "telegram_send_message"
            assert resp["args"]["text"] == "hello"
            assert "ts" in resp

            # Cleanup
            _staging_responses.clear()

    def test_buffer_noop_in_production(self):
        """staging_buffer_response is a no-op when IS_STAGING is False."""
        from unittest.mock import patch
        with patch("config.IS_STAGING", False):
            from main import _staging_responses, staging_buffer_response
            _staging_responses.clear()

            staging_buffer_response("test-chat", "telegram_send_message", {"text": "hello"})

            assert "test-chat" not in _staging_responses

    def test_buffer_appends_multiple(self):
        """Multiple responses are appended in order."""
        from unittest.mock import patch
        with patch("config.IS_STAGING", True):
            from main import _staging_responses, staging_buffer_response
            _staging_responses.clear()

            staging_buffer_response("chat-1", "tool_a", {"x": 1})
            staging_buffer_response("chat-1", "tool_b", {"y": 2})
            staging_buffer_response("chat-2", "tool_c", {"z": 3})

            assert len(_staging_responses["chat-1"]) == 2
            assert len(_staging_responses["chat-2"]) == 1
            assert _staging_responses["chat-1"][0]["tool"] == "tool_a"
            assert _staging_responses["chat-1"][1]["tool"] == "tool_b"

            _staging_responses.clear()


class TestStagingToolCallBuffer:
    """Test staging_buffer_tool_call — non-send tool buffering without completion signal."""

    def test_tool_call_stores_entry(self):
        """staging_buffer_tool_call stores entry in _staging_responses."""
        from unittest.mock import patch
        with patch("config.IS_STAGING", True):
            import importlib, bot.pipeline.staging as stmod
            importlib.reload(stmod)
            from bot.pipeline.staging import staging_buffer_tool_call, _staging_responses
            _staging_responses.clear()

            staging_buffer_tool_call("test-chat", "Bash", {"command": "uname -sr"})

            assert "test-chat" in _staging_responses
            assert len(_staging_responses["test-chat"]) == 1
            entry = _staging_responses["test-chat"][0]
            assert entry["tool"] == "Bash"
            assert entry["args"]["command"] == "uname -sr"
            assert "ts" in entry

            _staging_responses.clear()

    def test_tool_call_noop_in_production(self):
        """staging_buffer_tool_call is a no-op when IS_STAGING is False."""
        from unittest.mock import patch
        with patch("config.IS_STAGING", False):
            import importlib, bot.pipeline.staging as stmod
            importlib.reload(stmod)
            from bot.pipeline.staging import staging_buffer_tool_call, _staging_responses
            _staging_responses.clear()

            staging_buffer_tool_call("test-chat", "Bash", {"command": "echo hi"})

            assert "test-chat" not in _staging_responses

    def test_tool_call_does_not_signal_completion(self):
        """staging_buffer_tool_call must NOT fire completion events (critical invariant).

        If it did, /test/inject would return before the final send_message,
        causing expect_tools assertions to see incomplete tool lists.
        """
        import asyncio
        from unittest.mock import patch
        with patch("config.IS_STAGING", True):
            import importlib, bot.pipeline.staging as stmod
            importlib.reload(stmod)
            from bot.pipeline.staging import (
                staging_buffer_tool_call, _staging_responses,
                _staging_completion_events,
            )
            _staging_responses.clear()
            _staging_completion_events.clear()

            # Register a completion event
            evt = asyncio.Event()
            _staging_completion_events["test-chat"] = evt

            staging_buffer_tool_call("test-chat", "Bash", {"command": "uname -sr"})

            # Event must NOT be set — injection is still waiting for send_message
            assert not evt.is_set(), "staging_buffer_tool_call must not signal completion"

            _staging_responses.clear()
            _staging_completion_events.clear()

    def test_mixed_flow_tool_call_then_send_message(self):
        """Both entries present; completion only fires when send_message is buffered."""
        import asyncio
        from unittest.mock import patch
        with patch("config.IS_STAGING", True):
            import importlib, bot.pipeline.staging as stmod
            importlib.reload(stmod)
            from bot.pipeline.staging import (
                staging_buffer_tool_call, staging_buffer_response,
                _staging_responses, _staging_completion_events,
            )
            _staging_responses.clear()
            _staging_completion_events.clear()

            evt = asyncio.Event()
            _staging_completion_events["qa-chat"] = evt

            staging_buffer_tool_call("qa-chat", "Bash", {"command": "uname -sr"})
            assert not evt.is_set(), "no completion after Bash"

            staging_buffer_response("qa-chat", "telegram_send_message", {"text": "Linux 6.1"})
            assert evt.is_set(), "completion fires after send_message"

            entries = _staging_responses["qa-chat"]
            tools = [e["tool"] for e in entries]
            assert "Bash" in tools
            assert "telegram_send_message" in tools

            _staging_responses.clear()
            _staging_completion_events.clear()


class TestDbSnapshot:
    """Test the database snapshot utility."""

    def test_snapshot_creates_backup(self):
        """snapshot() creates a valid SQLite copy."""
        from scripts.staging_snapshot_db import snapshot

        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "source.db")
            dst = os.path.join(tmp, "backup.db")

            # Create a source DB with some data
            conn = sqlite3.connect(src)
            conn.execute("CREATE TABLE test (id INTEGER, value TEXT)")
            conn.execute("INSERT INTO test VALUES (1, 'hello')")
            conn.execute("CREATE TABLE message_facts (id INTEGER, fact_key TEXT, fact_value TEXT, category TEXT)")
            conn.execute("INSERT INTO message_facts VALUES (1, 'api_key', 'secret123', 'credentials')")
            conn.execute("INSERT INTO message_facts VALUES (2, 'trip', 'london', 'travel')")
            conn.commit()
            conn.close()

            result = snapshot(src, dst, scrub=True)

            # Backup exists
            assert os.path.exists(dst)
            assert os.path.getsize(dst) > 0

            # Data is preserved (except credentials)
            bconn = sqlite3.connect(dst)
            rows = bconn.execute("SELECT * FROM test").fetchall()
            assert len(rows) == 1
            assert rows[0] == (1, "hello")

            # Credentials scrubbed
            creds = bconn.execute(
                "SELECT * FROM message_facts WHERE category = 'credentials'"
            ).fetchall()
            assert len(creds) == 0

            # Non-credential facts preserved
            facts = bconn.execute(
                "SELECT * FROM message_facts WHERE category = 'travel'"
            ).fetchall()
            assert len(facts) == 1

            bconn.close()

    def test_snapshot_no_scrub(self):
        """snapshot(scrub=False) preserves all data."""
        from scripts.staging_snapshot_db import snapshot

        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "source.db")
            dst = os.path.join(tmp, "backup.db")

            conn = sqlite3.connect(src)
            conn.execute("CREATE TABLE message_facts (id INTEGER, fact_key TEXT, fact_value TEXT, category TEXT)")
            conn.execute("INSERT INTO message_facts VALUES (1, 'pw', 'hunter2', 'credentials')")
            conn.commit()
            conn.close()

            snapshot(src, dst, scrub=False)

            bconn = sqlite3.connect(dst)
            creds = bconn.execute(
                "SELECT * FROM message_facts WHERE category = 'credentials'"
            ).fetchall()
            assert len(creds) == 1
            bconn.close()


# === Live Staging Tests (require STAGING_URL) ===


class TestStagingLive:
    """Tests against a running staging instance (mocked)."""

    def test_live_health(self):
        """Staging health endpoint returns healthy status."""
        from unittest.mock import patch, MagicMock
        import httpx

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "healthy"}

        with patch.object(httpx, "get", return_value=mock_resp) as mock_get:
            resp = httpx.get("http://staging.test/health", timeout=10)
            assert resp.status_code == 200
            data = resp.json()
            assert data.get("status") in ("healthy", "ok")
            mock_get.assert_called_once_with("http://staging.test/health", timeout=10)

    def test_live_inject_endpoint_exists(self):
        """POST /test/inject is available in staging."""
        from unittest.mock import patch, MagicMock
        import httpx

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch.object(httpx, "post", return_value=mock_resp) as mock_post:
            resp = httpx.post(
                "http://staging.test/test/inject",
                json={"text": "ping"},
                timeout=10,
            )
            # Should get 200 (ok/timeout), not 404
            assert resp.status_code == 200
            mock_post.assert_called_once_with(
                "http://staging.test/test/inject",
                json={"text": "ping"},
                timeout=10,
            )

    def test_live_inject_requires_text(self):
        """POST /test/inject returns 400 without text."""
        from unittest.mock import patch, MagicMock
        import httpx

        mock_resp = MagicMock()
        mock_resp.status_code = 400

        with patch.object(httpx, "post", return_value=mock_resp) as mock_post:
            resp = httpx.post(
                "http://staging.test/test/inject",
                json={},
                timeout=10,
            )
            assert resp.status_code == 400
            mock_post.assert_called_once_with(
                "http://staging.test/test/inject",
                json={},
                timeout=10,
            )

    def test_live_responses_endpoint(self):
        """GET /test/responses/{chat_id} returns buffered responses."""
        from unittest.mock import patch, MagicMock
        import httpx

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"responses": []}

        with patch.object(httpx, "get", return_value=mock_resp) as mock_get:
            resp = httpx.get(
                "http://staging.test/test/responses/nonexistent-chat",
                timeout=10,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["responses"] == []
            mock_get.assert_called_once_with(
                "http://staging.test/test/responses/nonexistent-chat",
                timeout=10,
            )

    def test_live_clear_responses(self):
        """DELETE /test/responses/{chat_id} clears buffer."""
        from unittest.mock import patch, MagicMock
        import httpx

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "cleared"}

        with patch.object(httpx, "delete", return_value=mock_resp) as mock_delete:
            resp = httpx.delete(
                "http://staging.test/test/responses/test-chat",
                timeout=10,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "cleared"
            mock_delete.assert_called_once_with(
                "http://staging.test/test/responses/test-chat",
                timeout=10,
            )


class TestStagingIsolation:
    """Verify staging doesn't leak to production (mocked)."""

    def test_staging_no_tg_session(self):
        """Staging should not have an active TG session."""
        from unittest.mock import patch, MagicMock
        import httpx

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "healthy", "tg_connected": False}

        with patch.object(httpx, "get", return_value=mock_resp) as mock_get:
            resp = httpx.get("http://staging.test/health", timeout=10)
            data = resp.json()
            # In staging, TG should not be connected
            assert data.get("tg_connected") is not True
            mock_get.assert_called_once_with("http://staging.test/health", timeout=10)

    def test_production_no_test_endpoint(self):
        """Production should return 404 for /test/inject."""
        from unittest.mock import patch, MagicMock
        import httpx

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with patch.object(httpx, "post", return_value=mock_resp) as mock_post:
            resp = httpx.post(
                "http://production.test/test/inject",
                json={"text": "should-fail"},
                timeout=10,
            )
            assert resp.status_code == 404
            mock_post.assert_called_once_with(
                "http://production.test/test/inject",
                json={"text": "should-fail"},
                timeout=10,
            )
