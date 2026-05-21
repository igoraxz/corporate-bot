from __future__ import annotations

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from bot.rbac.models import Subject  # noqa: F401

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Data access isolation via AccessContext.

Flat permission model (CB-79):
- DM: subject = user. User owns their personal workspace.
- Group/topic: subject = chat. All users share the chat's permissions.
  Admin grants roles/creds/domains to chats, not to users-within-chats.

Canonical roles:
- bot_admin: full access (admin DMs + explicitly configured chats)
- employee: DM personal workspace, read-only teamwork domain
- teamwork: team group chats, R/W teamwork domain + shared OAuth

Data isolation:
- Admin DM (is_admin=True, empty allowed_chat_ids): global scope
- Admin group (is_admin=True, scoped allowed_chat_ids): chat-scoped
- Teamwork/employee: own chat data + cross-chat knowledge via domains

Knowledge domain isolation:
- allowed_domains controls which knowledge domains are searchable
- () = no domain access (legacy/shared mode)
- ("eng-runbooks", "shared") = only these domains
- Admin DM with empty allowed_domains = all domains
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AccessContext:
    """Determines what data a session can access.

    Visibility defined by chat's AccessContext, not admin role:
    - Admin DM (is_admin=True, empty allowed_chat_ids): global scope, no filter
    - Admin group (is_admin=True, allowed_chat_ids=(chat_id,)): chat-scoped data
    - Employee (is_admin=False, allowed_chat_ids=(own_chat,)): own chat only
    - External/Guest (is_admin=False, allowed_chat_ids=(own_chat,)): own chat only

    is_admin=True grants RBAC tool access (deploy, manage_chat, etc.)
    but does NOT bypass data visibility unless in a private DM.

    Employees never see other employees' chat data. Cross-chat knowledge
    sharing is handled by knowledge domains (allowed_domains), not by
    granting access to other chats' message history.
    """
    chat_id: str = ""                           # this session's chat
    is_admin: bool = False                      # True ONLY for admin-scope sessions
    allowed_chat_ids: tuple[str, ...] = ()      # () for admin = no filter; (own_chat,) for employee/external
    allowed_domains: tuple[str, ...] = ()       # knowledge domains this session can query

    @property
    def has_filter(self) -> bool:
        """True if data should be filtered.

        Admin DM (is_admin=True, empty allowed_chat_ids) → no filter.
        Admin group (is_admin=True, non-empty allowed_chat_ids) → filter.
        Non-admin → always filter.
        """
        return not self.is_admin or bool(self.allowed_chat_ids)


NO_ACCESS = AccessContext()


def build_access_context(
    chat_id: str,
    is_admin: bool,
    is_external_chat: bool = False,
    is_private_chat: bool = False,
) -> AccessContext:
    """Build AccessContext based on RBAC role + chat type.

    Data isolation rule: ONLY admin DMs have global scope.
    All other chats (including admin group chats, topics) are chat-scoped.

    - Admin in private DM: full access to all data (global scope)
    - Admin in group/topic: RBAC admin tools, but chat-scoped data
    - Non-admin: own chat data only (knowledge comes from domains)

    is_private_chat: True for private DM chats (TG chat_id > 0 or Teams 1:1).
    When False, admin sessions get chat-scoped data like employees.
    This prevents admin group chats from leaking data across chats.
    """
    if is_admin and is_private_chat and not is_external_chat:
        # Admin DM: full access to all data (global scope)
        return AccessContext(
            chat_id=chat_id,
            is_admin=True,
            allowed_chat_ids=(),
        )

    if is_admin and not is_private_chat and not is_external_chat:
        # Admin in group/topic: RBAC admin tools but chat-scoped DATA.
        # is_admin stays True for tool access (deploy, manage_chat, etc.)
        # but allowed_chat_ids restricts fact/message visibility.
        # SECURITY: empty chat_id in group context → deny data access
        # rather than accidentally granting global scope.
        return AccessContext(
            chat_id=chat_id,
            is_admin=True,
            allowed_chat_ids=(chat_id,) if chat_id else ("__none__",),
        )

    # Non-admin (or external chat): own chat data only.
    # Cross-chat knowledge comes from knowledge domains, not shared chat history.
    return AccessContext(
        chat_id=chat_id,
        is_admin=False,
        allowed_chat_ids=(chat_id,) if chat_id else (),
    )


def is_private_chat(chat_id: str, source: str = "", metadata: dict | None = None) -> bool:
    """Determine if a chat_id represents a private DM.

    Telegram: positive integer = DM, negative = group/supergroup.
    Teams: check metadata['conversation_type'] if available, else heuristic.
    Returns False when unsure (fail-closed — treats as group).
    """
    if not chat_id:
        return False
    # Check metadata first (Teams passes conversation_type)
    if metadata and metadata.get("conversation_type") == "personal":
        return True
    # Telegram: positive int = DM
    try:
        return int(chat_id) > 0
    except (ValueError, TypeError):
        pass
    # Teams heuristic: conversation IDs starting with certain prefixes indicate DMs
    # But fail-closed is safer — if we can't determine, treat as group
    if source == "teams" and metadata and metadata.get("conversation_type"):
        return metadata["conversation_type"] == "personal"
    return False  # fail-closed: non-integer without metadata = not private DM


