# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Seed built-in RBAC roles and baseline policies.

Three built-in roles:
- bot_admin: full access to everything (wildcard privileges)
- employee: own chat data + messaging + memory + filesystem + web tools, sandbox-enforced by hooks Layers 2/3/3b
- (system_agent removed — tasks run in admin DM context, agents in chat context)

Call seed_builtin_roles() at startup — idempotent (skips existing roles).
Also seeds canonical domains and assigns domain roles to chats (CB-79/91/95).
Legacy: migrate_harness_to_rbac() removed in CB-105 (replaced by domain system).
Previously ran harness security
fields into RBAC roles and policies (Sprint R3).
"""

import logging

from bot.rbac.store import add_policy, assign_role, create_role, get_role, remove_policy

log = logging.getLogger(__name__)


async def _migration_done(flag_key: str) -> bool:
    """Check if a one-time migration has already completed."""
    try:
        from bot.storage.db import open_db
        async with open_db() as db:
            row = await db.execute_fetchall(
                "SELECT 1 FROM message_facts WHERE fact_key = ? AND expired = 0 LIMIT 1",
                (flag_key,),
            )
            return bool(row)
    except Exception as e:
        log.debug("Migration flag check failed for %s: %s", flag_key, e)
        return False


async def _set_migration_done(flag_key: str) -> None:
    """Mark a one-time migration as completed."""
    try:
        from bot.storage.db import open_db
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        async with open_db() as db:
            await db.execute(
                "INSERT OR IGNORE INTO message_facts (fact_key, fact_value, category, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (flag_key, "done", "migration", now, now),
            )
            await db.commit()
    except Exception as e:
        log.debug("Failed to set migration flag %s: %s", flag_key, e)


# ---------------------------------------------------------------------------
# Built-in role definitions
# ---------------------------------------------------------------------------

_BUILTIN_ROLES = [
    {
        "role_id": "bot_admin",
        "name": "Bot Admin",
        "description": "Full access to all resources, tools, and administrative functions.",
    },
    {
        "role_id": "employee",
        "name": "Employee",
        "description": "Standard corporate user — messaging, memory, filesystem (sandbox-enforced), web search, agent tools.",
    },
    {
        "role_id": "teamwork",
        "name": "Teamwork",
        "description": "Team group chat role — R/W teamwork domain, shared OAuth, standard tools. All users in chat share this role.",
    },
    {
        "role_id": "external",
        "name": "External",
        "description": "Restricted role for third-party chats — minimal tools, no credentials, no domain writes, no filesystem.",
    },
]

# ---------------------------------------------------------------------------
# Policies per built-in role
# ---------------------------------------------------------------------------

_BUILTIN_POLICIES: dict[str, list[dict]] = {
    # bot_admin: wildcard on everything
    "bot_admin": [
        {"privilege": "*", "object_type": "*", "object_id": "*", "effect": "allow"},
    ],
}

# ---------------------------------------------------------------------------
# CB-5: DRY policy composition — shared base lists, roles extend them.
# Employee and teamwork share 90% of policies. Instead of copy-paste,
# compose each role from reusable policy sets.
# ---------------------------------------------------------------------------

# Common tool access: SDK + messaging + filesystem + web + memory + Google/M365 + PM + scheduler
_COMMON_TOOL_ALLOWS: list[dict] = [
    # SDK builtins
    {"privilege": "use", "object_type": "tools", "object_id": "ToolSearch", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "Skill", "effect": "allow"},
    # Chat data
    {"privilege": "read", "object_type": "chat_data", "object_id": "*", "effect": "allow"},
    {"privilege": "write", "object_type": "chat_data", "object_id": "*", "effect": "allow"},
    # Messaging
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_send_message", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "teams_send_message", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "teams_edit_message", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "teams_delete_message", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "teams_send_document", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_send_photo", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_send_document", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_send_video", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_send_audio", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_send_voice", "effect": "allow"},
    # Filesystem (sandbox-enforced by hooks Layers 2/3/3b)
    {"privilege": "use", "object_type": "tools", "object_id": "Read", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "Write", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "Edit", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "Grep", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "Glob", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "Bash", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "NotebookEdit", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "Agent", "effect": "allow"},
    # Web
    {"privilege": "use", "object_type": "tools", "object_id": "WebSearch", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "WebFetch", "effect": "allow"},
    # Memory
    {"privilege": "use", "object_type": "tools", "object_id": "memory_search", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "manage_fact", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "search_facts", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "get_recent_conversation", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "search_media", "effect": "allow"},
    # Google Workspace (credential gate in hooks Layer 1b3)
    {"privilege": "use", "object_type": "tools", "object_id": "get_events", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "manage_event", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "list_calendars", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "query_freebusy", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "search_gmail_messages", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "get_gmail_message_content", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "get_gmail_thread_content", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "get_gmail_attachment_content", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "draft_gmail_message", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "send_gmail_message", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "list_gmail_labels", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "download_gmail_attachment", "effect": "allow"},
    # Microsoft 365 (Softeria MCP)
    {"privilege": "use", "object_type": "tools", "object_id": "list-calendar-events", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "get-calendar-event", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "get-calendar-view", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "create-calendar-event", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "list-calendars", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "list-mail-messages", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "list-mail-folders", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "list-mail-folder-messages", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "get-mail-message", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "get-mail-attachment", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "send-mail", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "create-draft-email", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "move-mail-message", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "delete-mail-message", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "create-mail-folder", "effect": "allow"},
    # PM tools
    {"privilege": "use", "object_type": "tools", "object_id": "pm_auto_ticket", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "pm_create_ticket", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "pm_list_tickets", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "pm_move_ticket", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "pm_update_ticket", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "pm_list_projects", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "pm_board", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "pm_summary", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "pm_create_project", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "pm_create_sprint", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "pm_transition_sprint", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "pm_get_tcd", "effect": "allow"},
    # Scheduler
    {"privilege": "use", "object_type": "tools", "object_id": "list_scheduled_tasks", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "manage_scheduled_task", "effect": "allow"},
    # Report hosting + knowledge search
    {"privilege": "use", "object_type": "tools", "object_id": "save_report", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "search_knowledge", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "search_artifacts", "effect": "allow"},
    # Domain content tools — internal per-domain auth enforces access per-action:
    # admin actions (register/delete) require bot_admin, write actions (sync/index) require domain write.
    {"privilege": "use", "object_type": "tools", "object_id": "manage_domain", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "index_document", "effect": "allow"},
    # Session management
    {"privilege": "use", "object_type": "tools", "object_id": "switch_model", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "switch_oauth", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "reset_session", "effect": "allow"},
    # CB-5 Finding 2: Missing tool policies — image, phone, memory context, TG management
    {"privilege": "use", "object_type": "tools", "object_id": "generate_image", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "edit_image", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "phone_call", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "phone_get_transcript", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "get_document", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "get_message_context", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "get_messages_by_range", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "switch_effort", "effect": "allow"},
    # Telegram management tools (non-destructive)
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_edit_message", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_delete_message", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_send_location", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_forward_message", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_pin_message", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_unpin_message", "effect": "allow"},
    # Google Flights
    {"privilege": "use", "object_type": "tools", "object_id": "google_flights_search", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "google_flights_search_dates", "effect": "allow"},
]

# Common deny list: admin-only tools (deny-wins over any allow)
_COMMON_ADMIN_DENIES: list[dict] = [
    {"privilege": "use", "object_type": "tools", "object_id": "deploy_bot", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "deploy_staging", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "deploy_review", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "pipeline_orchestrate", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "staging_qa_run", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "manage_harness", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "manage_rbac", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "manage_chat", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "manage_skill", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "manage_oauth", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "extend_budget", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "deploy_status", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "deploy_staging_status", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "staging_command", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "usage_report", "effect": "deny"},
    # PM orchestration — admin only
    {"privilege": "use", "object_type": "tools", "object_id": "pm_dispatch_agent", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "pm_run_sprint", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "pm_run_ticket", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "pm_budget_report", "effect": "deny"},
    # CB-5 Finding 2: Admin-only tools (infrastructure management)
    # NOTE: manage_domain + index_document removed from deny — they have internal
    # per-domain auth checks (admin for structural changes, write for content ops).
    {"privilege": "use", "object_type": "tools", "object_id": "manage_external_chat", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "rag_backfill", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "rag_stats", "effect": "deny"},
    # Telegram admin tools (chat/group management)
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_get_chat_members", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_get_chat_history", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_set_chat_photo", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_set_chat_title", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_create_group", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_leave_chat", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_create_topic", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_close_topic", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_reopen_topic", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_delete_topic", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_set_profile_photo", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_update_profile", "effect": "deny"},
]

# Common execution scope: sandbox filesystem + sandboxed network
_COMMON_EXECUTION_SCOPE: list[dict] = [
    {"privilege": "execute", "object_type": "filesystem", "object_id": "sandbox", "effect": "allow"},
    {"privilege": "execute", "object_type": "network", "object_id": "sandboxed", "effect": "allow"},
]

# Common commands for all non-external roles
_COMMON_COMMANDS: list[dict] = [
    {"privilege": "use", "object_type": "commands", "object_id": "status", "effect": "allow"},
    {"privilege": "use", "object_type": "commands", "object_id": "help", "effect": "allow"},
    {"privilege": "use", "object_type": "commands", "object_id": "cancel", "effect": "allow"},
    {"privilege": "use", "object_type": "commands", "object_id": "continue_cmd", "effect": "allow"},
    {"privilege": "use", "object_type": "commands", "object_id": "knowledge", "effect": "allow"},
    {"privilege": "use", "object_type": "commands", "object_id": "reset", "effect": "allow"},
    # CB-139: Visibility commands — show filtered content per role (admin sees all)
    {"privilege": "use", "object_type": "commands", "object_id": "chats", "effect": "allow"},
    {"privilege": "use", "object_type": "commands", "object_id": "harnesses", "effect": "allow"},
    {"privilege": "use", "object_type": "commands", "object_id": "permissions", "effect": "allow"},
]

# Compose employee = common tools + common denies + execution scope + employee-specific
_BUILTIN_POLICIES["employee"] = (
    _COMMON_TOOL_ALLOWS
    + _COMMON_ADMIN_DENIES
    + _COMMON_EXECUTION_SCOPE
    + _COMMON_COMMANDS
    + [
        # Employee-specific: knowledge domain (read-only teamwork)
        {"privilege": "read", "object_type": "knowledge_domain", "object_id": "teamwork", "effect": "allow"},
        # Employee-specific commands (DM-only: auth, accounts, diagnose)
        {"privilege": "use", "object_type": "commands", "object_id": "drop_call", "effect": "allow"},
        {"privilege": "use", "object_type": "commands", "object_id": "onboarding", "effect": "allow"},
        {"privilege": "use", "object_type": "commands", "object_id": "diagnose", "effect": "allow"},
        {"privilege": "use", "object_type": "commands", "object_id": "claude_auth", "effect": "allow"},
        {"privilege": "use", "object_type": "commands", "object_id": "google_auth", "effect": "allow"},
        {"privilege": "use", "object_type": "commands", "object_id": "m365_auth", "effect": "allow"},
        {"privilege": "use", "object_type": "commands", "object_id": "accounts", "effect": "allow"},
        {"privilege": "use", "object_type": "commands", "object_id": "claude_accounts", "effect": "allow"},
        {"privilege": "use", "object_type": "commands", "object_id": "claude_set_oauth", "effect": "allow"},
        {"privilege": "use", "object_type": "commands", "object_id": "revoke", "effect": "allow"},
        {"privilege": "use", "object_type": "commands", "object_id": "github_auth", "effect": "allow"},
        # CB-140: Per-employee workstation setup (personal Mac registration)
        {"privilege": "use", "object_type": "commands", "object_id": "mac_setup", "effect": "allow"},
    ]
)

# Compose teamwork = common tools + common denies + execution scope + teamwork-specific
_BUILTIN_POLICIES["teamwork"] = (
    _COMMON_TOOL_ALLOWS
    + _COMMON_ADMIN_DENIES
    + _COMMON_EXECUTION_SCOPE
    + _COMMON_COMMANDS
    + [
        # Teamwork-specific: R/W on teamwork knowledge domain
        {"privilege": "read", "object_type": "knowledge_domain", "object_id": "teamwork", "effect": "allow"},
        {"privilege": "write", "object_type": "domain_data", "object_id": "teamwork", "effect": "allow"},
    ]
)

# CB-5: External role — restricted for third-party conversations.
# No credentials, no domains, no filesystem, no PM, no auth commands.
# Only messaging, memory (own chat), web search.
_BUILTIN_POLICIES["external"] = [
    # SDK builtins
    {"privilege": "use", "object_type": "tools", "object_id": "ToolSearch", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "Skill", "effect": "allow"},
    # Read/write own chat data
    {"privilege": "read", "object_type": "chat_data", "object_id": "*", "effect": "allow"},
    {"privilege": "write", "object_type": "chat_data", "object_id": "*", "effect": "allow"},
    # Messaging — send to own chat only
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_send_message", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "teams_send_message", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_send_photo", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_send_document", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "telegram_send_voice", "effect": "allow"},
    # Memory — own chat context only (no domain writes)
    {"privilege": "use", "object_type": "tools", "object_id": "memory_search", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "search_facts", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "get_recent_conversation", "effect": "allow"},
    # Web research
    {"privilege": "use", "object_type": "tools", "object_id": "WebSearch", "effect": "allow"},
    {"privilege": "use", "object_type": "tools", "object_id": "WebFetch", "effect": "allow"},
    # Knowledge search (read-only, no domain writes)
    {"privilege": "use", "object_type": "tools", "object_id": "search_knowledge", "effect": "allow"},
    # Report hosting
    {"privilege": "use", "object_type": "tools", "object_id": "save_report", "effect": "allow"},
    # Deny ALL sensitive tools explicitly (belt + suspenders with default-deny)
] + _COMMON_ADMIN_DENIES + [
    # Domain tools explicitly denied for external (removed from _COMMON_ADMIN_DENIES)
    {"privilege": "use", "object_type": "tools", "object_id": "manage_domain", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "index_document", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "switch_model", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "switch_oauth", "effect": "deny"},
    # No filesystem access
    {"privilege": "use", "object_type": "tools", "object_id": "Read", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "Write", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "Edit", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "Bash", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "Glob", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "Grep", "effect": "deny"},
    # No Google/M365 access
    {"privilege": "use", "object_type": "tools", "object_id": "get_events", "effect": "deny"},
    {"privilege": "use", "object_type": "tools", "object_id": "search_gmail_messages", "effect": "deny"},
    # No PM access
    {"privilege": "use", "object_type": "tools", "object_id": "pm_auto_ticket", "effect": "deny"},
    # No agent spawning
    {"privilege": "use", "object_type": "tools", "object_id": "Agent", "effect": "deny"},
    # Minimal commands
    {"privilege": "use", "object_type": "commands", "object_id": "status", "effect": "allow"},
    {"privilege": "use", "object_type": "commands", "object_id": "help", "effect": "allow"},
    {"privilege": "use", "object_type": "commands", "object_id": "cancel", "effect": "allow"},
]

# Domain ownership is a relationship (DomainConfig.owner field), not a separate role.


async def seed_builtin_roles() -> None:
    """Create built-in roles and baseline policies on startup.

    Idempotent — existing roles are checked for MISSING and STALE policies.
    New policies are added; policies no longer in _BUILTIN_POLICIES are removed
    (prevents stale deny-wins from blocking new allows). This handles seed
    updates between deployments without requiring a DB wipe.
    NOTE: builtin roles are fully managed by seed — custom policies added
    manually to builtin roles will be removed on restart.

    Also runs harness-to-RBAC migration (Sprint R3) after seeding builtins.
    """
    from bot.rbac.store import get_policies_for_role

    for role_def in _BUILTIN_ROLES:
        role_id = role_def["role_id"]
        existing = await get_role(role_id)

        if existing is None:
            await create_role(
                role_id=role_id,
                name=role_def["name"],
                description=role_def["description"],
                is_builtin=True,
            )
            # Add all baseline policies for new role
            policies = _BUILTIN_POLICIES.get(role_id, [])
            for p in policies:
                await add_policy(
                    role_id=role_id,
                    privilege=p["privilege"],
                    object_type=p["object_type"],
                    object_id=p["object_id"],
                    effect=p["effect"],
                    conditions=p.get("conditions"),
                )
            log.info(f"RBAC builtin role seeded: {role_id} ({len(policies)} policies)")
        else:
            # Role exists — check for missing policies (handles seed updates).
            # This ensures new policies added to _BUILTIN_POLICIES are applied
            # to existing roles without requiring a DB wipe.
            try:
                existing_policies = await get_policies_for_role(role_id)
                # Policy is a dataclass — use attribute access, not dict subscript
                existing_set = {
                    (p.privilege, p.object_type, p.object_id, p.effect)
                    for p in existing_policies
                }
                # Sanity check: if role exists but has 0 policies, skip migration
                # to avoid re-adding all policies on every boot due to transient DB issue
                desired = _BUILTIN_POLICIES.get(role_id, [])
                if not existing_policies and desired:
                    log.warning(f"RBAC role '{role_id}' exists but has 0 policies — skipping migration")
                    continue
                added = 0
                for p in desired:
                    key = (p["privilege"], p["object_type"], p["object_id"], p["effect"])
                    if key not in existing_set:
                        await add_policy(
                            role_id=role_id,
                            privilege=p["privilege"],
                            object_type=p["object_type"],
                            object_id=p["object_id"],
                            effect=p["effect"],
                            conditions=p.get("conditions"),
                        )
                        added += 1
                if added:
                    log.info(f"RBAC builtin role '{role_id}': {added} missing policies added")
                else:
                    log.debug(f"RBAC builtin role already exists: {role_id} (policies up to date)")

                # Remove stale policies no longer in desired set (deny-wins can block new allows)
                desired_set = {
                    (p["privilege"], p["object_type"], p["object_id"], p["effect"])
                    for p in desired
                }
                removed = 0
                for ep in existing_policies:
                    key = (ep.privilege, ep.object_type, ep.object_id, ep.effect)
                    if key not in desired_set:
                        await remove_policy(ep.policy_id)
                        removed += 1
                        log.info("RBAC removed stale policy from '%s': %s", role_id, key)
                if removed:
                    log.info("RBAC builtin role '%s': %d stale policies removed", role_id, removed)
            except Exception as e:
                log.warning(f"RBAC policy migration failed for '{role_id}' (non-blocking): {e}")

    # Auto-assign bot_admin to ADMIN_USERS on fresh deployment
    try:
        from config import ADMIN_USERS
        from bot.rbac.store import get_assignments_for_subject

        # Cleanup: remove stale tuple-format subject_ids from early seed bug
        # (e.g. "('telegram', '123456789')" instead of "telegram:123456789")
        from bot.storage.db import open_db
        async with open_db() as db:
            cursor = await db.execute(
                "DELETE FROM rbac_assignments WHERE subject_id LIKE '(%'"
            )
            if cursor.rowcount:
                await db.commit()
                log.info(f"RBAC: cleaned {cursor.rowcount} stale tuple-format assignments")

        for source, uid in ADMIN_USERS:
            subject_id = f"{source}:{uid}"
            existing_roles = await get_assignments_for_subject("user", subject_id)
            if not existing_roles:
                await assign_role(
                    subject_type="user",
                    subject_id=subject_id,
                    role_id="bot_admin",
                    granted_by="system:seed",
                )
                log.info(f"RBAC: auto-assigned bot_admin to {subject_id} (first boot)")
    except Exception as e:
        log.warning(f"RBAC admin auto-assignment failed (non-blocking): {e}")

    # system_agent role removed (CB-29) — scheduled tasks run in admin DM context,
    # agent runs in spawning chat's SDK session. No separate system identity needed.

    # CB-79: LEGACY — one-time group chat role assignment (guarded).
    # Replaced by CB-5 harness-driven role assignment below.
    # Kept for backward compat: assigns teamwork to any group chat that still
    # has NO role at all (safety net for pre-CB-5 chats).
    if not await _migration_done("migration:cb79_group_assignment"):
        try:
            from bot.chat_registry import all_entries as _all_chat_entries
            from bot.rbac.store import get_assignments_for_subject
            from bot.core.access import is_private_chat as _is_priv

            _chat_entries = _all_chat_entries()
            _assigned_count = 0
            for chat_key, _entry in _chat_entries.items():
                parts = chat_key.split(":")
                if len(parts) < 2:
                    continue
                _cid = ":".join(parts[1:]) if len(parts) >= 2 else ""
                _base_cid = parts[1] if len(parts) >= 2 else ""
                if _is_priv(_base_cid):
                    continue  # DMs use user subject, not chat subject
                if _entry.get("is_external") or _entry.get("mode") == "silent":
                    continue

                existing = await get_assignments_for_subject("chat", _cid)
                if existing:
                    continue  # Already has role — don't fight harness-driven assignment

                # Assign teamwork role to unassigned non-admin group chats (safety net)
                await assign_role(
                    subject_type="chat",
                    subject_id=_cid,
                    role_id="teamwork",
                    granted_by="system:cb79_migration",
                )
                _assigned_count += 1

            if _assigned_count:
                log.info(f"RBAC CB-79: assigned teamwork role to {_assigned_count} unassigned group chat(s)")
            await _set_migration_done("migration:cb79_group_assignment")
        except Exception as e:
            log.warning(f"RBAC chat role migration failed (non-blocking): {e}")

    # CB-95: DEPRECATED — domain config now flows from harness (CB-5).
    # No longer writes write_domain/reference_domains to chat_registry.
    # Harness is the single source of truth for domain config.

    # Sprint R3 harness→RBAC migration removed (CB-105).
    # Canonical domains (CB-91) + flat permission model (CB-79) replaced it.
    # harness_* RBAC roles cleaned up at startup below.
    if not await _migration_done("migration:cb105_stale_roles"):
        try:
            from bot.rbac.store import delete_role
            from bot.storage.db import open_db
            for _stale_role_id in ["harness_admin", "harness_external_chat", "harness_coding", "harness_teamwork", "domain_owner", "guest"]:
                if await get_role(_stale_role_id):
                    try:
                        # Clear is_builtin flag first (Finding 3: old seeds may have set it)
                        async with open_db() as _sdb:
                            await _sdb.execute(
                                "UPDATE rbac_roles SET is_builtin = 0 WHERE role_id = ?",
                                (_stale_role_id,),
                            )
                            await _sdb.commit()
                        await delete_role(_stale_role_id)
                        log.info(f"CB-105: cleaned up stale RBAC role: {_stale_role_id}")
                    except Exception as _del_e:
                        log.warning(f"CB-105: failed to delete stale role '{_stale_role_id}': {_del_e}")
            await _set_migration_done("migration:cb105_stale_roles")
        except Exception as e:
            log.warning(f"CB-105 stale role cleanup failed: {e}")


# ---------------------------------------------------------------------------
# Legacy harness→RBAC migration (Sprint R3) removed in CB-105.
# Replaced by canonical domains (CB-91) + flat permission model (CB-79).
# Code preserved in git history (commit before CB-105).
