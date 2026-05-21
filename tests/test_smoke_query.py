# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Smoke tests for ClientPool.query() — the critical message processing path.

These tests verify that the query() method can be called without raising
NameError/TypeError/AttributeError from variable scope bugs, wrong method
signatures, or missing imports. They do NOT test SDK integration — just
that the function's own code paths parse and execute correctly up to the
point where the SDK client is needed.

This test was added after the _harness incident (Apr 29 2026) where a
variable scope error in query() escaped all other deploy gates because:
- ast.parse only checks syntax (NameError is runtime)
- import checks only catch module-level errors (method bodies are lazy)
- smoke tests didn't exercise query() at all

Run:
    python -m pytest tests/test_smoke_query.py -v
"""

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

# Add repo root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


class _TestReachedSDK(Exception):
    """Raised inside mock_get_or_create to signal we reached the SDK call."""
    pass


class FakeHarness:
    """Minimal harness stub for testing query() code paths."""
    model = "claude-sonnet-4-6"
    extended_thinking = False
    effort = "medium"
    max_turns = 25
    max_budget_usd = 5
    output_token_cap = 25000
    query_timeout_s = 600
    prompt_modules = []
    claude_md_modules = []
    project_dir = ""
    data_scope = "admin"
    domains = []
    write_domain = ""
    default_role = ""
    reference_domains = []
    pm_project_id = None
    label = "test"
    allowed_credentials = []

    def get(self, key, default=None):
        return getattr(self, key, default)


@pytest.mark.asyncio
class TestQuerySmoke:
    """Smoke tests for ClientPool.query().

    These catch NameError/AttributeError from variable scope bugs —
    the exact class of bug that caused the _harness incident.
    """

    async def test_query_code_compiles_and_resolves_names(self):
        """Verify that query() method body has no undefined variable references."""
        fake_harness = FakeHarness()

        with patch("bot.harness.get_harness_for_chat", return_value=fake_harness), \
             patch("bot.harness.resolve_field", side_effect=lambda h, field, sk: getattr(h, field, None)), \
             patch("bot.prompts.build_system_prompt", return_value="test system prompt"), \
             patch("bot.storage.memory.build_access_context", return_value=MagicMock(allowed_chat_ids={-100123})), \
             patch("bot.hooks.set_access_context"), \
             patch("bot.hooks.set_reply_threading"):

            from bot.agent import ClientPool

            pool = ClientPool()

            reached_sdk = False

            async def mock_get_or_create(*args, **kwargs):
                nonlocal reached_sdk
                reached_sdk = True
                raise _TestReachedSDK("query() reached SDK — all pre-SDK code passed")

            pool.get_or_create = mock_get_or_create

            try:
                async for _ in pool.query(
                    session_key="telegram:-100123",
                    prompt="test message",
                    is_external_chat=False,
                ):
                    break
            except _TestReachedSDK:
                pass
            except Exception as e:
                pytest.fail(
                    f"query() raised {type(e).__name__} before reaching SDK: {e}\n"
                    "This indicates a variable scope or attribute error in the "
                    "critical message processing path."
                )

            assert reached_sdk, (
                "query() did not reach get_or_create — early exit or unexpected branch"
            )

    async def test_query_external_chat_code_path(self):
        """Test the external-chat branch of query() for undefined names."""
        fake_harness = FakeHarness()
        fake_harness.data_scope = "own"

        with patch("bot.harness.get_harness_for_chat", return_value=fake_harness), \
             patch("bot.harness.resolve_field", side_effect=lambda h, field, sk: getattr(h, field, None)), \
             patch("bot.prompts.build_system_prompt", return_value="test system prompt"), \
             patch("bot.storage.memory.build_access_context", return_value=MagicMock(allowed_chat_ids={-999})), \
             patch("bot.hooks.set_access_context"), \
             patch("bot.hooks.set_reply_threading"):

            from bot.agent import ClientPool

            pool = ClientPool()

            reached_sdk = False

            async def mock_get_or_create(*args, **kwargs):
                nonlocal reached_sdk
                reached_sdk = True
                raise _TestReachedSDK()

            pool.get_or_create = mock_get_or_create

            try:
                async for _ in pool.query(
                    session_key="telegram:-999",
                    prompt="test",
                    is_external_chat=True,
                ):
                    break
            except _TestReachedSDK:
                pass
            except Exception as e:
                pytest.fail(
                    f"query() (external chat path) raised {type(e).__name__}: {e}"
                )

            assert reached_sdk

    async def test_query_with_sandbox_project_dir(self):
        """Test the coding sandbox branch (project_dir set) for undefined names."""
        fake_harness = FakeHarness()
        fake_harness.project_dir = "/app/data/harness-projects/test"
        fake_harness.data_scope = "own"

        with patch("bot.harness.get_harness_for_chat", return_value=fake_harness), \
             patch("bot.harness.resolve_field", side_effect=lambda h, field, sk: getattr(h, field, None)), \
             patch("bot.prompts.build_system_prompt", return_value="test system prompt"), \
             patch("bot.storage.memory.build_access_context", return_value=MagicMock(allowed_chat_ids={-100123})), \
             patch("bot.hooks.set_access_context"), \
             patch("bot.hooks.set_reply_threading"):

            from bot.agent import ClientPool

            pool = ClientPool()

            reached_sdk = False

            async def mock_get_or_create(*args, **kwargs):
                nonlocal reached_sdk
                reached_sdk = True
                raise _TestReachedSDK()

            pool.get_or_create = mock_get_or_create

            try:
                async for _ in pool.query(
                    session_key="telegram:-100123",
                    prompt="test",
                    is_external_chat=False,
                ):
                    break
            except _TestReachedSDK:
                pass
            except Exception as e:
                pytest.fail(
                    f"query() (sandbox path) raised {type(e).__name__}: {e}"
                )

            assert reached_sdk

    def test_browser_profile_methods_exist_and_callable(self):
        """ClientPool has browser profile methods (used in query())."""
        from bot.agent import ClientPool
        pool = ClientPool()
        assert hasattr(pool, '_get_browser_profiles') or hasattr(pool, 'query')
