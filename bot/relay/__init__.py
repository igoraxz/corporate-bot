# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Employee workstation control package — SSH connection to employee Mac for Claude Code session management.

Provides managed channels for employees to interact with Claude Code on their
individual Mac workstations. Re-exports all public functions from sub-modules
so callers can use:
  from bot.relay import ping, execute, ...
  from bot import desktop_relay  # backwards compat shim still works
"""

# --- ssh.py ---
from bot.relay.ssh import (
    execute,
    fetch_file,
    push_file,
    _ssh_cmd_base,
    _clean_ssh_stderr,
    _SSH_CONFIG_DIR,
    _ssh_procs,
    _PUSH_FILE_EXTS,
    _PUSH_FILE_MAX_BYTES,
    _PUSH_FILE_TIMEOUT_S,
    _SSH_NOISE_PREFIXES,
)

# --- registry.py ---
from bot.relay.registry import (
    _load_registry,
    _save_registry,
    _update_synced_offset,
    _validate_project_path,
    link_session,
    update_pinned_jsonl as _registry_update_pinned_jsonl,  # noqa: F401 — re-exported
    unlink_session,
    get_linked_session,
    get_all_links,
    _touch_last_used,
    _uuid_from_entry,
    validate_account_label,
    _encode_project_path,
    _decode_project_path,
    _fetch_jsonl_cwd,
    list_all_jsonls,
    list_project_jsonls,
    _DATA_DIR,
    _REGISTRY_FILE,
    _registry_lock,
    _UUID_RE,
    _UUID_BARE_RE,
    _SAFE_PROJECT_PATH,
    _ACCOUNT_LABEL_RE,
)

# --- oauth.py ---
from bot.relay.oauth import (
    promote_oauth_token,
    list_mac_accounts,
    set_chat_account,
    push_oauth_to_mac,
    start_mac_oauth_flow,
    submit_mac_oauth_code,
    poll_mac_oauth_status,
    kill_mac_oauth_flow,
    _MAC_OAUTH_LAUNCH_TIMEOUT_S,
    _MAC_OAUTH_PASTE_TIMEOUT_S,
    _MAC_OAUTH_FINALIZE_TIMEOUT_S,
)

# --- sessions.py ---
from bot.relay.sessions import (
    list_daemons,
    kill_daemon,
    reap_orphans,
    set_relay_mode,
    get_relay_mode,
    get_mac_process_snapshot,
)

# --- watchdog.py ---
from bot.relay.watchdog import (
    mac_health_watchdog,
    _kick_one_chat,
    _kick_relay_daemons_for_post_online_catchup,
    _eager_respawn_after_kick,
    _eager_respawn_one,
    _respawn_unhealthy_daemons,
    _alert_relay_chats,
    _mac_was_online,
    _mac_offline_since,
    _mac_alerted,
    _ever_seen_online,
    _mac_was_authenticated,
    _kick_lock,
    _respawn_last_attempt,
    _RESPAWN_COOLDOWN_S,
    _active_relay_chats,
    _relay_tasks,
)

# --- core.py ---
from bot.relay.core import (
    ping,
    is_mac_online,
    authenticate,
    status,
    lock,
    is_enabled,
    get_cached_status,
    eager_respawn_daemon,
    install_mac_skills,
    install_mac_daemon,
    check_install_status,
    update_pinned_jsonl,
    _safe_main_attr,
    DESKTOP_ENABLED,
    DESKTOP_HOST,
    DESKTOP_USER,
    DESKTOP_SESSION_TIMEOUT,
    _last_ping,
    _last_ping_ok,
    _cached_authenticated,
    _cached_session_remaining,
    _cached_auth_time,
    _RELAY_QUERY_PRELUDE,
    _SAFE_TOTP_CODE,
    _SAFE_PATH,
)

__all__ = [
    # ssh
    "execute", "fetch_file", "push_file",
    "_ssh_cmd_base", "_clean_ssh_stderr",
    "_SSH_CONFIG_DIR", "_ssh_procs",
    "_PUSH_FILE_EXTS", "_PUSH_FILE_MAX_BYTES", "_PUSH_FILE_TIMEOUT_S",
    "_SSH_NOISE_PREFIXES",
    # registry
    "_load_registry", "_save_registry", "_update_synced_offset",
    "_validate_project_path", "link_session", "update_pinned_jsonl",
    "unlink_session", "get_linked_session", "get_all_links",
    "_touch_last_used", "_uuid_from_entry", "validate_account_label",
    "_encode_project_path", "_decode_project_path",
    "_fetch_jsonl_cwd", "list_all_jsonls", "list_project_jsonls",
    "_DATA_DIR", "_REGISTRY_FILE", "_registry_lock",
    "_UUID_RE", "_UUID_BARE_RE", "_SAFE_PROJECT_PATH", "_ACCOUNT_LABEL_RE",
    # oauth
    "promote_oauth_token", "list_mac_accounts", "set_chat_account",
    "push_oauth_to_mac", "start_mac_oauth_flow", "submit_mac_oauth_code",
    "poll_mac_oauth_status", "kill_mac_oauth_flow",
    "_MAC_OAUTH_LAUNCH_TIMEOUT_S", "_MAC_OAUTH_PASTE_TIMEOUT_S",
    "_MAC_OAUTH_FINALIZE_TIMEOUT_S",
    # sessions
    "list_daemons", "kill_daemon", "reap_orphans",
    "set_relay_mode", "get_relay_mode", "get_mac_process_snapshot",
    # watchdog
    "mac_health_watchdog",
    "_kick_one_chat", "_kick_relay_daemons_for_post_online_catchup",
    "_eager_respawn_after_kick", "_eager_respawn_one",
    "_respawn_unhealthy_daemons", "_alert_relay_chats",
    "_mac_was_online", "_mac_offline_since", "_mac_alerted",
    "_ever_seen_online", "_mac_was_authenticated",
    "_kick_lock", "_respawn_last_attempt", "_RESPAWN_COOLDOWN_S",
    "_active_relay_chats", "_relay_tasks",
    # core
    "ping", "is_mac_online", "authenticate", "status", "lock",
    "is_enabled", "get_cached_status", "eager_respawn_daemon",
    "install_mac_skills", "install_mac_daemon", "check_install_status",
    "_safe_main_attr",
    "DESKTOP_ENABLED", "DESKTOP_HOST", "DESKTOP_USER", "DESKTOP_SESSION_TIMEOUT",
    "_last_ping", "_last_ping_ok",
    "_cached_authenticated", "_cached_session_remaining", "_cached_auth_time",
    "_RELAY_QUERY_PRELUDE", "_SAFE_TOTP_CODE", "_SAFE_PATH",
]