def build_rbac_subject(
    source: str,
    user_id: str,
    chat_id: str,
) -> "Subject":
    """Build RBAC Subject with the correct identity for DM vs group.

    Flat permission model:
    - DM: subject = user (user_id). User owns their DM workspace.
    - Group/topic: subject = chat (chat_id). All users in the chat
      share the same permissions, domains, tools, and creds.

    This ensures all users in a group chat get identical RBAC treatment.
    Admin grants roles to chats, not to users-within-chats.

    user_id: pre-formatted RBAC user_id (e.g. "telegram:123456789").
    chat_id: raw chat_id (e.g. "-100XXXXXXXXXX" or "123456789").
    """
    from bot.rbac.models import Subject

    _is_dm = is_private_chat(chat_id)

    if _is_dm:
        # DM: user is the subject — personal workspace
        return Subject(user_id=user_id, source=source)
    else:
        # Group/topic: chat is the subject — shared workspace
        return Subject(chat_id=chat_id, source=source)


_ALLOWED_CHAT_ID_COLUMNS = frozenset({
    "chat_id", "source_chat_id",
    "m.chat_id", "mf.source_chat_id", "mc.chat_id",
})


def _access_sql_filter(
    ctx: AccessContext | None,
    chat_id_column: str,
) -> tuple[str, list[str]]:
    """Build SQL WHERE clause fragment for data access filtering.

    Returns (sql_fragment, params) to be appended with AND to existing WHERE.
    Legacy data (empty chat_id) is visible to admin only.
    Employees and external users see only their own chat data (strict filter).

    Args:
        ctx: AccessContext or None (None = use NO_ACCESS safe default)
        chat_id_column: SQL column name for chat_id (e.g. 'source_chat_id', 'chat_id')
    """
    if chat_id_column not in _ALLOWED_CHAT_ID_COLUMNS:
        raise ValueError(f"Invalid chat_id_column: {chat_id_column!r}")

    if ctx is None:
        ctx = NO_ACCESS

    if ctx.is_admin and not ctx.allowed_chat_ids:
        # Admin DM: no filter (global scope — sees all data)
        return "", []

    if not ctx.allowed_chat_ids:
        # Empty allowed list + not admin = no access
        return "AND 1=0", []

    placeholders = ",".join("?" for _ in ctx.allowed_chat_ids)
    # Corporate model: strict filter for all non-admin users.
    # Employees and external users see ONLY their own chat's data.
    # Legacy data (empty chat_id) belongs to admin scope only.
    return (
        f"AND {chat_id_column} IN ({placeholders})",
        list(ctx.allowed_chat_ids),
    )


_ALLOWED_DOMAIN_ID_COLUMNS = frozenset({"domain_id", "d.domain_id", "rc.domain_id"})


def _domain_sql_filter(
    ctx: AccessContext | None,
    domain_id_column: str = "domain_id",
) -> tuple[str, list[str]]:
    """Build SQL WHERE clause fragment for knowledge domain filtering.

    Returns (sql_fragment, params) to AND with existing WHERE.

    Rules:
    - Admin with empty allowed_domains: no domain filter (all domains)
    - Non-admin with empty allowed_domains: only non-domain data (domain_id='')
    - Non-empty allowed_domains: only matching domains + non-domain data
    """
    if domain_id_column not in _ALLOWED_DOMAIN_ID_COLUMNS:
        raise ValueError(f"Invalid domain_id_column: {domain_id_column!r}")

    if ctx is None:
        ctx = NO_ACCESS

    if ctx.is_admin and not ctx.allowed_domains:
        # Admin with no domain restriction: see everything
        return "", []

    if not ctx.allowed_domains:
        # No domain access: only see non-domain data (legacy messages, personal facts)
        return f"AND ({domain_id_column} = '' OR {domain_id_column} IS NULL)", []

    # Has specific domain access: see those domains + non-domain data
    placeholders = ",".join("?" for _ in ctx.allowed_domains)
    return (
        f"AND ({domain_id_column} IN ({placeholders}) OR {domain_id_column} = '' OR {domain_id_column} IS NULL)",
        list(ctx.allowed_domains),
    )


def _access_filter_rows(
    rows: list[dict],
    ctx: AccessContext | None,
    chat_id_key: str = "chat_id",
) -> list[dict]:
    """Filter a list of dict rows by AccessContext (post-query filtering).

    Used when SQL-level filtering isn't possible (e.g., FTS5 queries).
    Corporate model: non-admin users see only their own chat's data.
    Legacy data (empty chat_id) is admin-only.
    """
    if ctx is None:
        ctx = NO_ACCESS
    if ctx.is_admin and not ctx.allowed_chat_ids:
        # Admin DM (global scope): no filter
        return rows
    if not ctx.allowed_chat_ids:
        return []

    allowed = set(ctx.allowed_chat_ids)
    return [row for row in rows if (row.get(chat_id_key, "") or "") in allowed]
