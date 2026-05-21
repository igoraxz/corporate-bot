# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Per-user encrypted token vault for MS365 SSO.

Stores OAuth tokens (access + refresh) per user, encrypted at rest with
Fernet symmetric encryption. Supports the Teams SSO On-Behalf-Of (OBO)
flow where each employee's personal MS365 token is stored after consent.

Architecture:
- SQLite table: user_tokens (aad_object_id → encrypted token bundle)
- Fernet encryption: key from TOKEN_ENCRYPTION_KEY env var
- Token refresh: scheduler pre-refreshes before expiry
- Cleanup: tokens expired >90 days are purged

Security:
- Encryption key MUST be in env var (never in code/DB)
- Tokens are NEVER logged or returned in plain text
- Each user's token is isolated — no cross-user access
- DM sessions resolve token from vault by user's aad_object_id
"""

import asyncio
import json
import logging
import time

log = logging.getLogger(__name__)

# Lazy-init encryption
_fernet = None


def _get_fernet():
    """Get or create Fernet instance from env key."""
    global _fernet
    if _fernet is None:
        from config import TOKEN_ENCRYPTION_KEY
        if not TOKEN_ENCRYPTION_KEY:
            raise RuntimeError(
                "TOKEN_ENCRYPTION_KEY not configured. "
                "Generate: python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        from cryptography.fernet import Fernet
        _fernet = Fernet(TOKEN_ENCRYPTION_KEY.encode())
    return _fernet


def _encrypt(data: str) -> str:
    """Encrypt a string. Returns base64-encoded ciphertext."""
    return _get_fernet().encrypt(data.encode()).decode()


def _decrypt(ciphertext: str) -> str:
    """Decrypt a base64-encoded ciphertext. Returns plaintext."""
    return _get_fernet().decrypt(ciphertext.encode()).decode()


# ============================================================
# Database Operations
# ============================================================

async def init_token_tables() -> None:
    """Create user_tokens table if it doesn't exist."""
    from bot.storage.db import open_db
    async with open_db() as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS user_tokens (
                user_id TEXT PRIMARY KEY,
                token_bundle TEXT NOT NULL,
                scopes TEXT NOT NULL DEFAULT '',
                expires_at REAL NOT NULL DEFAULT 0,
                refresh_expires_at REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                acquired_at TEXT NOT NULL,
                refreshed_at TEXT,
                user_email TEXT NOT NULL DEFAULT '',
                user_name TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_user_tokens_expires
                ON user_tokens(expires_at);
            CREATE INDEX IF NOT EXISTS idx_user_tokens_status
                ON user_tokens(status);
        """)
        await db.commit()

    # Validate Fernet key at startup (fail-fast on misconfiguration)
    from config import TOKEN_ENCRYPTION_KEY
    if TOKEN_ENCRYPTION_KEY:
        try:
            _get_fernet()
            log.info("User token vault tables initialized (encryption key valid)")
        except Exception as e:
            log.error("TOKEN_ENCRYPTION_KEY is invalid: %s. Per-user SSO will not work.", e)
    else:
        log.info("User token vault tables initialized (no encryption key — SSO disabled)")


async def store_token(
    user_id: str,
    access_token: str,
    refresh_token: str,
    expires_in: int,
    scopes: list[str],
    user_email: str = "",
    user_name: str = "",
) -> None:
    """Store or update a user's OAuth token (encrypted).

    Args:
        user_id: Azure AD Object ID
        access_token: MS365 access token
        refresh_token: MS365 refresh token
        expires_in: Token TTL in seconds
        scopes: Granted scopes
        user_email: User's email (for display/audit)
        user_name: User's display name
    """
    now = time.time()
    bundle = json.dumps({
        "access_token": access_token,
        "refresh_token": refresh_token,
    })
    encrypted = _encrypt(bundle)

    from bot.storage.db import open_db
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    async with open_db() as db:
        await db.execute("""
            INSERT INTO user_tokens (user_id, token_bundle, scopes, expires_at,
                                     refresh_expires_at, status, acquired_at,
                                     user_email, user_name)
            VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                token_bundle=excluded.token_bundle,
                scopes=excluded.scopes,
                expires_at=excluded.expires_at,
                refresh_expires_at=excluded.refresh_expires_at,
                status='active',
                refreshed_at=?,
                user_email=excluded.user_email,
                user_name=excluded.user_name
        """, (
            user_id,
            encrypted,
            ",".join(scopes),
            now + expires_in,
            now + 90 * 86400,  # refresh token valid ~90 days
            now_iso,
            user_email,
            user_name,
            now_iso,
        ))
        await db.commit()

    log.info("Token stored for user %s (%s), expires in %ds",
             user_id[:8], user_email or "?", expires_in)


async def get_token(user_id: str) -> dict | None:
    """Retrieve and decrypt a user's token bundle.

    Returns: {"access_token": ..., "refresh_token": ..., "expires_at": ..., "scopes": ...}
             or None if not found/expired/revoked.
    """
    from bot.storage.db import open_db
    async with open_db() as db:
        cursor = await db.execute(
            "SELECT token_bundle, expires_at, scopes, status FROM user_tokens WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()

    if not row:
        return None

    if row["status"] != "active":
        return None

    try:
        bundle = json.loads(_decrypt(row["token_bundle"]))
    except Exception as e:
        log.error("Failed to decrypt token for user %s: %s", user_id[:8], e)
        return None

    is_expired = time.time() > row["expires_at"]
    if is_expired:
        log.warning("Token for user %s is expired (expired %.0fs ago) — caller should refresh",
                    user_id[:8], time.time() - row["expires_at"])

    return {
        "access_token": bundle["access_token"],
        "refresh_token": bundle["refresh_token"],
        "expires_at": row["expires_at"],
        "scopes": row["scopes"].split(",") if row["scopes"] else [],
        "is_expired": is_expired,
    }


async def delete_token(user_id: str) -> bool:
    """Delete a user's stored token (revocation)."""
    from bot.storage.db import open_db
    async with open_db() as db:
        await db.execute("DELETE FROM user_tokens WHERE user_id = ?", (user_id,))
        await db.commit()
    log.info("Token deleted for user %s", user_id[:8])
    return True


async def mark_needs_reauth(user_id: str) -> None:
    """Mark a user's token as needing re-authentication."""
    from bot.storage.db import open_db
    async with open_db() as db:
        await db.execute(
            "UPDATE user_tokens SET status='needs_reauth' WHERE user_id = ?",
            (user_id,),
        )
        await db.commit()


# ============================================================
# Token Refresh
# ============================================================

async def refresh_user_token(user_id: str) -> bool:
    """Refresh a user's access token using their refresh token.

    Uses Azure AD token endpoint with grant_type=refresh_token.

    Returns: True on success, False on failure (marks needs_reauth).
    """
    import httpx
    from config import MS365_CLIENT_ID, MS365_CLIENT_SECRET, MS365_TENANT_ID

    token_data = await get_token(user_id)
    if not token_data or not token_data.get("refresh_token"):
        await mark_needs_reauth(user_id)
        return False

    token_url = f"https://login.microsoftonline.com/{MS365_TENANT_ID}/oauth2/v2.0/token"

    try:
        async with httpx.AsyncClient(timeout=30) as http:
            resp = await http.post(token_url, data={
                "grant_type": "refresh_token",
                "client_id": MS365_CLIENT_ID,
                "client_secret": MS365_CLIENT_SECRET,
                "refresh_token": token_data["refresh_token"],
                "scope": " ".join(token_data.get("scopes", [])),
            })
            resp.raise_for_status()
            data = resp.json()

        # Store refreshed token
        await store_token(
            user_id=user_id,
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", token_data["refresh_token"]),
            expires_in=data.get("expires_in", 3600),
            scopes=data.get("scope", "").split(" "),
        )
        return True

    except httpx.HTTPStatusError as e:
        try:
            err = e.response.json()
            err_msg = f"{err.get('error', '?')}: {err.get('error_description', '?')[:80]}"
        except Exception:
            err_msg = f"HTTP {e.response.status_code}"
        log.warning("Token refresh failed for %s: %s %s",
                    user_id[:8], e.response.status_code, err_msg)
        if e.response.status_code in (400, 401):
            # Refresh token likely expired
            await mark_needs_reauth(user_id)
        return False
    except Exception as e:
        log.error("Token refresh error for %s: %s", user_id[:8], e)
        return False


# ============================================================
# OBO Flow (On-Behalf-Of)
# ============================================================

async def exchange_teams_token(
    teams_token: str,
    user_id: str,
    user_email: str = "",
    user_name: str = "",
) -> bool:
    """Exchange a Teams SSO token for Graph API token via OBO flow.

    This is called when a user first interacts with the bot in Teams DM
    and Teams provides their token via SSO.

    Args:
        teams_token: The token from Teams SSO
        user_id: User's AAD Object ID
        user_email: User's email
        user_name: User's display name

    Returns: True if exchange succeeded and token stored.
    """
    import httpx
    from config import MS365_CLIENT_ID, MS365_CLIENT_SECRET, MS365_TENANT_ID, MS365_SCOPES

    token_url = f"https://login.microsoftonline.com/{MS365_TENANT_ID}/oauth2/v2.0/token"
    scopes = " ".join(MS365_SCOPES) if isinstance(MS365_SCOPES, list) else MS365_SCOPES

    try:
        async with httpx.AsyncClient(timeout=30) as http:
            resp = await http.post(token_url, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "client_id": MS365_CLIENT_ID,
                "client_secret": MS365_CLIENT_SECRET,
                "assertion": teams_token,
                "scope": scopes,
                "requested_token_use": "on_behalf_of",
            })
            resp.raise_for_status()
            data = resp.json()

        await store_token(
            user_id=user_id,
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", ""),
            expires_in=data.get("expires_in", 3600),
            scopes=data.get("scope", "").split(" "),
            user_email=user_email,
            user_name=user_name,
        )
        log.info("OBO exchange successful for %s (%s)", user_id[:8], user_email)
        return True

    except httpx.HTTPStatusError as e:
        try:
            err = e.response.json()
            err_msg = f"{err.get('error', '?')}: {err.get('error_description', '?')[:80]}"
        except Exception:
            err_msg = f"HTTP {e.response.status_code}"
        log.error("OBO exchange failed for %s: %s %s",
                  user_id[:8], e.response.status_code, err_msg)
        return False
    except Exception as e:
        log.error("OBO exchange error for %s: %s", user_id[:8], e)
        return False


