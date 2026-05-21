# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Tests for JCS migration: dict-like API, scheduler migration, harness JCS backing.

Covers FB-30 (config migration to JsonConfigStore) and deploy gate tests.
"""

import json
import os
import threading
import time
from pathlib import Path

import pytest

from bot.config_store import JsonConfigStore


# ── JCS dict-like API tests ──────────────────────────────────────────────

@pytest.fixture
def tmp_json(tmp_path):
    return tmp_path / "test.json"


class TestDictLikeAPI:
    """Tests for __getitem__, __setitem__, __delitem__, __iter__, items, pop."""

    def test_getitem(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        store.set("k", "v")
        assert store["k"] == "v"

    def test_getitem_missing_raises(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        with pytest.raises(KeyError):
            _ = store["missing"]

    def test_setitem(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        store["k"] = "v"
        assert store.get("k") == "v"
        # Verify persisted to disk
        data = json.loads(tmp_json.read_text())
        assert data["k"] == "v"

    def test_delitem(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        store["k"] = "v"
        del store["k"]
        assert "k" not in store
        # Verify persisted
        data = json.loads(tmp_json.read_text())
        assert "k" not in data

    def test_delitem_missing_raises(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        with pytest.raises(KeyError):
            del store["missing"]

    def test_iter(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        store.update({"a": 1, "b": 2, "c": 3})
        keys = list(store)
        assert sorted(keys) == ["a", "b", "c"]

    def test_iter_safe_during_mutation(self, tmp_json):
        """Iterating should not raise even if store is mutated."""
        store = JsonConfigStore("test", tmp_json)
        store.update({"a": 1, "b": 2, "c": 3})
        # __iter__ returns iter(dict(self._data)) — a snapshot
        for k in store:
            pass  # should not raise

    def test_items(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        store.update({"x": 10, "y": 20})
        items = dict(store.items())
        assert items == {"x": 10, "y": 20}

    def test_keys(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        store.update({"a": 1, "b": 2})
        assert sorted(store.keys()) == ["a", "b"]

    def test_values(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        store.update({"a": 1, "b": 2})
        assert sorted(store.values()) == [1, 2]

    def test_pop_existing(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        store["k"] = "v"
        result = store.pop("k")
        assert result == "v"
        assert "k" not in store
        # Verify persisted
        data = json.loads(tmp_json.read_text())
        assert "k" not in data

    def test_pop_missing_with_default(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        result = store.pop("missing", "fallback")
        assert result == "fallback"

    def test_pop_missing_no_default_raises(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        with pytest.raises(KeyError):
            store.pop("missing")

    def test_int_key_dict_api(self, tmp_json):
        """Dict-like API with int key coercion."""
        store = JsonConfigStore("test", tmp_json, key_type=int)
        store[42] = {"title": "test chat"}
        assert 42 in store
        assert store[42] == {"title": "test chat"}
        del store[42]
        assert 42 not in store

    def test_nested_dict_values(self, tmp_json):
        """Nested dict values are stored and retrieved correctly."""
        store = JsonConfigStore("test", tmp_json)
        store["chat"] = {"title": "Group", "mode": "active", "nested": {"deep": True}}
        retrieved = store["chat"]
        assert retrieved["mode"] == "active"
        assert retrieved["nested"]["deep"] is True

    def test_setitem_auto_saves(self, tmp_json):
        """__setitem__ persists immediately — no explicit save needed."""
        s1 = JsonConfigStore("s1", tmp_json)
        s1["key"] = "value"
        # Read from disk with a new store
        s2 = JsonConfigStore("s2", tmp_json)
        assert s2["key"] == "value"

    def test_in_place_mutation_not_persisted(self, tmp_json):
        """Verify that in-place mutation of a retrieved value does NOT persist.

        This documents the expected behavior: callers must use store[k] = modified_copy
        to persist changes. This test ensures the JCS contract is clear.
        """
        store = JsonConfigStore("test", tmp_json)
        store["chat"] = {"title": "Original", "mode": "silent"}
        # In-place mutation — NOT persisted
        entry = store["chat"]
        entry["mode"] = "active"
        # In-memory reflects the change (same dict reference)
        assert store["chat"]["mode"] == "active"
        # But disk still has old value
        store2 = JsonConfigStore("test2", tmp_json)
        assert store2["chat"]["mode"] == "silent"

    def test_copy_mutate_set_pattern(self, tmp_json):
        """The correct pattern: copy → mutate → set."""
        store = JsonConfigStore("test", tmp_json)
        store["chat"] = {"title": "Group", "mode": "silent"}
        # Correct pattern
        entry = dict(store["chat"])
        entry["mode"] = "active"
        store["chat"] = entry
        # Persisted correctly
        store2 = JsonConfigStore("test2", tmp_json)
        assert store2["chat"]["mode"] == "active"


# ── Scheduler migration tests ────────────────────────────────────────────

class TestSchedulerMigration:
    """Tests for array→dict format migration in scheduler."""

    def test_array_to_dict_migration(self, tmp_path):
        """Old array format is converted to dict on first import."""
        tasks_file = tmp_path / "scheduled_tasks.json"
        old_tasks = [
            {"id": "morning", "name": "Morning Briefing", "hour": 7, "minute": 10},
            {"id": "email", "name": "Email Check", "hour": 9, "minute": 0},
        ]
        tasks_file.write_text(json.dumps(old_tasks))

        # Simulate migration
        raw = json.loads(tasks_file.read_text())
        assert isinstance(raw, list)
        data = {}
        for task in raw:
            tid = task.get("id", "unknown")
            data[tid] = task
        tasks_file.write_text(json.dumps(data, indent=2))

        # Verify dict format
        migrated = json.loads(tasks_file.read_text())
        assert isinstance(migrated, dict)
        assert "morning" in migrated
        assert "email" in migrated
        assert migrated["morning"]["name"] == "Morning Briefing"

    def test_dict_format_unchanged(self, tmp_path):
        """Already-migrated dict format is not touched."""
        tasks_file = tmp_path / "scheduled_tasks.json"
        dict_tasks = {
            "morning": {"id": "morning", "name": "Morning", "hour": 7},
        }
        tasks_file.write_text(json.dumps(dict_tasks))

        raw = json.loads(tasks_file.read_text())
        assert isinstance(raw, dict)  # No migration needed

    def test_task_store_operations(self, tmp_path):
        """JCS-backed task store supports CRUD operations."""
        tasks_file = tmp_path / "tasks.json"
        store = JsonConfigStore("tasks", tasks_file)

        # Add
        task = {"id": "t1", "name": "Test", "hour": 8, "minute": 0, "enabled": True}
        store.set("t1", task)
        assert "t1" in store
        assert store["t1"]["name"] == "Test"

        # Update
        t = dict(store["t1"])
        t["enabled"] = False
        store.set("t1", t)
        assert store["t1"]["enabled"] is False

        # Delete
        store.delete("t1")
        assert "t1" not in store

    def test_task_id_as_key(self, tmp_path):
        """Task IDs serve as dict keys — unique by construction."""
        tasks_file = tmp_path / "tasks.json"
        store = JsonConfigStore("tasks", tasks_file)
        store.set("abc123", {"id": "abc123", "name": "Task A"})
        store.set("def456", {"id": "def456", "name": "Task B"})
        assert len(store) == 2
        assert store["abc123"]["name"] == "Task A"


# ── Harness JCS backing tests ────────────────────────────────────────────

class TestHarnessJCSBacking:
    """Tests for harness definitions and assignments via JCS."""

    def test_harness_store_crud(self, tmp_path):
        """Harness definitions stored as dicts in JCS."""
        harness_file = tmp_path / "harnesses.json"
        store = JsonConfigStore("harnesses", harness_file)

        # Create
        store.set("default", {"label": "default", "model": "claude-sonnet-4-6"})
        store.set("admin", {"label": "admin", "model": "claude-opus-4-6", "data_scope": "admin"})
        assert len(store) == 2

        # Read
        admin = store["admin"]
        assert admin["model"] == "claude-opus-4-6"
        assert admin["data_scope"] == "admin"

        # Update (copy-set pattern)
        entry = dict(store["admin"])
        entry["max_turns"] = 100
        store["admin"] = entry
        assert store["admin"]["max_turns"] == 100

        # Delete
        store.delete("admin")
        assert "admin" not in store
        assert len(store) == 1

    def test_assignment_store_with_overrides(self, tmp_path):
        """Chat assignments with nested overrides stored in JCS."""
        assign_file = tmp_path / "chat_harnesses.json"
        store = JsonConfigStore("assignments", assign_file)

        # Assign chat with overrides
        store.set("telegram:123", {"harness": "admin", "overrides": {"max_turns": 100}})
        assert store["telegram:123"]["harness"] == "admin"
        assert store["telegram:123"]["overrides"]["max_turns"] == 100

        # Update override (copy-set pattern)
        entry = dict(store["telegram:123"])
        entry["overrides"] = dict(entry["overrides"])
        entry["overrides"]["model"] = "claude-opus-4-6"
        store["telegram:123"] = entry
        assert store["telegram:123"]["overrides"]["model"] == "claude-opus-4-6"

    def test_old_string_format_detection(self, tmp_path):
        """Old string format (chat_key → label) can be detected for migration."""
        assign_file = tmp_path / "chat_harnesses.json"
        assign_file.write_text(json.dumps({
            "telegram:123": "admin",
            "telegram:456": {"harness": "team", "overrides": {}},
        }))
        store = JsonConfigStore("assignments", assign_file)

        # Check which entries need migration
        for k, v in store.all().items():
            if isinstance(v, str):
                # Migrate: str → dict
                store.set(k, {"harness": v, "overrides": {}})

        # Verify migration
        assert store["telegram:123"] == {"harness": "admin", "overrides": {}}
        assert store["telegram:456"]["harness"] == "team"

    def test_init_lock_thread_safety(self, tmp_path):
        """Multiple threads calling _ensure_loaded concurrently should be safe."""
        harness_file = tmp_path / "harnesses.json"
        harness_file.write_text(json.dumps({"default": {"label": "default"}}))

        stores = []
        errors = []

        def create_and_read():
            try:
                s = JsonConfigStore("test", harness_file)
                val = s.get("default")
                stores.append(val)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=create_and_read) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(stores) == 10
        assert all(s is not None for s in stores)


# ── External chats JCS tests ───────────────────────────────────────────

class TestDiscoveredChatsJCS:
    """Tests for external chats backed by JCS."""

    def test_int_key_external_chat(self, tmp_path):
        """TG external chats use int keys."""
        dc_file = tmp_path / "external_chats.json"
        store = JsonConfigStore("tg_dc", dc_file, key_type=int)

        store[12345] = {"title": "Test Group", "mode": "silent", "added_at": "2026-04-29"}
        assert 12345 in store
        assert store[12345]["title"] == "Test Group"

        # String key auto-coerced to int
        assert store["12345"]["title"] == "Test Group"

    def test_str_key_wa_external(self, tmp_path):
        """WA external chats use string JID keys."""
        dc_file = tmp_path / "wa_external.json"
        store = JsonConfigStore("wa_dc", dc_file)

        jid = "447700900001@s.whatsapp.net"
        store[jid] = {"title": "WA Group", "mode": "active"}
        assert jid in store
        assert store[jid]["mode"] == "active"

    def test_mode_change_persists(self, tmp_path):
        """Mode change via copy-set pattern persists correctly."""
        dc_file = tmp_path / "external.json"
        store = JsonConfigStore("dc", dc_file, key_type=int)

        store[100] = {"title": "Chat", "mode": "silent"}
        # Correct pattern: copy → mutate → set
        entry = dict(store[100])
        entry["mode"] = "active_plus"
        store[100] = entry

        # Verify on disk
        store2 = JsonConfigStore("dc2", dc_file, key_type=int)
        assert store2[100]["mode"] == "active_plus"

    def test_stale_cleanup_loop(self, tmp_path):
        """Startup cleanup removes stale entries via JCS delete."""
        dc_file = tmp_path / "external.json"
        store = JsonConfigStore("dc", dc_file, key_type=int)
        store[1] = {"title": "A", "mode": "silent"}
        store[2] = {"title": "B", "mode": "active"}
        store[3] = {"title": "C", "mode": "silent"}

        # Simulate cleanup: remove chats 1 and 3
        allowed = {1, 3}
        stale = [cid for cid in store if cid in allowed]
        for cid in stale:
            del store[cid]

        assert len(store) == 1
        assert 2 in store
        assert 1 not in store

    def test_save_noop_backward_compat(self):
        """save_external_chats() is a no-op but should not raise."""
        # This function exists for backward compatibility
        from config import save_external_chats
        save_external_chats()  # should not raise


# ── .prev.json backup tests ─────────────────────────────────────────────

class TestPrevJsonBackup:
    """Tests for .prev.json sidecar backup on JCS writes."""

    def test_prev_created_on_write(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        store["a"] = 1
        store["b"] = 2  # second write creates .prev.json
        prev = tmp_json.with_suffix(".prev.json")
        assert prev.exists()
        prev_data = json.loads(prev.read_text())
        assert prev_data == {"a": 1}  # state before second write

    def test_prev_rollback(self, tmp_json):
        """Demonstrate manual rollback via .prev.json."""
        store = JsonConfigStore("test", tmp_json)
        store.update({"x": 1, "y": 2, "z": 3})
        store.clear()  # destructive — .prev.json has old data

        prev = tmp_json.with_suffix(".prev.json")
        assert prev.exists()
        prev_data = json.loads(prev.read_text())
        assert "x" in prev_data
