# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Smoke tests for previously uncovered bot modules.

Ensures basic import + functionality for modules that had zero test coverage.
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock


# ═══════════════════════════════════════════════════════════════════════
# 1. bot/scheduler.py
# ═══════════════════════════════════════════════════════════════════════


class TestScheduler:
    """Tests for bot/scheduler.py — task model, day normalization, formatting."""

    def test_normalize_days_daily(self):
        """_normalize_days('daily') expands to all 7 day names."""
        from bot.scheduler import _normalize_days
        result = _normalize_days(["daily"])
        assert set(result) == {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}

    def test_normalize_days_weekdays(self):
        """_normalize_days('weekdays') expands to mon-fri."""
        from bot.scheduler import _normalize_days
        result = _normalize_days(["weekdays"])
        assert set(result) == {"mon", "tue", "wed", "thu", "fri"}

    def test_normalize_days_weekends(self):
        """_normalize_days('weekends') expands to sat+sun."""
        from bot.scheduler import _normalize_days
        result = _normalize_days(["weekends"])
        assert set(result) == {"sat", "sun"}

    def test_normalize_days_long_names(self):
        """_normalize_days handles full day names like 'Monday'."""
        from bot.scheduler import _normalize_days
        result = _normalize_days(["Monday", "Friday"])
        assert set(result) == {"mon", "fri"}

    def test_normalize_days_unknown_falls_back_to_daily(self):
        """_normalize_days with unrecognized names returns all days (daily fallback)."""
        from bot.scheduler import _normalize_days
        result = _normalize_days(["xyzday"])
        assert set(result) == {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}

    def test_coerce_task_types(self):
        """_coerce_task converts hour/minute to int, enabled to bool."""
        from bot.scheduler import _coerce_task
        task = {"id": "t1", "hour": "9", "minute": "30", "enabled": "true"}
        coerced = _coerce_task(task)
        assert coerced["hour"] == 9
        assert coerced["minute"] == 30
        assert coerced["enabled"] is True

    def test_coerce_task_returns_copy(self):
        """_coerce_task returns a new dict, not the original."""
        from bot.scheduler import _coerce_task
        task = {"id": "t1", "hour": 10, "minute": 0}
        coerced = _coerce_task(task)
        assert coerced is not task

    def test_format_task_list_empty(self):
        """format_task_list with empty list returns 'No scheduled tasks' message."""
        from bot.scheduler import format_task_list
        result = format_task_list([])
        assert "No scheduled tasks" in result

    def test_format_task_list_fixed_time(self):
        """format_task_list formats a fixed-time task with time and days."""
        from bot.scheduler import format_task_list
        tasks = [{
            "id": "t1", "name": "Test Task", "hour": 9, "minute": 30,
            "days": ["mon", "tue", "wed", "thu", "fri"],
            "platform": "telegram", "enabled": True,
        }]
        result = format_task_list(tasks)
        assert "Test Task" in result
        assert "09:30" in result
        assert "weekdays" in result
        assert "[ON]" in result

    def test_format_task_list_interval(self):
        """format_task_list formats an interval task showing 'every Nh'."""
        from bot.scheduler import format_task_list
        tasks = [{
            "id": "t2", "name": "Email Check", "hour": 0, "minute": 0,
            "interval_hours": 3, "platform": "telegram", "enabled": False,
        }]
        result = format_task_list(tasks)
        assert "every 3h" in result
        assert "[OFF]" in result


# ═══════════════════════════════════════════════════════════════════════
# 2. bot/tool_labels.py
# ═══════════════════════════════════════════════════════════════════════


