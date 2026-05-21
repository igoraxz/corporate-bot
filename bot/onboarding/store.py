# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Onboarding CRUD — get, create, update, list onboarding state."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from bot.onboarding.models import OnboardingState

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def get_onboarding(user_id: str) -> OnboardingState | None:
    """Get onboarding state for a user. Returns None if not found."""
    from bot.storage.db import open_db
    async with open_db() as db:
        cursor = await db.execute(
            "SELECT * FROM user_onboarding WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        if row:
            return OnboardingState.from_row(dict(row))
    return None


async def create_onboarding(user_id: str, source: str = "",  # CB-132: caller must provide
                             display_name: str = "") -> OnboardingState:
    """Create a new onboarding record. Returns the state."""
    from bot.storage.db import open_db
    now = _now_iso()
    state = OnboardingState(
        user_id=user_id,
        source=source,
        display_name=display_name,
        status="pending",
        current_step="welcome",
        created_at=now,
        updated_at=now,
    )
    row = state.to_row()
    async with open_db() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO user_onboarding"
            " (user_id, source, display_name, status, current_step,"
            "  completed_steps, skipped_steps, step_data, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row["user_id"], row["source"], row["display_name"], row["status"],
             row["current_step"], row["completed_steps"], row["skipped_steps"],
             row["step_data"], row["created_at"], row["updated_at"]),
        )
        await db.commit()
        if cursor.rowcount == 0:
            # Race: record already exists — return the existing one
            existing = await get_onboarding(user_id)
            if existing:
                log.info("Onboarding already exists for %s (race), returning existing", user_id)
                return existing
    log.info("Onboarding created for %s (%s)", user_id, source)
    return state


async def update_onboarding(state: OnboardingState) -> None:
    """Persist updated onboarding state."""
    from bot.storage.db import open_db
    row = state.to_row()
    async with open_db() as db:
        await db.execute(
            "UPDATE user_onboarding SET"
            " source=?, display_name=?, status=?, current_step=?,"
            " completed_steps=?, skipped_steps=?, step_data=?,"
            " updated_at=?, completed_at=?"
            " WHERE user_id=?",
            (row["source"], row["display_name"], row["status"], row["current_step"],
             row["completed_steps"], row["skipped_steps"], row["step_data"],
             row["updated_at"], row["completed_at"], row["user_id"]),
        )
        await db.commit()


async def list_onboarding(status: str | None = None) -> list[OnboardingState]:
    """List all onboarding records, optionally filtered by status."""
    from bot.storage.db import open_db
    async with open_db() as db:
        if status:
            cursor = await db.execute(
                "SELECT * FROM user_onboarding WHERE status = ? ORDER BY created_at DESC",
                (status,),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM user_onboarding ORDER BY created_at DESC"
            )
        rows = await cursor.fetchall()
    return [OnboardingState.from_row(dict(r)) for r in rows]


async def delete_onboarding(user_id: str) -> bool:
    """Delete an onboarding record. Returns True if deleted."""
    from bot.storage.db import open_db
    async with open_db() as db:
        cursor = await db.execute(
            "DELETE FROM user_onboarding WHERE user_id = ?", (user_id,)
        )
        await db.commit()
        return cursor.rowcount > 0


# NOTE: domain_access_requests CRUD removed in CB-51 (onboarding overhaul).
# The DB table still exists (schema.py) but is unused — safe to drop in a future migration.
