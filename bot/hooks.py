# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Agent SDK hooks — admin gating, file protection, tool status notifications.

Hooks fire at key execution points in the SDK agent loop:
- PreToolUse: before tool execution (can deny/modify)
- PostToolUse: after tool completes (for status updates)
- Stop: when agent finishes (for cleanup)

SECURITY MODEL — 4-Tier Access Control:
- Container isolation is the PRIMARY boundary (Docker, non-root user)
- These hooks are DEFENSE-IN-DEPTH — they block sensitive operations even if
  prompt injection convinces the model to call dangerous tools
- 4 tiers: Admin / Non-admin member / Coding sandbox / External chat
- Coding sandbox: regex command checks (Layer 3) + bubblewrap kernel sandbox (Layer 3b)
- Bubblewrap wraps Bash commands in namespace isolation (cleared env, PID isolation,
  restricted filesystem, proxy-based network) via updatedInput hook mechanism
"""

import asyncio
import logging
import os
import re
import shlex
import shutil
import time
from config import MAX_TURNS

# Constants extracted to bot.tool_labels — re-exported here for backward compat.
# External code doing `from bot.hooks import SEND_TOOLS` etc. continues to work.
from bot.tool_labels import (  # noqa: F401 — re-exports
    SEND_TOOLS,
    TOOL_LABELS,
    _CHAT_ISOLATED_TOOLS,
    _BLOCKED_PATH_PATTERNS,
    _ADMIN_BLOCKED_PATH_PATTERNS,
    _WRITE_ONLY_BLOCKED_PATTERNS,
    _BLOCKED_EXTENSIONS,
    _WRITE_ONLY_ENV_PATTERN,
    _BASH_BLOCKLIST,
    _ADMIN_BASH_BLOCKLIST,
    _CODING_SANDBOX_BASH_BLOCKLIST,
    _MEDIA_BASH_PATTERNS,
    _SECRET_VAR_WORDS,
    _SECRET_VAR_RE,
    _DEPLOY_KEYWORDS,
    _DEPLOY_FORCE_PATTERN,
    _DEPLOY_NOQA_PATTERN,
    _EXTEND_BUDGET_KEYWORDS,
    _OUTBOUND_SEND_KEYWORDS,
    _REPLY_QUOTE_BLOCK,
    _ENRICHMENT_MARKER,
    _SAFE_MEDIA_DIRS,
    _FILE_SEND_PATH_PARAMS,
)

log = logging.getLogger(__name__)

# RBAC is the authoritative enforcement layer for tool access.
# Legacy layers (1b/1b2/1b3/1c) removed — RBAC governs all tool access decisions.
# Deploy gates, file-path checks (Layer 2), and bash checks (Layer 3) remain as defense-in-depth.

# Tools allowed in coding sandbox mode (bypass Layer 1b admin gate,
# enforced by Layer 2/3 sandbox containment instead)
_SANDBOX_TOOLS = frozenset({"Bash", "Write", "Edit", "NotebookEdit", "Read", "Grep", "Glob"})

# Static path patterns for sandbox bash checks (compiled once at import)
_SANDBOX_BASH_PATH_REFS = [
    re.compile(r"/app/data/(?!harness-projects/)"),  # bot data (DB, media, config)
    re.compile(r"/app/bot/"),                         # bot source
    re.compile(r"/app/integrations/"),                # integration source (TG/WA/Gemini)
    re.compile(r"/app/scripts/"),                     # utility scripts
    re.compile(r"/app/config"),                       # config.py, config_overrides.py
    re.compile(r"/app/main\.py"),                     # main entry point
    re.compile(r"/app/logs/"),                        # bot logs (may contain PII/keys)
    re.compile(r"/host-repo/"),                       # host repo
    re.compile(r"/app/\.env"),                        # env file
    re.compile(r"/etc/(shadow|passwd)"),              # system auth
    re.compile(r"/home/botuser/\.claude"),            # Claude config
]

# Credential patterns blocked even within sandbox dir (narrower than
# _BLOCKED_PATH_PATTERNS — coding projects use words like "token", "secret")
_SANDBOX_CREDENTIAL_PATTERNS = frozenset({
    ".env", ".ssh/", "id_rsa", "id_ed25519",
    "/etc/shadow", "/etc/passwd", ".docker/config",
    ".session", "tg_session/",
    # M5 audit: block credential directory paths in sandbox
    "credentials/", "oauth_profiles/", "google-workspace-creds/",
    "ms365-mcp/", ".credentials.json",
    # GitHub credential files (written by agent.py for sandbox git auth)
    ".netrc", ".git-credentials", "hosts.yml",
    # SSH deploy keys (CB-99 — temp keys in /dev/shm, permanent in credential_store)
    "deploy_key",
})

# ═══════════════════════════════════════════════════════════════════════
# PER-SESSION STATE — now lives in bot.core.session_state.
# All symbols re-exported here for backward compatibility.
# ═══════════════════════════════════════════════════════════════════════

from bot.core.session_state import (  # noqa: E402, F401
    # State template & constants
    _DEFAULT_STATE,
    _DEPLOY_GATE_FLAGS,
    # Module-level state dicts (mutable — importers get the SAME object)
    _session_state,
    _access_contexts,
    _chat_last_task,
    _sid_to_skey,
    _last_input_tokens,
    pending_compaction,
    compacted_sessions,
    # Access context
    set_access_context,
    get_access_context,
    # State lookup helpers
    _get_or_create_state,
    _get_state_for_hook,
    # Session lifecycle
    cleanup_session_state,
    # Public getters/setters
    get_current_user_ctx,
    set_current_user_ctx,
    get_tool_status_callback,
    set_tool_status_callback,
    get_send_tool_called,
    set_send_tool_called,
    set_reply_threading,
    get_reply_to_msg_id,
    get_task_last_sent_id,
    set_task_last_sent_id,
    get_current_task_id,
    get_origin_chat_id,
    # Chat activity tracking
    mark_chat_activity,
    get_chat_last_task,
    # Session ID mapping
    save_last_input_tokens,
    register_session_mapping,
    unregister_session_mapping,
    # Intentional silence
    get_intentional_silence,
    set_intentional_silence,
    # Deploy state
    set_deploy_state,
    mark_deploy_review_passed,
    mark_qa_staging_passed,
    set_extend_budget_state,
    # Tool use counter
    get_tool_use_counter,
    set_tool_use_counter,
    # User context
    set_user_context,
    # Parallel session file awareness
    record_repo_file,
    check_repo_conflicts,
)

# Nudge thresholds — dynamic, computed per-chat from harness max_turns.
# Uses 80% and 90% of the effective limit so high-turn harnesses (200, 999)
# don't trigger premature wrap-up warnings.
def _get_nudge_thresholds(session_key: str | None = None) -> tuple[int, int]:
    """Return (warning_turn, critical_turn) for the given session's max_turns.

    Returns (0, 0) for unlimited mode (max_turns=0) — caller must skip nudges.
    """
    effective = MAX_TURNS  # global default fallback
    if session_key:
        try:
            from bot.harness import get_harness_for_chat, resolve_field
            _harness = get_harness_for_chat(session_key)
            _ht = resolve_field(_harness, "max_turns", session_key)
            if _ht is not None and isinstance(_ht, int):
                if _ht == 0:
                    return (0, 0)  # unlimited — no nudges
                if _ht > 0:
                    effective = _ht
        except Exception:
            pass
    warning = max(1, int(effective * 0.80))   # "Start wrapping up"
    critical = max(1, int(effective * 0.90))   # "STOP NOW — send your reply"
    return warning, critical

# Per-chat overrides: allow specific blocked prefixes for specific external chats.
# Maps chat_id (str) → dict with:
#   "prefixes": tuple of tool prefixes that ARE allowed despite the block list
#   "allowed_emails": tuple of Google account emails permitted (checked via user_google_email param)
# If allowed_emails is set, ANY Google Workspace call MUST use one of these emails — others are blocked.
# Persisted to DATA_DIR/external_tool_overrides.json (runtime-manageable via admin tool).
# Initialized from EXTERNAL_CHAT_TOOL_OVERRIDES env var on first run if file doesn't exist.
from pathlib import Path as _Path
from bot.config_store import JsonConfigStore as _JsonConfigStore

_TOOL_OVERRIDES_FILE = _Path(os.environ.get("DATA_DIR", "data")) / "config" / "external_tool_overrides.json"
_tool_overrides_store = _JsonConfigStore("tool_overrides", _TOOL_OVERRIDES_FILE)

# Seed from env var if store is empty (first run or file deleted)
if not _tool_overrides_store.all():
    import json as _json_seed
    _raw_seed = os.environ.get("EXTERNAL_CHAT_TOOL_OVERRIDES", "{}")
    try:
        _seed_data = _json_seed.loads(_raw_seed)
        if _seed_data and isinstance(_seed_data, dict):
            _tool_overrides_store.update(_seed_data)
            log.info("Seeded %d external chat tool override(s) from env var", len(_seed_data))
    except Exception as _e:
        log.warning("Failed to parse EXTERNAL_CHAT_TOOL_OVERRIDES env var: %s", _e)


_MIN_TOOL_PREFIX_LEN = 8  # M8 audit: prevent overly broad prefixes like "" or "mcp__"


def set_external_tool_override(chat_id: str, prefixes: list[str], allowed_emails: list[str]) -> None:
    """Set or update tool override for a external chat. Persists atomically."""
    # M8 audit: validate prefixes aren't empty or dangerously broad
    validated = []
    for p in prefixes:
        p = p.strip()
        if not p:
            log.warning("Rejected empty tool prefix for chat %s", chat_id)
            continue
        if len(p) < _MIN_TOOL_PREFIX_LEN:
            log.warning("Rejected short tool prefix '%s' for chat %s (min %d chars)",
                        p, chat_id, _MIN_TOOL_PREFIX_LEN)
            continue
        validated.append(p)
    _tool_overrides_store.set(str(chat_id), {
        "prefixes": validated,
        "allowed_emails": list(allowed_emails),
    })
    log.info("Set tool override for chat %s: prefixes=%s emails=%s", chat_id, validated, allowed_emails)


def remove_external_tool_override(chat_id: str) -> bool:
    """Remove tool override for a external chat. Returns True if existed."""
    removed = _tool_overrides_store.delete(str(chat_id))
    if removed:
        log.info("Removed tool override for chat %s", chat_id)
    return removed


def get_external_tool_overrides() -> dict[str, dict]:
    """Get all external chat tool overrides (for display)."""
    return {k: {"prefixes": list(v.get("prefixes", ())),
                "allowed_emails": list(v.get("allowed_emails", ()))}
            for k, v in _tool_overrides_store.all().items()}


def _extract_tool_name(tool_input: dict) -> str:
    """Extract the bare tool name from an MCP-namespaced tool name."""
    name = tool_input.get("tool_name", "")
    # MCP tools are namespaced: mcp__bot-messaging__telegram_send_message
    parts = name.split("__")
    return parts[-1] if parts else name



def _extract_tab_id_from_response(tool_response) -> str:
    """Extract tab_id from a create_tab tool response.

    Camoufox returns: {"tabId": "uuid-string", ...} or text with tabId.
    Playwright returns: various formats depending on version.
    Defensively parses multiple formats.
    """
    if not tool_response:
        return ""
    try:
        # If it's already a dict
        if isinstance(tool_response, dict):
            return str(tool_response.get("tabId", ""))
        # If it's a string, try JSON parse
        if isinstance(tool_response, str):
            import json
            try:
                data = json.loads(tool_response)
                if isinstance(data, dict):
                    return str(data.get("tabId", ""))
            except (json.JSONDecodeError, ValueError):
                pass
            # Try to extract tabId from text response
            if "tabId" in tool_response:
                import re
                match = re.search(r'"tabId"\s*:\s*"([^"]+)"', tool_response)
                if match:
                    return match.group(1)
        # If it's a list (MCP content blocks), search for tabId in text
        if isinstance(tool_response, list):
            for item in tool_response:
                if isinstance(item, dict):
                    text = item.get("text", "")
                    if "tabId" in text:
                        import json as _json
                        try:
                            data = _json.loads(text)
                            if isinstance(data, dict):
                                return str(data.get("tabId", ""))
                        except (ValueError, _json.JSONDecodeError):
                            pass
    except Exception:
        pass
    return ""


def _check_file_path(file_path: str, is_write: bool = True,
                     is_admin: bool = False) -> str | None:
    """Check if a file path is allowed. Returns denial reason or None if OK.

    is_write: True for Write/Edit, False for Read.
    is_admin: True for admin users — uses relaxed blocklist (Docker escape only).
    """
    path_lower = file_path.lower()

    if is_admin:
        # Admin: only block Docker escape + host-level infra paths
        for pat in _ADMIN_BLOCKED_PATH_PATTERNS:
            if pat in path_lower:
                return f"Access denied: path contains '{pat}' (blocked for all users)"
        if is_write:
            for pat in _WRITE_ONLY_BLOCKED_PATTERNS:
                if pat in path_lower:
                    return f"Access denied: write blocked for '{pat}'"
            if _WRITE_ONLY_ENV_PATTERN.search(path_lower):
                return "Access denied: write blocked for '.env'"
        return None

    # Non-admin: full blocklist (credential + infra protection)
    for pat in _BLOCKED_PATH_PATTERNS:
        if pat in path_lower:
            return f"Access denied: path contains '{pat}'"
    if is_write:
        for pat in _WRITE_ONLY_BLOCKED_PATTERNS:
            if pat in path_lower:
                return f"Access denied: write blocked for '{pat}'"
        if _WRITE_ONLY_ENV_PATTERN.search(path_lower):
            return "Access denied: write blocked for '.env'"
    for ext in _BLOCKED_EXTENSIONS:
        if path_lower.endswith(ext):
            return f"Access denied: blocked file extension '{ext}'"
    return None


def _check_sandbox_path(file_path: str, sandbox_dir: str) -> str | None:
    """Check if a file path is within the coding sandbox directory.

    Returns denial reason or None if OK. Blocks ALL paths outside sandbox_dir,
    plus credential patterns within it.
    """
    resolved = os.path.realpath(file_path)
    sandbox_resolved = os.path.realpath(sandbox_dir)

    # Must be within sandbox dir
    if not resolved.startswith(sandbox_resolved + "/") and resolved != sandbox_resolved:
        return (f"Access denied: path '{file_path}' is outside your project "
                f"directory ({sandbox_dir})")

    # Even within sandbox, block actual credential file patterns
    # (narrower than _BLOCKED_PATH_PATTERNS — coding projects legitimately
    # use words like "token", "secret", "credentials" in source code)
    path_lower = resolved.lower()
    for pat in _SANDBOX_CREDENTIAL_PATTERNS:
        if pat in path_lower:
            return f"Access denied: path contains '{pat}'"
    for ext in _BLOCKED_EXTENSIONS:
        if path_lower.endswith(ext):
            return f"Access denied: blocked file extension '{ext}'"
    return None


def _check_bash_command(command: str, is_admin: bool = False) -> str | None:
    """Check if a Bash command is allowed. Returns denial reason or None if OK.

    is_admin: True for admin users — uses relaxed blocklist (Docker escape only).
    Non-admin uses the full blocklist (credential + infra + exfil protection).
    """
    cmd_lower = command.lower().strip()

    blocklist = _ADMIN_BASH_BLOCKLIST if is_admin else _BASH_BLOCKLIST
    for pattern in blocklist:
        if pattern.search(cmd_lower):
            return "Access denied: command matches security blocklist"

    if not is_admin:
        # Secret variable check only for non-admin
        for m in _SECRET_VAR_RE.finditer(cmd_lower):
            varname = m.group(1)
            if any(sw in varname for sw in _SECRET_VAR_WORDS):
                return "Access denied: references secret environment variable"

    # Block Bash for media file operations — use dedicated tools instead
    if _is_media_bash(cmd_lower):
        return (
            "Do NOT use Bash for media/file operations. Use the dedicated tools instead: "
            "get_document, search_media, telegram_send_photo, telegram_send_document, "
            "teams_send_message. Just try sending the file directly — the tool will error if missing."
        )

    return None


def _check_sandbox_bash(command: str, sandbox_dir: str) -> str | None:
    """Check if a Bash command is safe for coding sandbox.

    Applied to ALL users (admin + non-admin) in project-scoped coding chats.
    4-phase check: (1) blocklist, (2) secret vars, (3) path refs, (4) media bash.
    """
    cmd_lower = command.lower().strip()

    # Phase 1: Blocklist (env vars, Docker, privilege escalation)
    for pattern in _CODING_SANDBOX_BASH_BLOCKLIST:
        if pattern.search(cmd_lower):
            return "Access denied: command matches coding sandbox blocklist"

    # Phase 2: Secret variable check
    for m in _SECRET_VAR_RE.finditer(cmd_lower):
        varname = m.group(1)
        if any(sw in varname for sw in _SECRET_VAR_WORDS):
            return "Access denied: references secret environment variable"

    # Phase 3: Block references to sensitive paths outside sandbox
    for pat in _SANDBOX_BASH_PATH_REFS:
        if pat.search(cmd_lower):
            return (f"Access denied: command references paths outside your "
                    f"project ({sandbox_dir})")
    # Dynamic check: block other harness project dirs (allow own only)
    # M12 fix: quick string check avoids regex compilation when no harness paths present
    sandbox_name = os.path.basename(sandbox_dir.rstrip("/"))
    if "/app/data/harness-projects/" in cmd_lower:
        _other_projects = re.compile(
            r"/app/data/harness-projects/(?!" + re.escape(sandbox_name) + r")"
        )
        if _other_projects.search(cmd_lower):
            return (f"Access denied: command references paths outside your "
                    f"project ({sandbox_dir})")

    # Phase 4: Block Bash for media file operations
    if _is_media_bash(cmd_lower):
        return (
            "Do NOT use Bash for media/file operations. Use the dedicated tools instead."
        )

    return None


def _is_media_bash(cmd_lower: str) -> bool:
    """Check if a Bash command is a media file operation that should use dedicated tools.

    Blocks for ALL users (including admin) since dedicated tools handle media better.
    This doesn't affect admin code/deployment work — only media-specific file operations.
    """
    return any(p.search(cmd_lower) for p in _MEDIA_BASH_PATTERNS)


# Pre-resolved safe media directories — resolved once at import time to avoid
# per-request realpath() syscalls and eliminate TOCTOU between resolution and check.
_SAFE_MEDIA_DIRS_RESOLVED = tuple(os.path.realpath(d) for d in _SAFE_MEDIA_DIRS)

# Credential filename patterns blocked even within safe media dirs.
# Defense-in-depth: if credential files end up in /app/data/tmp/ (e.g. from
# backup scripts, staging tests), they must not be sendable.
_SAFE_DIR_CREDENTIAL_PATTERNS = frozenset({
    "credential", "token", "secret", "password",
    ".session", ".pem", ".key", ".p12", ".pfx", ".env",
})


def _is_in_safe_media_dir(resolved_path: str) -> bool:
    """Check if a resolved path is within any _SAFE_MEDIA_DIRS.

    Also blocks credential-like filenames within safe dirs as defense-in-depth.
    Used by _check_file_send_path (2 call sites) and _is_sandbox_safe_read_path.
    """
    for safe_resolved in _SAFE_MEDIA_DIRS_RESOLVED:
        if resolved_path.startswith(safe_resolved + "/"):
            # Block credential-like filenames even within safe dirs
            filename_lower = os.path.basename(resolved_path).lower()
            for pat in _SAFE_DIR_CREDENTIAL_PATTERNS:
                if pat in filename_lower:
                    return False
            return True
    return False


def _check_file_send_path(
    file_path: str,
    *,
    is_admin: bool,
    sandbox_dir: str | None,
    is_external: bool,
) -> str | None:
    """Validate file path for file-send tools (send_photo, send_document, etc.).

    Returns denial reason or None if OK. Tier-gated:
    - Sandbox: allowlist — sandbox_dir/* + _SAFE_MEDIA_DIRS
    - External (non-sandbox): allowlist — _SAFE_MEDIA_DIRS only
    - Non-admin member: blocklist — _BLOCKED_PATH_PATTERNS
    - Admin: blocklist — _ADMIN_BLOCKED_PATH_PATTERNS

    Uses os.path.realpath() BEFORE checking allowlists to prevent symlink traversal.
    URL inputs (http/https) are allowed unconditionally — they don't leak local files.
    """
    if not file_path:
        return None  # empty path — tool handler will error naturally

    # URLs don't leak local files — allow unconditionally
    if file_path.startswith(("http://", "https://")):
        return None

    resolved = os.path.realpath(file_path)

    if sandbox_dir:
        # Sandbox mode: strict allowlist — sandbox_dir/* + _SAFE_MEDIA_DIRS
        sandbox_resolved = os.path.realpath(sandbox_dir)
        if resolved.startswith(sandbox_resolved + "/") or resolved == sandbox_resolved:
            return None  # within sandbox — OK
        if _is_in_safe_media_dir(resolved):
            return None
        return "Access denied: file path is outside allowed directories for this chat"

    if is_external:
        # External (non-sandbox): only _SAFE_MEDIA_DIRS
        if _is_in_safe_media_dir(resolved):
            return None
        return "Access denied: file path is outside allowed directories for this chat"

    # Employee / admin — use existing blocklist pattern (matches Read behavior)
    path_lower = resolved.lower()
    blocked = _ADMIN_BLOCKED_PATH_PATTERNS if is_admin else _BLOCKED_PATH_PATTERNS
    for pat in blocked:
        if pat in path_lower:
            return "Access denied: file path matches security blocklist"
    for ext in _BLOCKED_EXTENSIONS:
        if path_lower.endswith(ext):
            return "Access denied: blocked file extension"
    return None


def _is_sandbox_safe_read_path(file_path: str) -> bool:
    """Check if a file path is in a directory safe for sandbox Read access.

    Used before _check_sandbox_path() to allow Read of browser screenshots
    and temp files that land outside the sandbox boundary.
    Keeps _check_sandbox_path() pure (sandbox containment invariant preserved).
    """
    resolved = os.path.realpath(file_path)
    return _is_in_safe_media_dir(resolved)


def _check_user_approval(message_text: str, pattern: re.Pattern) -> bool:
    """Shared approval-check helper for admin-gated tools.

    Strips reply-quote blocks + enrichment markers BEFORE regex match so a
    quoted bot response containing the keyword does NOT count as user
    approval. Used by deploy + extend_budget gates; keeping both on the
    same helper avoids drift if the strip pipeline ever changes.
    """
    if not message_text:
        return False
    stripped = _REPLY_QUOTE_BLOCK.sub("", message_text)
    stripped = _ENRICHMENT_MARKER.sub("", stripped)
    return bool(pattern.search(stripped))


def check_deploy_approval(message_text: str) -> bool:
    """Check if the USER's message contains the /deploy command."""
    return _check_user_approval(message_text, _DEPLOY_KEYWORDS)


def check_deploy_force(message_text: str) -> bool:
    """Check if the USER's message contains /deploy force."""
    return _check_user_approval(message_text, _DEPLOY_FORCE_PATTERN)


def check_deploy_noqa(message_text: str) -> bool:
    """Check if the USER's message contains /deploy no-qa."""
    return _check_user_approval(message_text, _DEPLOY_NOQA_PATTERN)


def check_outbound_approval(message_text: str) -> bool:
    """Check if the USER's message contains the /send command (FB-73)."""
    return _check_user_approval(message_text, _OUTBOUND_SEND_KEYWORDS)


def check_extend_budget_approval(message_text: str) -> bool:
    """Check if the USER's message contains explicit 'extend budget' phrase."""
    return _check_user_approval(message_text, _EXTEND_BUDGET_KEYWORDS)


def _deny(reason: str) -> dict:
    """Build a PreToolUse denial response."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _allow_with_updated_input(updated_input: dict) -> dict:
    """Build a PreToolUse response that modifies the tool input before execution.

    Uses the SDK's updatedInput mechanism to rewrite the command — e.g. wrapping
    a Bash command inside bubblewrap for kernel-level sandbox enforcement.
    """
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": updated_input,
        }
    }


# Path to the bwrap wrapper script (in host-repo mount, available in both prod and staging)
_SANDBOX_EXEC_PATH = "/host-repo/scripts/sandbox-exec.sh"

# Cache bwrap availability check (None = not checked yet)
_bwrap_available: bool | None = None


def _is_bwrap_available() -> bool:
    """Check if bubblewrap (bwrap) is installed and sandbox-exec.sh exists."""
    global _bwrap_available
    if _bwrap_available is None:
        has_bwrap = shutil.which("bwrap") is not None
        has_script = os.path.isfile(_SANDBOX_EXEC_PATH) and os.access(
            _SANDBOX_EXEC_PATH, os.X_OK)
        _bwrap_available = has_bwrap and has_script
        if _bwrap_available:
            log.info("Bubblewrap sandbox available: bwrap + sandbox-exec.sh found")
        else:
            log.warning(
                "Bubblewrap sandbox NOT available (bwrap=%s, script=%s) — "
                "sandbox Bash will be DENIED (regex-only has known bypasses)",
                has_bwrap, has_script)
    return _bwrap_available


def _wrap_command_in_bwrap(command: str, sandbox_dir: str, no_network: bool = False) -> dict | None:
    """Wrap a Bash command in bubblewrap for kernel-level sandboxing.

    Returns an updatedInput dict if bwrap is available, or None if unavailable
    (caller decides policy — currently hard-denies for sandbox chats).

    The bwrap wrapper (sandbox-exec.sh) provides:
    - --clearenv: no env vars (secrets) visible
    - --unshare-pid: PID isolation (no /proc mounted — hides parent env/PIDs)
    - --unshare-net: network namespace isolation (always)
    - --tmpfs /app: hides all bot code/data
    - --bind sandbox_dir: only the project dir is writable
    - Venv: auto-activates .venv/ in sandbox dir if present

    Network: bwrap always uses --unshare-net (kernel-level isolation). When
    network is enabled (no_network=False), a Unix socket proxy bridge provides
    controlled internet — only TCP 80/443 forwarded via HTTP proxy. DNS resolves
    at the proxy level using public DNS (8.8.8.8). Docker internal services are
    completely unreachable (no AF_INET sockets possible in sandbox).
    When no_network=True: fully offline (no proxy). No capabilities needed.
    """
    if not _is_bwrap_available():
        return None
    env_prefix = "SANDBOX_NO_NETWORK=1 " if no_network else ""
    wrapped = f"{env_prefix}{_SANDBOX_EXEC_PATH} {shlex.quote(sandbox_dir)} {shlex.quote(command)}"
    log.info("Wrapping sandbox command in bwrap: dir=%s no_network=%s cmd=%s",
             sandbox_dir, no_network, command[:80])
    return {"command": wrapped}


def _resolve_rbac_subject(state: dict):
    """Resolve RBAC Subject from session state, with on-the-fly fallback.

    Primary: state["rbac_subject"] (cached by agent.py during query init).
    Fallback: build from state["current_user_ctx"] (set by set_user_context
    in session_state.py). This ensures RBAC role changes take effect
    immediately without session reset.

    Returns Subject or None (caller must handle None = fail-closed).
    """
    subj = state.get("rbac_subject")
    if subj:
        return subj
    _ctx = state.get("current_user_ctx")
    if _ctx and _ctx.get("user_id"):
        # Flat permission model (CB-79): DM = user subject, group = chat subject.
        from bot.core.access import build_rbac_subject as _build_subj
        return _build_subj(
            source=_ctx.get("source", ""),
            user_id=_ctx["user_id"],
            chat_id=state.get("origin_chat_id", ""),
        )
    return None


def _is_rbac_admin(state: dict) -> bool:
    """Check if user has bot_admin role via RBAC (sync — uses cached assignments).

    PRECONDITION: engine cache should be warm (check_access called earlier in
    the same pre_tool_use_hook invocation). Returns False on empty cache (fail-closed).

    Bootstrap fallback: if RBAC engine has no cached data (not yet initialized or
    DB failure), falls back to current_user_ctx["is_admin"] boolean. This prevents
    total admin lockout when RBAC is unavailable (e.g., DB corruption, failed seed).
    """
    rbac_subject = _resolve_rbac_subject(state)
    if not rbac_subject:
        return False
    try:
        from bot.rbac import get_engine
        engine = get_engine()
        role_ids = engine._get_role_ids(rbac_subject)
        if role_ids:
            return "bot_admin" in role_ids
        # Empty role_ids could mean: (a) user genuinely has no roles, or
        # (b) cache is empty due to failed init/refresh. Fall through to bootstrap.
    except Exception:
        pass
    # Bootstrap fallback: use cached is_admin from session init (set from config.ADMIN_USERS).
    # This only activates when RBAC has no data — prevents admin lockout on DB failure.
    _ctx = state.get("current_user_ctx")
    if _ctx and _ctx.get("is_admin"):
        log.warning("RBAC admin check: engine has no data, using bootstrap fallback is_admin=True")
        return True
    return False


async def pre_tool_use_hook(input_data: dict, tool_use_id: str, context: dict) -> dict:
    """PreToolUse hook: RBAC enforcement + file protection + bash restrictions.

    Security layers (defense-in-depth):
    RBAC: Tool access governed by RBAC policies (authoritative)
    1a. Deploy tools require real admin (no system task bypass)
    1c.2. Chat isolation enforcement (argument validation)
    2. File paths checked against blocklist for credential/secret files
    3. Bash commands validated against blocklist of dangerous patterns
    4. Container isolation (Docker) is the ultimate boundary

    Note: Session ID → session_key registration is handled by build_hooks()
    wrapper closures (eager registration), not by this function.
    """
    bare_name = _extract_tool_name(input_data)
    tool_input = input_data.get("tool_input", {})
    state = _get_state_for_hook(input_data)

    # --- RBAC enforcement (authoritative) ---
    # RBAC is the sole tool-access enforcement layer. Legacy layers removed.
    # Subject resolved via _resolve_rbac_subject() — on-the-fly fallback from
    # current_user_ctx ensures role changes take effect without session reset.
    _rbac_decision = None
    try:
        rbac_subject = _resolve_rbac_subject(state)
        if rbac_subject:
            from bot.rbac import get_engine as _get_rbac_engine
            _rbac_engine = _get_rbac_engine()
            _rbac_decision = await _rbac_engine.check_access(
                subject=rbac_subject,
                privilege="use",
                object_type="tools",
                object_id=bare_name,
                session_key=_sid_to_skey.get(
                    input_data.get("session_id", "") if isinstance(input_data, dict) else "", ""
                ),
                tool_name=bare_name,
                log_decision=True,
            )
    except Exception as _rbac_err:
        log.error("RBAC check failed — all non-builtin tools denied: %s", _rbac_err)

    # RBAC enforcement: if RBAC denied, block immediately.
    if _rbac_decision is not None:
        if not _rbac_decision.allowed:
            log.warning("RBAC DENIED tool=%s reason=%s", bare_name, _rbac_decision.reason)
            return _deny(f"Access denied (RBAC): {_rbac_decision.reason}")
    else:
        # Fail-closed: if RBAC produced no decision (engine unavailable),
        # deny ALL tools except SDK builtins (ToolSearch, Skill) to prevent fail-open.
        _SDK_ALWAYS_ALLOWED = frozenset({"ToolSearch", "Skill"})
        if bare_name not in _SDK_ALWAYS_ALLOWED:
            log.error("RBAC: no decision for tool=%s — DENYING ALL (fail-closed)", bare_name)
            return _deny("Access denied: RBAC check unavailable — contact admin")
    # --- End RBAC enforcement ---

    # --- Layer 1b3: Credential access gating (allowed_credentials) ---
    # For Google Workspace, M365, and credential-bearing tools: check if the
    # session's harness allows access. Uses full tool name (not bare_name) for
    # prefix matching, since _extract_tool_name strips MCP namespace prefixes.
    _full_tool_name = input_data.get("tool_name", "") if isinstance(input_data, dict) else ""
    _GOOGLE_TOOL_PREFIX = "mcp__google-workspace__"
    _M365_TOOL_PREFIX = "mcp__ms365__"
    _CRED_BEARING_TOOLS = {"download_gmail_attachment"}  # tools with user_google_email param
    _is_cred_gated = (
        _full_tool_name.startswith(_GOOGLE_TOOL_PREFIX)
        or _full_tool_name.startswith(_M365_TOOL_PREFIX)
        or bare_name in _CRED_BEARING_TOOLS
    )
    if _is_cred_gated:
        try:
            # Resolve effective allowed_credentials: session state (includes domain creds) or harness.
            # Default-deny: start with [] (deny-all).
            _allowed_creds: list[str] = []
            _skey = _sid_to_skey.get(
                input_data.get("session_id", "") if isinstance(input_data, dict) else "", ""
            )
            if _skey:
                # Check ephemeral session state first (domain creds merged here by agent.py)
                from bot.core.session_state import _get_or_create_state as _gocs_hook
                _eph = _gocs_hook(_skey)
                _allowed_creds = _eph.get("effective_allowed_credentials") or []
                if not _allowed_creds:
                    # Fall back to harness definition + per-chat override
                    from bot.harness import get_harness_for_chat as _get_h, resolve_field as _rf_h
                    _h = _get_h(_skey)
                    _allowed_creds = _rf_h(_h, "allowed_credentials", _skey) or []
            else:
                log.warning("Layer 1b3: session key not resolved — using deny-all (fail-closed)")

            # Enforce allowed_credentials. ["*"] = unrestricted (admin only). [] = deny all.
            from bot.harness import is_credential_unrestricted as _is_cred_unr
            if not _is_cred_unr(_allowed_creds):
                # Admin bypass — but suppressed in external chats (same as Layer 2)
                _is_hook_admin = _is_rbac_admin(state) and not state.get("is_external_chat", False)
                if not _is_hook_admin:
                    # Check target email from tool args (download_gmail_attachment has it)
                    _target_email = tool_input.get("user_google_email", "")
                    _cred_type = "ms365" if _full_tool_name.startswith(_M365_TOOL_PREFIX) else "google-workspace"

                    if _target_email:
                        # Specific email targeted — check against allowlist.
                        # Use canonical prefix (google-workspace→google, ms365→m365).
                        from bot.auth.credentials import cred_id_prefix
                        _canonical_id = f"{cred_id_prefix(_cred_type)}:{_target_email}"
                        _full_id = f"{_cred_type}:{_target_email}"
                        if (_canonical_id not in _allowed_creds
                                and _full_id not in _allowed_creds
                                and _target_email not in _allowed_creds):
                            log.warning("Credential DENIED: tool=%s email=%s not in allowed_credentials=%s",
                                        bare_name, _target_email, _allowed_creds)
                            return _deny(f"Access denied: credential '{_target_email}' not in allowed_credentials")
                    else:
                        # No specific email — check if ANY matching credential is in allowlist.
                        # Check both canonical prefix (google:, m365:) and full type (google-workspace:, ms365:).
                        _canonical_prefix = f"{cred_id_prefix(_cred_type)}:"
                        _full_prefix = f"{_cred_type}:"
                        _has_matching = any(
                            c.startswith(_canonical_prefix) or c.startswith(_full_prefix)
                            for c in _allowed_creds
                        )
                        if not _has_matching:
                            log.warning("Credential DENIED: tool=%s no %s credentials in allowed_credentials=%s",
                                        bare_name, _cred_type, _allowed_creds)
                            return _deny(f"Access denied: no {_cred_type} credentials allowed for this chat")
        except Exception as _cred_err:
            # Fail-closed: if credential check errors, block the tool
            log.warning("Credential access check FAILED (denying): %s", _cred_err)
            return _deny("Access denied: credential access check failed")

    # --- Deploy tools require real admin — NO system task bypass ---
    # Uses live RBAC admin check — role changes take effect immediately.
    if bare_name in ("deploy_bot", "deploy_status"):
        if not _is_rbac_admin(state):
            ctx = state.get("current_user_ctx")
            log.warning(f"Deploy blocked: {bare_name} requires admin user, caller={ctx}")
            return _deny(f"Access denied: {bare_name} requires explicit admin approval in chat")

    # --- Layer 1a+ (FB-73): outbound action gate (/send command check) ---
    # send_gmail_message and phone_call require /send in user's CURRENT message.
    # Prevents model from showing a draft AND sending in the same query.
    _OUTBOUND_GATED_TOOLS = {"send_gmail_message", "send-mail", "create-draft-email", "phone_call"}
    if bare_name in _OUTBOUND_GATED_TOOLS:
        from bot.core.session_state import get_outbound_approved
        _osk = state.get("deploy_session_key_flag", "")  # session key stored by handler
        if not get_outbound_approved(_osk):
            # System tasks (scheduled emails) are exempt
            if not _osk.startswith("system:"):
                log.warning(f"Outbound BLOCKED: {bare_name} requires /send command (session={_osk})")
                return _deny(
                    f"OUTBOUND BLOCKED: {bare_name} requires the user to send the /send command "
                    "in a SEPARATE message to approve. Show the draft, then instruct: "
                    "\"Reply /send to send, or suggest edits.\""
                )

    # --- Layer 1a++: deploy_bot gate (/deploy command check) ---
    # Deploy requires the /deploy command in the user's message. Casual mentions
    # of "deploy" in conversation do NOT count (prevents false-positive deploys).
    # Variants: /deploy (all gates), /deploy no-qa (skip QA), /deploy force (skip ALL).
    if bare_name == "deploy_bot":
        skey = state.get("deploy_session_key_flag", "")
        confirmed = state.get("deploy_confirmed_flag", False)
        _cmd_force = state.get("deploy_force_flag", False)
        _cmd_noqa = state.get("deploy_noqa_flag", False)

        # /deploy command required
        if not confirmed:
            log.warning(f"Deploy BLOCKED: no /deploy command in user message (session={skey})")
            return _deny(
                "DEPLOY BLOCKED: The user must send the /deploy command to authorize deployment. "
                "Casual mentions of 'deploy' in conversation do NOT count. "
                "Commands: /deploy (all gates), /deploy no-qa (skip QA), /deploy force (skip all gates). "
                "Do NOT call deploy_bot until the user sends /deploy."
            )

        # /deploy force — skip ALL remaining hook-level gates
        if _cmd_force:
            log.warning(
                "Deploy gates FORCE-BYPASSED via /deploy force (session=%s)", skey,
            )
            # Fall through to defense-in-depth layers (RBAC already passed above)
        else:
            # --- Layer 1a++ (active sessions guard) ---
            action = tool_input.get("action", "rebuild")
            force = str(tool_input.get("force", "")).strip().lower() in ("true", "1", "yes")
            force_reason = (tool_input.get("force_reason") or "").strip()
            if not state.get("deploy_active_sessions_acked", False) and not (force and force_reason):
                from bot.pipeline.task_manager import task_manager
                current_task_id = state.get("current_task_id", "")
                others = [t for t in task_manager.get_active_tasks()
                          if t.task_id != current_task_id]
                if others:
                    state["deploy_active_sessions_acked"] = True
                    lines = [f"  \u2022 [{t.source}] task running for {t.elapsed()}"
                             for t in others]
                    task_list = "\n".join(lines)
                    log.warning(
                        "Deploy PAUSED: %d active session(s) running: %s (session=%s)",
                        len(others),
                        ", ".join(f"{t.source}:{t.chat_id}" for t in others),
                        skey,
                    )
                    return _deny(
                        f"DEPLOY PAUSED: {len(others)} active session(s) will be interrupted:\n"
                        f"{task_list}\n\n"
                        "Ask the admin to confirm they're OK interrupting these sessions, "
                        "then call deploy_bot again (this check won't block a second time)."
                    )

            # --- Layer 1a+++: deploy_review gate ---
            if force and force_reason:
                log.warning(
                    "Deploy gates FORCE-BYPASSED: reason=%s (session=%s)",
                    force_reason, skey,
                )
            elif action != "restart" and not state.get("deploy_review_passed", False):
                log.warning(
                    f"Deploy BLOCKED: deploy_review not run (session={skey})"
                )
                return _deny(
                    "DEPLOY BLOCKED: You must call the deploy_review tool BEFORE deploy_bot. "
                    "deploy_review runs mandatory automated checks (syntax, imports). "
                    "Emergency escape: /deploy force (skips all gates)."
                )

            # --- Layer 1a++++: staging QA gate ---
            # /deploy no-qa skips this gate only
            if not _cmd_noqa and not (force and force_reason) \
                    and action != "restart" \
                    and not state.get("qa_staging_passed", False):
                log.warning(
                    f"Deploy BLOCKED: staging QA not passed (session={skey})"
                )
                return _deny(
                    "DEPLOY BLOCKED: Staging QA has not passed. "
                    "Run staging_qa_run(regression=true) first, or use /deploy no-qa "
                    "to skip QA, or /deploy force to skip all gates."
                )

            # --- Layer 1a+++++: Pipeline Orchestrator gate (CB-1) ---
            # Independent verification must have run and approved.
            # The orchestrator_approval_token is HMAC-signed and verified
            # against current HEAD to prevent TOCTOU attacks.
            if action != "restart" and not (force and force_reason) \
                    and state.get("modified_repo_files"):
                _orch_token = state.get("orchestrator_approval_token", "")
                if not _orch_token:
                    log.warning("Deploy BLOCKED: no orchestrator approval (session=%s)", skey)
                    return _deny(
                        "DEPLOY BLOCKED: Pipeline Orchestrator has not approved this change.\n"
                        "Call pipeline_orchestrate(ticket_id) after completing all pipeline stages.\n"
                        "The orchestrator independently verifies your work before deploy.\n"
                        "Emergency escape: /deploy force (skips all gates)."
                    )
                # Verify HMAC token against current commit
                try:
                    # CB-141: Use async subprocess to avoid blocking event loop
                    _head_proc = await asyncio.create_subprocess_exec(
                        "git", "rev-parse", "HEAD",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        cwd="/host-repo",
                    )
                    try:
                        _head_out, _ = await asyncio.wait_for(_head_proc.communicate(), timeout=5)
                    except asyncio.TimeoutError:
                        _head_proc.kill()
                        raise
                    _current_commit = _head_out.decode().strip()
                    from bot.pm.orchestrator import verify_approval
                    _valid, _reason = verify_approval(_orch_token, _current_commit)
                    if not _valid:
                        log.warning("Deploy BLOCKED: orchestrator token invalid — %s", _reason)
                        return _deny(
                            f"DEPLOY BLOCKED: Orchestrator approval invalid — {_reason}\n"
                            "Re-run pipeline_orchestrate(ticket_id) to re-verify.\n"
                            "Emergency escape: /deploy force."
                        )
                except Exception as _orch_err:
                    log.warning("Deploy BLOCKED: orchestrator verification error: %s", _orch_err)
                    return _deny(
                        f"DEPLOY BLOCKED: Orchestrator verification failed — {_orch_err}\n"
                        "Re-run pipeline_orchestrate(ticket_id), or /deploy force."
                    )

            # --- Layer 1a++++++: PM pipeline gates (CB-30) ---
            # All bot code changes MUST have a PM ticket. Deploy is blocked if:
            # (a) code was modified but no ticket exists, or
            # (b) ticket exists but pipeline gates are incomplete, or
            # (c) ticket is not in review/done status (flow not finished).
            if action != "restart" and not (force and force_reason):
                _active_tid = state.get("active_ticket_id")
                _has_repo_edits = bool(state.get("modified_repo_files"))

                # (a) No ticket but code was changed → block
                if not _active_tid and _has_repo_edits:
                    log.warning("Deploy BLOCKED: code changes detected but no PM ticket")
                    return _deny(
                        "DEPLOY BLOCKED: Code changes detected in /host-repo/ but no PM ticket.\n"
                        "All bot changes require a ticket. Call pm_auto_ticket() first, "
                        "or use /deploy force to skip all gates."
                    )

                if _active_tid:
                    try:
                        from bot.pm.store import get_ticket
                        _ticket = await get_ticket(_active_tid)
                        if _ticket:
                            # (c) Ticket must be in review or done (flow finished)
                            _t_status = _ticket.get("status", "")
                            if _t_status not in ("review", "done"):
                                log.warning(
                                    "Deploy BLOCKED: ticket %s status=%s (must be review/done)",
                                    _active_tid, _t_status,
                                )
                                return _deny(
                                    f"DEPLOY BLOCKED: PM ticket #{_active_tid} is in '{_t_status}' status.\n"
                                    "Ticket must be in 'review' or 'done' before deploying.\n"
                                    "Move it via pm_move_ticket, or use /deploy force to skip all gates."
                                )

                            # (b) Check pipeline gates in TCD
                            if _ticket.get("tcd"):
                                import json as _json
                                _tcd = _json.loads(_ticket["tcd"]) if isinstance(_ticket["tcd"], str) else _ticket["tcd"]
                                from bot.pm.tcd import check_gates_for_deploy, format_gate_summary
                                _gates_ok, _missing = check_gates_for_deploy(_tcd)
                                if not _gates_ok:
                                    _summary = format_gate_summary(_tcd)
                                    _missing_str = "\n".join(f"  \u274c {m}" for m in _missing)
                                    log.warning(
                                        "Deploy BLOCKED: PM pipeline gates incomplete for ticket %s: %s",
                                        _active_tid, _missing,
                                    )
                                    return _deny(
                                        f"DEPLOY BLOCKED: PM ticket #{_active_tid} has incomplete pipeline gates:\n"
                                        f"{_missing_str}\n\n"
                                        f"Current gate status:\n{_summary}\n\n"
                                        "Complete all required stages and record them via mark_gate() + pm_update_ticket, "
                                        "or use /deploy force to skip all gates."
                                    )
                    except Exception as _pm_err:
                        # Non-blocking: if PM DB is unavailable, don't block deploy
                        log.warning("PM pipeline gate check failed (non-blocking): %s", _pm_err)

    # --- Layer 1a+: extend_budget gate (keyword check) ---
    # Same pattern as deploy — tool is admin-only (enforced by Layer 1b)
    # AND requires the user to have explicitly said "extend budget".
    # `extend_budget_confirmed_flag` is set by the query dispatcher when
    # check_extend_budget_approval() returns True on the user's message.
    # SINGLE-SHOT: flag is consumed (reset to False) on first pass through
    # this gate so the model cannot invoke extend_budget multiple times in
    # one turn. A user extension request = exactly one extension.
    if bare_name == "extend_budget":
        if not state.get("extend_budget_confirmed_flag", False):
            log.warning(
                "extend_budget BLOCKED: no 'extend budget' keyword in user message "
                f"(or already consumed this turn; session={state.get('deploy_session_key_flag', '')})"
            )
            return _deny(
                "EXTEND BUDGET BLOCKED: This tool is invocable ONLY ONCE per user message, "
                "and only when the user's current message contains the explicit phrase "
                "'extend budget' (or 'increase budget', or 'продли бюджет'). Never "
                "auto-invoke. Show the budget status to the user and wait for them to "
                "explicitly ask for an extension."
            )
        # Consume the flag — prevents N-invocations per turn. Next
        # set_extend_budget_state() call (next query) re-evaluates fresh.
        state["extend_budget_confirmed_flag"] = False

    # --- External chat detection ---
    is_external = state.get("is_external_chat", False)
    _sandbox_dir = state.get("sandbox_dir")

    # --- Resolve session_key from _sid_to_skey (needed by Layer 2+) ---
    _hook_sid = input_data.get("session_id", "") if isinstance(input_data, dict) else ""
    session_key = _sid_to_skey.get(_hook_sid, "") if _hook_sid else ""
    # Fail-closed: if we have a session_id but can't resolve it, deny rather
    # than skip — prevents bypass of defense-in-depth layers.
    if not session_key and _hook_sid:
        log.warning(f"Session mapping: session_id {_hook_sid!r} not in _sid_to_skey "
                    f"— denying {bare_name} (fail-closed)")
        return _deny(f"Tool {bare_name} blocked: session mapping unavailable")

    # --- Layer 1c.2: Chat isolation enforcement (ALWAYS active) ---
    # Tool access is governed by RBAC above; chat isolation validates arguments
    # to ensure tools target the correct chat (defense-in-depth).
    if is_external:
        chat_id_str = state.get("origin_chat_id", "")

        # Tools that target a specific chat must only target the session's own chat.
        # Prevents cross-chat messaging, editing, and data access from external chats.
        # A prompt-injected model must not be able to message the primary group or read other chats.
        # FAIL-CLOSED: if the target param is OMITTED, deny — tool handlers default
        # to TG_CHAT_ID (primary group), which is NOT the external chat's own ID.
        if bare_name in _CHAT_ISOLATED_TOOLS:
            target_params = _CHAT_ISOLATED_TOOLS[bare_name]
            for param_name in target_params:
                target_val = tool_input.get(param_name)
                if target_val is None:
                    # Parameter omitted — tool handler would default to primary chat.
                    # Deny to prevent silent cross-chat messaging.
                    log.warning(
                        f"SECURITY: External chat isolation — "
                        f"tool={bare_name} param={param_name} missing "
                        f"(would default to primary chat); origin={chat_id_str}"
                    )
                    return _deny(
                        f"Access denied: {bare_name} requires explicit {param_name} "
                        f"matching this chat ({chat_id_str}). "
                        f"External chats cannot use default chat targets."
                    )
                # Normalize both to string for comparison (chat_id may be int or str)
                if str(target_val).strip() != chat_id_str:
                    log.warning(
                        f"SECURITY: External chat isolation violation — "
                        f"tool={bare_name} param={param_name} "
                        f"target={target_val} origin={chat_id_str}"
                    )
                    return _deny(
                        f"Access denied: {bare_name} can only target this chat "
                        f"(got {param_name}={target_val}, expected {chat_id_str}). "
                        f"External chats cannot send to or access other chats."
                    )

    # --- Layer 2: File path protection ---
    # Use RBAC to determine admin status for file-path tier selection.
    _is_admin = _is_rbac_admin(state) and not is_external

    if bare_name in ("Write", "Edit", "Read", "NotebookEdit"):
        file_path = tool_input.get("file_path", "") or tool_input.get("notebook", "")
        if _sandbox_dir:
            # Sandbox Read allowlist: allow Read of browser screenshots and temp files
            # that land outside sandbox boundary (e.g. Camofox/Playwright screenshots
            # in /app/data/tmp/). Write/Edit are NOT allowed outside sandbox.
            if bare_name == "Read" and file_path and _is_sandbox_safe_read_path(file_path):
                denial = None  # safe media dir — skip sandbox containment check
            else:
                # Coding sandbox: must be within sandbox_dir (admin + non-admin)
                denial = _check_sandbox_path(file_path, _sandbox_dir)
        else:
            denial = _check_file_path(file_path, is_write=(bare_name != "Read"),
                                      is_admin=_is_admin)
        if denial:
            log.warning(f"Blocked file access: {bare_name} → {file_path}")
            return _deny(denial)
        # Size guard for Read — prevents SDK 1MB JSON buffer crash (FB-50).
        # Files >500KB base64-encode to >667KB which can exceed the 1MB buffer.
        # All chat tiers protected (admin too — crash affects the bot process).
        if bare_name == "Read" and file_path:
            try:
                _fsize = os.path.getsize(file_path)
                _MAX_READ_BYTES = 500 * 1024  # 500KB — matches MEDIA_INLINE_READ_MAX_KB
                if _fsize > _MAX_READ_BYTES:
                    log.warning("Read size guard: %s is %dKB (>500KB limit)",
                                file_path, _fsize // 1024)
                    return _deny(
                        f"File too large for Read ({_fsize // 1024}KB, limit 500KB). "
                        f"Use Bash to examine: head, tail, wc, or split the file."
                    )
            except OSError:
                pass  # file doesn't exist — SDK will handle gracefully

    # --- Layer 2b: File-send path protection ---
    # Validates file paths for send tools (send_photo, send_document, etc.)
    # to prevent file exfiltration via file-send tools. Tier-gated:
    # sandbox/external use allowlists, member/admin use blocklists.
    if bare_name in _FILE_SEND_PATH_PARAMS:
        _send_path_param = _FILE_SEND_PATH_PARAMS[bare_name]
        _send_file_path = tool_input.get(_send_path_param, "")
        if _send_file_path:
            denial = _check_file_send_path(
                _send_file_path,
                is_admin=_is_admin,
                sandbox_dir=_sandbox_dir,
                is_external=is_external,
            )
            if denial:
                log.warning("Blocked file-send path: %s param=%s path=%s",
                            bare_name, _send_path_param, _send_file_path)
                return _deny(denial)

    # Grep/Glob path protection
    if bare_name in ("Grep", "Glob"):
        search_path = tool_input.get("path", "")
        if _sandbox_dir and not search_path:
            # No path = defaults to cwd (/app) — would bypass sandbox
            return _deny(f"Access denied: {bare_name} requires an explicit path "
                         f"within your project directory ({_sandbox_dir})")
        if search_path:
            if _sandbox_dir:
                denial = _check_sandbox_path(search_path, _sandbox_dir)
            elif not _is_admin:
                denial = _check_file_path(search_path, is_write=False, is_admin=False)
            else:
                denial = None  # Admin without sandbox: unrestricted Grep/Glob
            if denial:
                log.warning(f"Blocked search path: {bare_name} → {search_path}")
                return _deny(denial)

    # --- Layer 3: Bash command restrictions ---
    if bare_name == "Bash":
        command = tool_input.get("command", "")
        if _sandbox_dir:
            # Coding sandbox: strict containment (admin + non-admin)
            denial = _check_sandbox_bash(command, _sandbox_dir)
        else:
            denial = _check_bash_command(command, is_admin=_is_admin)
        if denial:
            log.warning(f"Blocked bash command: {command[:100]}")
            return _deny(denial)

        # --- Layer 3b: Bubblewrap kernel-level sandbox (coding sessions only) ---
        # After regex checks pass, wrap the command in bwrap for defense-in-depth.
        # This prevents script-based escapes (python3 script.py that reads /proc,
        # accesses bot source, or uses urllib for exfiltration).
        if _sandbox_dir:
            # R4: no_network flag from RBAC execution scope (not harness)
            _rbac_exec = state.get("rbac_execution", {})
            _no_net = _rbac_exec.get("network") == "none"
            bwrap_input = _wrap_command_in_bwrap(command, _sandbox_dir, no_network=_no_net)
            if bwrap_input is not None:
                return _allow_with_updated_input(bwrap_input)
            # bwrap unavailable — DENY rather than fall through to regex-only.
            # Regex alone has known bypasses (script files, encoded commands,
            # perl/ruby). Kernel-level containment is required for sandbox chats.
            log.error("Sandbox Bash DENIED: bwrap unavailable for sandbox_dir=%s", _sandbox_dir)
            return _deny("Sandbox Bash blocked: bubblewrap (bwrap) is required but unavailable. "
                         "Contact admin to install bubblewrap in the container.")

    # --- Browser Pool: budget check + eviction on create_tab ---
    # bare_name is the LAST segment after __ split (e.g. "camofox_create_tab" or "create_tab")
    # Use full tool_name for browser type detection (contains "camofox" or "playwright")
    # Also check browser_navigate — Playwright auto-creates a tab on first navigate,
    # which bypasses create_tab. Budget check needed for implicit tab creation.
    _is_tab_creation = bare_name in ("create_tab", "camofox_create_tab")
    if not _is_tab_creation and bare_name in ("browser_navigate", "navigate", "navigate_and_snapshot"):
        try:
            from bot.browser_pool import get_pool
            _pool = get_pool()
            _full_tn = input_data.get("tool_name", "")
            _pre_skey = _sid_to_skey.get(input_data.get("session_id", ""), "")
            if _pre_skey and "playwright" in _full_tn and not _pool.get_entries_for_session(
                _pre_skey, browser="playwright"
            ):
                _is_tab_creation = True  # first navigate = implicit tab creation
        except Exception as e:
            log.debug("BrowserPool PreToolUse navigate check error: %s", e)
    if _is_tab_creation:
        try:
            from bot.browser_pool import get_pool, detect_tier, close_camofox_tab, TIER_LRU, TIER_IMMUNE
            pool = get_pool()
            full_tool_name = input_data.get("tool_name", "")
            browser = "camofox" if "camofox" in full_tool_name or "camofox" in bare_name else "playwright"
            tier = detect_tier(state)
            if pool.is_at_capacity(browser, tier):
                if tier == TIER_LRU:
                    # Evict oldest LRU entry to make room
                    evicted = await pool.evict_oldest(browser)
                    if evicted and evicted.browser == "camofox":
                        await close_camofox_tab(evicted.entry_id)
                    # Playwright eviction: subprocess dies when session resets —
                    # we just deregister here, actual cleanup is deferred
                elif tier == TIER_IMMUNE:
                    # Hard cap even for immune tier to prevent resource exhaustion.
                    # Admin should close_tab before creating new ones.
                    log.warning("BrowserPool: immune tier at hard cap for %s (%d slots)",
                                browser, pool.count(browser, TIER_IMMUNE))
                    return _deny(
                        f"Browser tab limit reached ({pool.count(browser, TIER_IMMUNE)} "
                        f"immune tabs for {browser}). Close unused tabs with close_tab "
                        f"before creating new ones."
                    )
        except Exception as e:
            log.warning("BrowserPool PreToolUse error: %s", e, exc_info=True)

    return {}


async def post_tool_use_hook(input_data: dict, tool_use_id: str, context: dict) -> dict:
    """PostToolUse hook: track send tools + emit status updates + turn-limit nudges + parallel session awareness."""
    bare_name = _extract_tool_name(input_data)
    state = _get_state_for_hook(input_data)

    # Resolve session_key for features that need it (nudges, file tracking).
    session_id = input_data.get("session_id", "") if isinstance(input_data, dict) else ""
    session_key = _sid_to_skey.get(session_id, "") if session_id else ""

    # Track if a send tool was called
    if bare_name in SEND_TOOLS:
        state["send_tool_called"] = True

    # Buffer non-send tool calls on staging for expect_tools QA assertions.
    # Uses staging_buffer_tool_call (no completion signal) so /test/inject
    # keeps waiting for the final send_message before returning results.
    # Send tools are already buffered by their own MCP handlers — skip them here.
    if bare_name not in SEND_TOOLS:
        from config import IS_STAGING
        if IS_STAGING:
            staging_chat_id = str(state.get("origin_chat_id", ""))
            if staging_chat_id:
                from bot.pipeline.staging import staging_buffer_tool_call
                staging_buffer_tool_call(staging_chat_id, bare_name, input_data.get("input", {}))

    # Accumulator for system notes injected after the tool result.
    # Multiple features (collision warnings, turn-limit nudges) can contribute;
    # they are joined and returned as a single additionalContext string.
    additional_contexts: list[str] = []

    # Reset deploy gates + track parallel session files when code is edited in /host-repo/.
    # Forces re-review + re-QA before next deploy after any code change.
    # Covers Edit, Write, and Bash (which can also modify repo files).
    if bare_name in ("Edit", "Write"):
        tool_input = input_data.get("input", {})
        file_path = tool_input.get("file_path", "")
        if file_path.startswith("/host-repo/"):
            # Deploy gate reset
            for flag in _DEPLOY_GATE_FLAGS:
                if state.get(flag):
                    # Use type-appropriate default (string flags → "", bool → False)
                    state[flag] = _DEFAULT_STATE.get(flag, False)
                    log.info(f"{flag} RESET: {bare_name} on {file_path}")
            # Parallel session awareness: record file + check conflicts
            if session_key:
                record_repo_file(session_key, file_path)
                conflicts = check_repo_conflicts(session_key, file_path)
                if conflicts:
                    other = ", ".join(conflicts)
                    additional_contexts.append(
                        f"\u26a0\ufe0f PARALLEL SESSION CONFLICT: {file_path} is also being "
                        f"modified by another coding session ({other}). Coordinate before "
                        f"committing \u2014 the other session's changes may be overwritten."
                    )
                    log.warning("File collision: %s edited by %s, also modified by %s",
                                file_path, session_key, other)
    elif bare_name == "Bash":
        cmd = input_data.get("input", {}).get("command", "")
        # Reset on ANY Bash touching /host-repo/ — false positive resets are
        # cheap (just re-run), but missing a modification is not.
        if "/host-repo/" in cmd:
            for flag in _DEPLOY_GATE_FLAGS:
                if state.get(flag):
                    state[flag] = _DEFAULT_STATE.get(flag, False)
                    log.info(f"{flag} RESET: Bash touching /host-repo/")
            # Track repo modification for PM mandatory ticket gate (CB-30).
            # Bash edits (sed, echo, cp, etc.) must be tracked same as Edit/Write.
            if session_key:
                record_repo_file(session_key, "/host-repo/ (via Bash)")

    # --- Browser Pool: register/deregister/touch on browser tool results ---
    # bare_name after _extract_tool_name: "camofox_navigate", "browser_click", "create_tab", etc.
    # Use full tool_name for browser type detection (contains "camofox" or "playwright")
    _is_browser_tool = (
        bare_name.startswith("camofox_") or bare_name.startswith("browser_")
        or bare_name in ("create_tab", "close_tab", "list_tabs",
                         "navigate", "snapshot", "screenshot", "click",
                         "navigate_and_snapshot", "scroll_and_snapshot")
    )
    if _is_browser_tool and session_key:
        try:
            from bot.browser_pool import get_pool, detect_tier
            pool = get_pool()
            full_tool_name = input_data.get("tool_name", "")
            browser = "camofox" if ("camofox" in full_tool_name or "camofox" in bare_name) else "playwright"
            tool_response = input_data.get("tool_response")

            if bare_name in ("create_tab", "camofox_create_tab"):
                # Parse tab_id from response and register
                tab_id = _extract_tab_id_from_response(tool_response)
                if tab_id:
                    tier = detect_tier(state)
                    url = input_data.get("tool_input", {}).get("url", "")
                    await pool.register(tab_id, session_key, browser, tier, url)
            elif bare_name in ("close_tab", "camofox_close_tab"):
                tab_id = input_data.get("tool_input", {}).get("tabId", "")
                if tab_id:
                    if pool.owns(tab_id, session_key):
                        await pool.deregister(tab_id)
                    else:
                        log.warning("BrowserPool: session %s tried to close tab %s it doesn't own",
                                    session_key[:30], tab_id[:12])
            else:
                # Touch last_used_at for any browser interaction
                tab_id = input_data.get("tool_input", {}).get("tabId", "")
                if tab_id and pool.owns(tab_id, session_key):
                    await pool.touch(tab_id)
                # Register Playwright tabs created implicitly by browser_navigate.
                # Playwright auto-creates a tab on navigate if none exists — this bypasses
                # create_tab, leaving the pool blind. Register a synthetic entry so budget,
                # LRU eviction, and session cleanup all work correctly.
                elif browser == "playwright" and not pool.get_entries_for_session(session_key, browser="playwright"):
                    tier = detect_tier(state)
                    url = input_data.get("tool_input", {}).get("url", "") or input_data.get("input", {}).get("url", "")
                    auto_id = "pw_auto_" + re.sub(r"[^a-zA-Z0-9_-]", "_", session_key)
                    await pool.register(auto_id, session_key, "playwright", tier, url)
                    log.info("BrowserPool: registered Playwright tab %s for %s (implicit navigate)", auto_id, session_key[:30])
        except Exception as e:
            log.warning("BrowserPool PostToolUse error: %s", e, exc_info=True)

    # Emit tool status label (for streaming placeholder updates)
    if bare_name in TOOL_LABELS and bare_name not in SEND_TOOLS:
        callback = state.get("tool_status_callback")
        if callback:
            try:
                await callback([bare_name])
            except Exception as e:
                log.warning(f"Tool status callback error: {e}")

    # ── Turn-limit nudge injection ──────────────────────────────────
    # Increment tool use counter and inject warnings as Claude approaches the limit.
    # Thresholds are dynamic — computed from per-chat harness max_turns (80%/90%).
    state["tool_use_counter"] = state.get("tool_use_counter", 0) + 1
    count = state["tool_use_counter"]

    nudge_warning, nudge_critical = _get_nudge_thresholds(session_key or None)

    # (0, 0) = unlimited mode — skip nudges entirely
    if nudge_warning > 0 or nudge_critical > 0:
        effective_max = nudge_critical + (nudge_critical - nudge_warning)
        if count == nudge_critical:
            log.warning(f"Turn limit CRITICAL nudge at tool call #{count}/{effective_max}")
            additional_contexts.append(
                f"\n\n\u26a0\ufe0f HARD STOP \u2014 TURN LIMIT \u26a0\ufe0f\n"
                f"You have used {count} of ~{effective_max} tool calls. You MUST send your reply NOW.\n"
                "Do NOT make any more research/analysis calls.\n"
                "Use your remaining turns ONLY for send_message.\n"
                "If work is incomplete, send partial results and tell the user "
                "to reply 'continue'."
            )
        elif count == nudge_warning:
            log.info(f"Turn limit WARNING nudge at tool call #{count}/{effective_max}")
            additional_contexts.append(
                f"\n\n\u26a0\ufe0f TURN LIMIT WARNING: You have used {count} of ~{effective_max} tool calls. "
                "Start wrapping up NOW. Finish your current task and send the reply. "
                "If you need more turns, send partial results first and ask the user "
                "to reply 'continue'."
            )

    # ── Elapsed-time heartbeat nudge ──────────────────────────────────
    # Prompt the model to send a progress update every 10 minutes.
    # No cap — coding sessions run indefinitely. Complements the existing
    # _task_checkpoint_loop (bot-generated messages) with model-generated
    # contextual updates during active tool chains.
    _HEARTBEAT_INTERVAL_S = 600  # 10 minutes
    _q_start = state.get("query_start_time", 0.0)
    if _q_start > 0:
        import time as _time_mod
        _elapsed_s = _time_mod.time() - _q_start
        _hb_count = state.get("heartbeat_count", 0)
        _next_hb_at = _HEARTBEAT_INTERVAL_S * (_hb_count + 1)
        if _elapsed_s >= _next_hb_at:
            state["heartbeat_count"] = _hb_count + 1
            _elapsed_min = int(_elapsed_s // 60)
            log.info(f"Heartbeat nudge #{_hb_count + 1} at {_elapsed_min}m elapsed")
            additional_contexts.append(
                f"\n\n\u23f0 TIME CHECK ({_elapsed_min} min elapsed): "
                "Send a BRIEF progress update to the user (1-2 sentences: "
                "what you've done so far, what remains). "
                "Then CONTINUE your current task \u2014 do NOT stop working."
            )

    # Return combined additionalContext (if any)
    if additional_contexts:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "\n".join(additional_contexts),
            }
        }
    return {}


async def pre_compact_hook(input_data, tool_use_id, context):
    """Hook called before SDK compacts the conversation context.

    Records pre-compaction token count for the compaction notification.
    SDK calls all hooks with (input_data, tool_use_id, context) — must match signature.
    """
    session_id = input_data.get("session_id", "")
    trigger = input_data.get("trigger", "auto")

    # Resolve session_key from session_id (set by agent.py via register_session_mapping)
    session_key = _sid_to_skey.get(session_id, "")

    # Pre-compaction tokens: JSONL only (single source of truth).
    # Reads the last assistant entry's per-call usage from the JSONL transcript.
    # If JSONL is unavailable, log warning and skip — no fallback.
    from bot.agent import _read_jsonl_context_tokens
    jsonl_tokens = _read_jsonl_context_tokens(session_id) if session_id else 0

    log.info(f"PreCompact hook: session={session_id[:12]}... key={session_key} "
             f"trigger={trigger} pre_tokens={jsonl_tokens // 1000}k (jsonl)")

    if not jsonl_tokens:
        log.warning(f"PreCompact: JSONL unavailable for session_id={session_id[:12]}... "
                    f"— compaction notification will be skipped")

    if session_key:
        pending_compaction[session_key] = {
            "approx_tokens": jsonl_tokens,
            "trigger": trigger,
            "started_at": time.time(),  # Ticker uses this to update TG placeholder
        }
    else:
        log.warning(f"PreCompact: no session_key mapping for session_id={session_id[:12]}...")

    return {}  # allow compaction to proceed


def build_hooks(session_key: str = "") -> dict:
    """Build the hooks dict for ClaudeAgentOptions.

    When session_key is provided, returns wrapper closures that eagerly register
    session_id → session_key in _sid_to_skey before calling the real hook.
    This ensures hooks always resolve to the correct session state, even when
    the SDK fires hooks BEFORE yielding the SystemMessage with session_id.

    Without session_key (empty string), hooks are returned as-is (backward compat).
    """
    try:
        from claude_agent_sdk import HookMatcher
    except ImportError as e:
        log.error(f"CRITICAL: failed to import hooks dependency: {e}")
        return {}

    if not session_key:
        # All callers should pass session_key — warn if missing
        log.warning("build_hooks() called without session_key — hooks will use "
                     "detached defaults (admin checks deny, safe but degraded)")
        return {
            "PreToolUse": [HookMatcher(hooks=[pre_tool_use_hook])],
            "PostToolUse": [HookMatcher(hooks=[post_tool_use_hook])],
            "PreCompact": [HookMatcher(hooks=[pre_compact_hook])],
        }

    # Per-client wrapper closures that capture session_key
    def _make_wrapper(real_hook):
        """Create a wrapper that eagerly registers session_id → session_key."""
        async def _wrapper(input_data, tool_use_id, context):
            try:
                if not isinstance(input_data, dict):
                    log.warning(f"Hook wrapper: unexpected input_data type: {type(input_data).__name__}")
                sid = input_data.get("session_id", "") if isinstance(input_data, dict) else ""
                if sid and sid not in _sid_to_skey:
                    register_session_mapping(sid, session_key)
                    log.debug(f"Hook wrapper: eager-registered {sid[:12]}... → {session_key[:30]}")
            except Exception:
                # Never let registration failure block the hook
                log.warning(f"Hook wrapper: registration error for {session_key[:30]}", exc_info=True)
            return await real_hook(input_data, tool_use_id, context)
        return _wrapper

    return {
        "PreToolUse": [HookMatcher(hooks=[_make_wrapper(pre_tool_use_hook)])],
        "PostToolUse": [HookMatcher(hooks=[_make_wrapper(post_tool_use_hook)])],
        "PreCompact": [HookMatcher(hooks=[_make_wrapper(pre_compact_hook)])],
    }
