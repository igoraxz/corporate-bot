# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Universal credential store — typed, scoped, encrypted.

Replaces the per-type token handling (allowed_emails, OAuth profiles, token vault)
with a single unified store. Each credential has:
- id: unique label (e.g. "hr-inbox", "github-eng", "user:abc-123")
- type: google-workspace, ms365, github, api-key, custom
- account: account identifier (email, username, key name)
- scope: system (bot-only) | harness (team-level) | user (personal DM)
- owner: harness label (scope=harness) or AAD object ID (scope=user)

Enforcement:
- Harness field `allowed_credentials` references credential IDs
- hooks.py Layer 1b3 checks: tool's credential_id in harness allowed_credentials?
- User-scope credentials: only accessible in owner's DM session
- System-scope credentials: not directly accessible via tools (bot internals)
"""

import json
import logging
import time

from bot.storage.db import open_db

log = logging.getLogger(__name__)

# Lazy-init encryption (reuses token_vault's Fernet)
def _encrypt(data: str) -> str:
    from bot.auth.token_vault import _encrypt as _enc
    return _enc(data)

def _decrypt(ciphertext: str) -> str:
    from bot.auth.token_vault import _decrypt as _dec
    return _dec(ciphertext)


# Valid credential types (+ claude-sdk for OAuth profiles)
CREDENTIAL_TYPES = frozenset({
    "google-workspace", "ms365", "claude-sdk", "github", "api-key",
    "jira", "slack", "custom", "ssh-key",
})

# Valid scopes
CREDENTIAL_SCOPES = frozenset({"system", "harness", "user"})

# Canonical cred_id prefix for each cred_type.
# cred_type is the database column (google-workspace, ms365, claude-sdk, github).
# cred_id prefix is the short human-readable form used in allowed_credentials.
# ALWAYS use cred_id_prefix() when constructing credential IDs — never ad-hoc f-strings.
_CRED_TYPE_TO_PREFIX: dict[str, str] = {
    "google-workspace": "google",
    "ms365": "m365",
    "claude-sdk": "claude",
    "github": "github",
    "ssh-key": "ssh",
}

# Reverse: prefix → all possible cred_types that map to it
_PREFIX_TO_CRED_TYPES: dict[str, list[str]] = {}
for _ct, _pf in _CRED_TYPE_TO_PREFIX.items():
    _PREFIX_TO_CRED_TYPES.setdefault(_pf, []).append(_ct)


def cred_id_prefix(cred_type: str) -> str:
    """Get the canonical cred_id prefix for a credential type.

    Examples:
        cred_id_prefix("google-workspace") → "google"
        cred_id_prefix("ms365") → "m365"
        cred_id_prefix("claude-sdk") → "claude"
        cred_id_prefix("github") → "github"
    """
    return _CRED_TYPE_TO_PREFIX.get(cred_type, cred_type)


def normalize_cred_id(raw_id: str) -> str:
    """Normalize a credential ID to use the canonical prefix.

    Handles both long and short forms:
        "google-workspace:user@co.com" → "google:user@co.com"
        "ms365:admin" → "m365:admin"
        "google:user@co.com" → "google:user@co.com" (already canonical)
    """
    if ":" not in raw_id:
        return raw_id
    prefix, rest = raw_id.split(":", 1)
    canonical = _CRED_TYPE_TO_PREFIX.get(prefix, prefix)
    return f"{canonical}:{rest}"


# ============================================================
# Schema
# ============================================================

async def init_credential_tables() -> None:
    """Create credential_store table."""
    async with open_db() as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS credential_store (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                account TEXT NOT NULL,
                scope TEXT NOT NULL,
                owner TEXT NOT NULL DEFAULT '',
                encrypted_data TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_cred_scope
                ON credential_store(scope);
            CREATE INDEX IF NOT EXISTS idx_cred_owner
                ON credential_store(owner);
            CREATE INDEX IF NOT EXISTS idx_cred_type
                ON credential_store(type);
        """)
        await db.commit()
    log.info("Credential store tables initialized")


# ============================================================
# CRUD
# ============================================================

