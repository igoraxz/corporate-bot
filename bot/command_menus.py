# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""RBAC-aware command menu management for Telegram Bot API.

Dynamically derives visible slash commands from RBAC policies:
- use:tools:<tool_name> -> slash command via TOOL_TO_COMMAND mapping
- use:commands:<cmd_id> -> slash command via command_registry.COMMANDS

Unregistered chats: NO commands visible (empty menu).
Registered chats: commands derived from their role policies.

TG limitation: per-chat only (BotCommandScopeChat), NOT per-topic.
All topics in a supergroup share the parent group's command menu.
"""

import logging

log = logging.getLogger(__name__)

# ============================================================
# Tool -> Slash Command Mapping (MCP tools)
# ============================================================
# Maps MCP tool names to their corresponding slash commands.
# Tools not listed here don't have a slash command equivalent.
# None = tool exists but has no user-facing command.

TOOL_TO_COMMAND: dict[str, dict | None] = {
    # Admin / deploy
    "deploy_bot": {"command": "deploy", "description": "Deploy bot (rebuild)"},
    "deploy_status": {"command": "deploy", "description": "Deploy bot (rebuild)"},  # same command
    "deploy_review": None,  # internal gate, not user command
    "staging_qa_run": None,  # internal gate
    "deploy_staging": None,  # internal

    # Session management (MCP tools, not lightweight commands)
    "switch_model": {"command": "switch_model", "description": "Switch AI model"},
    "switch_effort": {"command": "switch_effort", "description": "Switch effort level"},
    "extend_budget": None,  # keyword-gated, not a menu command
    "usage_report": {"command": "usage", "description": "Cost breakdown by timeframe"},

    # Knowledge — no slash command, model invokes search_knowledge MCP tool directly
    "search_knowledge": None,

    # Scheduled tasks
    "manage_scheduled_task": None,  # no slash command

    # Reports
    "save_report": None,  # used by model, not a user command

    # Phone
    "phone_call": None,  # model-invoked
    "phone_get_transcript": None,

    # Image
    "generate_image": None,  # model-invoked
    "edit_image": None,

    # RBAC
    "manage_rbac": None,  # admin uses via model, not slash command
}

# Forum topic commands (shown in group chats only)
GROUP_EXTRAS = [
    {"command": "topics", "description": "List forum topics"},
]


# ============================================================
# Dynamic Command Derivation
# ============================================================

def _get_commands_for_role_ids(role_ids: set[str], is_group: bool = False) -> list[dict]:
    """Derive visible commands from RBAC role policies.

    Checks two object types:
    1. "commands" -> slash commands via command_registry.COMMANDS
    2. "tools" -> slash commands via TOOL_TO_COMMAND

    Deny-overrides-allow. Wildcard -> all commands.
    """
    from bot.rbac import get_engine
    from bot.command_registry import COMMANDS as CMD_REGISTRY
    engine = get_engine()

    allowed_tools: set[str] = set()
    denied_tools: set[str] = set()
    allowed_cmds: set[str] = set()
    denied_cmds: set[str] = set()
    has_wildcard = False
    has_deny_all_tools = False
    has_deny_all_cmds = False

    # Collect ALL policies from ALL roles (deny-overrides-allow, order-independent)
    for rid in role_ids:
        for policy in engine._role_policies.get(rid, []):
            if policy.privilege not in ("use", "admin", "*"):
                continue

            # Tools
            if policy.object_type in ("tools", "*"):
                if policy.effect == "deny":
                    if policy.object_id == "*":
                        has_deny_all_tools = True
                    denied_tools.add(policy.object_id)
                elif policy.effect == "allow":
                    if policy.object_id == "*" or policy.object_type == "*":
                        has_wildcard = True
                    else:
                        allowed_tools.add(policy.object_id)

            # Commands
            if policy.object_type in ("commands", "*"):
                if policy.effect == "deny":
                    if policy.object_id == "*":
                        has_deny_all_cmds = True
                    denied_cmds.add(policy.object_id)
                elif policy.effect == "allow":
                    if policy.object_id == "*" or policy.object_type == "*":
                        has_wildcard = True
                    else:
                        allowed_cmds.add(policy.object_id)

    # Deny-all overrides everything for that category
    # Build command list
    seen: set[str] = set()
    commands: list[dict] = []

    # 1. Commands from registry (RBAC object_type="commands") — skip if deny-all
    if has_deny_all_cmds:
        pass  # deny-all on commands — show nothing from registry
    elif has_wildcard:
        for cmd_id, cdef in CMD_REGISTRY.items():
            if cdef.get("menu") and cmd_id not in denied_cmds:
                slash = cdef["slash"]
                if slash not in seen:
                    seen.add(slash)
                    commands.append({"command": slash, "description": cdef["description"]})
    else:
        for cmd_id in allowed_cmds:
            if cmd_id in denied_cmds:
                continue
            cdef = CMD_REGISTRY.get(cmd_id)
            if cdef and cdef.get("menu") and cdef["slash"] not in seen:
                seen.add(cdef["slash"])
                commands.append({"command": cdef["slash"], "description": cdef["description"]})

    # 2. Commands from tool->command mapping (RBAC object_type="tools") — skip if deny-all
    if has_deny_all_tools:
        pass  # deny-all on tools — show nothing from tool mapping
    elif has_wildcard:
        for tool_name, cmd in TOOL_TO_COMMAND.items():
            if cmd and tool_name not in denied_tools and cmd["command"] not in seen:
                seen.add(cmd["command"])
                commands.append(cmd)
    else:
        for tool_name in allowed_tools:
            if tool_name in denied_tools:
                continue
            cmd = TOOL_TO_COMMAND.get(tool_name)
            if cmd and cmd["command"] not in seen:
                seen.add(cmd["command"])
                commands.append(cmd)

    # Group extras
    if is_group:
        for cmd in GROUP_EXTRAS:
            if cmd["command"] not in seen:
                seen.add(cmd["command"])
                commands.append(cmd)

    return commands


# ============================================================
# Chat Menu Refresh
# ============================================================

async def refresh_chat_commands(chat_id: str) -> bool:
    """Set command menu for a specific chat based on its RBAC roles.

    Unregistered chats: empty menu (no commands visible).
    Registered chats: commands derived from role policies.
    """
    try:
        int_chat_id = int(str(chat_id).split(":")[0])
    except (ValueError, TypeError):
        return False

    from bot.rbac import get_engine
    engine = get_engine()
    await engine._refresh_cache()

    chat_key = f"chat:{chat_id}"
    parent_key = f"chat:{chat_id.split(':')[0]}" if ":" in str(chat_id) else chat_key

    role_ids = engine._subject_roles.get(chat_key, set())
    if not role_ids and parent_key != chat_key:
        role_ids = engine._subject_roles.get(parent_key, set())
    # DMs: positive chat_id == user_id -- check user roles (DM chats use USER roles only)
    if not role_ids and int_chat_id > 0:
        role_ids = engine._subject_roles.get(f"user:{int_chat_id}", set())

    from integrations.telegram_botapi import set_bot_commands, delete_bot_commands

    if not role_ids:
        # Unregistered -- delete all commands (empty menu)
        await delete_bot_commands(scope="chat", chat_id=int_chat_id)
        return True

    # Derive commands from role policies
    is_group = int_chat_id < 0
    commands = _get_commands_for_role_ids(role_ids, is_group=is_group)

    return await set_bot_commands(commands, scope="chat", chat_id=int_chat_id)


async def refresh_all_chat_commands() -> int:
    """Refresh command menus for ALL registered chats. Called on startup."""
    from bot.rbac import get_engine
    engine = get_engine()
    await engine._refresh_cache()

    updated = 0
    seen_chats: set[str] = set()
    for key in engine._subject_roles:
        chat_id = None
        if key.startswith("chat:"):
            chat_id = key[5:]
        elif key.startswith("user:"):
            # User DMs: user_id == chat_id for private chats
            uid = key[5:]
            try:
                if int(uid) > 0:
                    chat_id = uid
            except ValueError:
                pass
        if chat_id and chat_id not in seen_chats:
            seen_chats.add(chat_id)
            try:
                ok = await refresh_chat_commands(chat_id)
                if ok:
                    updated += 1
            except Exception as e:
                log.debug("Failed to set commands for chat %s: %s", chat_id, e)
    log.info("Refreshed command menus for %d chats", updated)
    return updated
