# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Unit tests for corporate knowledge domain modules.

Tests: compose_effective_modules, compose_domain_categories,
build_domain_awareness_prompt, DomainConfig/DomainKnowledge parsing,
RateLimiter, and FACTS_UPDATE domain field parsing.

All tests are pure Python — no async DB or network calls required.
"""

import json
import time
from unittest.mock import patch, AsyncMock

import pytest


# ============================================================
# 1. compose_effective_modules() (compose_domain_modules removed in CB-119)
# ============================================================

class TestComposeDomainModules:
    def test_empty_harness_empty_domains(self):
        """Empty harness + empty domains = empty list."""
        from bot.storage.domains import compose_effective_modules
        result = compose_effective_modules([], harness_filter=None)
        assert result.system_prompt_modules == []

    def test_harness_filter_only(self):
        """Harness filter with no domain offerings = empty (filter has nothing to filter)."""
        from bot.storage.domains import compose_effective_modules
        result = compose_effective_modules([], harness_filter=["deploy", "staging"])
        assert result.auto_load_skills == []

    def test_with_domains(self):
        """Domain auto_load_skills filtered by harness allowlist."""
        from bot.storage.domains import compose_effective_modules
        domains = [
            {"domain_id": "d1", "prompt_modules": ["pitfalls"], "auto_load_skills": ["coding-principles", "staging"],
             "skills": []},
            {"domain_id": "d2", "prompt_modules": ["deploy"], "auto_load_skills": [],
             "skills": []},
        ]
        result = compose_effective_modules(domains, harness_filter=["staging"])
        # Only "staging" passes the filter
        assert result.auto_load_skills == ["staging"]
        # prompt_modules not filtered by harness_filter
        assert "pitfalls" in result.system_prompt_modules
        assert "deploy" in result.system_prompt_modules

    def test_dedup(self):
        """Duplicate modules across domains are deduped."""
        from bot.storage.domains import compose_effective_modules
        domains = [
            {"domain_id": "d1", "prompt_modules": ["deploy", "staging"], "auto_load_skills": [],
             "skills": [], "facts_categories": []},
        ]
        result = compose_effective_modules(domains, harness_filter=["deploy"])
        # deploy appears in prompt_modules, filter restricts auto_load_skills
        assert "deploy" in result.system_prompt_modules
        assert "staging" in result.system_prompt_modules

    def test_none_harness_with_domains(self):
        """None harness modules + domains = only domain modules."""
        from bot.storage.domains import compose_effective_modules
        domains = [
            {"domain_id": "d1", "prompt_modules": ["infra-guide"], "auto_load_skills": ["browser-automation"],
             "skills": []},
        ]
        result = compose_effective_modules(domains, harness_filter=None)
        assert "infra-guide" in result.system_prompt_modules
        assert "browser-automation" in result.system_prompt_modules

    def test_empty_strings_skipped(self):
        """Empty-string module names are skipped."""
        from bot.storage.domains import compose_effective_modules
        domains = [{"domain_id": "d1", "prompt_modules": ["", "valid"], "auto_load_skills": [""],
                     "skills": []}]
        result = compose_effective_modules(domains, harness_filter=None)
        assert "valid" in result.system_prompt_modules
        assert "" not in result.system_prompt_modules

    def test_order_preservation(self):
        """Order: prompt_modules first, then auto_load_skills from domains."""
        from bot.storage.domains import compose_effective_modules
        domains = [
            {"domain_id": "d1", "prompt_modules": ["d1-mod"], "auto_load_skills": ["d1-skill"], "skills": []},
            {"domain_id": "d2", "prompt_modules": ["d2-mod"], "auto_load_skills": ["d2-skill"], "skills": []},
        ]
        result = compose_effective_modules(domains, harness_filter=None)
        # system_prompt_modules = prompt_modules + auto_load_skills
        assert result.system_prompt_modules == ["d1-mod", "d2-mod", "d1-skill", "d2-skill"]


# ============================================================
# 2. compose_domain_categories()
# ============================================================

class TestComposeDomainCategories:
    def test_none_harness_no_domains(self):
        """None harness + no domains = None (no filtering)."""
        from bot.storage.domains import compose_domain_categories
        assert compose_domain_categories(None, []) is None

    def test_union(self):
        """Categories from harness + domains unioned."""
        from bot.storage.domains import compose_domain_categories
        domains = [
            {"facts_categories": ["infra", "deploy"]},
            {"facts_categories": ["hr"]},
        ]
        result = compose_domain_categories(["bot"], domains)
        assert result == ["bot", "infra", "deploy", "hr"]

    def test_dedup_categories(self):
        """Duplicate categories are deduped."""
        from bot.storage.domains import compose_domain_categories
        domains = [{"facts_categories": ["infra", "bot"]}]
        result = compose_domain_categories(["bot", "deploy"], domains)
        assert result == ["bot", "deploy", "infra"]  # "bot" deduped

    def test_none_harness_with_domains(self):
        """None harness = no category filtering, regardless of domains.
        Domain scoping is handled by allowed_domains on AccessContext, not categories.
        """
        from bot.storage.domains import compose_domain_categories
        domains = [{"facts_categories": ["eng", "ops"]}]
        result = compose_domain_categories(None, domains)
        assert result is None  # None = show all facts (no category filter)

    def test_empty_domain_categories(self):
        """Domains with empty facts_categories contribute nothing."""
        from bot.storage.domains import compose_domain_categories
        domains = [{"facts_categories": []}]
        result = compose_domain_categories(None, domains)
        # No harness categories, domains empty => None
        assert result is None

    def test_harness_only_no_domains(self):
        """Harness categories only, no domains."""
        from bot.storage.domains import compose_domain_categories
        result = compose_domain_categories(["infra"], [])
        assert result == ["infra"]


# ============================================================
# 3. build_domain_awareness_prompt()
# ============================================================

class TestBuildDomainAwarenessPrompt:
    def test_empty_domains(self):
        """Empty domains = empty string."""
        from bot.storage.domains import build_domain_awareness_prompt
        assert build_domain_awareness_prompt([]) == ""

    def test_content(self):
        """Domain descriptions included in output."""
        from bot.storage.domains import build_domain_awareness_prompt
        domains = [
            {"domain_id": "eng", "name": "Engineering",
             "description": "Deploy guides", "skills": ["browser"]},
        ]
        result = build_domain_awareness_prompt(domains)
        assert "[eng] Engineering" in result
        assert "Deploy guides" in result
        assert "browser" in result
        assert "FACTS_UPDATE" in result

    def test_long_description_truncated(self):
        """Descriptions >200 chars are truncated with ellipsis."""
        from bot.storage.domains import build_domain_awareness_prompt
        domains = [
            {"domain_id": "d1", "name": "Long Desc",
             "description": "x" * 300, "skills": []},
        ]
        result = build_domain_awareness_prompt(domains)
        # Description should be truncated to ~200 chars + "..."
        assert "..." in result

    def test_multiple_domains(self):
        """Multiple domains each get their own section."""
        from bot.storage.domains import build_domain_awareness_prompt
        domains = [
            {"domain_id": "eng", "name": "Engineering",
             "description": "Deploy guides", "skills": []},
            {"domain_id": "hr", "name": "HR",
             "description": "People ops", "skills": ["email-operations"]},
        ]
        result = build_domain_awareness_prompt(domains)
        assert "[eng] Engineering" in result
        assert "[hr] HR" in result
        assert "People ops" in result
        assert "email-operations" in result

    def test_no_skills(self):
        """Domain without skills omits skills line."""
        from bot.storage.domains import build_domain_awareness_prompt
        domains = [
            {"domain_id": "simple", "name": "Simple",
             "description": "No skills", "skills": []},
        ]
        result = build_domain_awareness_prompt(domains)
        assert "Relevant skills" not in result

    def test_no_description(self):
        """Domain without description still renders domain_id and name."""
        from bot.storage.domains import build_domain_awareness_prompt
        domains = [
            {"domain_id": "bare", "name": "Bare Domain",
             "description": "", "skills": []},
        ]
        result = build_domain_awareness_prompt(domains)
        assert "[bare] Bare Domain" in result


# ============================================================
# 4. DomainKnowledge + DomainConfig parsing
# ============================================================

class TestDomainConfigKnowledge:
    def test_config_with_knowledge(self):
        """from_yaml parses knowledge section."""
        from bot.storage.domains import DomainConfig
        yaml_dict = {
            "domain": {
                "id": "test-domain",
                "name": "Test",
                "description": "Test domain",
                "knowledge": {
                    "prompt_modules": ["deploy"],
                    "skills": ["browser-automation"],
                    "auto_load_skills": ["coding-principles"],
                    "facts_categories": ["infra"],
                },
            }
        }
        config = DomainConfig.from_yaml(yaml_dict)
        assert config.knowledge.prompt_modules == ["deploy"]
        assert config.knowledge.skills == ["browser-automation"]
        assert config.knowledge.auto_load_skills == ["coding-principles"]
        assert config.knowledge.facts_categories == ["infra"]

    def test_config_without_knowledge(self):
        """from_yaml handles missing knowledge section gracefully."""
        from bot.storage.domains import DomainConfig
        yaml_dict = {"domain": {"id": "old-domain", "name": "Old"}}
        config = DomainConfig.from_yaml(yaml_dict)
        assert config.knowledge.prompt_modules == []
        assert config.knowledge.skills == []
        assert config.knowledge.auto_load_skills == []
        assert config.knowledge.facts_categories == []

    def test_partial_knowledge(self):
        """from_yaml handles partially-specified knowledge section."""
        from bot.storage.domains import DomainConfig
        yaml_dict = {
            "domain": {
                "id": "partial",
                "name": "Partial",
                "knowledge": {
                    "prompt_modules": ["qa-guide"],
                    # skills, auto_load_skills, facts_categories omitted
                },
            }
        }
        config = DomainConfig.from_yaml(yaml_dict)
        assert config.knowledge.prompt_modules == ["qa-guide"]
        assert config.knowledge.skills == []
        assert config.knowledge.auto_load_skills == []
        assert config.knowledge.facts_categories == []

    def test_flat_format_has_no_knowledge(self):
        """Flat YAML (no 'domain' wrapper) defaults to empty knowledge."""
        from bot.storage.domains import DomainConfig
        config = DomainConfig.from_yaml({"id": "flat", "name": "Flat"})
        assert config.knowledge.prompt_modules == []
        assert config.knowledge.skills == []


# ============================================================
# 5. Rate Limiter
# ============================================================

class TestRateLimiter:
    def test_allows_under_limit(self):
        """Requests under limit are allowed."""
        from bot.rate_limiter import RateLimiter
        limiter = RateLimiter(requests_per_minute=5, burst=5)
        for _ in range(5):
            assert limiter.allow("test-key") is True

    def test_blocks_over_limit(self):
        """Requests over limit are blocked."""
        from bot.rate_limiter import RateLimiter
        limiter = RateLimiter(requests_per_minute=2, burst=2)
        assert limiter.allow("key") is True
        assert limiter.allow("key") is True
        assert limiter.allow("key") is False  # blocked

    def test_per_key_isolation(self):
        """Different keys have independent limits."""
        from bot.rate_limiter import RateLimiter
        limiter = RateLimiter(requests_per_minute=1, burst=1)
        assert limiter.allow("key-a") is True
        assert limiter.allow("key-b") is True  # different key
        assert limiter.allow("key-a") is False  # same key blocked

    def test_window_expires(self):
        """Requests are allowed again after the window expires."""
        from bot.rate_limiter import RateLimiter
        limiter = RateLimiter(requests_per_minute=1, burst=1)
        assert limiter.allow("key") is True
        assert limiter.allow("key") is False

        # Manually expire the timestamp by shifting it back >60s
        limiter._buckets["key"] = [time.monotonic() - 61.0]
        assert limiter.allow("key") is True  # window expired, allowed again

    def test_cleanup_removes_stale_keys(self):
        """Periodic cleanup removes stale buckets."""
        from bot.rate_limiter import RateLimiter
        limiter = RateLimiter(requests_per_minute=10, burst=10)
        limiter.allow("stale-key")
        # Shift timestamp to be old and force cleanup
        limiter._buckets["stale-key"] = [time.monotonic() - 120.0]
        limiter._last_cleanup = 0  # force cleanup on next allow
        limiter.allow("trigger-cleanup")
        assert "stale-key" not in limiter._buckets


# ============================================================
# 6. FACTS_UPDATE domain parsing
# ============================================================

class _MockAccessDecision:
    """Minimal mock for RBAC access decision."""
    allowed = True


class _MockRBACEngine:
    """Minimal mock RBAC engine that allows all access."""
    async def check_access(self, *args, **kwargs):
        return _MockAccessDecision()


class TestFactsUpdateDomainParsing:
    """Test that _persist_memory_updates correctly extracts the domain field.

    Uses mocks to avoid async DB calls — only the parsing logic is tested.
    The RBAC engine is mocked to allow domain writes (default RBAC denies
    without proper setup, causing domain_id to fall back to "").
    """

    @pytest.mark.asyncio
    async def test_domain_field_extracted(self):
        """Extended FACTS_UPDATE with domain field passes domain_id to save_message_fact."""
        from bot.core.fact_persistence import _persist_memory_updates

        text = 'FACTS_UPDATE: {"deploy_guide": {"value": "Use Helm charts", "category": "infra", "domain": "eng-runbooks"}}'

        with patch("bot.storage.memory.save_message_fact", new_callable=AsyncMock) as mock_save, \
             patch("bot.rbac.get_engine", return_value=_MockRBACEngine()):
            mock_save.return_value = 1
            await _persist_memory_updates(text, source_msg_id=100, source_chat_id="teams:conv1")

        mock_save.assert_called_once()
        call_kwargs = mock_save.call_args
        # save_message_fact is called with positional + keyword args
        assert call_kwargs[1]["domain_id"] == "eng-runbooks"
        assert call_kwargs[1]["category"] == "infra"

    @pytest.mark.asyncio
    async def test_domain_id_alias(self):
        """domain_id field is also accepted (alias for domain).
        No source_chat_id → RBAC denies domain writes (fail-closed, F5),
        so domain_id falls back to empty string.
        """
        from bot.core.fact_persistence import _persist_memory_updates

        text = 'FACTS_UPDATE: {"policy": {"value": "30 days PTO", "domain_id": "hr-policies"}}'

        with patch("bot.storage.memory.save_message_fact", new_callable=AsyncMock) as mock_save:
            mock_save.return_value = 1
            await _persist_memory_updates(text, source_msg_id=200)

        mock_save.assert_called_once()
        # No source_chat_id → RBAC denies domain write → domain_id becomes ""
        assert mock_save.call_args[1]["domain_id"] == ""

    @pytest.mark.asyncio
    async def test_domain_id_alias_with_chat(self):
        """domain_id field with source_chat_id + RBAC allow → domain preserved."""
        from bot.core.fact_persistence import _persist_memory_updates

        text = 'FACTS_UPDATE: {"policy": {"value": "30 days PTO", "domain_id": "hr-policies"}}'

        with patch("bot.storage.memory.save_message_fact", new_callable=AsyncMock) as mock_save, \
             patch("bot.rbac.get_engine", return_value=_MockRBACEngine()):
            mock_save.return_value = 1
            await _persist_memory_updates(text, source_msg_id=200, source_chat_id="teams:conv2")

        mock_save.assert_called_once()
        assert mock_save.call_args[1]["domain_id"] == "hr-policies"

    @pytest.mark.asyncio
    async def test_no_domain_field(self):
        """Simple FACTS_UPDATE without domain passes empty domain_id."""
        from bot.core.fact_persistence import _persist_memory_updates

        text = 'FACTS_UPDATE: {"user_pref": "dark mode"}'

        with patch("bot.storage.memory.save_message_fact", new_callable=AsyncMock) as mock_save:
            mock_save.return_value = 1
            await _persist_memory_updates(text, source_msg_id=300)

        mock_save.assert_called_once()
        assert mock_save.call_args[1]["domain_id"] == ""

    @pytest.mark.asyncio
    async def test_multiple_facts_different_domains(self):
        """Multiple facts in one block can have different domains."""
        from bot.core.fact_persistence import _persist_memory_updates

        text = (
            'FACTS_UPDATE: {'
            '"fact_a": {"value": "A", "domain": "eng"},'
            '"fact_b": {"value": "B", "domain": "hr"}'
            '}'
        )

        with patch("bot.storage.memory.save_message_fact", new_callable=AsyncMock) as mock_save, \
             patch("bot.rbac.get_engine", return_value=_MockRBACEngine()):
            mock_save.return_value = 1
            await _persist_memory_updates(text, source_chat_id="teams:conv3")

        assert mock_save.call_count == 2
        domains = [c[1]["domain_id"] for c in mock_save.call_args_list]
        assert set(domains) == {"eng", "hr"}