async def store_credential(
    cred_id: str,
    cred_type: str,
    account: str,
    scope: str,
    owner: str = "",
    data: dict | None = None,
    metadata: dict | None = None,
) -> bool:
    """Store or update a credential.

    Args:
        cred_id: Unique credential label (e.g. "hr-inbox")
        cred_type: Type (google-workspace, ms365, github, api-key, custom)
        account: Account identifier (email, username)
        scope: system | harness | user
        owner: Harness label (scope=harness) or AAD ID (scope=user)
        data: Sensitive data dict (tokens, keys) — encrypted at rest
        metadata: Non-sensitive metadata (scopes, description, expiry)
    """
    if cred_type not in CREDENTIAL_TYPES:
        raise ValueError(f"Invalid credential type: {cred_type}. Valid: {sorted(CREDENTIAL_TYPES)}")
    if scope not in CREDENTIAL_SCOPES:
        raise ValueError(f"Invalid scope: {scope}. Valid: {sorted(CREDENTIAL_SCOPES)}")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    encrypted = _encrypt(json.dumps(data or {})) if data else ""
    meta_json = json.dumps(metadata or {})

    async with open_db() as db:
        await db.execute("""
            INSERT INTO credential_store (id, type, account, scope, owner,
                                          encrypted_data, metadata, status,
                                          created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                type=excluded.type, account=excluded.account,
                scope=excluded.scope, owner=excluded.owner,
                encrypted_data=CASE WHEN excluded.encrypted_data != '' THEN excluded.encrypted_data ELSE credential_store.encrypted_data END,
                metadata=excluded.metadata, status='active',
                updated_at=excluded.updated_at
        """, (cred_id, cred_type, account, scope, owner,
              encrypted, meta_json, now, now))
        await db.commit()

    log.info("Credential stored: %s (type=%s, scope=%s, owner=%s)",
             cred_id, cred_type, scope, owner[:20] if owner else "-")
    return True


async def get_credential(cred_id: str, decrypt: bool = False) -> dict | None:
    """Get a credential by ID.

    Args:
        cred_id: Credential ID
        decrypt: If True, include decrypted sensitive data (use carefully)

    Returns: Credential dict or None if not found/inactive.
    """
    async with open_db() as db:
        cursor = await db.execute(
            "SELECT * FROM credential_store WHERE id = ? AND status = 'active'",
            (cred_id,),
        )
        row = await cursor.fetchone()

    if not row:
        return None

    result = dict(row)
    if decrypt and result.get("encrypted_data"):
        try:
            result["data"] = json.loads(_decrypt(result["encrypted_data"]))
        except Exception as e:
            log.error("Failed to decrypt credential %s: %s", cred_id, e)
            result["data"] = None
    # Never expose encrypted_data in API responses
    result.pop("encrypted_data", None)
    return result


async def list_credentials(
    scope: str = "",
    owner: str = "",
    cred_type: str = "",
) -> list[dict]:
    """List credentials with optional filters.

    Args:
        scope: Filter by scope (system/harness/user)
        owner: Filter by owner (harness label or user ID)
        cred_type: Filter by type
    """
    filters = ["status = 'active'"]
    params: list[str] = []

    if scope:
        filters.append("scope = ?")
        params.append(scope)
    if owner:
        filters.append("owner = ?")
        params.append(owner)
    if cred_type:
        filters.append("type = ?")
        params.append(cred_type)

    where = " AND ".join(filters)

    async with open_db() as db:
        cursor = await db.execute(
            f"SELECT id, type, account, scope, owner, metadata, status, created_at, updated_at "
            f"FROM credential_store WHERE {where} ORDER BY scope, id",
            params,
        )
        rows = await cursor.fetchall()

    results = []
    for r in rows:
        entry = dict(r)
        # Strip internal file paths from metadata (defense-in-depth)
        if entry.get("metadata"):
            import json as _json
            try:
                md = _json.loads(entry["metadata"]) if isinstance(entry["metadata"], str) else entry["metadata"]
                md.pop("file_path", None)
                entry["metadata"] = _json.dumps(md)
            except (ValueError, TypeError):
                pass
        results.append(entry)
    return results


async def delete_credential(cred_id: str) -> bool:
    """Delete a credential."""
    async with open_db() as db:
        await db.execute(
            "DELETE FROM credential_store WHERE id = ?", (cred_id,)
        )
        await db.commit()
    log.info("Credential deleted: %s", cred_id)
    return True


