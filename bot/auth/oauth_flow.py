# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""OAuth authentication flows for Claude Code sessions.

Two modes:
1. Per-user OAuth (DM sessions): User authenticates their own Claude account
   via device code flow or token paste. Token stored per-user in token_vault.
2. Shared OAuth (group sessions): Admin configures a shared Claude token
   for team channels. All users in the channel share the token.

In-chat flows: auth happens IN the conversation (TG inline keyboards / Teams cards),
not via external web UI.
"""

import json
import logging
import time

# Valid OAuth providers (security review F4 — defense-in-depth)
VALID_PROVIDERS = frozenset({"claude", "m365", "google", "github"})

from bot.storage.db import open_db

log = logging.getLogger(__name__)


# === Per-User OAuth Token Storage ===

async def store_user_oauth_token(user_id: str, provider: str, token: str,
                                  metadata: dict | None = None) -> bool:
    """Store an OAuth token for a user.

    Args:
        user_id: Azure AD Object ID or TG user ID
        provider: "claude" | "google" | "m365" | "github"
        token: The OAuth access/refresh token
        metadata: Optional metadata (email, expiry, scopes)
    """
    # Validate provider
    if provider not in VALID_PROVIDERS:
        log.warning("Invalid provider '%s' for user %s", provider, user_id[:8])
        return False
    # Encrypt token before storage (security review F1)
    from bot.auth.token_vault import _encrypt
    encrypted_token = _encrypt(token)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    async with open_db() as db:
        await db.execute("""
            INSERT INTO user_oauth_tokens (user_id, provider, token, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, provider) DO UPDATE SET
                token=excluded.token, metadata=excluded.metadata, updated_at=excluded.updated_at
        """, (user_id, provider, encrypted_token, json.dumps(metadata or {}), now, now))
        await db.commit()
    log.info("Stored %s OAuth token for user %s", provider, user_id[:8])
    return True


async def get_user_oauth_token(user_id: str, provider: str) -> str | None:
    """Get a user's OAuth token (decrypted). Returns None if not stored."""
    async with open_db() as db:
        cursor = await db.execute(
            "SELECT token FROM user_oauth_tokens WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        try:
            from bot.auth.token_vault import _decrypt
            return _decrypt(row["token"])
        except Exception:
            log.warning("Token decrypt failed for %s/%s — returning None (re-auth needed)", user_id[:8], provider)
            return None  # Fail-closed: corrupt/wrong-key tokens are not usable


async def get_user_oauth_token_metadata(user_id: str, provider: str) -> dict | None:
    """Get a user's OAuth token metadata (no decryption). Returns None if not stored."""
    async with open_db() as db:
        cursor = await db.execute(
            "SELECT metadata FROM user_oauth_tokens WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        )
        row = await cursor.fetchone()
        if not row or not row["metadata"]:
            return None
        try:
            return json.loads(row["metadata"])
        except (json.JSONDecodeError, TypeError):
            return None


async def delete_user_oauth_token(user_id: str, provider: str) -> bool:
    """Remove a user's OAuth token."""
    async with open_db() as db:
        await db.execute(
            "DELETE FROM user_oauth_tokens WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        )
        await db.commit()
    log.info("Deleted %s OAuth token for user %s", provider, user_id[:8])
    return True


async def auto_reset_session_after_auth(source: str, chat_id: str, auth_type: str) -> None:
    """Auto-reset the SDK session after auth completion.

    GitHub/Google/M365 tokens are injected via opts.env at client creation time.
    If auth completes AFTER the session is already running, the subprocess doesn't
    pick up the new env vars. Resetting forces a new client creation on next query.
    """
    try:
        from bot.agent import client_pool
        session_key = f"{source}:{chat_id}"
        pool_status = client_pool.get_status()
        if session_key in pool_status:
            await client_pool.reset_session(session_key)
            log.info("Auto-reset session %s after %s auth (env refresh)", session_key, auth_type)
    except Exception as e:
        log.debug("Auto-reset after %s auth failed (non-blocking): %s", auth_type, e)


async def list_user_tokens(user_id: str) -> list[dict]:
    """List all OAuth tokens for a user (metadata only, NOT the token itself)."""
    async with open_db() as db:
        cursor = await db.execute(
            "SELECT provider, metadata, created_at, updated_at FROM user_oauth_tokens WHERE user_id = ?",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [{"provider": r["provider"], "metadata": json.loads(r["metadata"] or "{}"),
                 "created_at": r["created_at"], "updated_at": r["updated_at"]} for r in rows]


# === Shared Chat OAuth ===

async def store_chat_oauth_token(chat_id: str, provider: str, token: str,
                                  set_by: str = "", metadata: dict | None = None) -> bool:
    """Store a shared OAuth token for a chat/channel (encrypted)."""
    if provider not in VALID_PROVIDERS:
        log.warning("Invalid provider '%s' for chat %s", provider, chat_id[:12])
        return False
    from bot.auth.token_vault import _encrypt
    encrypted_token = _encrypt(token)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    async with open_db() as db:
        await db.execute("""
            INSERT INTO chat_oauth_tokens (chat_id, provider, token, set_by, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, provider) DO UPDATE SET
                token=excluded.token, set_by=excluded.set_by,
                metadata=excluded.metadata, updated_at=excluded.updated_at
        """, (chat_id, provider, encrypted_token, set_by, json.dumps(metadata or {}), now, now))
        await db.commit()
    log.info("Stored shared %s OAuth token for chat %s (by %s)", provider, chat_id[:12], set_by[:8] if set_by else "-")
    return True


async def get_chat_oauth_token(chat_id: str, provider: str) -> str | None:
    """Get a chat's shared OAuth token (decrypted)."""
    async with open_db() as db:
        cursor = await db.execute(
            "SELECT token FROM chat_oauth_tokens WHERE chat_id = ? AND provider = ?",
            (chat_id, provider),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        try:
            from bot.auth.token_vault import _decrypt
            return _decrypt(row["token"])
        except Exception:
            log.warning("Chat token decrypt failed for %s/%s — returning None (re-auth needed)", chat_id[:12], provider)
            return None


# === OAuth Token Resolution (per-session) ===

async def resolve_claude_token(user_id: str, chat_id: str) -> str | None:
    """Resolve which Claude OAuth token to use for a session.

    Priority:
    1. User's personal token (DM sessions)
    2. Chat's shared token (group sessions, set by admin)
    3. System default (from CLAUDE_CODE_OAUTH_TOKEN env var)
    """
    # Try user token first (DMs)
    user_token = await get_user_oauth_token(user_id, "claude")
    if user_token:
        return user_token

    # Try chat shared token (groups)
    chat_token = await get_chat_oauth_token(chat_id, "claude")
    if chat_token:
        return chat_token

    # Fall back to system default
    from config import CLAUDE_CODE_OAUTH_TOKEN
    return CLAUDE_CODE_OAUTH_TOKEN or None


# === Per-User M365/Gmail Auth Resolution (B5c) ===

async def resolve_credential(user_id: str, chat_id: str, provider: str) -> str | None:
    """Resolve a credential for a specific provider.

    Priority: user personal -> chat shared -> system default.
    """
    user_token = await get_user_oauth_token(user_id, provider)
    if user_token:
        return user_token
    chat_token = await get_chat_oauth_token(chat_id, provider)
    if chat_token:
        return chat_token
    return None
