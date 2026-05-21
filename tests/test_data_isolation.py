# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Tests that verify pytest DATA_DIR isolation is working.

These tests PROVE that test writes go to the temp directory, not
to production /app/data/. If any of these fail, the conftest.py
isolation is broken and tests could corrupt prod data.

Run: python -m pytest tests/test_data_isolation.py -v
"""

import json
import os
from pathlib import Path

import pytest


# The prod data path — tests must NEVER write here
_PROD_DATA_DIR = Path("/app/data")
_PROD_CONFIG_DIR = _PROD_DATA_DIR / "config"


class TestDataDirIsolation:
    """Verify that DATA_DIR env var points to temp dir, not prod."""

    def test_data_dir_env_is_temp(self):
        """DATA_DIR env var should point to a pytest temp directory."""
        data_dir = os.environ.get("DATA_DIR", "")
        assert data_dir, "DATA_DIR env var is not set"
        assert "pytest_botdata_" in data_dir, \
            f"DATA_DIR should contain 'pytest_botdata_', got: {data_dir}"
        assert data_dir != str(_PROD_DATA_DIR), \
            "DATA_DIR must NOT point to production /app/data"

    def test_config_module_uses_temp_dir(self):
        """config.py DATA_DIR should resolve to temp dir."""
        from config import DATA_DIR, CONFIG_DIR
        assert "pytest_botdata_" in str(DATA_DIR), \
            f"config.DATA_DIR should be temp, got: {DATA_DIR}"
        assert "pytest_botdata_" in str(CONFIG_DIR), \
            f"config.CONFIG_DIR should be temp, got: {CONFIG_DIR}"

    def test_relay_registry_uses_temp_dir(self):
        """Relay registry file path should be in temp dir."""
        from bot.relay.registry import _REGISTRY_FILE
        assert "pytest_botdata_" in str(_REGISTRY_FILE), \
            f"_REGISTRY_FILE should be in temp dir, got: {_REGISTRY_FILE}"
        assert str(_PROD_CONFIG_DIR) not in str(_REGISTRY_FILE), \
            "_REGISTRY_FILE must NOT point to prod config dir"


class TestDatabaseIsolation:
    """Verify that SQLite databases resolve to temp dir, not prod."""

    def test_db_path_is_temp(self):
        """conversations.db should be in temp dir."""
        from config import DB_PATH
        assert "pytest_botdata_" in str(DB_PATH), \
            f"DB_PATH should be in temp dir, got: {DB_PATH}"
        assert str(_PROD_DATA_DIR / "conversations.db") != str(DB_PATH), \
            "DB_PATH must NOT be the production database"

    def test_pm_db_path_is_temp(self):
        """pm.db should be in temp dir."""
        from config import PM_DB_PATH
        assert "pytest_botdata_" in str(PM_DB_PATH), \
            f"PM_DB_PATH should be in temp dir, got: {PM_DB_PATH}"

    @pytest.mark.asyncio
    async def test_db_write_isolated(self):
        """Writing to the DB should NOT touch prod conversations.db."""
        prod_db = _PROD_DATA_DIR / "conversations.db"
        if prod_db.exists():
            prod_size_before = prod_db.stat().st_size
        else:
            prod_size_before = None

        # Write a test row via the DB singleton
        from bot.storage.db import open_db
        async with open_db() as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS _test_isolation "
                "(id INTEGER PRIMARY KEY, val TEXT)"
            )
            await db.execute(
                "INSERT INTO _test_isolation (val) VALUES (?)",
                ("canary",),
            )
            await db.commit()

            # Verify canary exists in test DB
            cursor = await db.execute("SELECT val FROM _test_isolation")
            row = await cursor.fetchone()
            assert row and row[0] == "canary", "Canary should exist in test DB"

        # Verify prod DB was NOT modified
        if prod_db.exists():
            prod_size_after = prod_db.stat().st_size
            assert prod_size_before == prod_size_after, \
                f"Prod DB size changed: {prod_size_before} → {prod_size_after}"

    @pytest.mark.asyncio
    async def test_pm_db_write_isolated(self):
        """Writing to PM DB should NOT touch prod pm.db."""
        prod_db = _PROD_DATA_DIR / "pm.db"
        if prod_db.exists():
            prod_size_before = prod_db.stat().st_size
        else:
            prod_size_before = None

        # Write via PM DB
        from bot.pm.db import get_pm_db
        db = await get_pm_db()
        await db.execute(
            "CREATE TABLE IF NOT EXISTS _test_pm_isolation "
            "(id INTEGER PRIMARY KEY, val TEXT)"
        )
        await db.execute(
            "INSERT INTO _test_pm_isolation (val) VALUES (?)",
            ("pm_canary",),
        )
        await db.commit()

        # Verify prod PM DB was NOT modified
        if prod_db.exists():
            prod_size_after = prod_db.stat().st_size
            assert prod_size_before == prod_size_after, \
                f"Prod PM DB size changed: {prod_size_before} → {prod_size_after}"


class TestWriteIsolation:
    """Verify that JCS writes go to temp dir, not prod."""

    def test_relay_jcs_write_isolated(self):
        """Writing to relay JCS should NOT touch prod relay_sessions.json."""
        # Snapshot prod file before
        prod_file = _PROD_CONFIG_DIR / "relay_sessions.json"
        if prod_file.exists():
            prod_before = prod_file.read_text()
        else:
            prod_before = None

        # Write a canary via JCS
        from bot.relay.registry import _get_store
        store = _get_store()
        try:
            store.set("__test_canary__", {"test": True, "from": "pytest"})

            # Verify canary is in the JCS
            assert "__test_canary__" in store, "Canary should be in JCS store"

            # Verify canary is NOT in prod file
            if prod_file.exists():
                prod_after = prod_file.read_text()
                assert prod_before == prod_after, \
                    "Prod relay_sessions.json was modified by test write!"
                prod_data = json.loads(prod_after)
                assert "__test_canary__" not in prod_data, \
                    "Test canary found in prod relay_sessions.json — isolation BROKEN!"

            # Verify canary IS in temp dir file
            temp_file = Path(os.environ["DATA_DIR"]) / "config" / "relay_sessions.json"
            assert temp_file.exists(), "Temp relay_sessions.json should exist"
            temp_data = json.loads(temp_file.read_text())
            assert "__test_canary__" in temp_data, \
                "Test canary should be in temp relay_sessions.json"
        finally:
            store.delete("__test_canary__")

    def test_harness_jcs_write_isolated(self):
        """Writing to harness JCS should NOT touch prod harnesses.json."""
        prod_file = _PROD_CONFIG_DIR / "harnesses.json"
        if prod_file.exists():
            prod_before = prod_file.read_text()
        else:
            prod_before = None

        # Write via harness public API (create + delete)
        from bot.harness import create_harness, delete_harness, get_harness
        try:
            create_harness("__test_harness__")
            assert get_harness("__test_harness__") is not None

            # Verify NOT in prod
            if prod_file.exists():
                prod_after = prod_file.read_text()
                assert prod_before == prod_after, \
                    "Prod harnesses.json was modified by test write!"
        finally:
            # Cleanup
            try:
                delete_harness("__test_harness__")
            except Exception:
                pass

    def test_external_chats_jcs_write_isolated(self):
        """Writing to external chats store should NOT touch prod."""
        from config import TG_EXTERNAL_CHATS
        prod_file = _PROD_CONFIG_DIR / "external_chats.json"
        if prod_file.exists():
            prod_before = prod_file.read_text()
        else:
            prod_before = None

        # Write canary
        TG_EXTERNAL_CHATS.set(99999999, {"mode": "silent", "test": True})

        # Verify NOT in prod
        if prod_file.exists():
            prod_after = prod_file.read_text()
            assert prod_before == prod_after, \
                "Prod external_chats.json was modified by test write!"

        # Cleanup
        TG_EXTERNAL_CHATS.delete(99999999)
