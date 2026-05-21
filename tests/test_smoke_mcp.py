# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Smoke tests for MCP tool handlers in bot/mcp/*.py and bot/pm/tools.py.

These tests verify that each tool handler is callable without crashing --
catching import ordering bugs, typos, missing lazy imports, etc.
They do NOT test business logic, only that the function executes without
raising an unhandled exception.

The MCP tools follow a pattern:
  1. @tool(...) decorator wraps function into SdkMcpTool
  2. We call tool_obj.handler(args) to invoke the underlying async function
  3. Handler pops _session_key from args
  4. Defines an inner async _do() with lazy imports
  5. Calls _run_with_timeout(_do(), tool_name)

Run:
    python -m pytest tests/test_smoke_mcp.py -v
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add repo root to path (same pattern as existing tests)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# TELEGRAM TOOLS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestTelegramToolSmoke:
    """Smoke tests for bot/mcp/telegram.py tool handlers."""

    @patch("integrations.telegram.send_message", new_callable=AsyncMock,
           return_value={"message_id": 42})
    @patch("bot.hooks.get_reply_to_msg_id", return_value=None)
    @patch("bot.hooks.get_task_last_sent_id", return_value=None)
    @patch("bot.hooks.get_current_task_id", return_value="task-1")
    @patch("bot.hooks.get_origin_chat_id", return_value="-100123")
    @patch("bot.hooks.get_chat_last_task", return_value=None)
    @patch("bot.hooks.set_task_last_sent_id")
    @patch("bot.hooks.mark_chat_activity")
    @patch("bot.agent.strip_facts_update", side_effect=lambda x: x)
    @patch("bot.storage.memory.store_message", new_callable=AsyncMock)
    async def test_send_message(self, *mocks):
        with patch("config.IS_STAGING", False), \
             patch("config.TG_CHAT_ID", -100123):
            from bot.mcp.telegram import telegram_send_message
            result = await telegram_send_message.handler({
                "text": "Hello test",
                "parse_mode": "HTML",
                "chat_id": -100123,
                "_session_key": "telegram:-100123",
            })
        assert "content" in result

    @patch("integrations.telegram.edit_message", new_callable=AsyncMock, return_value=True)
    @patch("bot.agent.strip_facts_update", side_effect=lambda x: x)
    async def test_edit_message(self, mock_strip, mock_edit):
        with patch("config.TG_CHAT_ID", -100123):
            from bot.mcp.telegram import telegram_edit_message
            result = await telegram_edit_message.handler({
                "message_id": 42,
                "text": "Edited text",
                "chat_id": -100123,
            })
        assert "content" in result

    @patch("integrations.telegram.delete_message", new_callable=AsyncMock, return_value=True)
    async def test_delete_message(self, mock_del):
        with patch("config.TG_CHAT_ID", -100123):
            from bot.mcp.telegram import telegram_delete_message
            result = await telegram_delete_message.handler({
                "message_id": 42,
                "chat_id": -100123,
            })
        assert "content" in result

    @patch("integrations.telegram.send_photo", new_callable=AsyncMock,
           return_value={"message_id": 43})
    @patch("bot.hooks.get_reply_to_msg_id", return_value=None)
    @patch("bot.hooks.get_task_last_sent_id", return_value=None)
    @patch("bot.hooks.get_current_task_id", return_value="task-1")
    @patch("bot.hooks.get_origin_chat_id", return_value="-100123")
    @patch("bot.hooks.get_chat_last_task", return_value=None)
    @patch("bot.hooks.set_task_last_sent_id")
    @patch("bot.hooks.mark_chat_activity")
    async def test_send_photo(self, *mocks):
        with patch("config.TG_CHAT_ID", -100123):
            from bot.mcp.telegram import telegram_send_photo
            result = await telegram_send_photo.handler({
                "photo_path": "/tmp/test.jpg",
                "chat_id": -100123,
                "_session_key": "telegram:-100123",
            })
        assert "content" in result

    @patch("integrations.telegram.send_document", new_callable=AsyncMock,
           return_value={"message_id": 44})
    @patch("bot.hooks.get_reply_to_msg_id", return_value=None)
    @patch("bot.hooks.get_task_last_sent_id", return_value=None)
    @patch("bot.hooks.get_current_task_id", return_value="task-1")
    @patch("bot.hooks.get_origin_chat_id", return_value="-100123")
    @patch("bot.hooks.get_chat_last_task", return_value=None)
    @patch("bot.hooks.set_task_last_sent_id")
    @patch("bot.hooks.mark_chat_activity")
    async def test_send_document(self, *mocks):
        with patch("config.TG_CHAT_ID", -100123):
            from bot.mcp.telegram import telegram_send_document
            result = await telegram_send_document.handler({
                "document_path": "/tmp/test.pdf",
                "chat_id": -100123,
                "_session_key": "telegram:-100123",
            })
        assert "content" in result

    @patch("integrations.telegram.send_video", new_callable=AsyncMock,
           return_value={"message_id": 45})
    @patch("bot.hooks.get_reply_to_msg_id", return_value=None)
    @patch("bot.hooks.get_task_last_sent_id", return_value=None)
    @patch("bot.hooks.get_current_task_id", return_value="task-1")
    @patch("bot.hooks.get_origin_chat_id", return_value="-100123")
    @patch("bot.hooks.get_chat_last_task", return_value=None)
    @patch("bot.hooks.set_task_last_sent_id")
    @patch("bot.hooks.mark_chat_activity")
    async def test_send_video(self, *mocks):
        with patch("config.TG_CHAT_ID", -100123):
            from bot.mcp.telegram import telegram_send_video
            result = await telegram_send_video.handler({
                "video_path": "/tmp/test.mp4",
                "chat_id": -100123,
                "_session_key": "telegram:-100123",
            })
        assert "content" in result

    @patch("integrations.telegram.send_audio", new_callable=AsyncMock,
           return_value={"message_id": 46})
    @patch("bot.hooks.get_reply_to_msg_id", return_value=None)
    @patch("bot.hooks.get_task_last_sent_id", return_value=None)
    @patch("bot.hooks.get_current_task_id", return_value="task-1")
    @patch("bot.hooks.get_origin_chat_id", return_value="-100123")
    @patch("bot.hooks.get_chat_last_task", return_value=None)
    @patch("bot.hooks.set_task_last_sent_id")
    @patch("bot.hooks.mark_chat_activity")
    async def test_send_audio(self, *mocks):
        with patch("config.TG_CHAT_ID", -100123):
            from bot.mcp.telegram import telegram_send_audio
            result = await telegram_send_audio.handler({
                "audio_path": "/tmp/test.mp3",
                "chat_id": -100123,
                "_session_key": "telegram:-100123",
            })
        assert "content" in result

    @patch("integrations.telegram.send_voice", new_callable=AsyncMock,
           return_value={"message_id": 47})
    @patch("bot.hooks.get_reply_to_msg_id", return_value=None)
    @patch("bot.hooks.get_task_last_sent_id", return_value=None)
    @patch("bot.hooks.get_current_task_id", return_value="task-1")
    @patch("bot.hooks.get_origin_chat_id", return_value="-100123")
    @patch("bot.hooks.get_chat_last_task", return_value=None)
    @patch("bot.hooks.set_task_last_sent_id")
    @patch("bot.hooks.mark_chat_activity")
    async def test_send_voice(self, *mocks):
        with patch("config.TG_CHAT_ID", -100123):
            from bot.mcp.telegram import telegram_send_voice
            result = await telegram_send_voice.handler({
                "voice_path": "/tmp/test.ogg",
                "chat_id": -100123,
                "_session_key": "telegram:-100123",
            })
        assert "content" in result

    @patch("integrations.telegram.send_location", new_callable=AsyncMock,
           return_value={"message_id": 48})
    @patch("bot.hooks.get_reply_to_msg_id", return_value=None)
    @patch("bot.hooks.get_task_last_sent_id", return_value=None)
    @patch("bot.hooks.get_current_task_id", return_value="task-1")
    @patch("bot.hooks.get_origin_chat_id", return_value="-100123")
    @patch("bot.hooks.get_chat_last_task", return_value=None)
    @patch("bot.hooks.set_task_last_sent_id")
    @patch("bot.hooks.mark_chat_activity")
    async def test_send_location(self, *mocks):
        with patch("config.TG_CHAT_ID", -100123):
            from bot.mcp.telegram import telegram_send_location
            result = await telegram_send_location.handler({
                "latitude": 51.5074,
                "longitude": -0.1278,
                "chat_id": -100123,
                "_session_key": "telegram:-100123",
            })
        assert "content" in result

    @patch("integrations.telegram.forward_message", new_callable=AsyncMock,
           return_value={"message_id": 49})
    async def test_forward_message(self, mock_fwd):
        with patch("config.TG_CHAT_ID", -100123):
            from bot.mcp.telegram import telegram_forward_message
            result = await telegram_forward_message.handler({
                "message_id": 42,
                "from_chat_id": -100123,
                "to_chat_id": -100456,
            })
        assert "content" in result

    @patch("integrations.telegram.pin_message", new_callable=AsyncMock, return_value=True)
    async def test_pin_message(self, mock_pin):
        with patch("config.TG_CHAT_ID", -100123):
            from bot.mcp.telegram import telegram_pin_message
            result = await telegram_pin_message.handler({
                "message_id": 42,
                "chat_id": -100123,
            })
        assert "content" in result

    @patch("integrations.telegram.unpin_message", new_callable=AsyncMock, return_value=True)
    async def test_unpin_message(self, mock_unpin):
        with patch("config.TG_CHAT_ID", -100123):
            from bot.mcp.telegram import telegram_unpin_message
            result = await telegram_unpin_message.handler({
                "message_id": 42,
                "chat_id": -100123,
            })
        assert "content" in result

    @patch("integrations.telegram.get_chat_members", new_callable=AsyncMock, return_value=[])
    async def test_get_chat_members(self, mock_members):
        with patch("config.TG_CHAT_ID", -100123):
            from bot.mcp.telegram import telegram_get_chat_members
            result = await telegram_get_chat_members.handler({
                "chat_id": -100123,
            })
        assert "content" in result

    @patch("integrations.telegram.get_chat_history", new_callable=AsyncMock, return_value=[])
    async def test_get_chat_history(self, mock_history):
        with patch("config.TG_CHAT_ID", -100123):
            from bot.mcp.telegram import telegram_get_chat_history
            result = await telegram_get_chat_history.handler({
                "chat_id": -100123,
                "limit": 5,
            })
        assert "content" in result

    @patch("integrations.telegram.set_chat_photo", new_callable=AsyncMock, return_value={"success": True})
    async def test_set_chat_photo(self, mock_set):
        from bot.mcp.telegram import telegram_set_chat_photo
        result = await telegram_set_chat_photo.handler({
            "chat_id": -100123,
            "photo_path": "/tmp/photo.jpg",
        })
        assert "content" in result

    @patch("integrations.telegram.set_chat_title", new_callable=AsyncMock, return_value=True)
    async def test_set_chat_title(self, mock_set):
        with patch("config.TG_CHAT_ID", -100123):
            from bot.mcp.telegram import telegram_set_chat_title
            result = await telegram_set_chat_title.handler({
                "chat_id": -100123,
                "title": "New Title",
            })
        assert "content" in result

    @patch("integrations.telegram.set_profile_photo", new_callable=AsyncMock, return_value=True)
    async def test_set_profile_photo(self, mock_set):
        from bot.mcp.telegram import telegram_set_profile_photo
        result = await telegram_set_profile_photo.handler({
            "photo_path": "/tmp/avatar.jpg",
        })
        assert "content" in result

    @patch("integrations.telegram.update_profile", new_callable=AsyncMock, return_value=True)
    async def test_update_profile(self, mock_update):
        from bot.mcp.telegram import telegram_update_profile
        result = await telegram_update_profile.handler({
            "first_name": "Test Bot",
        })
        assert "content" in result

    @patch("integrations.telegram.create_group", new_callable=AsyncMock,
           return_value={"success": True, "chat_id": -100999, "title": "Test Group"})
    async def test_create_group(self, mock_create):
        with patch("config.TG_ADMIN_CHAT_IDS", [999999]), \
             patch("config.promote_chat_to_admin_level", MagicMock()):
            from bot.mcp.telegram import telegram_create_group
            result = await telegram_create_group.handler({
                "title": "Test Group",
                "user_ids": "999999",
                "_session_key": "telegram:999999",
            })
        assert "content" in result
        assert mock_create.called

    @patch("integrations.telegram.leave_chat", new_callable=AsyncMock, return_value=True)
    async def test_leave_chat(self, mock_leave):
        from bot.mcp.telegram import telegram_leave_chat
        result = await telegram_leave_chat.handler({
            "chat_id": -100999,
        })
        assert "content" in result