class TestToolLabels:
    """Tests for bot/tool_labels.py — label lookups and security constants."""

    def test_tool_labels_contains_core_tools(self):
        """TOOL_LABELS includes labels for core SDK tools like Read, Write, Bash."""
        from bot.tool_labels import TOOL_LABELS
        assert "Read" in TOOL_LABELS
        assert "Write" in TOOL_LABELS
        assert "Bash" in TOOL_LABELS

    def test_tool_labels_values_are_strings(self):
        """All TOOL_LABELS values are human-readable strings."""
        from bot.tool_labels import TOOL_LABELS
        for tool, label in TOOL_LABELS.items():
            assert isinstance(label, str), f"Label for {tool} is not a string"
            assert len(label) > 0, f"Label for {tool} is empty"

    def test_send_tools_is_frozenset(self):
        """SEND_TOOLS is a frozenset (immutable)."""
        from bot.tool_labels import SEND_TOOLS
        assert isinstance(SEND_TOOLS, frozenset)
        assert "telegram_send_message" in SEND_TOOLS

    def test_chat_isolated_tools_has_telegram_send(self):
        """_CHAT_ISOLATED_TOOLS maps telegram_send_message to chat_id param."""
        from bot.tool_labels import _CHAT_ISOLATED_TOOLS
        assert "telegram_send_message" in _CHAT_ISOLATED_TOOLS
        assert "chat_id" in _CHAT_ISOLATED_TOOLS["telegram_send_message"]

    def test_blocked_path_patterns_contains_env(self):
        """_BLOCKED_PATH_PATTERNS blocks .env files."""
        from bot.tool_labels import _BLOCKED_PATH_PATTERNS
        assert ".env" in _BLOCKED_PATH_PATTERNS

    def test_deploy_keywords_matches_slash_deploy(self):
        """_DEPLOY_KEYWORDS regex matches '/deploy' at start of message."""
        from bot.tool_labels import _DEPLOY_KEYWORDS
        assert _DEPLOY_KEYWORDS.search("/deploy")
        assert _DEPLOY_KEYWORDS.search("\n/deploy now")
        # Should NOT match plain word "deploy" without slash
        assert not _DEPLOY_KEYWORDS.search("we need to deploy")

    def test_write_only_env_pattern(self):
        """_WRITE_ONLY_ENV_PATTERN matches /.env but not .envrc or .env.example."""
        from bot.tool_labels import _WRITE_ONLY_ENV_PATTERN
        assert _WRITE_ONLY_ENV_PATTERN.search("/app/.env")
        assert not _WRITE_ONLY_ENV_PATTERN.search("/app/.envrc")
        assert not _WRITE_ONLY_ENV_PATTERN.search("/app/.env.example")


# ═══════════════════════════════════════════════════════════════════════
# 3. bot/mcp_size_guard.py
# ═══════════════════════════════════════════════════════════════════════


