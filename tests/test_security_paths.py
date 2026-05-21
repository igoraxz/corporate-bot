# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Unit tests for critical security paths in the Corporate Bot codebase.

Covers:
1. bot/harness.py — CREDENTIAL_WILDCARD, is_credential_unrestricted, resolve_field, set_chat_override
2. bot/auth/credentials.py — check_credential_access, normalize_cred_id
3. bot/core/access.py — build_rbac_subject, is_private_chat, build_access_context
4. bot/relay/workstation_config.py — _SAFE_HOSTNAME, _SAFE_USERNAME validation
5. bot/chat_registry.py — is_authorized (topic vs group vs admin)
6. bot/mcp/__init__.py — _safe_int (the actual location of safe int parsing)
"""

import os
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ============================================================
# 1. bot/harness.py — CREDENTIAL_WILDCARD
# ============================================================

class TestCredentialWildcard:
    """Test is_credential_unrestricted and wildcard logic."""

    def test_wildcard_list_is_unrestricted(self):
        from bot.harness import is_credential_unrestricted
        assert is_credential_unrestricted(["*"]) is True

    def test_empty_list_is_not_unrestricted(self):
        from bot.harness import is_credential_unrestricted
        assert is_credential_unrestricted([]) is False

    def test_specific_credential_is_not_unrestricted(self):
        from bot.harness import is_credential_unrestricted
        assert is_credential_unrestricted(["google:user@co.com"]) is False

    def test_wildcard_among_others_is_unrestricted(self):
        from bot.harness import is_credential_unrestricted
        assert is_credential_unrestricted(["google:user@co.com", "*"]) is True

    def test_non_wildcard_string_is_not_unrestricted(self):
        from bot.harness import is_credential_unrestricted
        assert is_credential_unrestricted(["**"]) is False
        assert is_credential_unrestricted(["all"]) is False


class TestHarnessResolveField:
    """Test resolve_field returns correct allowed_credentials for different harnesses."""

    def test_admin_harness_has_wildcard(self):
        from bot.harness import get_harness
        h = get_harness("admin")
        assert h is not None
        assert h.allowed_credentials == ["*"]

    def test_coding_harness_has_wildcard(self):
        from bot.harness import get_harness
        h = get_harness("coding")
        assert h is not None
        assert h.allowed_credentials == ["*"]

    def test_external_chat_harness_deny_all(self):
        from bot.harness import get_harness
        h = get_harness("external_chat")
        assert h is not None
        assert h.allowed_credentials == []

    def test_teamwork_harness_deny_all(self):
        from bot.harness import get_harness
        h = get_harness("teamwork")
        assert h is not None
        assert h.allowed_credentials == []

    def test_personal_harness_deny_all(self):
        from bot.harness import get_harness
        h = get_harness("personal")
        assert h is not None
        assert h.allowed_credentials == []

    def test_resolve_field_with_admin_harness(self):
        from bot.harness import get_harness, resolve_field
        h = get_harness("admin")
        result = resolve_field(h, "allowed_credentials")
        assert result == ["*"]

    def test_resolve_field_with_non_admin_harness(self):
        from bot.harness import get_harness, resolve_field
        h = get_harness("external_chat")
        result = resolve_field(h, "allowed_credentials")
        assert result == []


class TestSetChatOverride:
    """Test set_chat_override rejects wildcard for non-admin harness."""

    def test_wildcard_rejected_for_non_admin(self):
        from bot.harness import set_chat_harness, set_chat_override
        # Assign a non-admin harness first
        test_key = "telegram:999999"
        set_chat_harness(test_key, "teamwork")
        with pytest.raises(ValueError, match="Wildcard.*only allowed for"):
            set_chat_override(test_key, "allowed_credentials", ["*"])

    def test_wildcard_accepted_for_admin(self):
        from bot.harness import set_chat_harness, set_chat_override, get_chat_overrides
        test_key = "telegram:999998"
        set_chat_harness(test_key, "admin")
        # Should NOT raise
        set_chat_override(test_key, "allowed_credentials", ["*"])
        overrides = get_chat_overrides(test_key)
        assert overrides.get("allowed_credentials") == ["*"]

    def test_wildcard_accepted_for_coding(self):
        from bot.harness import set_chat_harness, set_chat_override, get_chat_overrides
        test_key = "telegram:999997"
        set_chat_harness(test_key, "coding")
        set_chat_override(test_key, "allowed_credentials", ["*"])
        overrides = get_chat_overrides(test_key)
        assert overrides.get("allowed_credentials") == ["*"]

    def test_specific_creds_accepted_for_non_admin(self):
        from bot.harness import set_chat_harness, set_chat_override, get_chat_overrides
        test_key = "telegram:999996"
        set_chat_harness(test_key, "teamwork")
        # Specific credentials should be fine for non-admin
        set_chat_override(test_key, "allowed_credentials", ["google:user@co.com"])
        overrides = get_chat_overrides(test_key)
        assert overrides.get("allowed_credentials") == ["google:user@co.com"]


class TestHarnessFromDict:
    """Test Harness.from_dict null coercion for allowed_credentials."""

    def test_null_credentials_coerced_to_empty_list(self):
        from bot.harness import Harness
        h = Harness.from_dict({"allowed_credentials": None, "label": "test"})
        assert h.allowed_credentials == []

    def test_missing_credentials_default_to_empty_list(self):
        from bot.harness import Harness
        h = Harness.from_dict({"label": "test"})
        assert h.allowed_credentials == []

    def test_wildcard_credentials_preserved(self):
        from bot.harness import Harness
        h = Harness.from_dict({"allowed_credentials": ["*"], "label": "test"})
        assert h.allowed_credentials == ["*"]

    def test_removed_security_fields_ignored(self):
        from bot.harness import Harness
        h = Harness.from_dict({
            "label": "test",
            "data_scope": "global",
            "allowed_tools": ["all"],
            "sandbox": True,
        })
        assert h.label == "test"
        assert not hasattr(h, "data_scope")


class TestValidateFields:
    """Test _validate_fields security checks."""

    def test_wildcard_rejected_for_non_admin_label(self):
        from bot.harness import _validate_fields
        with pytest.raises(ValueError, match="Wildcard"):
            _validate_fields(
                {"allowed_credentials": ["*"]},
                target_label="teamwork",
            )

    def test_wildcard_allowed_for_admin_label(self):
        from bot.harness import _validate_fields
        # Should NOT raise
        _validate_fields(
            {"allowed_credentials": ["*"]},
            target_label="admin",
        )

    def test_none_credentials_rejected(self):
        from bot.harness import _validate_fields
        with pytest.raises(ValueError, match="no longer valid"):
            _validate_fields({"allowed_credentials": None}, target_label="teamwork")

    def test_bot_admin_role_rejected(self):
        from bot.harness import _validate_fields
        with pytest.raises(ValueError, match="bot_admin"):
            _validate_fields({"default_role": "bot_admin"}, target_label="custom")

    def test_path_traversal_rejected(self):
        from bot.harness import _validate_fields
        with pytest.raises(ValueError):
            _validate_fields({"claude_md": "../../../etc/passwd"})
        with pytest.raises(ValueError):
            _validate_fields({"settings_json": "/etc/shadow"})


# ============================================================
# 2. bot/auth/credentials.py — check_credential_access
# ============================================================

class TestCheckCredentialAccess:
    """Test check_credential_access for authorization logic."""

    def test_admin_gets_access(self):
        from bot.auth.credentials import check_credential_access
        assert check_credential_access("google:user@co.com", [], is_admin=True) is True

    def test_admin_gets_access_with_empty_list(self):
        from bot.auth.credentials import check_credential_access
        assert check_credential_access("anything:secret", [], is_admin=True) is True

    def test_wildcard_grants_access(self):
        from bot.auth.credentials import check_credential_access
        assert check_credential_access("google:user@co.com", ["*"]) is True

    def test_empty_list_denies_all(self):
        from bot.auth.credentials import check_credential_access
        assert check_credential_access("google:user@co.com", []) is False

    def test_specific_id_in_list_grants_access(self):
        from bot.auth.credentials import check_credential_access
        assert check_credential_access(
            "google:user@co.com",
            ["google:user@co.com", "m365:admin"],
        ) is True

    def test_credential_not_in_list_denied(self):
        from bot.auth.credentials import check_credential_access
        assert check_credential_access(
            "google:user@co.com",
            ["m365:admin", "github:org"],
        ) is False

    def test_normalized_id_matching(self):
        """google-workspace:x@y should match google:x@y in the allowlist."""
        from bot.auth.credentials import check_credential_access
        # Long form cred_id, short form in allowlist
        assert check_credential_access(
            "google-workspace:user@co.com",
            ["google:user@co.com"],
        ) is True

    def test_normalized_allowlist_matching(self):
        """Short form cred_id should match long form in the allowlist."""
        from bot.auth.credentials import check_credential_access
        # Short form cred_id, long form in allowlist
        assert check_credential_access(
            "google:user@co.com",
            ["google-workspace:user@co.com"],
        ) is True

    def test_user_scope_credential_access(self):
        """user:XYZ can access their own user-scope credential when allowlist is non-empty.

        Note: empty allowlist [] triggers early deny-all before user-scope check.
        The user-scope fallback only fires when there IS an allowlist but the
        cred_id isn't in it directly.
        """
        from bot.auth.credentials import check_credential_access
        assert check_credential_access(
            "user:abc-123",
            ["google:something"],  # non-empty allowlist (doesn't include user:abc-123)
            user_id="abc-123",
        ) is True

    def test_user_scope_credential_denied_empty_allowlist(self):
        """Empty allowlist denies all, even user-scope credentials (fail-closed)."""
        from bot.auth.credentials import check_credential_access
        assert check_credential_access(
            "user:abc-123",
            [],
            user_id="abc-123",
        ) is False

    def test_user_scope_credential_denied_wrong_user(self):
        from bot.auth.credentials import check_credential_access
        assert check_credential_access(
            "user:abc-123",
            ["google:something"],
            user_id="different-user",
        ) is False

    def test_non_admin_non_wildcard_non_listed_denied(self):
        from bot.auth.credentials import check_credential_access
        assert check_credential_access(
            "ssh:deploy-key",
            ["google:admin@co.com"],
            user_id="",
            is_admin=False,
        ) is False


class TestNormalizeCredId:
    """Test credential ID normalization."""

    def test_google_workspace_normalized(self):
        from bot.auth.credentials import normalize_cred_id
        assert normalize_cred_id("google-workspace:user@co.com") == "google:user@co.com"

    def test_ms365_normalized(self):
        from bot.auth.credentials import normalize_cred_id
        assert normalize_cred_id("ms365:admin") == "m365:admin"

    def test_already_canonical_unchanged(self):
        from bot.auth.credentials import normalize_cred_id
        assert normalize_cred_id("google:user@co.com") == "google:user@co.com"

    def test_no_colon_unchanged(self):
        from bot.auth.credentials import normalize_cred_id
        assert normalize_cred_id("simple-id") == "simple-id"

    def test_unknown_prefix_unchanged(self):
        from bot.auth.credentials import normalize_cred_id
        assert normalize_cred_id("custom:my-key") == "custom:my-key"


# ============================================================
# 3. bot/core/access.py — build_rbac_subject, is_private_chat
# ============================================================

class TestBuildRbacSubject:
    """Test build_rbac_subject for DM vs group contexts."""

    def test_telegram_dm_subject_is_user(self):
        """Positive chat_id in Telegram = DM, subject should be user."""
        from bot.core.access import build_rbac_subject
        subject = build_rbac_subject(
            source="telegram",
            user_id="telegram:123456789",
            chat_id="123456789",  # positive = DM
        )
        assert subject.user_id == "telegram:123456789"
        assert subject.source == "telegram"

    def test_telegram_group_subject_is_chat(self):
        """Negative chat_id in Telegram = group, subject should be chat."""
        from bot.core.access import build_rbac_subject
        subject = build_rbac_subject(
            source="telegram",
            user_id="telegram:123456789",
            chat_id="-1003969846997",  # negative = group
        )
        assert subject.chat_id == "-1003969846997"
        assert subject.source == "telegram"

    def test_teams_chat_subject(self):
        """Teams non-numeric chat_id treated as group (fail-closed)."""
        from bot.core.access import build_rbac_subject
        subject = build_rbac_subject(
            source="teams",
            user_id="teams:aad-object-id-123",
            chat_id="19:meeting_abc@thread.v2",
        )
        # Non-numeric, no metadata => fail-closed => group => chat subject
        assert subject.chat_id == "19:meeting_abc@thread.v2"
        assert subject.source == "teams"


class TestIsPrivateChat:
    """Test is_private_chat for different chat ID formats."""

    def test_positive_int_is_dm(self):
        from bot.core.access import is_private_chat
        assert is_private_chat("123456789") is True

    def test_negative_int_is_group(self):
        from bot.core.access import is_private_chat
        assert is_private_chat("-1003969846997") is False

    def test_zero_is_not_dm(self):
        from bot.core.access import is_private_chat
        assert is_private_chat("0") is False

    def test_empty_string_is_not_dm(self):
        from bot.core.access import is_private_chat
        assert is_private_chat("") is False

    def test_non_numeric_without_metadata_is_not_dm(self):
        from bot.core.access import is_private_chat
        assert is_private_chat("19:meeting_abc@thread.v2") is False

    def test_teams_personal_metadata(self):
        from bot.core.access import is_private_chat
        assert is_private_chat(
            "19:abc@thread.v2",
            source="teams",
            metadata={"conversation_type": "personal"},
        ) is True

    def test_teams_group_metadata(self):
        from bot.core.access import is_private_chat
        assert is_private_chat(
            "19:abc@thread.v2",
            source="teams",
            metadata={"conversation_type": "channel"},
        ) is False


class TestBuildAccessContext:
    """Test build_access_context for admin vs non-admin scopes."""

    def test_admin_dm_gets_global_scope(self):
        from bot.core.access import build_access_context
        ctx = build_access_context(
            chat_id="123456789",
            is_admin=True,
            is_private_chat=True,
        )
        assert ctx.is_admin is True
        assert ctx.allowed_chat_ids == ()  # empty = global scope

    def test_admin_group_gets_chat_scoped(self):
        from bot.core.access import build_access_context
        ctx = build_access_context(
            chat_id="-1003969846997",
            is_admin=True,
            is_private_chat=False,
        )
        assert ctx.is_admin is True
        assert ctx.allowed_chat_ids == ("-1003969846997",)  # chat-scoped

    def test_non_admin_gets_own_chat_only(self):
        from bot.core.access import build_access_context
        ctx = build_access_context(
            chat_id="-100123",
            is_admin=False,
        )
        assert ctx.is_admin is False
        assert ctx.allowed_chat_ids == ("-100123",)

    def test_external_chat_not_global_even_if_admin(self):
        from bot.core.access import build_access_context
        ctx = build_access_context(
            chat_id="123456789",
            is_admin=True,
            is_external_chat=True,
            is_private_chat=True,
        )
        assert ctx.is_admin is False
        assert ctx.allowed_chat_ids == ("123456789",)

    def test_admin_group_empty_chat_id_safety(self):
        """Empty chat_id in admin group context should NOT grant global scope."""
        from bot.core.access import build_access_context
        ctx = build_access_context(
            chat_id="",
            is_admin=True,
            is_private_chat=False,
        )
        assert ctx.allowed_chat_ids == ("__none__",)


# ============================================================
# 4. bot/relay/workstation_config.py — regex validation
# ============================================================

class TestSafeHostnameRegex:
    """Test _SAFE_HOSTNAME regex for SSH option injection prevention."""

    def test_valid_hostname(self):
        from bot.relay.workstation_config import _SAFE_HOSTNAME
        assert _SAFE_HOSTNAME.match("my-mac.local") is not None

    def test_valid_ip_address(self):
        from bot.relay.workstation_config import _SAFE_HOSTNAME
        assert _SAFE_HOSTNAME.match("192.168.1.100") is not None

    def test_valid_tailscale_hostname(self):
        from bot.relay.workstation_config import _SAFE_HOSTNAME
        assert _SAFE_HOSTNAME.match("johns-macbook.tail12345.ts.net") is not None

    def test_valid_single_char_hostname(self):
        from bot.relay.workstation_config import _SAFE_HOSTNAME
        assert _SAFE_HOSTNAME.match("a") is not None

    def test_rejects_leading_hyphen(self):
        from bot.relay.workstation_config import _SAFE_HOSTNAME
        assert _SAFE_HOSTNAME.match("-bad-host") is None

    def test_rejects_too_long(self):
        from bot.relay.workstation_config import _SAFE_HOSTNAME
        long_name = "a" * 256
        assert _SAFE_HOSTNAME.match(long_name) is None

    def test_rejects_spaces(self):
        from bot.relay.workstation_config import _SAFE_HOSTNAME
        assert _SAFE_HOSTNAME.match("my host") is None

    def test_rejects_ssh_injection(self):
        from bot.relay.workstation_config import _SAFE_HOSTNAME
        assert _SAFE_HOSTNAME.match("-oProxyCommand=evil") is None

    def test_rejects_semicolon(self):
        from bot.relay.workstation_config import _SAFE_HOSTNAME
        assert _SAFE_HOSTNAME.match("host;rm -rf /") is None

    def test_rejects_backticks(self):
        from bot.relay.workstation_config import _SAFE_HOSTNAME
        assert _SAFE_HOSTNAME.match("host`id`") is None

    def test_rejects_empty(self):
        from bot.relay.workstation_config import _SAFE_HOSTNAME
        assert _SAFE_HOSTNAME.match("") is None


class TestSafeUsernameRegex:
    """Test _SAFE_USERNAME regex for SSH option injection prevention."""

    def test_valid_username(self):
        from bot.relay.workstation_config import _SAFE_USERNAME
        assert _SAFE_USERNAME.match("john") is not None

    def test_valid_username_with_underscore_prefix(self):
        from bot.relay.workstation_config import _SAFE_USERNAME
        assert _SAFE_USERNAME.match("_admin") is not None

    def test_valid_username_with_dots(self):
        from bot.relay.workstation_config import _SAFE_USERNAME
        assert _SAFE_USERNAME.match("john.doe") is not None

    def test_valid_username_with_hyphens(self):
        from bot.relay.workstation_config import _SAFE_USERNAME
        assert _SAFE_USERNAME.match("john-doe") is not None

    def test_rejects_starting_with_digit(self):
        from bot.relay.workstation_config import _SAFE_USERNAME
        assert _SAFE_USERNAME.match("1john") is None

    def test_rejects_starting_with_hyphen(self):
        from bot.relay.workstation_config import _SAFE_USERNAME
        assert _SAFE_USERNAME.match("-john") is None

    def test_rejects_too_long(self):
        from bot.relay.workstation_config import _SAFE_USERNAME
        long_name = "a" * 33
        assert _SAFE_USERNAME.match(long_name) is None

    def test_rejects_spaces(self):
        from bot.relay.workstation_config import _SAFE_USERNAME
        assert _SAFE_USERNAME.match("john doe") is None

    def test_rejects_shell_injection(self):
        from bot.relay.workstation_config import _SAFE_USERNAME
        assert _SAFE_USERNAME.match("user;rm -rf /") is None

    def test_rejects_empty(self):
        from bot.relay.workstation_config import _SAFE_USERNAME
        assert _SAFE_USERNAME.match("") is None

    def test_max_length_accepted(self):
        from bot.relay.workstation_config import _SAFE_USERNAME
        # 1 starting char + 31 following chars = 32 total (max)
        name = "a" + "b" * 31
        assert _SAFE_USERNAME.match(name) is not None


# ============================================================
# 5. bot/chat_registry.py — is_authorized
# ============================================================

class TestChatRegistryIsAuthorized:
    """Test is_authorized for topic/group/admin scenarios."""

    def _setup_registry(self):
        """Register a test group and topic for auth tests."""
        from bot.chat_registry import register
        # Register a group
        register("telegram:-100123", title="Test Group", mode="active_plus")
        # Register a specific topic in that group
        register("telegram:-100123:42", title="Test Topic", mode="active",
                 parent="telegram:-100123")

    def test_registered_topic_is_authorized(self):
        self._setup_registry()
        from bot.chat_registry import is_authorized
        assert is_authorized("telegram", "-100123", thread_id="42") is True

    def test_unregistered_topic_is_denied(self):
        self._setup_registry()
        from bot.chat_registry import is_authorized
        # Topic 999 is NOT registered
        assert is_authorized("telegram", "-100123", thread_id="999") is False

    def test_parent_group_does_not_authorize_topics(self):
        """Parent group registration does NOT grant access to unregistered topics."""
        self._setup_registry()
        from bot.chat_registry import is_authorized
        # Group -100123 is registered, but topic 888 is NOT
        assert is_authorized("telegram", "-100123", thread_id="888") is False

    def test_registered_group_authorized_non_topic(self):
        self._setup_registry()
        from bot.chat_registry import is_authorized
        # Non-topic message in registered group
        assert is_authorized("telegram", "-100123") is True

    def test_admin_authorized_in_non_topic(self):
        """Admin users are authorized in non-topic contexts even for unregistered chats."""
        from bot.chat_registry import is_authorized
        with patch("config.is_admin_user", return_value=True):
            assert is_authorized("telegram", "-999888", user_id="12345") is True

    def test_admin_not_authorized_in_unregistered_topic(self):
        """Admin role does NOT override topic registration requirement."""
        self._setup_registry()
        from bot.chat_registry import is_authorized
        with patch("config.is_admin_user", return_value=True):
            # Topic must be explicitly registered, admin doesn't bypass
            assert is_authorized("telegram", "-100123", user_id="12345", thread_id="777") is False

    def test_unregistered_group_non_admin_denied(self):
        from bot.chat_registry import is_authorized
        with patch("config.is_admin_user", return_value=False):
            assert is_authorized("telegram", "-999777", user_id="12345") is False


# ============================================================
# 6. bot/mcp/__init__.py — _safe_int
# ============================================================

class TestSafeInt:
    """Test _safe_int from bot.mcp for safe integer parsing."""

    def test_valid_int(self):
        from bot.mcp import _safe_int
        assert _safe_int(42, 0) == 42

    def test_valid_string_int(self):
        from bot.mcp import _safe_int
        assert _safe_int("10", 0) == 10

    def test_none_returns_default(self):
        from bot.mcp import _safe_int
        assert _safe_int(None, 99) == 99

    def test_invalid_string_returns_default(self):
        from bot.mcp import _safe_int
        assert _safe_int("not_a_number", 5) == 5

    def test_empty_string_returns_default(self):
        from bot.mcp import _safe_int
        assert _safe_int("", 7) == 7

    def test_float_truncated(self):
        from bot.mcp import _safe_int
        assert _safe_int(3.9, 0) == 3

    def test_negative_int(self):
        from bot.mcp import _safe_int
        assert _safe_int("-5", 0) == -5

    def test_negative_string_int(self):
        from bot.mcp import _safe_int
        assert _safe_int("-100", 0) == -100

    def test_bool_coerced(self):
        from bot.mcp import _safe_int
        # True/False are ints in Python
        assert _safe_int(True, 0) == 1
        assert _safe_int(False, 0) == 0

    def test_float_string_returns_default(self):
        from bot.mcp import _safe_int
        # "3.14" cannot be converted by int() directly
        assert _safe_int("3.14", 0) == 0


class TestToBool:
    """Test _to_bool from bot.mcp for safe boolean parsing."""

    def test_bool_passthrough(self):
        from bot.mcp import _to_bool
        assert _to_bool(True) is True
        assert _to_bool(False) is False

    def test_string_true_values(self):
        from bot.mcp import _to_bool
        assert _to_bool("true") is True
        assert _to_bool("True") is True
        assert _to_bool("TRUE") is True
        assert _to_bool("1") is True
        assert _to_bool("yes") is True

    def test_string_false_values(self):
        from bot.mcp import _to_bool
        assert _to_bool("false") is False
        assert _to_bool("False") is False
        assert _to_bool("0") is False
        assert _to_bool("no") is False
        assert _to_bool("") is False

    def test_none_returns_default(self):
        from bot.mcp import _to_bool
        assert _to_bool(None) is False
        assert _to_bool(None, default=True) is True