# ---------------------------------------------------------------------------
# WHATSAPP TOOLS
# ---------------------------------------------------------------------------

# WhatsApp tools removed (Sprint B2)


# ---------------------------------------------------------------------------
# MEMORY TOOLS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestMemoryToolSmoke:
    """Smoke tests for bot/mcp/memory_tools.py tool handlers."""

    @patch("bot.storage.memory.get_recent_messages", new_callable=AsyncMock, return_value=[])
    @patch("bot.hooks.get_access_context", return_value=None)
    async def test_get_recent_conversation(self, mock_ctx, mock_msgs):
        from bot.mcp.memory_tools import get_recent_conversation
        result = await get_recent_conversation.handler({
            "limit": 10,
            "_session_key": "telegram:-100123",
        })
        assert "content" in result

    @patch("bot.storage.memory.search_messages", new_callable=AsyncMock, return_value=[])
    @patch("bot.storage.memory.search_message_facts", new_callable=AsyncMock, return_value=[])
    @patch("bot.storage.rag.rag_search", new_callable=AsyncMock, return_value=[])
    @patch("bot.storage.memory.search_media_cache", new_callable=AsyncMock, return_value=[])
    @patch("bot.hooks.get_access_context", return_value=None)
    @patch("bot.storage.db.open_db", new_callable=AsyncMock)
    async def test_memory_search(self, mock_db, mock_ctx, mock_media,
                                 mock_rag, mock_facts, mock_fts):
        from bot.mcp.memory_tools import memory_search
        result = await memory_search.handler({
            "query": "test search",
            "limit": 5,
            "_session_key": "telegram:-100123",
        })
        assert "content" in result

    @patch("bot.storage.memory.get_all_message_facts", new_callable=AsyncMock, return_value=[])
    @patch("bot.hooks.get_access_context", return_value=None)
    async def test_search_facts_list(self, mock_ctx, mock_list):
        from bot.mcp.memory_tools import search_facts_tool
        result = await search_facts_tool.handler({
            "action": "list",
            "_session_key": "telegram:-100123",
        })
        assert "content" in result

    @patch("bot.storage.memory.expire_message_fact", new_callable=AsyncMock, return_value=True)
    async def test_manage_fact_expire(self, mock_expire):
        from bot.mcp.memory_tools import manage_fact_tool
        result = await manage_fact_tool.handler({
            "action": "expire",
            "fact_key": "test_key",
            "_session_key": "telegram:-100123",
        })
        assert "content" in result

    @patch("bot.storage.memory.search_message_facts", new_callable=AsyncMock, return_value=[
        {"fact_key": "doc_key", "fact_value": "test", "doc_pointer": "/tmp/doc.pdf"},
    ])
    @patch("bot.hooks.get_access_context", return_value=None)
    async def test_get_document(self, mock_ctx, mock_search):
        from bot.mcp.memory_tools import get_document_tool
        result = await get_document_tool.handler({
            "fact_key": "doc_key",
            "_session_key": "telegram:-100123",
        })
        assert "content" in result

    @patch("bot.storage.memory.get_messages_around", new_callable=AsyncMock, return_value=[])
    @patch("bot.hooks.get_access_context", return_value=None)
    async def test_get_message_context(self, mock_ctx, mock_msgs):
        from bot.mcp.memory_tools import get_message_context
        result = await get_message_context.handler({
            "message_id": 1,
            "context_size": 5,
            "_session_key": "telegram:-100123",
        })
        assert "content" in result

    @patch("bot.storage.memory.get_messages_by_range", new_callable=AsyncMock, return_value=[])
    @patch("bot.hooks.get_access_context", return_value=None)
    async def test_get_messages_by_range(self, mock_ctx, mock_msgs):
        from bot.mcp.memory_tools import get_messages_by_range
        result = await get_messages_by_range.handler({
            "start_id": 1,
            "end_id": 10,
            "_session_key": "telegram:-100123",
        })
        assert "content" in result

    @patch("bot.storage.memory.search_media_cache", new_callable=AsyncMock, return_value=[])
    @patch("bot.hooks.get_access_context", return_value=None)
    async def test_search_media(self, mock_ctx, mock_search):
        from bot.mcp.memory_tools import search_media
        result = await search_media.handler({
            "query": "photo",
            "limit": 5,
            "_session_key": "telegram:-100123",
        })
        assert "content" in result

    @patch("bot.storage.rag.backfill_chunks", new_callable=AsyncMock, return_value={"chunks": 0})
    @patch("bot.storage.rag.get_rag_stats", new_callable=AsyncMock, return_value={})
    async def test_rag_backfill(self, mock_stats, mock_rebuild):
        from bot.mcp.memory_tools import rag_backfill_tool
        result = await rag_backfill_tool.handler({
            "_session_key": "telegram:-100123",
        })
        assert "content" in result

    @patch("bot.storage.rag.get_rag_stats", new_callable=AsyncMock,
           return_value={"total_chunks": 0, "msg_range": "N/A"})
    async def test_rag_stats(self, mock_stats):
        from bot.mcp.memory_tools import rag_stats_tool
        result = await rag_stats_tool.handler({
            "_session_key": "telegram:-100123",
        })
        assert "content" in result