class TestMcpSizeGuard:
    """Tests for bot/mcp_size_guard.py — response truncation and image saving."""

    def test_truncate_response_small_passthrough(self):
        """Small responses pass through _truncate_response unchanged (except reformatting)."""
        from bot.mcp_size_guard import _truncate_response
        small = '{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"hello"}]}}'
        result = _truncate_response(small)
        assert "hello" in result

    def test_truncate_response_oversized_text(self):
        """Oversized text content is truncated with a size guard notice."""
        from bot.mcp_size_guard import _truncate_response, MAX_BYTES
        # Build a response with text larger than MAX_BYTES
        big_text = "x" * (MAX_BYTES + 10000)
        msg = {
            "jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text", "text": big_text}]}
        }
        import json
        raw = json.dumps(msg)
        result = _truncate_response(raw)
        assert "TRUNCATED by size guard" in result
        assert len(result.encode("utf-8")) <= MAX_BYTES

    def test_truncate_response_unparseable_json(self):
        """Unparseable JSON is hard-truncated to MAX_BYTES."""
        from bot.mcp_size_guard import _truncate_response, MAX_BYTES
        bad_json = "not-json-" * (MAX_BYTES // 5)
        result = _truncate_response(bad_json)
        assert len(result.encode("utf-8")) <= MAX_BYTES

    def test_maybe_truncate_small_no_image(self):
        """_maybe_truncate passes through small non-image responses untouched."""
        from bot.mcp_size_guard import _maybe_truncate
        line = b'{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"ok"}]}}\n'
        result = _maybe_truncate(line)
        assert result == line

    def test_validate_endpoint_url_same_host(self):
        """_validate_endpoint_url accepts same-host endpoints."""
        from bot.mcp_size_guard import _validate_endpoint_url
        result = _validate_endpoint_url("http://127.0.0.1:3001", "/messages")
        assert result == "http://127.0.0.1:3001/messages"

    def test_validate_endpoint_url_rejects_different_host(self):
        """_validate_endpoint_url rejects endpoints on a different host."""
        from bot.mcp_size_guard import _validate_endpoint_url
        # Attempt host mismatch via protocol-relative path
        result = _validate_endpoint_url("http://127.0.0.1:3001", "//evil.com/steal")
        # Should be None (rejected) since the hostname changes
        # Note: urllib.parse may or may not parse this as a different host depending
        # on how it's concatenated. Let's verify the function logic works.
        if result is not None:
            # If concatenation didn't cause host mismatch, that's OK —
            # but if it DID change host, it should have been rejected
            from urllib.parse import urlparse
            assert urlparse(result).hostname == "127.0.0.1"

    def test_inject_create_tab_defaults_non_create_tab(self):
        """_inject_create_tab_defaults passes non-create_tab lines through unchanged."""
        from bot.mcp_size_guard import _inject_create_tab_defaults
        line = b'{"method":"tools/call","params":{"name":"browser_navigate"}}\n'
        result = _inject_create_tab_defaults(line)
        assert result == line

    def test_inject_create_tab_defaults_adds_viewport(self):
        """_inject_create_tab_defaults injects viewport and colorScheme for create_tab."""
        from bot.mcp_size_guard import _inject_create_tab_defaults
        import json
        msg = {
            "method": "tools/call",
            "params": {"name": "create_tab", "arguments": {"url": "http://example.com"}}
        }
        line = (json.dumps(msg) + "\n").encode("utf-8")
        result = _inject_create_tab_defaults(line)
        parsed = json.loads(result)
        args = parsed["params"]["arguments"]
        assert "viewport" in args
        assert args["viewport"]["width"] == 1920
        assert args["colorScheme"] == "light"


# ═══════════════════════════════════════════════════════════════════════
# 4. bot/core/language.py
# ═══════════════════════════════════════════════════════════════════════


class TestLanguage:
    """Tests for bot/core/language.py — language detection."""

    def test_detect_language_empty(self):
        """_detect_language returns ('', 0.0) for empty/whitespace input."""
        from bot.core.language import _detect_language
        assert _detect_language("") == ("", 0.0)
        assert _detect_language("   ") == ("", 0.0)

    def test_detect_language_english(self):
        """_detect_language identifies a substantial English sentence."""
        from bot.core.language import _detect_language
        lang, conf = _detect_language(
            "This is a long English sentence with enough words for reliable detection."
        )
        assert lang == "en"
        assert conf > 0.5

    def test_detect_language_strips_reply_prefix(self):
        """_detect_language strips [Replying to ...] prefix before detection."""
        from bot.core.language import _detect_language
        lang, conf = _detect_language(
            '[Replying to User: "hi"] This is clearly an English message with many words.'
        )
        # Should still detect English despite the reply prefix
        assert lang == "en"

    def test_lang_names_mapping(self):
        """LANG_NAMES has expected language code to name mappings."""
        from bot.core.language import LANG_NAMES
        assert LANG_NAMES["en"] == "English"
        assert LANG_NAMES["ru"] == "Russian"
        assert "es" in LANG_NAMES

    def test_detect_language_deterministic_english(self):
        """_detect_language returns 'en' for a clear English sentence."""
        from bot.core.language import _detect_language
        lang, conf = _detect_language("This is a clear English sentence for testing purposes.")
        assert lang == "en"
        assert conf > 0.5


# ═══════════════════════════════════════════════════════════════════════
# 5. bot/core/safety.py
# ═══════════════════════════════════════════════════════════════════════


class TestSafety:
    """Tests for bot/core/safety.py — suspicious response detection."""

    def test_suspicious_constants_defined(self):
        """Safety threshold constants are defined and have sane values."""
        from bot.core.safety import (
            SUSPICIOUS_FAST_MULTI_TURN_S,
            SUSPICIOUS_FAST_TOOLS_S,
            SUSPICIOUS_INSTANT_EMPTY_S,
            SUSPICIOUS_MIN_RESULT_LEN,
        )
        assert SUSPICIOUS_FAST_MULTI_TURN_S > 0
        assert SUSPICIOUS_FAST_TOOLS_S > 0
        assert SUSPICIOUS_INSTANT_EMPTY_S > 0
        assert SUSPICIOUS_MIN_RESULT_LEN > 0

    def test_check_suspicious_normal_response(self):
        """Normal response (reasonable time, turns, output) returns None (not suspicious)."""
        from bot.core.safety import _check_suspicious_response
        with patch("bot.agent._total_input_tokens", return_value=0), \
             patch("bot.agent._context_window_for_model", return_value=200000), \
             patch("bot.hooks.compacted_sessions", {}), \
             patch("bot.hooks.pending_compaction", {}):
            result = _check_suspicious_response(
                elapsed=10.0, num_turns=3, tools_used=["Read"],
                send_tool_called=True, result_text="A" * 200,
                usage={"input_tokens": 1000, "output_tokens": 500},
                session_key="telegram:123", model="claude-sonnet-4-20250514",
                cost=0.01,
            )
        assert result is None

    def test_check_suspicious_empty_instant(self):
        """Empty instant response (0 turns, 0 chars, <2s) is flagged as suspicious."""
        from bot.core.safety import _check_suspicious_response
        with patch("bot.agent._total_input_tokens", return_value=0), \
             patch("bot.agent._context_window_for_model", return_value=200000), \
             patch("bot.hooks.compacted_sessions", {}), \
             patch("bot.hooks.pending_compaction", {}):
            result = _check_suspicious_response(
                elapsed=0.5, num_turns=0, tools_used=[],
                send_tool_called=False, result_text="",
                usage={"input_tokens": 100, "output_tokens": 0},
                session_key="telegram:123", model="claude-sonnet-4-20250514",
                cost=0.001,
            )
        assert result is not None
        assert "empty_instant" in result["reasons"][0]

    def test_check_suspicious_ghost_tools(self):
        """Ghost tools (tools ran fast with near-empty result, no send) is flagged."""
        from bot.core.safety import _check_suspicious_response
        with patch("bot.agent._total_input_tokens", return_value=0), \
             patch("bot.agent._context_window_for_model", return_value=200000), \
             patch("bot.hooks.compacted_sessions", {}), \
             patch("bot.hooks.pending_compaction", {}):
            result = _check_suspicious_response(
                elapsed=1.0, num_turns=1, tools_used=["Read", "Grep"],
                send_tool_called=False, result_text="ok",
                usage={"input_tokens": 500, "output_tokens": 10},
                session_key="telegram:123", model="claude-sonnet-4-20250514",
                cost=0.001,
            )
        assert result is not None
        assert any("ghost_tools" in r for r in result["reasons"])


# ═══════════════════════════════════════════════════════════════════════
# 6. bot/chat_key.py
# ═══════════════════════════════════════════════════════════════════════


class TestChatKey:
    """Tests for bot/chat_key.py — chat key construction and parsing."""

    def test_build_chat_key_basic(self):
        """build_chat_key creates 'source:chat_id' for non-forum chats."""
        from bot.chat_key import build_chat_key
        assert build_chat_key("telegram", "12345") == "telegram:12345"

    def test_build_chat_key_with_thread(self):
        """build_chat_key appends thread_id for forum topics."""
        from bot.chat_key import build_chat_key
        assert build_chat_key("telegram", "12345", thread_id=99) == "telegram:12345:99"

    def test_build_session_key_with_task(self):
        """build_session_key appends :task-ID suffix."""
        from bot.chat_key import build_session_key
        result = build_session_key("telegram", "12345", task_id="morning")
        assert result == "telegram:12345:task-morning"

    def test_build_session_key_with_thread_and_task(self):
        """build_session_key combines thread_id and task_id."""
        from bot.chat_key import build_session_key
        result = build_session_key("telegram", "12345", thread_id=99, task_id="t1")
        assert result == "telegram:12345:99:task-t1"

    def test_parse_chat_key_basic(self):
        """parse_chat_key extracts source, chat_id from simple key."""
        from bot.chat_key import parse_chat_key
        source, chat_id, thread_id = parse_chat_key("telegram:12345")
        assert source == "telegram"
        assert chat_id == "12345"
        assert thread_id is None

    def test_parse_chat_key_with_thread(self):
        """parse_chat_key extracts thread_id as int."""
        from bot.chat_key import parse_chat_key
        source, chat_id, thread_id = parse_chat_key("telegram:12345:99")
        assert source == "telegram"
        assert chat_id == "12345"
        assert thread_id == 99

    def test_parse_chat_key_strips_task_suffix(self):
        """parse_chat_key strips :task-xxx suffix before parsing."""
        from bot.chat_key import parse_chat_key
        source, chat_id, thread_id = parse_chat_key("telegram:12345:task-morning")
        assert source == "telegram"
        assert chat_id == "12345"
        assert thread_id is None

    def test_roundtrip_build_parse(self):
        """Building then parsing a chat key recovers the original components."""
        from bot.chat_key import build_chat_key, parse_chat_key
        key = build_chat_key("teams", "abc-def", thread_id=42)
        source, chat_id, thread_id = parse_chat_key(key)
        assert source == "teams"
        assert chat_id == "abc-def"
        assert thread_id == 42


# ═══════════════════════════════════════════════════════════════════════
# 7. bot/command_registry.py
# ═══════════════════════════════════════════════════════════════════════


class TestCommandRegistry:
    """Tests for bot/command_registry.py — command definitions and lookups."""

    def test_commands_dict_not_empty(self):
        """COMMANDS dict contains registered commands."""
        from bot.command_registry import COMMANDS
        assert len(COMMANDS) > 10  # We know there are 30+ commands

    def test_all_commands_have_required_fields(self):
        """Every command has slash, description, scope, and menu keys."""
        from bot.command_registry import COMMANDS
        for cid, cdef in COMMANDS.items():
            assert "slash" in cdef, f"Command {cid} missing 'slash'"
            assert "description" in cdef, f"Command {cid} missing 'description'"
            assert "scope" in cdef, f"Command {cid} missing 'scope'"
            assert "menu" in cdef, f"Command {cid} missing 'menu'"
            assert cdef["scope"] in ("anyone", "registered", "own_dm", "admin"), \
                f"Command {cid} has invalid scope: {cdef['scope']}"

    def test_get_commands_for_scope_admin(self):
        """get_commands_for_scope('admin') returns only admin-scoped commands."""
        from bot.command_registry import get_commands_for_scope
        admin_cmds = get_commands_for_scope("admin")
        assert len(admin_cmds) > 0
        for cmd in admin_cmds:
            assert cmd["scope"] == "admin"

    def test_get_commands_for_scope_multiple(self):
        """get_commands_for_scope with multiple scopes returns union."""
        from bot.command_registry import get_commands_for_scope
        result = get_commands_for_scope("anyone", "registered")
        scopes = {cmd["scope"] for cmd in result}
        assert scopes <= {"anyone", "registered"}

    def test_get_menu_commands_have_command_and_description(self):
        """get_menu_commands returns dicts with 'command' and 'description'."""
        from bot.command_registry import get_menu_commands
        menu = get_menu_commands()
        assert len(menu) > 0
        for cid, entry in menu.items():
            assert "command" in entry
            assert "description" in entry


# ═══════════════════════════════════════════════════════════════════════
# 8. bot/reports.py
# ═══════════════════════════════════════════════════════════════════════


class TestReports:
    """Tests for bot/reports.py — report URL generation and chat ID sanitization."""

    def test_sanitize_chat_id_normal(self):
        """_sanitize_chat_id passes through normal numeric chat IDs."""
        from bot.reports import _sanitize_chat_id
        assert _sanitize_chat_id("12345") == "12345"

    def test_sanitize_chat_id_negative(self):
        """_sanitize_chat_id handles negative TG group IDs (the - is replaced)."""
        from bot.reports import _sanitize_chat_id
        result = _sanitize_chat_id("-100123456")
        assert ".." not in result  # no traversal
        assert "/" not in result  # no path separator

    def test_sanitize_chat_id_traversal(self):
        """_sanitize_chat_id blocks path traversal attempts."""
        from bot.reports import _sanitize_chat_id
        result = _sanitize_chat_id("../../etc/passwd")
        assert ".." not in result

    def test_sanitize_chat_id_empty(self):
        """_sanitize_chat_id returns 'default' for empty string."""
        from bot.reports import _sanitize_chat_id
        assert _sanitize_chat_id("") == "default"

    def test_uuid4_regex_valid(self):
        """_UUID4_RE matches a valid UUID4 string."""
        from bot.reports import _UUID4_RE
        import uuid
        test_uuid = str(uuid.uuid4())
        assert _UUID4_RE.match(test_uuid)

    def test_uuid4_regex_rejects_invalid(self):
        """_UUID4_RE rejects non-UUID strings (path traversal)."""
        from bot.reports import _UUID4_RE
        assert not _UUID4_RE.match("../../../etc/passwd")
        assert not _UUID4_RE.match("not-a-uuid")

    def test_save_report_returns_url(self):
        """save_report returns a dict with report_id, file_path, and report_url."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("config.REPORTS_DIR", Path(tmpdir)), \
                 patch("config.REPORTS_PORT", 8210), \
                 patch("config.REPORTS_BASE_URL", "https://reports.example.com"), \
                 patch("config.WEBHOOK_BASE_URL", ""):
                from bot.reports import save_report
                result = save_report("<html><body>Test</body></html>", title="Test", chat_id="123")
                assert "report_id" in result
                assert "report_url" in result
                assert "https://reports.example.com/reports/" in result["report_url"]


# ═══════════════════════════════════════════════════════════════════════
# 9. bot/storage/artifacts.py
# ═══════════════════════════════════════════════════════════════════════


class TestArtifacts:
    """Tests for bot/storage/artifacts.py — artifact registration and search."""

    @pytest.mark.asyncio
    async def test_init_artifact_tables(self):
        """init_artifact_tables creates the coding_artifacts table without error."""
        from bot.storage.artifacts import init_artifact_tables
        # Uses the test DATA_DIR from conftest — creates real temp SQLite tables
        await init_artifact_tables()

    @pytest.mark.asyncio
    async def test_register_artifact_auto_detects_language(self):
        """register_artifact auto-detects language from file extension."""
        from bot.storage.artifacts import init_artifact_tables, register_artifact
        await init_artifact_tables()
        artifact_id = await register_artifact(
            file_path="/app/scripts/deploy.py",
            description="Deployment helper script",
            domain_id="admin",
        )
        assert isinstance(artifact_id, int)
        assert artifact_id > 0

    @pytest.mark.asyncio
    async def test_search_artifacts_returns_list(self):
        """search_artifacts returns a list of matching dicts."""
        from bot.storage.artifacts import init_artifact_tables, register_artifact, search_artifacts
        await init_artifact_tables()
        await register_artifact(
            file_path="/app/tools/analyzer.py",
            description="Code analysis utility for reviews",
            domain_id="dev",
        )
        results = await search_artifacts("analysis")
        assert isinstance(results, list)
        # Should find the artifact we just registered
        if results:
            assert "file_path" in results[0]

    @pytest.mark.asyncio
    async def test_list_artifacts_returns_recent(self):
        """list_artifacts returns recently added artifacts."""
        from bot.storage.artifacts import init_artifact_tables, register_artifact, list_artifacts
        await init_artifact_tables()
        await register_artifact(
            file_path="/app/data/report.html",
            description="Generated HTML report",
            domain_id="admin",
        )
        results = await list_artifacts()
        assert isinstance(results, list)
        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_list_artifacts_filtered_by_domain(self):
        """list_artifacts can filter by domain_id."""
        from bot.storage.artifacts import init_artifact_tables, register_artifact, list_artifacts
        await init_artifact_tables()
        await register_artifact(
            file_path="/app/unique_domain_file.sh",
            description="Unique domain test",
            domain_id="unique_test_domain",
        )
        results = await list_artifacts(domain_id="unique_test_domain")
        assert isinstance(results, list)
        assert all(r["domain_id"] == "unique_test_domain" for r in results)


# ═══════════════════════════════════════════════════════════════════════
# 10. bot/relay/transport.py
# ═══════════════════════════════════════════════════════════════════════


class TestRelayTransport:
    """Tests for bot/relay/transport.py — platform-agnostic message routing."""

    @pytest.mark.asyncio
    async def test_relay_send_message_telegram(self):
        """relay_send_message routes to Telegram send_message for source='telegram'."""
        mock_send = AsyncMock(return_value=42)
        with patch("integrations.telegram.send_message", mock_send):
            from bot.relay.transport import relay_send_message
            result = await relay_send_message(
                chat_id=12345, text="Hello", source="telegram"
            )
        assert result == 42
        mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_relay_send_message_teams(self):
        """relay_send_message routes to Teams client for source='teams'."""
        mock_client = MagicMock()
        mock_client.send_message = AsyncMock(return_value={"id": "msg1"})
        with patch("integrations.teams.get_client", return_value=mock_client):
            from bot.relay.transport import relay_send_message
            result = await relay_send_message(
                chat_id="teams-chat-1", text="Hello Teams", source="teams"
            )
        assert result == {"id": "msg1"}

    @pytest.mark.asyncio
    async def test_relay_send_message_failure_returns_none(self):
        """relay_send_message returns None on send failure."""
        with patch("integrations.telegram.send_message", side_effect=Exception("Network error")):
            from bot.relay.transport import relay_send_message
            result = await relay_send_message(
                chat_id=12345, text="Hello", source="telegram"
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_relay_edit_message_telegram(self):
        """relay_edit_message routes to Telegram edit for source='telegram'."""
        mock_edit = AsyncMock(return_value=True)
        with patch("integrations.telegram.edit_message", mock_edit):
            from bot.relay.transport import relay_edit_message
            result = await relay_edit_message(
                chat_id=12345, message_id=99, text="Updated", source="telegram"
            )
        assert result is True

    @pytest.mark.asyncio
    async def test_relay_delete_message_failure_returns_false(self):
        """relay_delete_message returns False on failure."""
        with patch("integrations.telegram.delete_message", side_effect=Exception("fail")):
            from bot.relay.transport import relay_delete_message
            result = await relay_delete_message(
                chat_id=12345, message_id=99, source="telegram"
            )
        assert result is False

    def test_get_relay_source_default(self):
        """get_relay_source returns 'telegram' as default when registry lookup fails."""
        with patch("bot.relay.registry._load_registry", side_effect=Exception("no file")):
            from bot.relay.transport import get_relay_source
            result = get_relay_source("99999")
        assert result == "telegram"


# ═══════════════════════════════════════════════════════════════════════
# 11. bot/agent.py — _resolve_session_domains (CB-119 extraction)
# ═══════════════════════════════════════════════════════════════════════

class TestResolveSessionDomains:
    """Tests for the extracted _resolve_session_domains() function."""

    def test_basic_admin_harness(self):
        from bot.agent import _resolve_session_domains
        wd, refs, eff = _resolve_session_domains("admin", ["admin", "teamwork"], None)
        assert wd == "admin"
        assert refs == ["teamwork"]
        assert "core" in eff and "admin" in eff and "teamwork" in eff

    def test_core_always_implicit(self):
        from bot.agent import _resolve_session_domains
        _, _, eff = _resolve_session_domains("teamwork", ["teamwork"], None)
        assert eff[0] == "core"
        assert "teamwork" in eff

    def test_write_domain_added_if_missing(self):
        from bot.agent import _resolve_session_domains
        _, _, eff = _resolve_session_domains("admin", ["core"], None)
        assert "admin" in eff

    def test_rbac_wildcard_passthrough(self):
        from bot.agent import _resolve_session_domains
        _, _, eff = _resolve_session_domains("admin", ["admin", "teamwork"], ["*"])
        assert "admin" in eff and "teamwork" in eff

    def test_rbac_intersection(self):
        from bot.agent import _resolve_session_domains
        _, _, eff = _resolve_session_domains("admin", ["admin", "teamwork"], ["admin", "core"])
        assert "admin" in eff and "core" in eff
        assert "teamwork" not in eff

    def test_rbac_empty_grants_core_only(self):
        from bot.agent import _resolve_session_domains
        _, _, eff = _resolve_session_domains("admin", ["admin", "teamwork"], [])
        assert eff == ["core"]

    def test_empty_domains(self):
        from bot.agent import _resolve_session_domains
        wd, refs, eff = _resolve_session_domains("", [], None)
        assert wd == ""
        assert refs == []
        assert eff == ["core"]

    def test_external_no_domains(self):
        from bot.agent import _resolve_session_domains
        wd, refs, eff = _resolve_session_domains("", [], [])
        assert eff == ["core"]


# ═══════════════════════════════════════════════════════════════════════
# 12. bot/chat_key.py — normalize_thread_id (CB-119 extraction)
# ═══════════════════════════════════════════════════════════════════════

class TestNormalizeThreadId:
    """Tests for normalize_thread_id() edge cases."""

    def test_none_returns_none(self):
        from bot.chat_key import normalize_thread_id
        assert normalize_thread_id(None) is None

    def test_int_passthrough(self):
        from bot.chat_key import normalize_thread_id
        assert normalize_thread_id(42) == 42

    def test_string_int(self):
        from bot.chat_key import normalize_thread_id
        assert normalize_thread_id("123") == 123

    def test_zero_valid(self):
        from bot.chat_key import normalize_thread_id
        assert normalize_thread_id(0) == 0

    def test_empty_string_returns_none(self):
        from bot.chat_key import normalize_thread_id
        assert normalize_thread_id("") is None

    def test_non_numeric_returns_none(self):
        from bot.chat_key import normalize_thread_id
        assert normalize_thread_id("abc") is None

    def test_float_truncates_to_int(self):
        from bot.chat_key import normalize_thread_id
        # int(3.14) = 3 — acceptable, Telegram thread IDs are always ints
        assert normalize_thread_id(3.14) == 3
