# SPDX-License-Identifier: MIT
"""Unified command registry — single source of truth for all lightweight commands.

Each command has:
- command_id: RBAC object_id (use:commands:<id>)
- slash: the /command the user types (display only, regex matching stays in dispatch)
- description: TG bot menu description
- scope: "anyone" | "registered" | "own_dm" | "admin" — informational, enforcement via RBAC
- menu: True = show in TG bot menus when RBAC allows

The registry is used by:
1. command_menus.py — derive per-chat TG bot menus from RBAC policies
2. dispatch.py — RBAC access check before running handler
3. seed.py — generate employee/admin command policies
"""

import logging

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Command definitions
# ---------------------------------------------------------------------------

# scope values (informational — actual enforcement is RBAC):
#   "anyone"     — no RBAC check (status, help, cancel)
#   "registered" — any registered user (RBAC employee or admin)
#   "own_dm"     — registered user in their own DM
#   "admin"      — bot_admin role only

COMMANDS: dict[str, dict] = {
    # --- Anyone (no RBAC check) ---
    "status":       {"slash": "status",          "description": "Bot status",                "scope": "anyone",     "menu": True},
    "help":         {"slash": "help",            "description": "Show commands",             "scope": "anyone",     "menu": True},
    "cancel":       {"slash": "cancel",          "description": "Stop active task",          "scope": "anyone",     "menu": True},
    "continue_cmd": {"slash": "continue",        "description": "Resume paused task",        "scope": "anyone",     "menu": False},
    "drop_call":    {"slash": "drop",            "description": "End phone call",            "scope": "anyone",     "menu": False},

    # --- Registered users (employee + admin) ---
    "onboarding":   {"slash": "onboarding",      "description": "Setup progress",            "scope": "registered", "menu": True},
    "diagnose":     {"slash": "diagnose",         "description": "Self-diagnostics",          "scope": "registered", "menu": True},
    "knowledge":    {"slash": "knowledge",        "description": "Knowledge domains",          "scope": "registered", "menu": True},

    # --- Own DM (employee in own DM + admin anywhere) ---
    "reset":        {"slash": "reset",           "description": "Reset session",             "scope": "own_dm",     "menu": True},
    "claude_auth":  {"slash": "claude_auth",     "description": "Set up Claude access",      "scope": "own_dm",     "menu": True},
    "google_auth":  {"slash": "google_auth",     "description": "Connect Google account",    "scope": "own_dm",     "menu": True},
    "m365_auth":    {"slash": "m365_auth",       "description": "Connect Microsoft 365",     "scope": "own_dm",     "menu": True},
    "accounts":     {"slash": "accounts",        "description": "Your connected accounts",   "scope": "own_dm",     "menu": True},
    "claude_accounts": {"slash": "claude_accounts", "description": "Your Claude profiles",   "scope": "own_dm",     "menu": False},
    "claude_set_oauth": {"slash": "claude_set_oauth", "description": "Switch Claude profile", "scope": "own_dm",    "menu": True},
    "revoke":       {"slash": "revoke",          "description": "Revoke credentials",        "scope": "own_dm",     "menu": True},
    "github_auth":  {"slash": "github_auth",    "description": "Connect GitHub account",    "scope": "own_dm",     "menu": True},

    # --- Visibility commands (filtered by role: admin sees all, others see own) ---
    "chats":        {"slash": "chats",           "description": "Registered chats",          "scope": "registered", "menu": True},
    "harnesses":    {"slash": "harnesses",       "description": "Harness profiles",          "scope": "registered", "menu": True},
    "permissions":  {"slash": "permissions",      "description": "Your permissions",          "scope": "registered", "menu": True},

    # --- Admin only ---
    "usage":        {"slash": "usage",           "description": "Cost breakdown",            "scope": "admin",      "menu": True},
    "set_harness":  {"slash": "set_harness",     "description": "Assign harness to chat",    "scope": "admin",      "menu": False},
    "rollback":     {"slash": "rollback",        "description": "Revert last deploy",        "scope": "admin",      "menu": False},
    "deploy":       {"slash": "deploy",          "description": "Deploy bot",                "scope": "admin",      "menu": True},
    "delete_chat":  {"slash": "delete_chat",     "description": "Remove external chat",      "scope": "admin",      "menu": False},

    # --- Admin: Mac/relay ---
    "mac_setup":    {"slash": "mac_setup",        "description": "Set up your workstation",    "scope": "own_dm",     "menu": True},
    "mac_status":   {"slash": "mac",             "description": "Workstation status",         "scope": "admin",      "menu": True},
    "mac_lock":     {"slash": "lock",            "description": "Revoke Mac auth",            "scope": "admin",      "menu": False},
    "mac_auth":     {"slash": "auth",            "description": "TOTP authenticate to Mac",   "scope": "admin",      "menu": False},
    "mac_sessions": {"slash": "sessions",        "description": "Mac project list",           "scope": "admin",      "menu": False},
    "session":      {"slash": "session",         "description": "Relay session management",   "scope": "admin",      "menu": False},
    "relays":       {"slash": "relays",          "description": "Relay channels list",        "scope": "admin",      "menu": True},
    "create_relay": {"slash": "create_relay",    "description": "Create relay channel",       "scope": "admin",      "menu": False},
    "delete_relay": {"slash": "delete_relay",    "description": "Remove relay channel",       "scope": "admin",      "menu": False},
    "relay_reset":  {"slash": "relay_reset",     "description": "Restart relay daemon",       "scope": "admin",      "menu": False},
    "relay_reap":   {"slash": "relay_reap",      "description": "Clean dead pidfiles",        "scope": "admin",      "menu": False},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_commands_for_scope(*scopes: str) -> list[dict]:
    """Get commands matching given scopes. Used for RBAC seed policy generation."""
    return [
        {"command_id": cid, **cdef}
        for cid, cdef in COMMANDS.items()
        if cdef["scope"] in scopes
    ]


def get_menu_commands() -> dict[str, dict]:
    """Get all commands that should appear in TG menus (menu=True)."""
    return {
        cid: {"command": cdef["slash"], "description": cdef["description"]}
        for cid, cdef in COMMANDS.items()
        if cdef.get("menu")
    }
