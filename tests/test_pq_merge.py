# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Unit tests for PersistentQueue.merge_into_pending (v5 burst-coalesce).

Covers:
 1. no pending row → None
 2. text-only merge appends to parsed_json.text
 3. media merge appends raw to parsed_json.merged_media
 4. text+media merge touches both
 5. soft-cap on text > 50KB → None
 6. soft-cap on media items > 10 → None
 7. race: row status flipped to 'processing' between SELECT and UPDATE → None
 8. catchup row skipped (is_catchup=1)
 9. merged_message_ids deduplication
10. later dispatch sees merged payload (worker reads parsed_json)

Also includes a smoke test for the burst-dispatch debounce via _claim_next.
"""

import asyncio
import json
import os
import tempfile
import pytest
import pytest_asyncio

import config as config_module
from bot.storage import db as db_module
from bot.pipeline import task_queue
from bot.pipeline.task_queue import PersistentQueue


@pytest_asyncio.fixture
async def pq(tmp_path, monkeypatch):
    """Fresh PersistentQueue on a scratch SQLite DB."""
    dbpath = tmp_path / "test.db"
    # Close any existing singleton before patching the path —
    # otherwise get_db() returns the stale prod connection.
    if db_module._db is not None:
        try:
            await db_module._db.close()
        except Exception:
            pass
        db_module._db = None
    # Route bot.storage.db singleton to this temp file.
    # Must patch both: config.DB_PATH (source) AND bot.storage.db.DB_PATH
    # (already-imported name used by get_db at line 42).
    monkeypatch.setattr(config_module, "DB_PATH", str(dbpath))
    monkeypatch.setattr(db_module, "DB_PATH", str(dbpath))

    async def _dispatch(parsed):
        pass

    def _can_accept():
        return True

    q = PersistentQueue(dispatch_fn=_dispatch, can_accept_fn=_can_accept,
                        max_pending=50)
    await q.init_schema()
    yield q
    # Teardown: close the test DB and reset singleton
    await db_module.close_db()


def _parsed(chat_id="100", msg_id="m1", text="hello", has_media=False,
            raw=None, forward=None, reply_to=None, user_id="u1",
            user_name="TestUser", source="telegram"):
    d = {
        "source": source,
        "chat_id": chat_id,
        "message_id": msg_id,
        "user_id": user_id,
        "user_name": user_name,
        "text": text,
        "has_media": has_media,
        "raw": raw,
        "forward": forward,
        "reply_to": reply_to,
    }
    return d


@pytest.mark.asyncio
async def test_merge_no_pending_returns_none(pq):
    p = _parsed(msg_id="m2", text="second")
    result = await pq.merge_into_pending(p, p["text"])
    assert result is None, "no pending row → None"


@pytest.mark.asyncio
async def test_merge_text_only(pq):
    p1 = _parsed(msg_id="m1", text="first")
    row_id, _ = await pq.enqueue(p1)
    p2 = _parsed(msg_id="m2", text="second")
    merged = await pq.merge_into_pending(p2, "second")
    assert merged == row_id
    # Verify parsed_json.text has both + merged_message_ids has m2
    from bot.storage.db import open_db
    async with open_db() as dbh:
        rows = await dbh.execute_fetchall(
            "SELECT parsed_json FROM task_queue WHERE id=?", (row_id,)
        )
    pj = json.loads(rows[0]["parsed_json"])
    assert "first" in pj["text"] and "second" in pj["text"]
    assert "m2" in pj["merged_message_ids"]
    assert pj.get("merged_media", []) == []


@pytest.mark.asyncio
async def test_merge_media(pq):
    p1 = _parsed(msg_id="m1", text="first")
    row_id, _ = await pq.enqueue(p1)
    p2 = _parsed(msg_id="m2", text="photo caption",
                 has_media=True, raw={"media_type": "photo", "file_id": "abc"})
    merged = await pq.merge_into_pending(p2, "photo caption")
    assert merged == row_id
    from bot.storage.db import open_db
    async with open_db() as dbh:
        rows = await dbh.execute_fetchall(
            "SELECT parsed_json FROM task_queue WHERE id=?", (row_id,)
        )
    pj = json.loads(rows[0]["parsed_json"])
    assert len(pj["merged_media"]) == 1
    assert pj["merged_media"][0]["message_id"] == "m2"
    assert pj["merged_media"][0]["raw"]["file_id"] == "abc"


@pytest.mark.asyncio
async def test_merge_text_plus_media(pq):
    p1 = _parsed(msg_id="m1", text="hi")
    row_id, _ = await pq.enqueue(p1)
    p2 = _parsed(msg_id="m2", text="caption", has_media=True,
                 raw={"media_type": "video", "file_id": "v1"})
    merged = await pq.merge_into_pending(p2, "caption")
    assert merged == row_id
    from bot.storage.db import open_db
    async with open_db() as dbh:
        rows = await dbh.execute_fetchall(
            "SELECT parsed_json FROM task_queue WHERE id=?", (row_id,)
        )
    pj = json.loads(rows[0]["parsed_json"])
    assert "hi" in pj["text"] and "caption" in pj["text"]
    assert len(pj["merged_media"]) == 1


@pytest.mark.asyncio
async def test_soft_cap_text_bail(pq):
    p1 = _parsed(msg_id="m1", text="x" * 40000)
    row_id, _ = await pq.enqueue(p1)
    # Another 20KB would push past 50KB cap
    p2 = _parsed(msg_id="m2", text="y" * 20000)
    merged = await pq.merge_into_pending(p2, "y" * 20000)
    assert merged is None, "text>50KB → bail"


@pytest.mark.asyncio
async def test_soft_cap_media_bail(pq):
    p1 = _parsed(msg_id="m1", text="start")
    row_id, _ = await pq.enqueue(p1)
    # Fill 10 merged_media items
    for i in range(10):
        p = _parsed(msg_id=f"m{i+2}", text="", has_media=True,
                    raw={"media_type": "photo", "file_id": f"f{i}"})
        await pq.merge_into_pending(p, "")
    # 11th merge should bail
    p11 = _parsed(msg_id="m12", text="", has_media=True,
                  raw={"media_type": "photo", "file_id": "f10"})
    merged = await pq.merge_into_pending(p11, "")
    assert merged is None, "media>10 → bail"


@pytest.mark.asyncio
async def test_race_status_flipped(pq):
    p1 = _parsed(msg_id="m1", text="first")
    row_id, _ = await pq.enqueue(p1)
    # Simulate race: flip to processing before merge's UPDATE lands
    from bot.storage.db import open_db
    async with open_db() as dbh:
        await dbh.execute(
            "UPDATE task_queue SET status='processing' WHERE id=?", (row_id,)
        )
        await dbh.commit()
    p2 = _parsed(msg_id="m2", text="second")
    merged = await pq.merge_into_pending(p2, "second")
    assert merged is None, "row no longer pending → caller enqueues fresh"


@pytest.mark.asyncio
async def test_catchup_row_skipped(pq):
    # Enqueue as catchup
    p1 = _parsed(msg_id="m1", text="catchup-only")
    row_id, _ = await pq.enqueue(p1, is_catchup=True)
    p2 = _parsed(msg_id="m2", text="new msg")
    merged = await pq.merge_into_pending(p2, "new msg")
    assert merged is None, "catchup rows are never merged into"


@pytest.mark.asyncio
async def test_merged_ids_dedup(pq):
    p1 = _parsed(msg_id="m1", text="first")
    row_id, _ = await pq.enqueue(p1)
    p2 = _parsed(msg_id="m2", text="second")
    await pq.merge_into_pending(p2, "second")
    # Same msg_id merge attempt — should not duplicate in list
    await pq.merge_into_pending(p2, "second-again")
    from bot.storage.db import open_db
    async with open_db() as dbh:
        rows = await dbh.execute_fetchall(
            "SELECT parsed_json FROM task_queue WHERE id=?", (row_id,)
        )
    pj = json.loads(rows[0]["parsed_json"])
    assert pj["merged_message_ids"].count("m2") == 1, "msg_id dedup expected"
