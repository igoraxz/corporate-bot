# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Tests for recent features: strip_facts_update, media bash blocking."""
import re
import pytest


class TestStripFactsUpdate:
    """Test that FACTS_UPDATE blocks are stripped from display text."""

    def test_strip_basic(self):
        from bot.agent import strip_facts_update
        text = 'Here is your passport!\nFACTS_UPDATE: {"key": "val"}\nEnjoy!'
        result = strip_facts_update(text)
        assert "FACTS_UPDATE" not in result
        assert "passport" in result
        assert "Enjoy" in result

    def test_no_facts_update(self):
        from bot.agent import strip_facts_update
        text = "Just a normal message"
        result = strip_facts_update(text)
        assert result == text

    def test_multiple_facts_updates(self):
        from bot.agent import strip_facts_update
        text = 'A\nFACTS_UPDATE: {"a": "b"}\nB\nFACTS_UPDATE: {"c": "d"}\nC'
        result = strip_facts_update(text)
        assert "FACTS_UPDATE" not in result
        assert "A" in result and "B" in result and "C" in result

    def test_facts_only_returns_empty(self):
        from bot.agent import strip_facts_update
        text = 'FACTS_UPDATE: {"key": "value"}'
        result = strip_facts_update(text)
        assert result == ""

    def test_extended_format(self):
        from bot.agent import strip_facts_update
        text = 'Saved!\nFACTS_UPDATE: {"k": {"value": "v", "category": "docs", "doc_pointer": "/path"}}'
        result = strip_facts_update(text)
        assert "FACTS_UPDATE" not in result
        assert "}" not in result  # no stray braces
        assert "Saved!" in result

    def test_nested_json_no_stray_braces(self):
        from bot.agent import strip_facts_update
        text = 'Done!\nFACTS_UPDATE: {"a": {"b": {"c": "d"}}}\nMore text'
        result = strip_facts_update(text)
        assert "FACTS_UPDATE" not in result
        assert "}" not in result
        assert "Done!" in result
        assert "More text" in result

    def test_streaming_partial(self):
        from bot.agent import strip_facts_update
        text = 'Hello\nFACTS_UPDATE: {"key": "val'
        result = strip_facts_update(text, streaming=True)
        assert "FACTS_UPDATE" not in result
        assert "Hello" in result

    def test_streaming_complete_then_partial(self):
        from bot.agent import strip_facts_update
        text = 'A\nFACTS_UPDATE: {"a": "b"}\nB\nFACTS_UPDATE: {"c": '
        result = strip_facts_update(text, streaming=True)
        assert "FACTS_UPDATE" not in result
        assert "A" in result and "B" in result


class TestMediaBashBlocking:
    """Test that Bash commands for media operations are blocked."""

    def test_ls_media_cache_blocked(self):
        from bot.hooks import _is_media_bash
        assert _is_media_bash("ls /app/data/media_cache/")

    def test_cp_media_blocked(self):
        from bot.hooks import _is_media_bash
        assert _is_media_bash("cp /app/data/media_cache/photo.jpg /tmp/out.jpg")

    def test_mv_media_cache_blocked(self):
        from bot.hooks import _is_media_bash
        assert _is_media_bash("mv /app/data/media_cache/file.jpg /tmp/out.jpg")

    def test_ls_tmp_blocked(self):
        from bot.hooks import _is_media_bash
        assert _is_media_bash("ls /app/data/tmp/")

    def test_find_media_cache_blocked(self):
        from bot.hooks import _is_media_bash
        assert _is_media_bash("find /app/data/media_cache -name '*.jpg'")

    def test_find_outside_media_not_blocked(self):
        """find in code dirs should NOT be blocked (was a false positive before)."""
        from bot.hooks import _is_media_bash
        assert not _is_media_bash("find /host-repo -name '*.jpg'")

    def test_grep_jpg_not_blocked(self):
        """Grepping for .jpg references in code should NOT be blocked."""
        from bot.hooks import _is_media_bash
        assert not _is_media_bash("grep -r '.jpg' /host-repo/docs/")

    def test_test_exists_media_blocked(self):
        from bot.hooks import _is_media_bash
        assert _is_media_bash("test -f /app/data/media_cache/photo.jpg")

    def test_git_commands_not_blocked(self):
        from bot.hooks import _is_media_bash
        assert not _is_media_bash("git status")
        assert not _is_media_bash("git log --oneline -5")

    def test_python_syntax_check_not_blocked(self):
        from bot.hooks import _is_media_bash
        assert not _is_media_bash("python3 -c \"import ast; ast.parse(open('file.py').read())\"")

    def test_grep_not_blocked(self):
        from bot.hooks import _is_media_bash
        assert not _is_media_bash("grep -r 'pattern' /host-repo/bot/")


class TestContextTruncation:
    """Test line-boundary truncation in format_recent_context."""

    def test_truncates_at_line_boundary(self):
        from bot.storage.memory import format_recent_context
        long_text = "Line one of the message\nLine two of the message\nLine three which is extra content"
        messages = [
            {"role": "user", "source": "telegram", "user_name": "Test",
             "text": long_text, "timestamp": "2026-03-08T10:00:00", "id": 1},
            {"role": "assistant", "source": "telegram", "user_name": "Bot",
             "text": "Short reply", "timestamp": "2026-03-08T10:01:00", "id": 2},
        ]
        # With only 2 messages, both are within CONTEXT_FULL_TAIL (default 8), so no truncation
        result = format_recent_context(messages)
        assert "Line one" in result

    def test_tail_messages_not_truncated(self):
        from bot.storage.memory import format_recent_context
        from config import CONTEXT_FULL_TAIL
        # Need more messages than CONTEXT_FULL_TAIL (default 8) to trigger truncation
        n = CONTEXT_FULL_TAIL + 4
        messages = []
        for i in range(n):
            messages.append({
                "role": "user", "source": "telegram", "user_name": "Test",
                "text": f"Message {i} " + "x" * 2000,
                "timestamp": f"2026-03-08T10:{i:02d}:00", "id": i + 1,
            })
        result = format_recent_context(messages)
        # Last CONTEXT_FULL_TAIL messages should be in full
        assert f"Message {n - 1}" in result
        # Earlier messages should be truncated
        assert "[truncated]" in result
