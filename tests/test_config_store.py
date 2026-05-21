# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Tests for bot.config_store.JsonConfigStore."""

import json
import os
import threading
import time
from pathlib import Path

import pytest

from bot.config_store import JsonConfigStore


@pytest.fixture
def tmp_json(tmp_path):
    """Return a path for a temporary JSON config file."""
    return tmp_path / "test_config.json"


class TestBasics:
    def test_empty_on_missing_file(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        assert store.all() == {}
        assert len(store) == 0

    def test_set_and_get(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        store.set("key1", "value1")
        assert store.get("key1") == "value1"
        assert store.get("missing") is None
        assert store.get("missing", "fallback") == "fallback"

    def test_delete(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        store.set("key1", "value1")
        assert store.delete("key1") is True
        assert store.get("key1") is None
        assert store.delete("key1") is False

    def test_update_batch(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        store.update({"a": 1, "b": 2, "c": 3})
        assert store.all() == {"a": 1, "b": 2, "c": 3}

    def test_clear(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        store.update({"a": 1, "b": 2})
        store.clear()
        assert store.all() == {}
        assert len(store) == 0

    def test_replace(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        store.update({"old1": 1, "old2": 2})
        store.replace({"new1": "a", "new2": "b"})
        assert store.all() == {"new1": "a", "new2": "b"}
        assert "old1" not in store
        assert len(store) == 2

    def test_replace_empty(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        store.update({"a": 1, "b": 2})
        store.replace({})
        assert store.all() == {}
        assert len(store) == 0

    def test_contains(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        store.set("exists", True)
        assert "exists" in store
        assert "nope" not in store

    def test_len(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        assert len(store) == 0
        store.set("a", 1)
        assert len(store) == 1
        store.update({"b": 2, "c": 3})
        assert len(store) == 3

    def test_repr(self, tmp_json):
        store = JsonConfigStore("mystore", tmp_json)
        r = repr(store)
        assert "mystore" in r
        assert str(tmp_json) in r


class TestPersistence:
    def test_persists_to_disk(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        store.set("key", "val")
        data = json.loads(tmp_json.read_text())
        assert data == {"key": "val"}

    def test_loads_from_existing_file(self, tmp_json):
        tmp_json.write_text(json.dumps({"pre": "existing"}))
        store = JsonConfigStore("test", tmp_json)
        assert store.get("pre") == "existing"

    def test_atomic_write_no_tmp_left(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        store.set("key", "val")
        tmp_file = tmp_json.with_suffix(".tmp")
        assert not tmp_file.exists()

    def test_creates_parent_dirs(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c" / "config.json"
        store = JsonConfigStore("test", deep)
        store.set("key", "val")
        assert deep.exists()
        assert json.loads(deep.read_text()) == {"key": "val"}

    def test_second_store_reads_first(self, tmp_json):
        s1 = JsonConfigStore("s1", tmp_json)
        s1.set("shared", "data")
        s2 = JsonConfigStore("s2", tmp_json)
        assert s2.get("shared") == "data"


class TestReload:
    def test_mtime_reload(self, tmp_json):
        store = JsonConfigStore("test", tmp_json, mtime_interval_s=0)
        store.set("original", True)
        # External modification — bump mtime to ensure change detection
        tmp_json.write_text(json.dumps({"external": True}))
        future = time.time() + 2
        os.utime(tmp_json, (future, future))
        assert store.get("external") is True
        assert store.get("original") is None

    def test_reload_if_changed_explicit(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        store.set("a", 1)
        tmp_json.write_text(json.dumps({"b": 2}))
        future = time.time() + 2
        os.utime(tmp_json, (future, future))
        reloaded = store.reload_if_changed()
        assert reloaded is True
        assert store.get("b") == 2
        assert store.get("a") is None

    def test_throttled_reload(self, tmp_json):
        store = JsonConfigStore("test", tmp_json, mtime_interval_s=10)
        store.set("a", 1)
        tmp_json.write_text(json.dumps({"b": 2}))
        # Throttled — should NOT reload yet
        assert store.get("a") == 1


class TestKeyCoercion:
    def test_int_keys(self, tmp_json):
        store = JsonConfigStore("test", tmp_json, key_type=int)
        store.set(42, "answer")
        store.set("123", "number")
        assert store.get(42) == "answer"
        assert store.get("42") == "answer"
        assert store.get(123) == "number"

    def test_int_keys_persist_as_strings(self, tmp_json):
        store = JsonConfigStore("test", tmp_json, key_type=int)
        store.set(42, "val")
        data = json.loads(tmp_json.read_text())
        assert "42" in data

    def test_int_keys_loaded_from_disk(self, tmp_json):
        tmp_json.write_text(json.dumps({"42": "val", "bad": "dropped"}))
        store = JsonConfigStore("test", tmp_json, key_type=int)
        assert store.get(42) == "val"
        # "bad" can't convert to int — silently dropped
        assert len(store) == 1

    def test_str_key_coercion(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        store.set(123, "val")
        assert store.get("123") == "val"
        assert store.get(123) == "val"


class TestLazy:
    def test_lazy_init(self, tmp_json):
        tmp_json.write_text(json.dumps({"lazy": True}))
        store = JsonConfigStore("test", tmp_json, lazy=True)
        assert store._loaded is False
        assert store.get("lazy") is True
        assert store._loaded is True

    def test_non_lazy_init(self, tmp_json):
        tmp_json.write_text(json.dumps({"eager": True}))
        store = JsonConfigStore("test", tmp_json, lazy=False)
        assert store._loaded is True


class TestEdgeCases:
    def test_invalid_json(self, tmp_json):
        tmp_json.write_text("not valid json {{{")
        store = JsonConfigStore("test", tmp_json)
        assert store.all() == {}

    def test_non_dict_json(self, tmp_json):
        tmp_json.write_text(json.dumps([1, 2, 3]))
        store = JsonConfigStore("test", tmp_json)
        assert store.all() == {}

    def test_complex_values(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        store.set("nested", {"a": [1, 2], "b": {"c": True}})
        assert store.get("nested") == {"a": [1, 2], "b": {"c": True}}

    def test_unicode_values(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        store.set("ru", "Привет мир")
        store.set("emoji", "🚀")
        store2 = JsonConfigStore("test2", tmp_json)
        assert store2.get("ru") == "Привет мир"
        assert store2.get("emoji") == "🚀"

    def test_overwrite_value(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        store.set("k", "v1")
        store.set("k", "v2")
        assert store.get("k") == "v2"
        assert len(store) == 1


class TestThreadSafety:
    def test_concurrent_writes(self, tmp_json):
        store = JsonConfigStore("test", tmp_json)
        errors = []

        def writer(prefix, count):
            try:
                for i in range(count):
                    store.set(f"{prefix}_{i}", i)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=("a", 50)),
            threading.Thread(target=writer, args=("b", 50)),
            threading.Thread(target=writer, args=("c", 50)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(store) == 150
        for prefix in "abc":
            for i in range(50):
                assert store.get(f"{prefix}_{i}") == i
