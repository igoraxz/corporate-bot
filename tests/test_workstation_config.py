# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Tests for bot.relay.workstation_config — per-user workstation configuration."""

import os
import tempfile
import time
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path


# Patch DATA_DIR before importing the module
_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("DATA_DIR", _tmpdir)


class TestWorkstationConfig:
    """Test WorkstationConfig dataclass."""

    def test_from_dict_basic(self):
        from bot.relay.workstation_config import WorkstationConfig
        d = {"mac_host": "my-mac", "mac_user": "john", "enabled": True}
        ws = WorkstationConfig.from_dict(d)
        assert ws.mac_host == "my-mac"
        assert ws.mac_user == "john"
        assert ws.enabled is True

    def test_from_dict_ignores_unknown_keys(self):
        from bot.relay.workstation_config import WorkstationConfig
        d = {"mac_host": "x", "mac_user": "y", "future_field": "z", "another": 123}
        ws = WorkstationConfig.from_dict(d)
        assert ws.mac_host == "x"
        assert not hasattr(ws, "future_field")

    def test_from_dict_defaults(self):
        from bot.relay.workstation_config import WorkstationConfig
        ws = WorkstationConfig.from_dict({})
        assert ws.mac_host == ""
        assert ws.mac_user == ""
        assert ws.enabled is True
        assert ws.session_timeout == 7200
        assert ws.authenticated_until == 0.0

    def test_to_dict_roundtrip(self):
        from bot.relay.workstation_config import WorkstationConfig
        ws = WorkstationConfig(mac_host="test", mac_user="user1", enabled=False)
        d = ws.to_dict()
        ws2 = WorkstationConfig.from_dict(d)
        assert ws2.mac_host == "test"
        assert ws2.mac_user == "user1"
        assert ws2.enabled is False

    def test_is_authenticated_expired(self):
        from bot.relay.workstation_config import WorkstationConfig
        ws = WorkstationConfig(authenticated_until=time.time() - 100)
        assert ws.is_authenticated() is False

    def test_is_authenticated_valid(self):
        from bot.relay.workstation_config import WorkstationConfig
        ws = WorkstationConfig(authenticated_until=time.time() + 3600)
        assert ws.is_authenticated() is True


class TestEncryption:
    """Test TOTP secret encryption/decryption."""

    def test_encrypt_empty_returns_empty(self):
        from bot.relay.workstation_config import _encrypt_secret
        assert _encrypt_secret("") == ""

    def test_encrypt_without_key_raises(self):
        """Should raise RuntimeError when TOKEN_ENCRYPTION_KEY not set."""
        from bot.relay.workstation_config import _encrypt_secret
        # Force _fernet to None to simulate missing key
        import bot.relay.workstation_config as mod
        old = mod._fernet
        mod._fernet = None
        try:
            with patch.dict(os.environ, {"TOKEN_ENCRYPTION_KEY": ""}, clear=False):
                # Re-force None after patch
                mod._fernet = None
                with pytest.raises(RuntimeError, match="TOKEN_ENCRYPTION_KEY"):
                    _encrypt_secret("mysecret")
        finally:
            mod._fernet = old

    def test_decrypt_non_enc_prefix_returns_as_is(self):
        from bot.relay.workstation_config import _decrypt_secret
        assert _decrypt_secret("plaintext") == "plaintext"
        assert _decrypt_secret("") == ""

    def test_encrypt_decrypt_roundtrip(self):
        """Roundtrip test with a real Fernet key."""
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()
        import bot.relay.workstation_config as mod
        old = mod._fernet
        mod._fernet = Fernet(key.encode())
        try:
            encrypted = mod._encrypt_secret("JBSWY3DPEHPK3PXP")
            assert encrypted.startswith("ENC:")
            decrypted = mod._decrypt_secret(encrypted)
            assert decrypted == "JBSWY3DPEHPK3PXP"
        finally:
            mod._fernet = old


class TestInputValidation:
    """Test hostname/username validation regexes."""

    def test_safe_hostname_valid(self):
        from bot.relay.workstation_config import _SAFE_HOSTNAME
        assert _SAFE_HOSTNAME.fullmatch("my-mac")
        assert _SAFE_HOSTNAME.fullmatch("100.113.76.19")
        assert _SAFE_HOSTNAME.fullmatch("johns-mbp.tail12345.ts.net")

    def test_safe_hostname_invalid(self):
        from bot.relay.workstation_config import _SAFE_HOSTNAME
        assert _SAFE_HOSTNAME.fullmatch("--proxy-command=evil") is None
        assert _SAFE_HOSTNAME.fullmatch("host with spaces") is None
        assert _SAFE_HOSTNAME.fullmatch("") is None
        assert _SAFE_HOSTNAME.fullmatch("-starts-with-dash") is None

    def test_safe_username_valid(self):
        from bot.relay.workstation_config import _SAFE_USERNAME
        assert _SAFE_USERNAME.fullmatch("john")
        assert _SAFE_USERNAME.fullmatch("john_doe")
        assert _SAFE_USERNAME.fullmatch("_admin")

    def test_safe_username_invalid(self):
        from bot.relay.workstation_config import _SAFE_USERNAME
        assert _SAFE_USERNAME.fullmatch("") is None
        assert _SAFE_USERNAME.fullmatch("-nope") is None
        assert _SAFE_USERNAME.fullmatch("user name") is None
        assert _SAFE_USERNAME.fullmatch("a" * 33) is None  # too long


# TestCommandRegex removed — /workstation and /set-topic-harness commands
# are planned (CB-63) but not yet implemented. Tests will be added when the
# regex patterns are created in bot/commands.py.