# ============================================================
# Scheduled Pre-Refresh
# ============================================================

async def prerefresh_expiring_tokens(buffer_minutes: int = 10, batch_size: int = 50) -> dict:
    """Refresh all tokens expiring within buffer_minutes.

    Called by scheduler (e.g. every 5 minutes).

    Returns: {"refreshed": N, "failed": N, "total_active": N}
    """
    from bot.storage.db import open_db
    cutoff = time.time() + (buffer_minutes * 60)

    async with open_db() as db:
        cursor = await db.execute("""
            SELECT user_id FROM user_tokens
            WHERE status = 'active' AND expires_at < ? AND expires_at > 0
            ORDER BY expires_at ASC
            LIMIT ?
        """, (cutoff, batch_size))
        rows = await cursor.fetchall()

    refreshed = 0
    failed = 0
    for row in rows:
        success = await refresh_user_token(row["user_id"])
        if success:
            refreshed += 1
        else:
            failed += 1
        # Small delay to avoid rate limiting
        await asyncio.sleep(0.5)

    # Count total active tokens
    async with open_db() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM user_tokens WHERE status = 'active'"
        )
        total_row = await cursor.fetchone()
        total = total_row["cnt"] if total_row else 0

    if refreshed or failed:
        log.info("Token pre-refresh: %d refreshed, %d failed, %d total active",
                 refreshed, failed, total)

    return {"refreshed": refreshed, "failed": failed, "total_active": total}


# ============================================================
# Cleanup
# ============================================================

async def cleanup_stale_tokens(max_age_days: int = 90) -> int:
    """Remove tokens that haven't been refreshed in max_age_days.

    Called periodically (e.g. daily) to clean expired/abandoned tokens.
    """
    from bot.storage.db import open_db
    cutoff = time.time() - (max_age_days * 86400)

    async with open_db() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM user_tokens WHERE refresh_expires_at < ?",
            (cutoff,),
        )
        row = await cursor.fetchone()
        count = row["cnt"] if row else 0

        if count > 0:
            await db.execute(
                "DELETE FROM user_tokens WHERE refresh_expires_at < ?",
                (cutoff,),
            )
            await db.commit()
            log.info("Cleaned up %d stale tokens (>%d days)", count, max_age_days)

    return count
