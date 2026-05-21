# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Unit tests for knowledge domain system.

Tests: domain config parsing, chunking strategies, content hashing,
AccessContext domain filtering, and domain registry CRUD.
"""

import pytest
import asyncio
from unittest.mock import patch, AsyncMock


# ============================================================
# Domain Config Parsing
# ============================================================

class TestDomainConfig:
    def test_from_yaml_basic(self):
        from bot.storage.domains import DomainConfig
        raw = {
            "domain": {
                "id": "eng-runbooks",
                "name": "Engineering Runbooks",
                "description": "Deploy guides",
                "owner": "team@co.com",
                "source": {"type": "github", "repo": "org/repo", "branch": "main", "paths": ["docs/"]},
                "access": {"teams": ["engineering"], "admins": ["admin@co.com"]},
                "indexing": {"chunking_strategy": "markdown_headers", "chunk_size": 1500},
            }
        }
        config = DomainConfig.from_yaml(raw)
        assert config.id == "eng-runbooks"
        assert config.name == "Engineering Runbooks"
        assert config.source.repo == "org/repo"
        assert config.source.branch == "main"
        assert config.source.paths == ["docs/"]
        assert config.access.teams == ["engineering"]
        assert config.access.admins == ["admin@co.com"]
        assert config.indexing.chunk_size == 1500

    def test_from_yaml_defaults(self):
        from bot.storage.domains import DomainConfig
        config = DomainConfig.from_yaml({"domain": {"id": "test"}})
        assert config.id == "test"
        assert config.source.branch == "main"
        assert config.source.type == "github"
        assert config.indexing.chunking_strategy == "markdown_headers"
        assert config.indexing.chunk_size == 1500

    def test_from_yaml_flat_format(self):
        """Support both nested and flat YAML."""
        from bot.storage.domains import DomainConfig
        config = DomainConfig.from_yaml({"id": "flat", "name": "Flat Format"})
        assert config.id == "flat"
        assert config.name == "Flat Format"


# ============================================================
# Chunking
# ============================================================

class TestChunking:
    def test_chunk_markdown_by_headings(self):
        from bot.storage.domains import chunk_markdown
        content = """# Introduction
Some intro text here.

## Section A
Content of section A with more details.

## Section B
Content of section B.

### Subsection B1
Details of B1.
"""
        chunks = chunk_markdown(content, chunk_size=80)
        assert len(chunks) >= 2
        assert "Introduction" in chunks[0]

    def test_chunk_markdown_respects_size(self):
        from bot.storage.domains import chunk_markdown
        content = "# Title\n\n" + "x" * 3000
        chunks = chunk_markdown(content, chunk_size=500)
        for chunk in chunks:
            # Allow some overflow at boundaries
            assert len(chunk) <= 600

    def test_chunk_code_by_functions(self):
        from bot.storage.domains import chunk_code
        content = """import os

def function_one():
    return "one"

def function_two():
    return "two"

class MyClass:
    def method(self):
        pass