# ---------------------------------------------------------------------------
# SERVICES TOOLS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestServicesToolSmoke:
    """Smoke tests for bot/mcp/services.py tool handlers."""

    @patch("integrations.gemini.generate_image", new_callable=AsyncMock,
           return_value={"file_path": "/tmp/test.png"})
    async def test_generate_image(self, mock_gen):
        from bot.mcp.services import generate_image
        result = await generate_image.handler({
            "prompt": "A cat",
            "filename": "cat.png",
        })
        assert "content" in result

    @patch("integrations.gemini.edit_image", new_callable=AsyncMock,
           return_value={"file_path": "/tmp/edited.png"})
    async def test_edit_image(self, mock_edit):
        from bot.mcp.services import edit_image
        result = await edit_image.handler({
            "image_path": "/tmp/original.png",
            "prompt": "Make it blue",
            "filename": "edited.png",
        })
        assert "content" in result

    @patch("integrations.phone.make_call", new_callable=AsyncMock,
           return_value={"call_id": "call-123", "status": "initiated"})
    async def test_phone_call(self, mock_call):
        from bot.mcp.services import phone_call
        result = await phone_call.handler({
            "to_number": "+441234567890",
            "objective": "Test call",
            "first_message": "Hello",
        })
        assert "content" in result

    @patch("integrations.phone.get_call_transcript", new_callable=AsyncMock,
           return_value={"transcript": "Hello...", "status": "completed"})
    async def test_phone_get_transcript(self, mock_transcript):
        from bot.mcp.services import phone_get_transcript
        result = await phone_get_transcript.handler({
            "call_id": "call-123",
        })
        assert "content" in result

    async def test_switch_model_show_current(self):
        """switch_model -- no model arg, shows current."""
        with patch("config.get_model_primary", return_value="claude-sonnet-4-6"), \
             patch("config.get_model_for_chat", return_value="claude-sonnet-4-6"), \
             patch("config.set_model_for_chat", return_value="claude-opus-4-6"), \
             patch("config.clear_model_for_chat"), \
             patch("config.get_chat_model_overrides", return_value={}), \
             patch("config.ALLOWED_MODELS", {"claude-sonnet-4-6", "claude-opus-4-6"}), \
             patch("bot.agent.schedule_session_reset"):
            from bot.mcp.services import switch_model
            result = await switch_model.handler({
                "model": "",
                "_session_key": "telegram:999999",
            })
        assert "content" in result

    async def test_switch_effort_show_current(self):
        """switch_effort -- no level arg, shows current."""
        with patch("config.get_effort_level", return_value="medium"), \
             patch("config.set_effort_level", return_value="high"), \
             patch("config.ALLOWED_EFFORTS", {"low", "medium", "high", "max"}):
            from bot.mcp.services import switch_effort
            result = await switch_effort.handler({
                "level": "",
            })
        assert "content" in result

    async def test_switch_effort_set(self):
        """switch_effort -- set to high."""
        with patch("config.get_effort_level", return_value="medium"), \
             patch("config.set_effort_level", return_value="high"), \
             patch("config.ALLOWED_EFFORTS", {"low", "medium", "high", "max"}):
            from bot.mcp.services import switch_effort
            result = await switch_effort.handler({
                "level": "high",
            })
        assert "content" in result

    async def test_switch_oauth_show_current(self):
        """switch_oauth -- no label, shows current."""
        with patch("config.get_oauth_for_chat", return_value="default"), \
             patch("config.set_oauth_for_chat"), \
             patch("config.clear_oauth_for_chat"), \
             patch("config.get_chat_oauth_overrides", return_value={}), \
             patch("config.list_oauth_profiles", return_value=[]), \
             patch("config.is_default_oauth", return_value=True), \
             patch("config.get_default_oauth_label", return_value="default"), \
             patch("bot.agent.schedule_session_reset"):
            from bot.mcp.services import switch_oauth
            result = await switch_oauth.handler({
                "label": "",
                "_session_key": "telegram:999999",
            })
        assert "content" in result

    async def test_manage_oauth_list(self):
        """manage_oauth -- list action."""
        with patch("config.list_oauth_profiles", return_value=[
            {"label": "default", "exists": True, "is_default": True},
        ]):
            from bot.mcp.services import manage_oauth
            result = await manage_oauth.handler({
                "action": "list",
            })
        assert "content" in result

    async def test_extend_budget(self):
        """extend_budget -- mock pool.extend_budget."""
        mock_pool = MagicMock()
        mock_pool.extend_budget = AsyncMock(return_value=70.0)
        with patch("bot.agent.client_pool", mock_pool):
            from bot.mcp.services import extend_budget
            result = await extend_budget.handler({
                "delta_usd": 20.0,
                "_session_key": "telegram:999999",
            })
        assert "content" in result

    async def test_extend_budget_no_delta(self):
        """extend_budget -- missing delta_usd."""
        from bot.mcp.services import extend_budget
        result = await extend_budget.handler({
            "_session_key": "telegram:999999",
        })
        assert "content" in result
        assert "error" in result["content"][0]["text"]

    async def test_usage_report_summary(self):
        """usage_report -- summary action."""
        with patch("bot.storage.memory.get_usage_summary", new_callable=AsyncMock,
                    return_value={"cost_usd": 1.0, "queries": 10}):
            from bot.mcp.services import usage_report
            result = await usage_report.handler({
                "action": "summary",
                "hours": 24,
            })
        assert "content" in result

    async def test_usage_report_top(self):
        """usage_report -- top queries."""
        with patch("bot.storage.memory.get_top_queries", new_callable=AsyncMock, return_value=[]):
            from bot.mcp.services import usage_report
            result = await usage_report.handler({
                "action": "top",
            })
        assert "content" in result

    async def test_usage_report_rate_limits(self):
        """usage_report -- rate_limits."""
        with patch("bot.agent.get_rate_limit_state", return_value={}):
            from bot.mcp.services import usage_report
            result = await usage_report.handler({
                "action": "rate_limits",
            })
        assert "content" in result

    async def test_usage_report_by_profile(self):
        """usage_report -- by_profile."""
        with patch("bot.storage.memory.get_usage_by_profile", new_callable=AsyncMock, return_value=[]):
            from bot.mcp.services import usage_report
            result = await usage_report.handler({
                "action": "by_profile",
                "hours": 24,
            })
        assert "content" in result

    async def test_reset_session_info_mode(self):
        """reset_session_tool -- no target, info mode."""
        mock_pool = MagicMock()
        mock_pool.get_status.return_value = {"telegram:999999": {"connected": True}}
        with patch("bot.agent.client_pool", mock_pool):
            from bot.mcp.services import reset_session_tool
            result = await reset_session_tool.handler({
                "_session_key": "telegram:999999",
            })
        assert "content" in result

    async def test_reset_session_self_blocked(self):
        """reset_session_tool -- targeting own session, blocked."""
        mock_pool = MagicMock()
        mock_pool.get_status.return_value = {}
        with patch("bot.agent.client_pool", mock_pool):
            from bot.mcp.services import reset_session_tool
            result = await reset_session_tool.handler({
                "session_key": "telegram:999999",
                "_session_key": "telegram:999999",
            })
        assert "content" in result
        assert "BLOCKED" in result["content"][0]["text"]

    async def test_manage_harness_list(self):
        """manage_harness -- list action."""
        with patch("bot.harness.list_harnesses", return_value={}), \
             patch("bot.harness.get_harness", return_value=None), \
             patch("bot.harness.create_harness"), \
             patch("bot.harness.update_harness"), \
             patch("bot.harness.delete_harness"), \
             patch("bot.harness.set_chat_harness"), \
             patch("bot.harness.clear_chat_harness"), \
             patch("bot.harness.get_chat_assignments", return_value={}), \
             patch("bot.harness.format_harness", return_value="default"):
            from bot.mcp.services import manage_harness
            result = await manage_harness.handler({
                "action": "list",
            })
        assert "content" in result

    async def test_manage_external_chat_list(self):
        """manage_external_chat_tool -- list action."""
        with patch("bot.chat_registry.all_entries", return_value={}):
            from bot.mcp.services import manage_external_chat_tool
            result = await manage_external_chat_tool.handler({
                "action": "list",
                "platform": "telegram",
            })
        assert "content" in result

    async def test_list_scheduled_tasks(self):
        """list_scheduled_tasks -- returns task list."""
        with patch("bot.scheduler.list_tasks", return_value=[]), \
             patch("bot.scheduler.format_task_list", return_value="No tasks"):
            from bot.mcp.services import list_scheduled_tasks
            result = await list_scheduled_tasks.handler({})
        assert "content" in result

    async def test_manage_scheduled_task_add(self):
        """manage_scheduled_task -- add action."""
        with patch("bot.scheduler.add_task", return_value={"id": "task-1", "name": "test"}):
            from bot.mcp.services import manage_scheduled_task
            result = await manage_scheduled_task.handler({
                "action": "add",
                "name": "Test Task",
                "hour": 9,
                "minute": 0,
                "prompt": "Check weather",
                "_session_key": "telegram:999999",
            })
        assert "content" in result