async def revoke_credential(cred_id: str) -> bool:
    """Revoke a credential (soft delete — keeps record for audit)."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    async with open_db() as db:
        await db.execute(
            "UPDATE credential_store SET status='revoked', updated_at=? WHERE id=?",
            (now, cred_id),
        )
        await db.commit()
    log.info("Credential revoked: %s", cred_id)
    return True


# ============================================================
# Access Checking
# ============================================================

async def get_credential_checked(
    cred_id: str,
    allowed_credentials: list[str],
    user_id: str = "",
    is_admin: bool = False,
    decrypt: bool = False,
) -> dict | None:
    """Get a credential ONLY if access is permitted. The safe public API.

    Composes access check + retrieval in one call. Prevents callers from
    accidentally decrypting without checking access first.
    """
    if not check_credential_access(cred_id, allowed_credentials, user_id, is_admin):
        return None
    cred = await get_credential(cred_id, decrypt=decrypt)
    if cred and cred.get("scope") == "system" and not is_admin:
        log.warning("Non-admin attempted to access system-scope credential: %s", cred_id)
        return None  # system-scope guard (defense-in-depth)
    return cred


def check_credential_access(
    cred_id: str,
    allowed_credentials: list[str],
    user_id: str = "",
    is_admin: bool = False,
) -> bool:
    """Check if a credential is accessible in the current context.

    Args:
        cred_id: Credential being accessed
        allowed_credentials: Harness allowed_credentials list.
            ["*"] = unrestricted access (admin/coding harnesses only).
            [] = empty list → deny all.
            ["google:user@..."] = only listed credentials allowed.
            NOTE: hooks.py Layer 1b3 checks wildcard before calling this.
        user_id: Current user's AAD ID (for user-scope credential check)
        is_admin: Whether user is Bot Admin

    Returns: True if access is permitted.
    """
    if is_admin:
        return True  # admins access everything

    from bot.harness import is_credential_unrestricted
    if is_credential_unrestricted(allowed_credentials):
        return True  # wildcard grants full access

    if not allowed_credentials:
        return False  # empty list = deny all (fail-closed)

    # Check if credential ID is in the allowed list (normalize both sides)
    _normalized = normalize_cred_id(cred_id)
    if _normalized in allowed_credentials or cred_id in allowed_credentials:
        return True
    # Also normalize the allowlist entries for comparison
    if any(normalize_cred_id(c) == _normalized for c in allowed_credentials):
        return True

    # User-scope credentials: accessible if owner matches
    if cred_id.startswith("user:") and user_id and cred_id == f"user:{user_id}":
        return True

    return False


# ============================================================
# Migration helper
# ============================================================

async def migrate_from_allowed_emails(harness_config: dict) -> list[str]:
    """Convert old allowed_emails to credential IDs.

    For backward compat during migration from the old model.
    Creates credential entries for each email and returns IDs.
    """
    emails = harness_config.get("allowed_emails", [])
    if not emails:
        return []

    cred_ids = []
    for email in emails:
        # Derive credential ID from full email (prevent collisions across domains)
        cred_id = email.replace("@", "-at-").replace(".", "-")
        await store_credential(
            cred_id=cred_id,
            cred_type="google-workspace",
            account=email,
            scope="harness",
            owner=harness_config.get("label", ""),
        )
        cred_ids.append(cred_id)
        log.info("Migrated allowed_email %s → credential %s", email, cred_id)

    return cred_ids


# ============================================================
# Metadata-only registration (hybrid approach — files remain
# source of truth, DB holds RBAC metadata + audit trail)
# ============================================================

async def register_credential_metadata(
    cred_id: str,
    cred_type: str,
    account: str,
    scope: str,
    owner: str = "",
    file_path: str = "",
    extra_metadata: dict | None = None,
) -> bool:
    """Register a credential's metadata for RBAC and audit purposes.

    Unlike store_credential(), this does NOT store the actual secret tokens.
    The tokens remain in their native file format (for MCP server consumption).
    The DB tracks: who owns it, what type, where the file is, last refresh, etc.

    Args:
        cred_id: Unique ID (e.g. "google:admin@company.com", "m365:admin")
        cred_type: google-workspace, ms365, claude-sdk, etc.
        account: Account identifier (email, profile label)
        scope: system | harness | user
        owner: Harness label or user session key (e.g. "telegram:123456789")
        file_path: Path to the native credential file
        extra_metadata: Additional non-sensitive metadata
    """
    metadata = {
        "file_path": file_path,
        **(extra_metadata or {}),
    }
    # store_credential with empty data = metadata-only (encrypted_data stays empty)
    return await store_credential(
        cred_id=cred_id,
        cred_type=cred_type,
        account=account,
        scope=scope,
        owner=owner,
        data=None,  # no encrypted blob — file is source of truth
        metadata=metadata,
    )


async def get_credentials_for_account(
    account: str,
    cred_type: str = "",
) -> list[dict]:
    """Find credentials by account identifier (email, profile label).

    Useful for: "does the user admin@company.com have a registered credential?"
    """
    filters = ["status = 'active'", "account = ?"]
    params: list[str] = [account]
    if cred_type:
        filters.append("type = ?")
        params.append(cred_type)

    async with open_db() as db:
        cursor = await db.execute(
            f"SELECT id, type, account, scope, owner, metadata, status, "
            f"created_at, updated_at FROM credential_store "
            f"WHERE {' AND '.join(filters)} ORDER BY created_at DESC",
            params,
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def update_credential_status(
    cred_id: str,
    status: str = "active",
    error: str = "",
) -> bool:
    """Update a credential's status (active, needs_reauth, error)."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    metadata_update = {}
    if error:
        metadata_update["last_error"] = error
    metadata_update["last_status_change"] = now

    async with open_db() as db:
        # Read existing metadata to merge
        cursor = await db.execute(
            "SELECT metadata FROM credential_store WHERE id = ?", (cred_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return False
        existing_meta = json.loads(row["metadata"] or "{}")
        existing_meta.update(metadata_update)

        await db.execute(
            "UPDATE credential_store SET status=?, metadata=?, updated_at=? WHERE id=?",
            (status, json.dumps(existing_meta), now, cred_id),
        )
        await db.commit()
    log.info("Credential %s status → %s%s", cred_id, status,
             f" error={error[:60]}" if error else "")
    return True