"""
        chunks = chunk_code(content, chunk_size=60)
        assert len(chunks) >= 2

    def test_chunk_document_strategy_dispatch(self):
        from bot.storage.domains import chunk_document
        content = "# Hello\n\nWorld"
        chunks_md = chunk_document(content, strategy="markdown_headers")
        chunks_rec = chunk_document(content, strategy="recursive")
        assert len(chunks_md) >= 1
        assert len(chunks_rec) >= 1

    def test_empty_content(self):
        from bot.storage.domains import chunk_markdown
        chunks = chunk_markdown("")
        assert len(chunks) == 1  # returns original (empty)


# ============================================================
# Content Hashing
# ============================================================

class TestContentHash:
    def test_hash_deterministic(self):
        from bot.storage.domains import content_hash
        h1 = content_hash("hello world")
        h2 = content_hash("hello world")
        assert h1 == h2

    def test_hash_different_content(self):
        from bot.storage.domains import content_hash
        h1 = content_hash("hello")
        h2 = content_hash("world")
        assert h1 != h2

    def test_hash_is_sha256(self):
        from bot.storage.domains import content_hash
        h = content_hash("test")
        assert len(h) == 64  # SHA-256 hex = 64 chars


# ============================================================
# AccessContext Domain Filtering
# ============================================================

class TestAccessContextDomains:
    def test_admin_no_domain_filter(self):
        from bot.core.access import AccessContext, _domain_sql_filter
        ctx = AccessContext(chat_id="admin-dm", is_admin=True, allowed_domains=())
        sql, params = _domain_sql_filter(ctx)
        assert sql == ""  # no filter
        assert params == []

    def test_employee_with_domains(self):
        from bot.core.access import AccessContext, _domain_sql_filter
        ctx = AccessContext(
            chat_id="teams:conv1", is_admin=False,
            allowed_domains=("eng-runbooks", "shared-policies"),
        )
        sql, params = _domain_sql_filter(ctx)
        assert "domain_id IN" in sql
        assert "eng-runbooks" in params
        assert "shared-policies" in params
        # Also allows non-domain data (personal messages)
        assert "OR domain_id = ''" in sql

    def test_no_domains_blocks_domain_data(self):
        from bot.core.access import AccessContext, _domain_sql_filter
        ctx = AccessContext(chat_id="teams:conv1", is_admin=False, allowed_domains=())
        sql, params = _domain_sql_filter(ctx)
        # Only non-domain data visible
        assert "domain_id = ''" in sql or "domain_id IS NULL" in sql

    def test_none_context_safe(self):
        from bot.core.access import _domain_sql_filter
        sql, params = _domain_sql_filter(None)
        assert "1=0" in sql or "domain_id = ''" in sql


# ============================================================
# Permission Tiers
# ============================================================

class TestRBAC:
    """Tests for RBAC-based access control (replaces old roles.py tier tests)."""

    def test_rbac_subject_creation(self):
        from bot.rbac.models import Subject
        s = Subject(user_id="admin-aad-id", source="teams")
        assert s.user_id == "admin-aad-id"
        assert s.source == "teams"
        assert s.team_ids == ()

    def test_rbac_access_decision_model(self):
        from bot.rbac.models import AccessDecision
        allowed = AccessDecision(allowed=True, reason="test allow", role_id="admin")
        denied = AccessDecision(allowed=False, reason="no policy")
        assert allowed.allowed is True
        assert denied.allowed is False

    def test_rbac_policy_matches(self):
        from bot.rbac.models import Policy
        p = Policy(privilege="use", object_type="tools", object_id="Bash", effect="allow")
        assert p.matches("use", "tools", "Bash") is True
        assert p.matches("use", "tools", "Read") is False
        assert p.matches("admin", "tools", "Bash") is False

    def test_rbac_policy_wildcard(self):
        from bot.rbac.models import Policy
        p = Policy(privilege="*", object_type="*", object_id="*", effect="allow")
        assert p.matches("use", "tools", "Bash") is True
        assert p.matches("admin", "knowledge_domain", "eng") is True

    def test_rbac_policy_prefix_match(self):
        from bot.rbac.models import Policy
        p = Policy(privilege="use", object_type="tools", object_id="mcp__bot-*", effect="allow")
        assert p.matches("use", "tools", "mcp__bot-messaging__send") is True
        assert p.matches("use", "tools", "mcp__google__calendar") is False

    def test_is_domain_admin_import(self):
        """Verify is_domain_admin is importable from rbac.engine."""
        from bot.rbac.engine import is_domain_admin
        assert callable(is_domain_admin)


# ============================================================
# Harness Rules Engine
# ============================================================

class TestHarnessRules:
    def test_match_dm_default(self):
        from bot.harness_rules import match_rule, seed_default_rules, _get_rules_store

        # Seed rules
        store = _get_rules_store()
        store.replace({})  # clear
        seed_default_rules()

        rule = match_rule(conversation_type="personal")
        assert rule is not None
        assert rule["harness"] == "personal"  # CB-5: renamed from employee-dm

    def test_match_channel_default(self):
        from bot.harness_rules import match_rule, seed_default_rules, _get_rules_store

        store = _get_rules_store()
        store.replace({})
        seed_default_rules()

        rule = match_rule(conversation_type="channel")
        assert rule is not None
        assert rule["harness"] == "teamwork"  # CB-5: renamed from employee

    def test_custom_rule_priority(self):
        from bot.harness_rules import add_rule, match_rule, _get_rules_store

        store = _get_rules_store()
        store.replace({})

        add_rule({"id": "eng", "priority": 10,
                  "match": {"team_name": "Engineering*"}, "harness": "engineering"})
        add_rule({"id": "default", "priority": 100,
                  "match": {"conversation_type": "channel"}, "harness": "employee"})

        # Engineering team matches first (lower priority number = higher priority)
        rule = match_rule(conversation_type="channel", team_name="Engineering Team")
        assert rule["harness"] == "engineering"

        # Other teams fall through to default
        rule = match_rule(conversation_type="channel", team_name="Sales Team")
        assert rule["harness"] == "employee"


# ============================================================
# Document Parser
# ============================================================

class TestDocParser:
    def test_markdown_passthrough(self):
        from bot.storage.doc_parser import parse_document
        result = parse_document("test.md", "# Hello\n\nWorld")
        assert "Hello" in result
        assert "World" in result

    def test_code_wraps_in_fence(self):
        from bot.storage.doc_parser import parse_document
        result = parse_document("app.py", "def hello(): pass")
        assert "```py" in result
        assert "def hello" in result

    def test_unsupported_format(self):
        from bot.storage.doc_parser import parse_document
        with pytest.raises(ValueError, match="Unsupported"):
            parse_document("file.xyz", "content")

    def test_is_supported(self):
        from bot.storage.doc_parser import is_supported
        assert is_supported("doc.md") is True
        assert is_supported("code.py") is True
        assert is_supported("file.pdf") is True
        assert is_supported("file.xyz") is False

    def test_parse_file_content_strategy(self):
        from bot.storage.doc_parser import parse_file_content
        text, strategy = parse_file_content("main.py", "def foo(): pass")
        assert strategy == "code"
        text, strategy = parse_file_content("readme.md", "# Title")
        assert strategy == "markdown_headers"


# ============================================================
# GitHub Webhook Signature
# ============================================================

class TestWebhookSignature:
    def test_valid_signature(self):
        from bot.storage.github_sync import verify_webhook_signature
        import hmac, hashlib
        secret = "test-secret"
        payload = b'{"action": "push"}'
        sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        assert verify_webhook_signature(payload, sig, secret) is True

    def test_invalid_signature(self):
        from bot.storage.github_sync import verify_webhook_signature
        assert verify_webhook_signature(b"payload", "sha256=wrong", "secret") is False

    def test_missing_signature(self):
        from bot.storage.github_sync import verify_webhook_signature
        assert verify_webhook_signature(b"payload", "", "secret") is False


# ============================================================
# Teams Cards
# ============================================================

class TestTeamsCards:
    def test_welcome_card_structure(self):
        from bot.teams_cards import build_welcome_card
        card = build_welcome_card("Admin", available_domains=["eng", "hr"])
        assert "body" in card
        assert len(card["body"]) > 0

    def test_error_card(self):
        from bot.teams_cards import build_error_card
        card = build_error_card("Test Error", "Something went wrong", "Try again")
        assert "body" in card
        body_text = str(card["body"])
        assert "Test Error" in body_text

    def test_status_card(self):
        from bot.teams_cards import build_status_card
        card = build_status_card(version="1.0", domains_count=5)
        assert "body" in card

    def test_card_attachment_wrapper(self):
        from bot.teams_cards import card_attachment
        att = card_attachment({"body": []})
        assert att["contentType"] == "application/vnd.microsoft.card.adaptive"
        assert att["content"]["type"] == "AdaptiveCard"
        assert att["content"]["version"] == "1.6"


# ============================================================
# Audit Logging (schema)
# ============================================================

class TestAudit:
    def test_audit_module_imports(self):
        """Verify audit module imports without errors."""
        from bot.audit import log_event, get_audit_summary, export_audit_csv
        assert callable(log_event)
        assert callable(get_audit_summary)
        assert callable(export_audit_csv)