# ---------------------------------------------------------------------------
# DEPLOY TOOLS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDeployToolSmoke:
    """Smoke tests for bot/mcp/deploy.py tool handlers."""

    async def test_deploy_review(self):
        """deploy_review -- runs syntax + import checks."""
        import subprocess
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""
        with patch("subprocess.run", return_value=mock_result), \
             patch("pathlib.Path.glob", return_value=[]), \
             patch("bot.hooks.mark_deploy_review_passed"), \
             patch("bot.mcp.deploy._check_docs_staleness", return_value=[]):
            from bot.mcp.deploy import deploy_review
            result = await deploy_review.handler({
                "_session_key": "telegram:999999",
            })
        assert "content" in result

    async def test_deploy_status(self):
        """deploy_status -- check deploy status."""
        with patch("pathlib.Path.exists", return_value=False):
            from bot.mcp.deploy import deploy_status
            result = await deploy_status.handler({})
        assert "content" in result

    async def test_deploy_staging_status(self):
        """deploy_staging_status -- check staging status."""
        with patch("pathlib.Path.exists", return_value=False), \
             patch("config.IS_STAGING", False):
            from bot.mcp.deploy import deploy_staging_status
            result = await deploy_staging_status.handler({})
        assert "content" in result


# ---------------------------------------------------------------------------
# RELAY TOOL
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRelayToolSmoke:
    """Smoke tests for bot/mcp/relay.py desktop_relay tool."""

    async def test_ping_disabled(self):
        """desktop_relay -- relay disabled."""
        with patch("bot.desktop_relay.is_enabled", return_value=False):
            from bot.mcp.relay import desktop_relay_tool
            result = await desktop_relay_tool.handler({
                "action": "ping",
            })
        assert "content" in result
        assert "disabled" in result["content"][0]["text"].lower()

    async def test_ping_enabled(self):
        """desktop_relay -- ping action."""
        with patch("bot.desktop_relay.is_enabled", return_value=True), \
             patch("bot.desktop_relay.ping", new_callable=AsyncMock,
                   return_value={"online": True, "latency_ms": 42}):
            from bot.mcp.relay import desktop_relay_tool
            result = await desktop_relay_tool.handler({
                "action": "ping",
            })
        assert "content" in result

    async def test_status(self):
        """desktop_relay -- status action."""
        with patch("bot.desktop_relay.is_enabled", return_value=True), \
             patch("bot.desktop_relay.status", new_callable=AsyncMock,
                   return_value={"online": True}):
            from bot.mcp.relay import desktop_relay_tool
            result = await desktop_relay_tool.handler({
                "action": "status",
            })
        assert "content" in result

    async def test_lock(self):
        """desktop_relay -- lock action."""
        with patch("bot.desktop_relay.is_enabled", return_value=True), \
             patch("bot.desktop_relay.lock", new_callable=AsyncMock,
                   return_value={"locked": True}):
            from bot.mcp.relay import desktop_relay_tool
            result = await desktop_relay_tool.handler({
                "action": "lock",
            })
        assert "content" in result

    async def test_auth_bad_code(self):
        """desktop_relay -- auth action with invalid code."""
        with patch("bot.desktop_relay.is_enabled", return_value=True):
            from bot.mcp.relay import desktop_relay_tool
            result = await desktop_relay_tool.handler({
                "action": "auth",
                "totp_code": "123",
            })
        assert "content" in result
        assert "6-digit" in result["content"][0]["text"]

    async def test_list_all_jsonls(self):
        """desktop_relay -- list_all_jsonls action."""
        with patch("bot.desktop_relay.is_enabled", return_value=True), \
             patch("bot.desktop_relay.list_all_jsonls", new_callable=AsyncMock,
                   return_value=[]):
            from bot.mcp.relay import desktop_relay_tool
            result = await desktop_relay_tool.handler({
                "action": "list_all_jsonls",
            })
        assert "content" in result

    async def test_link_missing_fields(self):
        """desktop_relay -- link without required fields."""
        with patch("bot.desktop_relay.is_enabled", return_value=True):
            from bot.mcp.relay import desktop_relay_tool
            result = await desktop_relay_tool.handler({
                "action": "link",
                "chat_id": "12345",
            })
        assert "content" in result
        assert "required" in result["content"][0]["text"].lower()

    async def test_reap_orphans(self):
        """desktop_relay -- reap_orphans action."""
        with patch("bot.desktop_relay.is_enabled", return_value=True), \
             patch("bot.desktop_relay.reap_orphans", new_callable=AsyncMock,
                   return_value={"ok": True, "reaped_count": 0}):
            from bot.mcp.relay import desktop_relay_tool
            result = await desktop_relay_tool.handler({
                "action": "reap_orphans",
            })
        assert "content" in result


