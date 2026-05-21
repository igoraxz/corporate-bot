# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Unit test for main._cancel_if_pending helper.

Covers the 3 cases used in handle_telegram_message's deferred-placeholder
cancellation:

  1. None task — no-op (the skip_streaming path never creates _ph_deferred)
  2. Already-done task — no-op (prevents double-cancel errors)
  3. Live pending task — cancelled, awaitable joins with CancelledError

Full integration coverage of the on_stream_chunk recreate path (placeholder
content-leak regression) is not attempted here — it requires mocking Pyrogram
+ SDK + ActiveTask. This unit test guards the primitive the fix is built on.
"""
import asyncio

import pytest


@pytest.fixture
def cancel_if_pending():
    from main import _cancel_if_pending
    return _cancel_if_pending


@pytest.mark.asyncio
async def test_none_task_is_noop(cancel_if_pending):
    # Must not raise on None.
    cancel_if_pending(None)


@pytest.mark.asyncio
async def test_already_done_task_is_noop(cancel_if_pending):
    async def _done():
        return 42

    t = asyncio.create_task(_done())
    await t  # ensure done
    assert t.done()
    # No-op — must not raise.
    cancel_if_pending(t)
    # Task result still reachable (not cancelled).
    assert t.result() == 42


@pytest.mark.asyncio
async def test_live_task_is_cancelled(cancel_if_pending):
    started = asyncio.Event()

    async def _long():
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise

    t = asyncio.create_task(_long())
    await started.wait()
    assert not t.done()

    cancel_if_pending(t)

    # Task should be cancelled promptly.
    with pytest.raises(asyncio.CancelledError):
        await t
    assert t.cancelled()


@pytest.mark.asyncio
async def test_already_cancelled_task_is_noop(cancel_if_pending):
    """Double-cancel safety: cancelling an already-cancelled task must not raise."""
    async def _long():
        await asyncio.sleep(10)

    t = asyncio.create_task(_long())
    # Let the event loop schedule the task before cancelling.
    await asyncio.sleep(0)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert t.cancelled()

    # Second cancel via helper — must be a no-op.
    cancel_if_pending(t)