# ---------------------------------------------------------------------------
# PM TOOLS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestPMToolSmoke:
    """Smoke tests for bot/pm/tools.py tool handlers."""

    @patch("bot.pm.store.create_project", new_callable=AsyncMock,
           return_value={"id": 1, "name": "Test"})
    async def test_pm_create_project(self, mock_create):
        from bot.pm.tools import pm_create_project
        result = await pm_create_project.handler({
            "name": "Test Project",
            "_session_key": "telegram:999999",
        })
        assert "content" in result

    @patch("bot.pm.store.list_projects", new_callable=AsyncMock, return_value=[])
    async def test_pm_list_projects(self, mock_list):
        from bot.pm.tools import pm_list_projects
        result = await pm_list_projects.handler({
            "_session_key": "telegram:999999",
        })
        assert "content" in result

    @patch("bot.pm.store.check_project_access", new_callable=AsyncMock, return_value=True)
    @patch("bot.pm.store.create_sprint", new_callable=AsyncMock,
           return_value={"id": 1, "name": "Sprint 1"})
    async def test_pm_create_sprint(self, mock_create, mock_access):
        from bot.pm.tools import pm_create_sprint
        result = await pm_create_sprint.handler({
            "project_id": 1,
            "name": "Sprint 1",
            "_session_key": "telegram:999999",
        })
        assert "content" in result

    @patch("bot.pm.store.transition_sprint", new_callable=AsyncMock,
           return_value={"id": 1, "status": "running"})
    async def test_pm_transition_sprint(self, mock_transition):
        from bot.pm.tools import pm_transition_sprint
        result = await pm_transition_sprint.handler({
            "sprint_id": 1,
            "new_status": "running",
            "_session_key": "telegram:999999",
        })
        assert "content" in result

    @patch("bot.pm.store.transition_sprint", new_callable=AsyncMock,
           return_value={"id": 1, "status": "aborted"})
    @patch("bot.pm.store.list_tickets", new_callable=AsyncMock, return_value=[])
    @patch("bot.pm.store.move_ticket", new_callable=AsyncMock)
    async def test_pm_cancel_sprint(self, mock_move, mock_list, mock_transition):
        from bot.pm.tools import pm_cancel_sprint
        result = await pm_cancel_sprint.handler({
            "sprint_id": 1,
            "_session_key": "telegram:999999",
        })
        assert "content" in result

    @patch("bot.pm.store.create_ticket", new_callable=AsyncMock,
           return_value={"id": 1, "title": "Test Ticket"})
    async def test_pm_create_ticket(self, mock_create):
        from bot.pm.tools import pm_create_ticket
        result = await pm_create_ticket.handler({
            "sprint_id": 1,
            "title": "Test Ticket",
            "_session_key": "telegram:999999",
        })
        assert "content" in result

    @patch("bot.pm.store.update_ticket", new_callable=AsyncMock,
           return_value={"id": 1, "title": "Updated"})
    @patch("bot.pm.tools._resolve_ticket_id", new_callable=AsyncMock, return_value=1)
    async def test_pm_update_ticket(self, mock_resolve, mock_update):
        from bot.pm.tools import pm_update_ticket
        result = await pm_update_ticket.handler({
            "ticket_id": 1,
            "title": "Updated Ticket",
            "_session_key": "telegram:999999",
        })
        assert "content" in result

    @patch("bot.pm.store.move_ticket", new_callable=AsyncMock,
           return_value={"id": 1, "status": "done"})
    @patch("bot.pm.tools._resolve_ticket_id", new_callable=AsyncMock, return_value=1)
    async def test_pm_move_ticket(self, mock_resolve, mock_move):
        from bot.pm.tools import pm_move_ticket
        result = await pm_move_ticket.handler({
            "ticket_id": 1,
            "new_status": "done",
            "_session_key": "telegram:999999",
        })
        assert "content" in result

    @patch("bot.pm.store.move_ticket", new_callable=AsyncMock,
           return_value={"id": 1, "status": "ready"})
    @patch("bot.pm.store.update_ticket", new_callable=AsyncMock,
           return_value={"id": 1})
    @patch("bot.pm.store.cleanup_stale_runs", new_callable=AsyncMock, return_value=0)
    @patch("bot.pm.store.get_ticket_ref", new_callable=AsyncMock, return_value="FB-1")
    @patch("bot.pm.tools._resolve_ticket_id", new_callable=AsyncMock, return_value=1)
    async def test_pm_reset_ticket(self, mock_resolve, mock_ref, mock_cleanup,
                                   mock_update, mock_move):
        from bot.pm.tools import pm_reset_ticket
        result = await pm_reset_ticket.handler({
            "ticket_id": 1,
            "_session_key": "telegram:999999",
        })
        assert "content" in result

    @patch("bot.pm.store.list_tickets", new_callable=AsyncMock, return_value=[])
    async def test_pm_list_tickets(self, mock_list):
        from bot.pm.tools import pm_list_tickets
        result = await pm_list_tickets.handler({
            "sprint_id": 1,
            "_session_key": "telegram:999999",
        })
        assert "content" in result

    @patch("bot.pm.store.get_board", new_callable=AsyncMock,
           return_value={"tickets": [], "summary": {}})
    async def test_pm_board(self, mock_board):
        from bot.pm.tools import pm_board
        result = await pm_board.handler({
            "project_id": 1,
            "_session_key": "telegram:999999",
        })
        assert "content" in result

    @patch("bot.pm.store.get_ticket", new_callable=AsyncMock,
           return_value={"id": 1, "title": "Test", "tcd": None})
    @patch("bot.pm.store.get_ticket_ref", new_callable=AsyncMock, return_value="FB-1")
    @patch("bot.pm.tools._resolve_ticket_id", new_callable=AsyncMock, return_value=1)
    async def test_pm_get_tcd(self, mock_resolve, mock_ref, mock_get):
        from bot.pm.tools import pm_get_tcd
        result = await pm_get_tcd.handler({
            "ticket_id": 1,
            "_session_key": "telegram:999999",
        })
        assert "content" in result

    @patch("bot.pm.retro.generate_retro", new_callable=AsyncMock,
           return_value={"retro": "Sprint went well"})
    async def test_pm_retro(self, mock_retro):
        from bot.pm.tools import pm_retro
        result = await pm_retro.handler({
            "sprint_id": 1,
            "_session_key": "telegram:999999",
        })
        assert "content" in result

    @patch("bot.pm.retro.generate_summary", new_callable=AsyncMock,
           return_value={"summary": "Sprint complete"})
    async def test_pm_summary(self, mock_summary):
        from bot.pm.tools import pm_summary
        result = await pm_summary.handler({
            "sprint_id": 1,
            "_session_key": "telegram:999999",
        })
        assert "content" in result

    @patch("bot.pm.budget.get_sprint_budget_summary", new_callable=AsyncMock,
           return_value={"budget": {}})
    async def test_pm_budget_report(self, mock_budget):
        from bot.pm.tools import pm_budget_report
        result = await pm_budget_report.handler({
            "sprint_id": 1,
            "_session_key": "telegram:999999",
        })
        assert "content" in result
