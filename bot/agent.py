# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Core AI agent — Claude Agent SDK wrapper with ClientPool for multi-chat sessions.

Leverages SDK's native session management, context compaction, tool execution loop,
streaming, and adaptive thinking.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time

import anyio

# Increase SDK initialize timeout from default 60s to 180s (3 min).
# With 6+ external MCP servers, cold-start init can exceed 60s.
# SDK reads this env var in client.py → QueryRunner(initialize_timeout=...).
os.environ.setdefault("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT", "180000")
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable

from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    AssistantMessage, ResultMessage, SystemMessage,
)
from claude_agent_sdk.types import (
    TextBlock, ThinkingBlock, ToolUseBlock, RateLimitEvent,
    TaskStartedMessage, TaskProgressMessage, TaskNotificationMessage,
)

from config import (
    SESSION_TIMEOUT, SESSION_FILE_RETENTION, MAX_TURNS,
    CONNECT_MAX_RETRIES, CONNECT_BACKOFF_BASE,
    CLAUDE_CODE_OAUTH_TOKEN, CLAUDE_CREDENTIALS_PATH,
    MODEL_PRIMARY, get_model_primary, get_model_for_chat, get_effort_level, is_admin_user,
    MAX_BUDGET_DEFAULT, MAX_BUDGET_HEAVY,
    QUERY_OUTPUT_CAP_DEFAULT, QUERY_OUTPUT_CAP_HEAVY,
    RATE_LIMIT_WARN_PCT, RATE_LIMIT_ALERT_PCT, RATE_LIMIT_ALERT_COOLDOWN_S,
    QUERY_COST_WARN,
    SYSTEM_PROMPT_MAX_BYTES,
)
from bot.storage.db import open_db
from bot.hooks import (
    build_hooks, set_user_context, register_session_mapping,
    get_send_tool_called, set_send_tool_called,
    set_intentional_silence,
    set_tool_status_callback, set_tool_use_counter,
    set_deploy_state, check_deploy_approval, check_deploy_force, check_deploy_noqa,
    check_outbound_approval,
    set_extend_budget_state, check_extend_budget_approval,
    set_access_context,
    _get_or_create_state,
    SEND_TOOLS,
)
from bot.mcp_tools import get_custom_mcp_servers
from bot.mcp_config import get_external_mcp_servers
from bot.prompts import build_system_prompt, build_volatile_header

log = logging.getLogger(__name__)


# Garbage responses that Claude sometimes produces instead of actual replies
_GARBAGE_RESPONSES = {"no response requested", "no response requested.", "no response needed",
                      "no response needed.", "n/a", "none", "skip"}

# Language detection (extracted to bot/core/language.py)
from bot.core.language import (
    LANG_NAMES as _LANG_NAMES,
    _detect_language_with_fallback, _get_silence_patterns,
)

# Suspicious response detection (extracted to bot/core/safety.py)
from bot.core.safety import _check_suspicious_response, _notify_suspicious_response

# FACTS_UPDATE extraction and persistence (extracted to bot/core/fact_persistence.py)
from bot.core.fact_persistence import (
    strip_facts_update,
    _persist_memory_updates,
)

# Rate limit detection: SDK returns this as normal ResultMessage.result text
_RATE_LIMIT_PATTERNS = re.compile(
    r"out of extra usage|rate.limit|you.?re out of|usage.*resets?",
    re.IGNORECASE,
)


@dataclass
class ManagedClient:
    """Wraps a ClaudeSDKClient with lifecycle metadata."""
    client: ClaudeSDKClient
    session_key: str
    session_id: str | None = None
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    query_count: int = 0
    is_fresh_session: bool = False  # True if this session was just created (not resumed)
    last_usage: dict | None = None  # Last ResultMessage.usage for token tracking
    # Session-cumulative cost. Updated from every ResultMessage.total_cost_usd
    # (which the SDK reports as the running session total, NOT per-query).
    # Injected into LIVE CONTEXT so the model always sees accrued spend
    # alongside the cap (from _session_budget_overrides / defaults) — prevents
    # "I lied about budget" incidents when the model estimates remaining.
    session_cost_usd: float = 0.0
    # Cap actually baked into the running CLI subprocess. Set at _build_options
    # time (the max_budget_usd arg). LIVE CONTEXT reads this so the displayed
    # cap matches the SDK's real cap — previously re-deriving from `model` per
    # query returned stale/wrong values when model changed mid-session or the
    # session was resumed with a cap derived from a different model variant.
    max_budget_cap_usd: float = 0.0
    # Fact delta hashing: skip sending full facts when unchanged between queries.
    # After compaction or first query, hash is empty → full facts sent.
    last_facts_hash: str = ""
    # checkpoint_session_id removed — fork mechanism deprecated (sequential per-chat model)
    project_cwd: str = ""  # cwd from opts, stored for per-query .netrc writes (avoids private API)
    max_turns_cap: int = 0  # 0 = unlimited; stored at build time to avoid reading private SDK API


def _check_github_credential_allowed(session_key: str, cred_id: str) -> bool:
    """Check if a GitHub credential is allowed for this session.

    Returns True if allowed, False if denied. Defaults to DENY on errors
    (fail-closed — prevents accidental admin-level access on config errors).
    """
    try:
        from bot.harness import get_harness_for_chat, resolve_field, is_credential_unrestricted
        from bot.auth.credentials import normalize_cred_id
        _h = get_harness_for_chat(session_key)
        _allowed = resolve_field(_h, "allowed_credentials", session_key)
        if _allowed and is_credential_unrestricted(_allowed):
            return True  # admin harness: unrestricted (wildcard)
        if not cred_id or not _allowed:
            return False
        _normalized = normalize_cred_id(cred_id)
        return (cred_id in _allowed
                or _normalized in _allowed
                or any(normalize_cred_id(c) == _normalized for c in _allowed))
    except Exception as e:
        log.debug("GitHub credential check failed (denying): %s", e)
        return False  # fail-closed on errors


def _is_github_system_token_allowed(session_key: str) -> bool:
    """Check if the system GITHUB_TOKEN is allowed for this session.

    Requires either admin harness (wildcard ["*"]) or explicit 'github:system' in
    allowed_credentials. Having 'github:<personal_user>' does NOT grant
    access to the system token — prevents privilege escalation.
    """
    try:
        from bot.harness import get_harness_for_chat, resolve_field, is_credential_unrestricted
        from bot.auth.credentials import normalize_cred_id
        _h = get_harness_for_chat(session_key)
        _allowed = resolve_field(_h, "allowed_credentials", session_key)
        if _allowed and is_credential_unrestricted(_allowed):
            return True  # admin: unrestricted (wildcard)
        if not _allowed:
            return False
        return "github:system" in _allowed or normalize_cred_id("github:system") in _allowed
    except Exception:
        return False  # fail-closed


def _write_github_file_auth(auth_dir: str, token: str) -> bool:
    """Write .netrc + gh hosts.yml for file-based GitHub auth.

    Returns True if files were written/updated, False if skipped (no change).
    Uses atomic tmp+rename with 0o600 permissions.
    """
    import os
    from pathlib import Path
    _dir = Path(auth_dir)
    _netrc = _dir / ".netrc"
    _content = f"machine github.com\nlogin x-access-token\npassword {token}\n"
    # Skip if unchanged
    try:
        if _netrc.exists() and _netrc.read_text() == _content:
            return False
    except Exception as _exc:
        log.debug("Suppressed: %s", _exc)
    _tmp = _netrc.with_suffix(".tmp")
    _fd = os.open(str(_tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(_fd, "w") as f:
        f.write(_content)
    _tmp.rename(_netrc)
    # Write gh hosts.yml
    _gh_dir = _dir / ".config" / "gh"
    _gh_dir.mkdir(parents=True, exist_ok=True)
    _hosts = _gh_dir / "hosts.yml"
    _htmp = _hosts.with_suffix(".tmp")
    _fd2 = os.open(str(_htmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(_fd2, "w") as f:
        f.write(f"github.com:\n    oauth_token: {token}\n    user: x-access-token\n    git_protocol: https\n")
    _htmp.rename(_hosts)
    return True


def _resolve_session_domains(
    harness_write_domain: str,
    harness_domains: list[str],
    rbac_domains: list[str] | None,
) -> tuple[str, list[str], list[str]]:
    """Resolve write_domain, reference_domains, and effective_domain_ids.

    Derives reference domains from harness config and intersects the full
    domain list with RBAC grants. Core domain is always implicit.

    Returns:
        (write_domain, ref_domains, effective_domain_ids)
    """
    # reference_domains derived at runtime: domains - write_domain - core
    ref_domains = [d for d in harness_domains
                   if d != harness_write_domain and d != "core"]

    # Build effective domain list (core always implicit)
    all_domains: list[str] = list(harness_domains) if harness_domains else []
    if "core" not in all_domains:
        all_domains.insert(0, "core")
    if harness_write_domain and harness_write_domain not in all_domains:
        all_domains.append(harness_write_domain)

    # Intersect harness intent with RBAC grants (None = RBAC unavailable)
    if rbac_domains is None or rbac_domains == ["*"]:
        effective_domain_ids = all_domains
    elif rbac_domains:
        effective_domain_ids = [d for d in all_domains if d in rbac_domains]
    else:
        effective_domain_ids = ["core"]

    return harness_write_domain, ref_domains, effective_domain_ids


def _safe_int_chat_id(chat_id: str | int) -> int | None:
    """Convert chat_id to int for Pyrogram, returning None if not numeric.

    Staging uses string chat_ids (e.g. 'ab_test_1') which can't be converted.
    Callers should skip TG send operations when this returns None.
    """
    try:
        return int(chat_id)
    except (ValueError, TypeError):
        return None


def _get_external_chat_instructions(source: str, chat_id: str) -> str:
    """Look up custom_instructions for an external chat via chat_registry.

    Returns custom_instructions string (empty if not configured).
    """
    try:
        import bot.chat_registry as _cr
        chat_key = f"{source}:{chat_id}"
        entry = _cr.get(chat_key)
        if entry:
            return entry.get("custom_instructions", "")
        return ""
    except Exception as e:
        log.warning(f"Failed to get external chat config for {source}:{chat_id}: {e}", exc_info=True)
        return ""


class ClientPool:
    """Manages one ClaudeSDKClient per chat session.

    Each client maintains a persistent Claude CLI subprocess with its own
    conversation context. Sessions expire after SESSION_TIMEOUT (default 365 days).
    Sessions are reset on fatal errors, re-authentication, or expiry.
    """

    def __init__(self):
        self._clients: dict[str, ManagedClient] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._session_map_loaded = False

    def _get_lock(self, session_key: str) -> asyncio.Lock:
        return self._locks.setdefault(session_key, asyncio.Lock())

    @staticmethod
    def _camofox_profiles_dir(session_key: str, is_external: bool = False) -> str:
        """Compute per-session Camoufox profiles directory for chat-level isolation.

        Primary chats share /app/data/camofox-profiles/primary/.
        External chats get /app/data/camofox-profiles/<chat_id>/.
        """
        import re
        base = "/app/data/camofox-profiles"
        if is_external:
            # Extract chat_id from session_key (format: "source:chat_id" or "source:chat_id:task-xxx")
            parts = session_key.split(":")
            chat_id = parts[1] if len(parts) >= 2 else "unknown"
            # Sanitize: strip path traversal, keep only safe chars (alphanumeric, dash, underscore)
            chat_id = re.sub(r"[^a-zA-Z0-9_-]", "_", chat_id) or "unknown"
            return f"{base}/{chat_id}"
        return f"{base}/primary"

    @staticmethod
    def _playwright_profiles_dir(session_key: str, is_external: bool = False,
                                  has_sandbox: bool = False,
                                  harness_label: str = "") -> str:
        """Compute per-session Playwright profiles directory.

        Tier 1 (primary/admin/promoted): returns empty string → uses shared SSE server.
        Tier 2 (coding): per-project dir for stdio mode.
        Tier 3 (external): per-chat dir for stdio mode.
        """
        import re
        base = "/app/data/playwright-profiles"
        if not is_external and not has_sandbox:
            # Tier 1: primary/admin/promoted → shared SSE (no per-chat dir)
            return ""
        if has_sandbox:
            # Tier 2: coding → per-project dir
            label = re.sub(r"[^a-zA-Z0-9_-]", "_", harness_label or "default")
            return f"{base}/project-{label}"
        # Tier 3: external → per-chat dir
        parts = session_key.split(":")
        chat_id = parts[1] if len(parts) >= 2 else "unknown"
        chat_id = re.sub(r"[^a-zA-Z0-9_-]", "_", chat_id) or "unknown"
        return f"{base}/{chat_id}"

    def _build_options(self, system_prompt: str, model: str | None = None,
                       resume_session: str | None = None,
                       session_key: str = "",
                       camofox_profiles_dir: str = "",
                       playwright_profiles_dir: str = "") -> ClaudeAgentOptions:
        """Build ClaudeAgentOptions for a new or resumed client."""
        # Safety net: reject prompts that would crash the subprocess with Errno 7.
        # Primary enforcement is in prompts.py; this is the last-resort guard.
        encoded = system_prompt.encode("utf-8")
        if len(encoded) > SYSTEM_PROMPT_MAX_BYTES:
            log.error(
                f"System prompt exceeds hard limit: {len(encoded)} bytes > "
                f"{SYSTEM_PROMPT_MAX_BYTES} for {session_key}. "
                f"Hard-truncating to prevent Errno 7 crash."
            )
            # Truncate at byte boundary; errors="ignore" drops incomplete UTF-8 chars
            system_prompt = encoded[:SYSTEM_PROMPT_MAX_BYTES].decode("utf-8", errors="ignore")
        # Auth: per-chat OAuth profile support.  All profiles are labeled files in
        # OAUTH_PROFILES_DIR.  One is flagged as default (runtime-changeable).
        # Token is read from the profile file and injected via CLAUDE_CODE_OAUTH_TOKEN.
        # Fallback chain: profile file → ~/.claude/.credentials.json → env var.
        env = {}
        from config import get_oauth_for_chat, get_oauth_token, get_default_oauth_label
        oauth_label = get_oauth_for_chat(session_key) if session_key else get_default_oauth_label()
        token = get_oauth_token(oauth_label)
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
            log.debug("Auth: using OAuth profile '%s'", oauth_label)
        else:
            # Fallback: credentials.json (auto-refresh) or bare env var
            log.warning("Auth: profile '%s' has no token file, trying fallback", oauth_label)
            creds_file = CLAUDE_CREDENTIALS_PATH
            if creds_file.exists():
                log.debug("Auth: fallback to file-based credentials (auto-refresh)")
            elif CLAUDE_CODE_OAUTH_TOKEN:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = CLAUDE_CODE_OAUTH_TOKEN
                log.debug("Auth: fallback to env var token")

        # Resolve harness for this chat — provides model, effort, turns, budget defaults.
        # Per-session budget override (from extend_budget) takes precedence over harness.
        from bot.harness import get_harness_for_chat, resolve_field, get_harness_cwd, get_system_agent_cwd
        _harness = get_harness_for_chat(session_key)
        # System agent dispatch (PM): use a CWD with permissive settings.json.
        # For PM agents, use the system agent CWD (writable project dir with
        # permissive SDK settings). For regular chats, use harness CWD.
        if system_prompt is not None:
            _harness_cwd = get_system_agent_cwd()
        else:
            _harness_cwd = get_harness_cwd(_harness.label or "default", chat_key=session_key)
        # Model resolution chain: harness (base) → caller override → global default.
        # model=None means "inherit from harness" (used by PM roles with model=None).
        # model=explicit means "caller overrides harness" (role-specific model).
        _harness_model = resolve_field(_harness, "model", session_key)
        if not model:
            # No explicit model — use harness, fall back to global default
            model = _harness_model or get_model_primary()  # M1 fix: use getter not import-time constant
        # else: explicit model from caller wins over harness
        effort = resolve_field(_harness, "effort", session_key) or get_effort_level()
        _ht = resolve_field(_harness, "max_turns", session_key)
        _harness_turns = _ht if (_ht is not None and _ht > 0) else (MAX_TURNS if _ht is None else None)  # 0 = unlimited (None)
        _harness_budget = resolve_field(_harness, "max_budget_usd", session_key)
        if _harness_budget is None:
            _harness_budget = MAX_BUDGET_HEAVY if "opus" in model else MAX_BUDGET_DEFAULT
        # Per-session override (from extend_budget) takes precedence over harness
        _budget = _session_budget_overrides.get(session_key, _harness_budget)
        opts = ClaudeAgentOptions(
            system_prompt=system_prompt,
            model=model,
            thinking={"type": "adaptive"},
            effort=effort,
            **( {"max_turns": _harness_turns} if _harness_turns else {} ),
            **( {"max_budget_usd": _budget} if _budget > 0 else {} ),
            permission_mode="bypassPermissions",
            mcp_servers={**get_custom_mcp_servers(session_key),
                        **get_external_mcp_servers(
                            camofox_profiles_dir=camofox_profiles_dir,
                            playwright_profiles_dir=playwright_profiles_dir,
                        )},
            hooks=build_hooks(session_key),
            env=env,
            cwd=_harness_cwd,
            setting_sources=["project"],
        )
        # Fallback to Sonnet when using Opus (graceful degradation on rate limit)
        if "opus" in model:
            opts.fallback_model = "claude-sonnet-4-6"
        # Resume session if provided
        if resume_session:
            opts.resume = resume_session
        return opts

    async def get_main_session_id(self, source: str, chat_id: str) -> str | None:
        """Get the session_id of the main (non-task) session for a chat.

        Used to fork task sessions from the main session, inheriting context.
        """
        main_key = f"{source}:{chat_id}"
        # Check in-memory first
        if main_key in self._clients:
            return self._clients[main_key].session_id
        # Fall back to session map
        return await self._load_session_id(main_key)

    # get_checkpoint_session_id removed — fork mechanism deprecated

    async def _connect_with_retry(
        self,
        client: ClaudeSDKClient,
        session_key: str,
        on_retry: Callable | None = None,
    ) -> None:
        """Connect to SDK with exponential backoff retry.

        Retries CONNECT_MAX_RETRIES times with CONNECT_BACKOFF_BASE * 2^attempt delay.
        Calls optional on_retry(attempt, delay, error) callback for UI updates.
        """
        last_err: Exception | None = None
        for attempt in range(CONNECT_MAX_RETRIES + 1):
            try:
                await client.connect()
                if attempt > 0:
                    log.info(f"SDK connect succeeded for {session_key} on attempt {attempt + 1}")
                return
            except Exception as e:
                last_err = e
                if attempt >= CONNECT_MAX_RETRIES:
                    break  # exhausted retries
                delay = CONNECT_BACKOFF_BASE * (2 ** attempt)
                log.warning(f"SDK connect attempt {attempt + 1} failed for {session_key}: {e}; "
                            f"retrying in {delay:.0f}s")
                if on_retry:
                    try:
                        on_retry(attempt + 1, delay, str(e))
                    except Exception as _exc:
                        log.debug("Suppressed: %s", _exc)
                await asyncio.sleep(delay)
        raise last_err  # type: ignore[misc]

    async def get_or_create(self, session_key: str,
                            system_prompt: str | None = None,
                            model: str = MODEL_PRIMARY,
                            resume_session: str | None = None,
                            camofox_profiles_dir: str = "",
                            playwright_profiles_dir: str = "") -> ManagedClient:
        """Get an existing client or create a new one for this session."""
        now = time.time()

        # Check if we have a live client WITH a healthy transport.
        # A client can exist in _clients but have a dead CLI subprocess
        # (transport=None, process exited). Without this check, we'd
        # return a zombie client whose query() call would fail or produce
        # stale output from buffered pipes.
        if session_key in self._clients:
            mc = self._clients[session_key]
            transport = getattr(mc.client, "_transport", None)
            transport_alive = transport is not None
            if transport_alive:
                # Additional check: subprocess may have exited but transport object still exists
                proc = getattr(transport, "_process", None)
                if proc is not None and proc.returncode is not None:
                    transport_alive = False
                    log.warning(
                        f"Client {session_key}: CLI subprocess exited "
                        f"(code={proc.returncode}), will recreate"
                    )
            if transport_alive:
                mc.last_activity = now
                mc.is_fresh_session = False
                return mc
            else:
                # Dead transport — clean up and fall through to create a new client.
                # Preserve session_id so we can resume the same conversation.
                saved_sid = mc.session_id
                log.warning(f"Client {session_key}: dead transport, recreating "
                            f"(preserving session_id={saved_sid[:8] + '...' if saved_sid else 'None'})")
                await self._disconnect_client(session_key)
                # Use saved session_id for resume if no explicit one was given
                if not resume_session and saved_sid:
                    resume_session = saved_sid

        # Try to resume from session map if no resume_session given
        if not resume_session:
            resume_session = await self._load_session_id(session_key)

        # Build system prompt
        if system_prompt is None:
            system_prompt = build_system_prompt()

        # Create new client
        is_fresh = not resume_session
        mode = "resume" if resume_session else "new"
        log.info(f"Creating client for {session_key} (mode={mode}, fresh={is_fresh})")
        opts = self._build_options(system_prompt, model, resume_session,
                                   session_key=session_key,
                                   camofox_profiles_dir=camofox_profiles_dir,
                                   playwright_profiles_dir=playwright_profiles_dir)

        # GitHub token injection — RBAC-gated via allowed_credentials.
        try:
            from bot.auth.oauth_flow import get_user_oauth_token, get_user_oauth_token_metadata
            _sk_parts = session_key.split(":") if session_key else []
            _gh_user_id = f"{_sk_parts[0]}:{_sk_parts[1]}" if len(_sk_parts) >= 2 else ""
            if _gh_user_id:
                _gh_token = await get_user_oauth_token(_gh_user_id, "github")
                if _gh_token:
                    _gh_meta = await get_user_oauth_token_metadata(_gh_user_id, "github")
                    _gh_username = (_gh_meta or {}).get("username", "")
                    _gh_cred_id = f"github:{_gh_username}" if _gh_username else ""
                    if _gh_cred_id and _check_github_credential_allowed(session_key, _gh_cred_id):
                        if opts.env is None:
                            opts.env = {}
                        opts.env["GH_TOKEN"] = _gh_token
                        opts.env["GITHUB_TOKEN"] = _gh_token
                        log.debug("GitHub token injected for %s (cred=%s)", _gh_user_id[:20], _gh_cred_id)
                    else:
                        log.debug("GitHub token denied — %s not in allowed_credentials", _gh_cred_id or "no username")
            if not opts.env or "GH_TOKEN" not in opts.env:
                from config import GITHUB_TOKEN as _sys_gh_token
                if _sys_gh_token and _is_github_system_token_allowed(session_key):
                    if opts.env is None:
                        opts.env = {}
                    opts.env["GH_TOKEN"] = _sys_gh_token
                    opts.env["GITHUB_TOKEN"] = _sys_gh_token
                    log.debug("GitHub: using system GITHUB_TOKEN")
        except Exception as _gh_err:
            log.debug("GitHub token injection skipped: %s", _gh_err)

        # Write .netrc + gh hosts.yml for file-based GitHub auth
        _gh_tok = (opts.env or {}).get("GH_TOKEN", "")
        from pathlib import Path as _P_auth
        _auth_cwd = opts.cwd if opts.cwd and opts.cwd != "/app" else str(_P_auth.home())
        if _gh_tok:
            try:
                if _write_github_file_auth(_auth_cwd, _gh_tok):
                    log.debug("GitHub .netrc written to %s", _auth_cwd)
            except Exception as _netrc_err:
                log.debug("GitHub .netrc write skipped: %s", _netrc_err)

        client = ClaudeSDKClient(options=opts)
        await self._connect_with_retry(client, session_key)

        # Capture the exact cap baked into this CLI subprocess so LIVE CONTEXT
        # displays the truthful running cap (not a re-derivation that can drift
        # from the model arg at spawn time).
        _spawn_cap = float(getattr(opts, "max_budget_usd", 0.0) or 0.0)
        mc = ManagedClient(
            client=client,
            session_key=session_key,
            session_id=resume_session,
            created_at=now,
            last_activity=now,
            is_fresh_session=is_fresh,
            max_budget_cap_usd=_spawn_cap,
            max_turns_cap=int(opts.max_turns) if getattr(opts, "max_turns", None) else 0,
            project_cwd=_auth_cwd,
        )
        self._clients[session_key] = mc
        # Register reverse mapping for compaction tracking
        if resume_session:
            register_session_mapping(resume_session, session_key)
        return mc

    async def query(
        self,
        session_key: str,
        prompt: str,
        model: str | None = None,
        source: str = "",
        user_id: str = "",
        user_name: str = "",
        on_tool_status: Callable | None = None,
        is_external_chat: bool = False,
        detected_language: str = "",
        system_prompt: str | None = None,
    ) -> AsyncIterator:
        """Send a query to the session's client and yield response messages.

        Acquires per-session lock to prevent concurrent processing.
        Resumes the existing session for this chat (one main session per chat).

        system_prompt: when provided (e.g. PM agent dispatch), used as-is
        for the SDK system prompt. Skips build_system_prompt(), volatile header,
        facts injection, claude_md_modules, and harness custom_instructions.
        The prompt becomes the user message only. This enables Claude API
        prompt caching (90% discount on identical system prompt prefixes).
        """
        lock = self._get_lock(session_key)

        async with lock:
            # Set user context for hooks (explicit session_key, no globals)
            if source and user_id:
                set_user_context(source, user_id, user_name, session_key=session_key)
            if on_tool_status:
                set_tool_status_callback(on_tool_status, session_key=session_key)

            # Extract chat_id and source from session_key
            sk_parts = session_key.split(":")
            sk_chat_id = sk_parts[1] if len(sk_parts) >= 2 else ""
            sk_source = sk_parts[0] if sk_parts else ""

            # Determine admin status
            admin = is_admin_user(source, user_id) if source and user_id else False
            # System tasks: inherit access from their originating chat_id.
            # No chat_id (ephemeral) = admin (built-in tasks see all facts).
            # With chat_id: admin if it's an admin user's DM, otherwise shared.
            if sk_source == "system" and not admin:
                if not sk_chat_id or sk_chat_id == "ephemeral":
                    admin = True  # built-in tasks: full access
                elif system_prompt is not None:
                    # PM agent dispatch: always admin — agents operate on code,
                    # not personal data. Matches AccessContext (lines below).
                    admin = True
                else:
                    # Detect source from chat_id format: WA JIDs contain "@"
                    id_source = "whatsapp" if "@" in sk_chat_id else "telegram"
                    admin = is_admin_user(id_source, sk_chat_id)

            # When system_prompt is provided (PM agent dispatch), skip the
            # heavy prompt construction: build_system_prompt, harness,
            # claude_md_modules, custom_instructions. The caller provides a lean
            # role-specific prompt that enables Claude API prompt caching.
            # Resolve harness BEFORE branch — needed by both paths
            # (system prompt building AND AccessContext resolution).
            from bot.harness import get_harness_for_chat, resolve_field
            _h = get_harness_for_chat(session_key)

            # --- RBAC subject resolution (Sprint R4, authoritative) ---
            # RBAC is the primary source for domains, execution scope, and access context.
            # Harness provides only behavior fields (model, effort, turns, budget).
            _rbac_domains: list[str] | None = None  # None = RBAC didn't run; [] = explicit no access
            _rbac_execution: dict = {"filesystem": "none", "network": "none"}
            try:
                from bot.rbac import get_engine as _get_rbac_engine

                # user_id in RBAC must match the DB subject_id format: "source:id"
                # (e.g. "telegram:123456789"). The seed stores it this way.
                # System tasks (scheduler, PM agents) resolve to root admin identity
                # so they inherit bot_admin RBAC permissions. No separate system role.
                # Flat permission model (CB-79):
                # DM → subject = user (personal workspace)
                # Group/topic → subject = chat (shared workspace)
                # System tasks resolve to root admin identity (always DM-like).
                if sk_source == "system":
                    from config import ROOT_ADMIN
                    _rbac_user_id = f"{ROOT_ADMIN[0]}:{ROOT_ADMIN[1]}"
                elif user_id and sk_source:
                    _rbac_user_id = f"{sk_source}:{user_id}"
                else:
                    _rbac_user_id = str(user_id or "")

                from bot.core.access import build_rbac_subject as _build_subj
                _rbac_subject = _build_subj(
                    source=sk_source,
                    user_id=_rbac_user_id,
                    chat_id=sk_chat_id,
                )

                _rbac_engine = _get_rbac_engine()
                _rbac_domains = await _rbac_engine.get_allowed_domains(_rbac_subject)
                _rbac_execution = await _rbac_engine.get_execution_scope(_rbac_subject)

                # Store RBAC context in session state for hooks to use
                _rbac_state = _get_or_create_state(session_key)
                _rbac_state["rbac_subject"] = _rbac_subject
                _rbac_state["rbac_domains"] = _rbac_domains
                _rbac_state["rbac_execution"] = _rbac_execution
            except Exception as _rbac_err:
                log.warning("RBAC subject resolution failed (non-blocking): %s", _rbac_err)
            # --- End RBAC subject resolution ---

            if system_prompt is None:
                # Build STABLE system prompt (only used for new/forked sessions).
                # Volatile data (date/time, facts, is_admin, language hint, runtime)
                # is injected per-query via build_volatile_header() to stay fresh.

                # CB-5: Domain config from harness (single source of truth).
                _harness_write_domain = resolve_field(_h, "write_domain", session_key) or ""
                _harness_domains = resolve_field(_h, "domains", session_key) or []
                # CB-119: extracted domain resolution (HIGH-1/HIGH-3 fixes preserved)
                _harness_write_domain, _harness_ref_domains, _effective_domain_ids = (
                    _resolve_session_domains(_harness_write_domain, _harness_domains, _rbac_domains)
                )

                # Store write_domain in session state for fact auto-routing (CB-94)
                try:
                    _dc_state = _get_or_create_state(session_key)
                    _dc_state["write_domain"] = _harness_write_domain
                except Exception as _exc:
                    log.debug("Suppressed: %s", _exc)
                # facts_visible, facts_categories all come from domain config.
                chat_instructions = ""
                if is_external_chat:
                    dc_instructions = _get_external_chat_instructions(sk_source, sk_chat_id)
                    if dc_instructions:
                        chat_instructions = dc_instructions

                # Modules + instructions from effective domains
                _h_modules: list[str] | None = None
                _domain_configs: list[dict] = []
                _hot_summaries: dict[str, str] | None = None
                _rbac_domain_ids = _effective_domain_ids
                _effective_composition = None
                if _rbac_domain_ids:
                    try:
                        from bot.storage.domains import (
                            get_domain_knowledge_configs,
                            compose_effective_modules,
                            compose_domain_instructions,
                        )
                        _domain_configs = await get_domain_knowledge_configs(
                            _rbac_domain_ids if _rbac_domain_ids else []
                        )
                        if _domain_configs:
                            # CB-118: Single composition function for BOTH CLAUDE.md + system prompt.
                            # Domains = source of truth. Harness claude_md_modules = allowlist filter.
                            _harness_filter = resolve_field(_h, "claude_md_modules", session_key)
                            _prompt_filter = resolve_field(_h, "prompt_modules", session_key)
                            _effective_composition = compose_effective_modules(
                                _domain_configs,
                                harness_filter=_harness_filter,
                                prompt_filter=_prompt_filter,
                                chat_key=session_key,
                            )
                            _h_modules = _effective_composition.system_prompt_modules
                            try:
                                _domain_instructions = compose_domain_instructions(_domain_configs)
                            except Exception as e:
                                log.warning("Failed to compose domain instructions: %s", e)
                                _domain_instructions = ""
                            if _domain_instructions:
                                chat_instructions = f"{chat_instructions}\n\n{_domain_instructions}".strip() if chat_instructions else _domain_instructions
                    except Exception as e:
                        log.warning("COMPOSITION_FAILED: domain modules unavailable: %s — NO FALLBACK to stale lists", e)
                    # Get cached hot summaries for prompt injection
                    if _domain_configs:
                        try:
                            from bot.storage.domain_summary import get_domain_hot_summaries
                            _hot_summaries = await get_domain_hot_summaries(
                                _rbac_domain_ids if _rbac_domain_ids else []
                            )
                        except Exception as e:
                            log.warning("Failed to get domain hot summaries: %s", e)

                # Collect domain-linked credentials into session state (ephemeral, not persistent).
                # Domain credentials are shared with all members of that domain.
                # Computed fresh each query from domain YAML configs — never accumulates stale IDs.
                if _rbac_domain_ids:
                    try:
                        _domain_cred_ids: list[str] = []
                        from bot.storage.domains import get_domain, DomainConfig
                        import yaml as _yaml_dc
                        for _did in _rbac_domain_ids:
                            _dc_row = await get_domain(_did)
                            if _dc_row and _dc_row.get("config_yaml"):
                                try:
                                    _dc_parsed = DomainConfig.from_yaml(
                                        _yaml_dc.safe_load(_dc_row["config_yaml"]) or {}
                                    )
                                    _dc_creds = _dc_parsed.access.credentials or []
                                    _domain_cred_ids.extend(_dc_creds)
                                except Exception as _yaml_err:
                                    log.warning("Domain %s credential parse failed: %s", _did, _yaml_err)
                        if _domain_cred_ids:
                            # Store in ephemeral session state (NOT persistent override)
                            from bot.core.session_state import _get_or_create_state as _gocs
                            _sess = _gocs(session_key)
                            # Only set effective_allowed_credentials if harness is NOT unrestricted.
                            # ["*"] = unrestricted (admin) — domain creds don't restrict admins.
                            from bot.harness import resolve_field as _rf_dc, is_credential_unrestricted
                            _h_creds = _rf_dc(_h, "allowed_credentials", session_key) or []
                            if _h_creds and is_credential_unrestricted(_h_creds):
                                pass  # harness unrestricted (wildcard) — don't enforce domain creds
                            else:
                                _sess["effective_allowed_credentials"] = list(set(_h_creds + _domain_cred_ids))
                                log.debug("Domain credentials: %s → effective=%s", _domain_cred_ids, _sess.get("effective_allowed_credentials"))
                    except Exception as _dc_err:
                        log.debug("Domain credential collection failed: %s", _dc_err)

                # Forum topic thread ID from session state (set by handler)
                from bot.core.session_state import get_message_thread_id as _get_tid
                _msg_thread_id = _get_tid(session_key=session_key)
                # CB-118: Pass preloaded_note from compose_effective_modules
                _preloaded_note = ""
                if _effective_composition:
                    _preloaded_note = _effective_composition.preloaded_note
                _built_system_prompt = build_system_prompt(
                    session_source=sk_source,
                    session_chat_id=sk_chat_id,
                    is_external_chat=is_external_chat,
                    message_thread_id=_msg_thread_id,
                    custom_instructions=chat_instructions,
                    claude_md_modules=_h_modules,
                    active_domains=_domain_configs if _domain_configs else None,
                    hot_summaries=_hot_summaries,
                    write_domain=_harness_write_domain,
                    reference_domains=_harness_ref_domains,
                    preloaded_note=_preloaded_note,
                )
            else:
                _built_system_prompt = system_prompt

            # Build and register AccessContext for data isolation.
            # R4: Access context comes from RBAC execution scope (not harness data_scope).
            # Data isolation rule: admin role grants tool access, NOT data visibility.
            # Only admin DMs (private chats) get global data scope.
            # Admin group/topic chats see only their own chat's data + domains.
            from bot.core.access import build_access_context as _bac
            _ac_admin = _rbac_execution.get("filesystem") == "full" or admin
            # Determine if this is a private DM (Telegram: positive chat_id, Teams: 1:1)
            _is_private = False
            if sk_chat_id:
                try:
                    _is_private = int(sk_chat_id) > 0
                except (ValueError, TypeError):
                    _is_private = ":" not in sk_chat_id  # Teams DMs don't have colons typically
            access_ctx = _bac(
                chat_id=sk_chat_id,
                is_admin=_ac_admin,
                is_private_chat=_is_private,
            )
            # HIGH-2 fix: Use effective_domain_ids (harness∩RBAC), not raw RBAC
            # This ensures AccessContext matches what the prompt actually shows.
            _access_domains = _effective_domain_ids if system_prompt is None else (
                _rbac_domains if _rbac_domains else []
            )
            if _access_domains:
                from bot.core.access import AccessContext as _AC
                access_ctx = _AC(
                    chat_id=access_ctx.chat_id,
                    is_admin=access_ctx.is_admin,
                    allowed_chat_ids=access_ctx.allowed_chat_ids,
                    allowed_domains=tuple(_access_domains),
                )
            set_access_context(session_key, access_ctx)

            # Compute per-session browser profiles dirs for isolation
            cfx_profiles = self._camofox_profiles_dir(session_key, is_external_chat)
            # Playwright: Tier 1 (primary/admin) → "" (use shared SSE), Tier 2/3 → per-chat dir
            # R4: sandbox determined from RBAC execution scope (filesystem=sandbox|full)
            _has_sandbox = _rbac_execution.get("filesystem") in ("sandbox", "full")
            pw_profiles = self._playwright_profiles_dir(
                session_key, is_external_chat, _has_sandbox,
                _h.label or "default",
            )

            # Get or create client (resume existing session or create new)
            try:
                mc = await self.get_or_create(
                    session_key, system_prompt=_built_system_prompt, model=model,
                    camofox_profiles_dir=cfx_profiles,
                    playwright_profiles_dir=pw_profiles,
                )
            except Exception as resume_err:
                # Resume failed (stale session_map entry, deleted JSONL, etc.).
                # Fall back to fresh session instead of losing the message.
                log.warning(f"Session resume failed for {session_key}, "
                            f"falling back to fresh session: {resume_err}")
                self._clients.pop(session_key, None)
                await self._clear_session_id(session_key)
                mc = await self.get_or_create(
                    session_key, system_prompt=_built_system_prompt, model=model,
                    camofox_profiles_dir=cfx_profiles,
                    playwright_profiles_dir=pw_profiles,
                )
            mc.query_count += 1
            mc.last_activity = time.time()

            # PM agent dispatch (system_prompt provided): skip facts, volatile
            # header, language detection — agents don't need organization context.
            # This saves ~96KB of system prompt + ~15KB of facts per dispatch.
            if system_prompt is not None:
                enriched_prompt = prompt
            else:
                # Build fresh volatile header with current date/time, facts, admin status, language
                # R4: facts_visible and facts_categories come from RBAC-granted domains.
                # Domain configs carry facts_visible and facts_categories per-domain.
                _facts_visible_from_domains = True  # default: show facts
                if _domain_configs:
                    # If ALL domain configs have facts_visible=False, hide facts
                    _facts_visible_from_domains = any(
                        dc.get("facts_visible", True) for dc in _domain_configs
                    )
                if not _facts_visible_from_domains:
                    facts_summary = ("(Facts not shown inline for this chat. "
                                     "Use memory_search or search_facts tools to look up stored information.)")
                else:
                    try:
                        from bot.storage.memory import get_facts_for_prompt
                        # CB-5: fact categories unfiltered (harness no longer declares categories)
                        facts_summary = await get_facts_for_prompt(
                            access_ctx=access_ctx,
                            categories=None,
                        )
                    except Exception as e:
                        log.warning(f"Failed to load facts for prompt: {e}")
                        facts_summary = ""

                # Fact delta optimization: hash facts and skip full injection when unchanged.
                # Saves ~15KB cache write per query when facts haven't changed.
                # Always send full facts on first query or after compaction (context lost).
                import hashlib
                facts_hash = hashlib.md5(facts_summary.encode()).hexdigest() if facts_summary else ""
                from bot.hooks import compacted_sessions
                just_compacted = session_key in compacted_sessions
                if just_compacted:
                    compacted_sessions.discard(session_key)
                    mc.last_facts_hash = ""  # force full send after compaction
                if facts_summary and mc.last_facts_hash and mc.last_facts_hash == facts_hash and not just_compacted:
                    # Facts unchanged — send abbreviated reference with count + hint
                    import re as _re
                    _subjects = _re.findall(r"^\[([^\]]+)\]", facts_summary, _re.MULTILINE)
                    _subj_str = f" across {len(_subjects)} subjects" if _subjects else ""
                    _total_match = _re.search(r"Total:\s*(\d+)\s*facts", facts_summary)
                    _total = _total_match.group(1) if _total_match else "?"
                    facts_for_header = (
                        f"({_total} facts{_subj_str}, unchanged since last query. "
                        f"Full list in prior turn's KNOWN FACTS. Use search_facts for lookups.)"
                    )
                    log.debug(f"Facts delta: hash unchanged ({facts_hash[:8]}), abbreviated ({len(facts_for_header)} vs {len(facts_summary)} chars)")
                else:
                    facts_for_header = facts_summary
                    if facts_hash:
                        log.debug(f"Facts delta: full send ({len(facts_summary)} chars, hash={facts_hash[:8]}, prev={mc.last_facts_hash[:8] if mc.last_facts_hash else 'none'})")
                mc.last_facts_hash = facts_hash
                # Build language directive
                if detected_language:
                    lang_name = _LANG_NAMES.get(detected_language, detected_language.upper())
                    lang_directive = f"RESPOND IN: {lang_name} (detected from current message)"
                else:
                    # Default to English when detection fails (short/ambiguous messages).
                    # Prevents model from guessing based on context (group name, prior
                    # conversation language) which often produces wrong-language replies.
                    lang_directive = "RESPOND IN: English (default — detection inconclusive)"
                msg_preview = (prompt[:200].strip()) if prompt else ""
                lang_hint = f"[detected={detected_language}] {msg_preview}" if detected_language else msg_preview
                # Budget info
                try:
                    _cap = float(mc.max_budget_cap_usd or 0.0)
                    if _cap <= 0:
                        _cap = float(_session_budget_overrides.get(
                            session_key,
                            MAX_BUDGET_HEAVY if "opus" in model else MAX_BUDGET_DEFAULT,
                        ))
                    _spent = float(mc.session_cost_usd or 0.0)
                    if _cap > 0:
                        _remain = max(0.0, _cap - _spent)
                        session_budget_info = (
                            f"${_spent:.2f} spent / ${_cap:.2f} cap / "
                            f"${_remain:.2f} remaining"
                        )
                    else:
                        session_budget_info = f"${_spent:.2f} spent (no cap)"
                except Exception:
                    session_budget_info = ""
                # Look up user's OAuth emails for auto-routing Google/M365 tool calls.
                # Uses session_key (source:chat_id) — works for DMs where chat_id==user_id.
                # Group chats won't match (auth tokens stored under user's DM key).
                _user_creds: dict[str, str] = {}
                try:
                    from bot.auth.oauth_flow import get_user_oauth_token_metadata
                    _sk_parts = session_key.split(":") if session_key else []
                    _cred_uid = f"{_sk_parts[0]}:{_sk_parts[1]}" if len(_sk_parts) >= 2 else ""
                    if _cred_uid:
                        for _provider in ("google", "m365"):
                            _meta = await get_user_oauth_token_metadata(_cred_uid, _provider)
                            if _meta and _meta.get("email"):
                                _user_creds[f"{_provider}_email"] = _meta["email"]
                except Exception as _cred_err:
                    log.debug("User credential lookup for volatile header: %s", _cred_err)

                # Per-query GitHub .netrc refresh — RBAC-gated, uses shared helpers
                try:
                    from bot.auth.oauth_flow import get_user_oauth_token, get_user_oauth_token_metadata
                    _gh_uid_pq = f"{_sk_parts[0]}:{_sk_parts[1]}" if len(_sk_parts) >= 2 else ""
                    _gh_tok_pq = await get_user_oauth_token(_gh_uid_pq, "github") if _gh_uid_pq else None
                    if _gh_tok_pq:
                        _gh_meta_pq = await get_user_oauth_token_metadata(_gh_uid_pq, "github")
                        _gh_cid_pq = f"github:{(_gh_meta_pq or {}).get('username', '')}"
                        if not _check_github_credential_allowed(session_key, _gh_cid_pq):
                            _gh_tok_pq = None  # denied
                    if not _gh_tok_pq:
                        from config import GITHUB_TOKEN as _sys_gh_pq
                        if _sys_gh_pq and _is_github_system_token_allowed(session_key):
                            _gh_tok_pq = _sys_gh_pq
                    if _gh_tok_pq and mc.project_cwd:
                        if _write_github_file_auth(mc.project_cwd, _gh_tok_pq):
                            log.debug("GitHub .netrc refreshed (per-query) for %s", session_key)
                except Exception as _gh_pq_err:
                    log.debug("Per-query GitHub .netrc refresh skipped: %s", _gh_pq_err)

                volatile_header = build_volatile_header(
                    is_admin=admin,
                    facts_summary=facts_for_header,
                    message_language_hint=lang_hint,
                    language_directive=lang_directive,
                    session_budget_info=session_budget_info,
                    user_credentials=_user_creds or None,
                )
                enriched_prompt = volatile_header + prompt

            # Drain stale messages from SDK buffer before sending new query.
            # The SDK uses a single anyio MemoryObjectStream (buffer=100) shared
            # across ALL queries on one connection. Post-ResultMessage output from
            # the CLI (compaction, internal bookkeeping) accumulates in this buffer
            # and contaminates the next receive_response() call — causing it to
            # return stale data instantly (the "impossibly fast multi-turn" bug).
            query_obj = getattr(mc.client, "_query", None)
            if query_obj is not None:
                recv_stream = getattr(query_obj, "_message_receive", None)
                if recv_stream is not None:
                    drained = 0
                    drained_details = []
                    max_drain = 200  # safety limit (buffer=100, 2x margin)
                    while drained < max_drain:
                        try:
                            msg = recv_stream.receive_nowait()
                            drained += 1
                            # Log each drained message for hypothesis verification
                            try:
                                if isinstance(msg, ResultMessage):
                                    drained_details.append(
                                        f"ResultMessage(turns={msg.num_turns}, "
                                        f"dur={msg.duration_ms}ms, "
                                        f"api_dur={msg.duration_api_ms}ms, "
                                        f"sub={msg.subtype}, "
                                        f"stop={msg.stop_reason}, "
                                        f"err={msg.is_error})"
                                    )
                                elif isinstance(msg, AssistantMessage):
                                    tools = [
                                        b.name for b in (msg.content or [])
                                        if isinstance(b, ToolUseBlock)
                                    ]
                                    drained_details.append(
                                        f"AssistantMessage(model={msg.model}, "
                                        f"tools={tools}, "
                                        f"stop={msg.stop_reason})"
                                    )
                                elif isinstance(msg, SystemMessage):
                                    drained_details.append(
                                        f"SystemMessage(sub={msg.subtype})"
                                    )
                                else:
                                    drained_details.append(
                                        f"{type(msg).__name__}"
                                    )
                            except Exception:
                                drained_details.append(f"{type(msg).__name__}(?)")
                        except anyio.WouldBlock:
                            break
                        except Exception as drain_err:
                            log.debug(f"Buffer drain stopped after {drained}: {drain_err}")
                            break
                    if drained:
                        log.warning(
                            f"Drained {drained} stale messages from SDK buffer "
                            f"for {session_key}:\n"
                            + "\n".join(f"  [{i+1}] {d}" for i, d in enumerate(drained_details))
                        )

            # Send query with fresh volatile context prepended
            await mc.client.query(enriched_prompt)

            # Yield response messages
            async for message in mc.client.receive_response():
                # Capture session_id from SystemMessage (init) or ResultMessage
                if isinstance(message, SystemMessage):
                    sid = message.data.get("session_id")
                    if sid and not mc.session_id:
                        mc.session_id = sid
                        register_session_mapping(sid, session_key)
                        # Don't persist task-specific sessions (ephemeral)
                        if ":task-" not in session_key:
                            await self._save_session_id(session_key, mc.session_id)
                    # Note: SDK never emits subtype="compaction"/"compact" in SystemMessage.
                    # Compaction is detected via PreCompact hook (primary) and
                    # token-drop fallback in ResultMessage handler.
                elif isinstance(message, ResultMessage) and message.session_id:
                    mc.session_id = message.session_id
                    register_session_mapping(message.session_id, session_key)
                    # Save session_id for resume on next query
                    await self._save_session_id(session_key, mc.session_id)

                yield message

    def is_processing(self, session_key: str) -> bool:
        """Check if a session is currently processing a query (lock held)."""
        lock = self._locks.get(session_key)
        return lock is not None and lock.locked()

    async def inject_message(self, session_key: str, prompt: str) -> bool:
        """Inject a message into an actively-processing session.

        Writes directly to the SDK subprocess stdin via mc.client.query().
        Does NOT acquire the per-session lock — the message is picked up
        at the next listening checkpoint by the running subprocess.

        Returns True if injected, False if no active client found.
        """
        mc = self._clients.get(session_key)
        if not mc or not mc.client:
            return False
        try:
            await mc.client.query(prompt)
            log.info(f"Mid-task injection: sent {len(prompt)} chars to {session_key}")
            return True
        except Exception as e:
            log.warning(f"Mid-task injection failed for {session_key}: {e}")
            return False

    async def cleanup_expired(self):
        """Disconnect clients that have been inactive too long.

        All sessions use SESSION_TIMEOUT (365 days — effectively never).
        """
        now = time.time()
        expired = []
        for k, mc in self._clients.items():
            if now - mc.last_activity > SESSION_TIMEOUT:
                expired.append(k)
        for key in expired:
            mc = self._clients.get(key)
            age_min = int((now - mc.created_at) / 60) if mc else 0
            log.warning(f"Hard-killing stale session: {key} "
                        f"(age={age_min}min, queries={mc.query_count if mc else '?'})")
            await self._disconnect_client(key)
            # M4 fix: clear any budget override too — a stale session
            # being hard-killed means the user abandoned it; a FRESH
            # session under the same session_key must start at the
            # model-default cap, not inherit the ghost override.
            clear_budget_override(key)

    def cleanup_stale_session_files(self):
        """Delete orphaned SDK session files (JSONL transcripts + data dirs).

        The SDK writes per-session artifacts in _JSONL_DIR:
        - {session_id}.jsonl — conversation transcript
        - {session_id}/ — directory with subagents/ and tool-results/
        These accumulate on disk (~35/day from forks + external chats).
        Files for active sessions are preserved; orphaned artifacts are kept
        for SESSION_FILE_RETENTION (default 1 day) for debugging, then deleted.
        """
        if not os.path.isdir(_JSONL_DIR):
            return
        # Collect session IDs of all active clients (main + checkpoint)
        active_ids: set[str] = set()
        for mc in self._clients.values():
            if mc.session_id:
                active_ids.add(mc.session_id)
        now = time.time()
        deleted_files = 0
        deleted_dirs = 0
        errors = 0
        try:
            for entry in os.listdir(_JSONL_DIR):
                # Validate entry name: session IDs are UUIDs, no path separators
                if "/" in entry or "\\" in entry or ".." in entry:
                    continue
                fpath = os.path.join(_JSONL_DIR, entry)
                # Skip symlinks — only process real files/dirs
                if os.path.islink(fpath):
                    continue
                # Determine session ID from entry name
                sid = entry[:-6] if entry.endswith(".jsonl") else entry
                if sid in active_ids:
                    continue
                try:
                    mtime = os.path.getmtime(fpath)
                    if now - mtime <= SESSION_FILE_RETENTION:
                        continue
                    if os.path.isfile(fpath):
                        os.unlink(fpath)
                        deleted_files += 1
                    elif os.path.isdir(fpath):
                        shutil.rmtree(fpath, ignore_errors=True)
                        deleted_dirs += 1
                except Exception as e:
                    log.debug(f"Session file cleanup error for {entry}: {e}")
                    errors += 1
        except Exception as e:
            log.warning(f"Session file cleanup scan failed: {e}")
            return
        if deleted_files or deleted_dirs or errors:
            log.info(f"Session file cleanup: files={deleted_files} dirs={deleted_dirs} "
                     f"errors={errors} active={len(active_ids)}")

    async def cleanup_stale_session_map(self):
        """Remove session_map entries whose JSONL files no longer exist on disk.

        Prevents stale resume attempts that fail because the JSONL was deleted
        (manually or by cleanup_stale_session_files). Runs periodically alongside
        cleanup_stale_session_files.
        """
        if not os.path.isdir(_JSONL_DIR):
            return
        try:
            async with open_db() as db:
                cursor = await db.execute("SELECT session_key, session_id FROM session_map")
                rows = await cursor.fetchall()
                if not rows:
                    return
                # Collect active session_ids (don't prune those)
                active_ids: set[str] = set()
                for mc in self._clients.values():
                    if mc.session_id:
                        active_ids.add(mc.session_id)
                stale_keys: list[str] = []
                for session_key, session_id in rows:
                    if session_id in active_ids:
                        continue
                    jsonl_path = os.path.join(_JSONL_DIR, f"{session_id}.jsonl")
                    if not os.path.exists(jsonl_path):
                        stale_keys.append(session_key)
                if stale_keys:
                    for sk in stale_keys:
                        await db.execute(
                            "DELETE FROM session_map WHERE session_key = ?", (sk,)
                        )
                    await db.commit()
                    log.info(f"Session map cleanup: pruned {len(stale_keys)} "
                             f"stale entries (JSONL missing)")
        except Exception as e:
            log.warning(f"Session map cleanup failed: {e}")

    async def disconnect_all(self):
        """Disconnect all clients (called on shutdown)."""
        for key in list(self._clients.keys()):
            await self._disconnect_client(key)

    async def _disconnect_client(self, session_key: str):
        """Safely disconnect and remove a client, killing orphan subprocesses."""
        mc = self._clients.pop(session_key, None)
        if mc and mc.client:
            # Clean up session_id → session_key reverse mapping
            if mc.session_id:
                from bot.hooks import unregister_session_mapping
                unregister_session_mapping(mc.session_id)
            # Clean up per-session state
            from bot.hooks import cleanup_session_state
            cleanup_session_state(session_key)
            # Clean up browser pool entries for this session
            try:
                from bot.browser_pool import get_pool
                removed = await get_pool().cleanup_session(session_key)
                if removed:
                    log.info("Cleaned up %d browser pool entries for %s", len(removed), session_key[:30])
            except Exception as e:
                log.warning("Browser pool cleanup error for %s: %s", session_key[:30], e)
            try:
                await mc.client.disconnect()
            except Exception as e:
                log.warning(f"Error disconnecting client {session_key}: {e}")
            # Kill any orphan child processes (Chromium, MCP servers) that SDK didn't clean up
            await self._kill_orphan_children()
        self._locks.pop(session_key, None)

    @staticmethod
    async def _kill_orphan_children():
        """Clean up stale browser lock files left by crashed Chromium sessions.

        NOTE: Chromium/Playwright processes are managed by the shared SSE server
        (started in main.py). We do NOT kill those processes here — only clean
        lock files that prevent the shared server from starting.
        """
        from main import clean_browser_locks
        clean_browser_locks(kill_processes=False)

    @staticmethod
    async def resource_watchdog():
        """Periodic check: clean stale browser locks if resource usage is high.

        Browser processes are managed by the shared Playwright SSE server
        (started in main.py). This watchdog only cleans stale lock files.
        Call from scheduler loop every few minutes.
        """
        import subprocess
        try:
            # Count all child processes (main.py PID=1 in container)
            result = subprocess.run(
                ["sh", "-c", "ls /proc/*/status 2>/dev/null | wc -l"],
                capture_output=True, text=True, timeout=5,
            )
            proc_count = int(result.stdout.strip()) if result.stdout.strip() else 0

            # Check RSS memory of this process tree (in MB)
            mem_result = subprocess.run(
                ["sh", "-c", "cat /proc/1/status 2>/dev/null | grep VmRSS | awk '{print $2}'"],
                capture_output=True, text=True, timeout=5,
            )
            rss_kb = int(mem_result.stdout.strip()) if mem_result.stdout.strip() else 0
            rss_mb = rss_kb // 1024

            # Thresholds: >40 processes or >1.5GB RSS = orphan cleanup
            if proc_count > 40 or rss_mb > 1500:
                log.warning(f"Resource watchdog: {proc_count} procs, {rss_mb}MB RSS — cleaning stale locks")
                await ClientPool._kill_orphan_children()
            elif proc_count > 10:
                log.debug(f"Resource watchdog: {proc_count} procs, {rss_mb}MB RSS — OK")
        except Exception as e:
            log.debug(f"Resource watchdog error: {e}")

    # --- Session ID persistence (SQLite) ---

    async def _save_session_id(self, session_key: str, session_id: str):
        """Save session_key → session_id mapping to SQLite."""
        try:
            async with open_db() as db:
                await db.execute(
                    "INSERT OR REPLACE INTO session_map (session_key, session_id, last_activity) "
                    "VALUES (?, ?, ?)",
                    (session_key, session_id, time.time()),
                )
                await db.commit()
        except Exception as e:
            log.warning(f"Failed to save session_id for {session_key}: {e}")

    async def _clear_session_id(self, session_key: str):
        """Remove session_id mapping from SQLite (used on fatal errors)."""
        try:
            async with open_db() as db:
                await db.execute(
                    "DELETE FROM session_map WHERE session_key = ?",
                    (session_key,),
                )
                await db.commit()
            log.info(f"Cleared session_id for {session_key}")
        except Exception as e:
            log.warning(f"Failed to clear session_id for {session_key}: {e}")

    async def get_session_id(self, session_key: str) -> str | None:
        """Public wrapper for loading saved session_id."""
        return await self._load_session_id(session_key)

    async def _load_session_id(self, session_key: str) -> str | None:
        """Load session_id for a session_key from SQLite."""
        try:
            async with open_db() as db:
                cursor = await db.execute(
                    "SELECT session_id FROM session_map WHERE session_key = ? "
                    "AND last_activity > ?",
                    (session_key, time.time() - SESSION_TIMEOUT),
                )
                row = await cursor.fetchone()
                if row:
                    return row[0]
        except Exception as e:
            log.debug(f"Failed to load session_id for {session_key}: {e}")
        return None

    async def restore_clients(self):
        """Load session map from SQLite on startup. Clients are lazily created on first query.

        On SDK/CLI version change, clears the session map so all sessions start
        fresh with the current CLI's context limits and capabilities.  Without this,
        resumed sessions carry the OLD CLI's context window/compaction thresholds.
        """
        try:
            from claude_agent_sdk._cli_version import __cli_version__ as cli_version
        except ImportError:
            cli_version = "unknown"

        version_file = os.path.join(os.environ.get("DATA_DIR", "/app/data"), "sdk_cli_version")

        try:
            async with open_db() as db:
                # Ensure table exists
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS session_map ("
                    "  session_key TEXT PRIMARY KEY,"
                    "  session_id TEXT NOT NULL,"
                    "  last_activity REAL NOT NULL"
                    ")"
                )
                await db.commit()

                # Check for SDK/CLI version change → force fresh sessions
                prev_version = ""
                try:
                    if os.path.exists(version_file):
                        with open(version_file) as f:
                            prev_version = f.read().strip()
                except OSError as e:
                    log.warning(f"Failed to read SDK version marker: {e}")

                if prev_version and prev_version != cli_version:
                    log.warning(
                        f"SDK CLI version changed: {prev_version} → {cli_version}. "
                        f"Clearing session map to force fresh sessions with new CLI limits."
                    )
                    await db.execute("DELETE FROM session_map")
                    await db.commit()

                # Update version marker
                try:
                    with open(version_file, "w") as f:
                        f.write(cli_version)
                except Exception as e:
                    log.warning(f"Failed to write SDK version marker: {e}")

                # List active sessions for resume
                cursor = await db.execute(
                    "SELECT session_key FROM session_map WHERE last_activity > ? "
                    "ORDER BY last_activity DESC",
                    (time.time() - SESSION_TIMEOUT,),
                )
                rows = await cursor.fetchall()
                log.info(f"Session map: {len(rows)} active session(s) available for resume (CLI {cli_version})")
                for (sk,) in rows:
                    log.info(f"  Resumable session: {sk}")

        except Exception as e:
            log.warning(f"Failed to init session map: {e}")

        self._session_map_loaded = True

    def is_session_fresh(self, session_key: str) -> bool:
        """Check if a session was freshly created (not resumed from prior context).

        Returns True if the session has no prior conversation history,
        meaning the model should proactively load recent context.
        """
        mc = self._clients.get(session_key)
        return mc.is_fresh_session if mc else False

    def get_client(self, session_key: str):
        """Get an existing client by session key."""
        return self._clients.get(session_key)

    async def load_session_id(self, session_key: str) -> str | None:
        """Load a saved session ID for the given key."""
        return await self._load_session_id(session_key)

    async def save_session_id(self, session_key: str, session_id: str) -> None:
        """Save a session ID for the given key."""
        await self._save_session_id(session_key, session_id)

    async def disconnect_client(self, session_key: str) -> None:
        """Disconnect and remove a client."""
        await self._disconnect_client(session_key)

    async def clear_session_id(self, session_key: str) -> None:
        """Clear a saved session ID."""
        await self._clear_session_id(session_key)

    async def reset_session(self, session_key: str) -> None:
        """Disconnect a session and clear its saved ID, forcing a fresh start.

        Used for model switching and rate limit recovery. Next query() creates
        a new CLI subprocess with a fresh session. Also clears any budget
        override so the fresh session starts at the model-default cap.
        """
        await self._disconnect_client(session_key)
        await self._clear_session_id(session_key)
        clear_budget_override(session_key)
        log.info(f"Session reset: {session_key}")

    async def extend_budget(self, session_key: str, delta_usd: float) -> float:
        """Raise this session's SDK max_budget_usd cap WITHOUT losing context.

        Mechanics (DEFERRED-DISCONNECT pattern, same as switch_model):
          1. Validate delta (positive + finite; clamped to MAX_EXTEND_DELTA)
          2. Compute new absolute cap = (current override OR model default) + delta
             (clamped to MAX_TOTAL_BUDGET_CAP)
          3. Store in _session_budget_overrides[session_key]
          4. SCHEDULE a deferred session disconnect (NOT immediate) — the live
             CLI subprocess keeps running so the current tool's reply actually
             reaches the user. The finally-block of the streaming query loop
             processes _pending_session_resets AFTER the ResultMessage fires.
          5. On next query(), a fresh CLI subprocess spawns with --resume <id>
             and the new max_budget_usd — conversation context is preserved via
             JSONL replay, cumulative cost tracking resets to 0 against new cap

        The delta is added to the CURRENT effective cap; calling this twice
        with delta=20 from a default of 50 yields 90 (not 70).

        Returns the new absolute cap.
        Raises ValueError if delta_usd is not a positive finite number.
        """
        import math  # noqa: PLC0415 — local import keeps top-level imports minimal
        # Reject None, non-positive, NaN, inf. `math.isfinite(nan)` is False;
        # `math.isfinite(inf)` is False; both caught here before any math runs.
        if delta_usd is None:
            raise ValueError("delta_usd required")
        try:
            delta_f = float(delta_usd)
        except (TypeError, ValueError) as e:
            raise ValueError(f"delta_usd must be numeric, got {delta_usd!r}: {e}")
        if not math.isfinite(delta_f) or delta_f <= 0:
            raise ValueError(f"delta_usd must be positive and finite, got {delta_usd}")
        if delta_f > MAX_EXTEND_DELTA_USD:
            raise ValueError(
                f"delta_usd ${delta_f:.2f} exceeds per-extension cap "
                f"${MAX_EXTEND_DELTA_USD:.2f}"
            )

        # Derive current effective cap: override if set, else model default.
        current = _session_budget_overrides.get(session_key)
        if current is None:
            current = _default_budget_for_session(session_key)

        new_cap = float(current) + delta_f
        if new_cap > MAX_TOTAL_BUDGET_CAP:
            raise ValueError(
                f"new cap ${new_cap:.2f} exceeds total session cap "
                f"${MAX_TOTAL_BUDGET_CAP:.2f} (current=${current:.2f})"
            )

        _session_budget_overrides[session_key] = new_cap

        # DEFERRED disconnect-only: schedule via _pending_budget_disconnects
        # (NOT _pending_session_resets — that would clear session_id + override).
        # Streaming query loop's finally-block tears down the client AFTER
        # ResultMessage fires so the tool's reply reaches the user. On next
        # query, _load_session_id returns the preserved ID → SDK --resume UUID
        # → conversation context restored via JSONL replay.
        schedule_budget_disconnect(session_key)
        log.info(
            f"Budget extended (deferred disconnect): session={session_key} "
            f"old_cap=${current:.2f} delta=${delta_f:.2f} new_cap=${new_cap:.2f}"
        )
        return new_cap

    def get_status(self) -> dict:
        """Get pool status for health endpoint."""
        return {
            k: {
                "queries": mc.query_count,
                "session_id": mc.session_id[:8] + "..." if mc.session_id else None,
                "age_min": int((time.time() - mc.created_at) / 60),
                "connected": mc.client._transport is not None,
                "idle_min": int((time.time() - mc.last_activity) / 60),
                "cost_usd": round(mc.session_cost_usd, 2) if mc.session_cost_usd else 0,
            }
            for k, mc in self._clients.items()
        }


# Singleton pool instance
client_pool = ClientPool()

# Context window sizes — determined by model name suffix
CONTEXT_WINDOW_200K = 200_000
CONTEXT_WINDOW_1M = 1_000_000


def _context_window_for_model(model: str) -> int:
    """Return context window size based on model name."""
    return CONTEXT_WINDOW_1M if "[1m]" in model else CONTEXT_WINDOW_200K


# --- Deferred Session Reset ---
# When switch_model sets a per-chat override, the session must be reset
# AFTER the current query completes (not during — that kills the response).
_pending_session_resets: set[str] = set()


def schedule_session_reset(session_key: str) -> None:
    """Schedule a session reset after the current query completes."""
    _pending_session_resets.add(session_key)


def has_pending_teardown(session_key: str) -> bool:
    """Check if a session has a pending reset or budget disconnect.

    Used by main.py to skip mid-task injection when the session is about
    to be torn down — injected text would be silently lost.
    """
    return (session_key in _pending_session_resets
            or session_key in _pending_budget_disconnects)


# --- Per-session budget overrides ---
# Set by the `extend_budget` MCP tool to raise the session's SDK max_budget_usd
# cap WITHOUT losing conversation context. The override is consulted in
# _build_options() — next client spawn for this session_key uses the override
# as max_budget_usd. Paired with _disconnect_client() (NOT reset_session) so
# the SQLite session_id mapping survives, enabling resume via --resume UUID.
_session_budget_overrides: dict[str, float] = {}

# Hard caps for extend_budget (prevents runaway extension via confused admin
# or reply-quote bypass). Both are env-overridable for ops flexibility.
MAX_EXTEND_DELTA_USD: float = float(os.environ.get("MAX_EXTEND_DELTA_USD", "100"))
MAX_TOTAL_BUDGET_CAP: float = float(os.environ.get("MAX_TOTAL_BUDGET_CAP", "500"))

# Deferred budget-driven disconnects. Processed in the streaming query loop's
# finally-block — disconnects the client AFTER ResultMessage fires so the
# tool's reply reaches the user. Unlike _pending_session_resets (which calls
# full reset_session → clears session_id → loses context), this set is
# consumed by a disconnect-only path that preserves the SQLite session_id
# mapping. Next query auto-resumes via --resume UUID with the new cap.
_pending_budget_disconnects: set[str] = set()


def get_budget_override(session_key: str) -> float | None:
    """Public accessor for the current budget override (None if unset)."""
    return _session_budget_overrides.get(session_key)


def clear_budget_override(session_key: str) -> None:
    """Remove a session's budget override — used when the session is
    genuinely reset (full reset_session path) so a fresh session starts
    with the default cap."""
    _session_budget_overrides.pop(session_key, None)


def schedule_budget_disconnect(session_key: str) -> None:
    """Schedule a deferred disconnect-only (preserves session_id) for a
    session whose budget override was just raised. Processed in the
    streaming query loop's finally-block."""
    _pending_budget_disconnects.add(session_key)


def _default_budget_for_session(session_key: str) -> float:
    """Compute the model-default budget cap for a session_key.

    Used by extend_budget to derive the baseline before adding delta.
    Single source of truth for default-cap resolution (paired with
    _build_options); avoids duplicated `"opus" in model` checks.
    """
    # H3 fix: pass full session_key (get_model_for_chat normalizes it)
    model = get_model_for_chat(session_key) if session_key else MODEL_PRIMARY
    return MAX_BUDGET_HEAVY if "opus" in model else MAX_BUDGET_DEFAULT


# --- Rate Limit State (from SDK RateLimitEvent) ---
# Keyed by (oauth_label, rate_limit_type), e.g. ("personal", "seven_day")
_rate_limit_state: dict[str, dict] = {}
_rate_limit_alert_cooldown: dict[str, float] = {}


def _update_rate_limit_state(info, oauth_label: str = "default") -> None:
    """Update module-level rate limit state from a RateLimitEvent."""
    rl_type = info.rate_limit_type or "unknown"
    key = f"{oauth_label}:{rl_type}"
    _rate_limit_state[key] = {
        "status": info.status,
        "utilization": info.utilization,
        "resets_at": info.resets_at,
        "updated_at": time.time(),
        "oauth_label": oauth_label,
        "rate_limit_type": rl_type,
    }
    pct = f"{info.utilization:.0%}" if info.utilization is not None else "n/a"
    raw_keys = list(info.raw.keys()) if info.raw else []
    resets_str = f" resets_at={info.resets_at}" if info.resets_at else ""
    log.info(f"Rate limit: {oauth_label}:{rl_type} — {info.status} utilization={pct}{resets_str} raw={raw_keys}")


def get_rate_limit_state() -> dict:
    """Public getter for /health and status commands."""
    return dict(_rate_limit_state)


async def _safe_store_usage(**kwargs) -> None:
    """Fire-and-forget usage logging with error handling."""
    try:
        from bot.storage.memory import store_usage
        await store_usage(**kwargs)
    except Exception as e:
        log.warning(f"Usage log write failed: {e}")


async def _rate_limit_alert(session_key: str, info, level: str,
                           source: str = "", chat_id: str = "",
                           message_thread_id: int | None = None) -> None:
    """Send rate limit alert to the originating admin chat. Cooldown prevents spam."""
    key = f"{info.rate_limit_type}:{level}"
    now = time.time()
    if now - _rate_limit_alert_cooldown.get(key, 0) < RATE_LIMIT_ALERT_COOLDOWN_S:
        return
    _rate_limit_alert_cooldown[key] = now
    icon = "🔴" if level == "alert" else "⚠️"
    pct = f"{info.utilization:.0%}" if info.utilization is not None else "?"
    # Human-readable quota type
    _type_labels = {
        "seven_day": "weekly quota",
        "seven_day_opus": "weekly Opus quota",
        "seven_day_sonnet": "weekly Sonnet quota",
        "five_hour": "5-hour quota",
    }
    type_label = _type_labels.get(info.rate_limit_type or "", (info.rate_limit_type or "quota")[:40])
    # Human-readable reset time
    resets = ""
    if info.resets_at:
        from datetime import datetime, timezone
        try:
            reset_dt = datetime.fromtimestamp(info.resets_at, tz=timezone.utc)
            remaining_min = max(0, int((info.resets_at - now) / 60))
            if remaining_min < 60:
                time_str = f"{remaining_min} min"
            elif remaining_min < 1440:
                hrs = remaining_min // 60
                mins = remaining_min % 60
                time_str = f"~{hrs}h {mins}m" if mins else f"~{hrs}h"
            else:
                days = remaining_min // 1440
                hrs = (remaining_min % 1440) // 60
                time_str = f"~{days}d {hrs}h" if hrs else f"~{days}d"
            resets = f"\n🔄 Resets in {time_str} ({reset_dt.strftime('%a %H:%M UTC')})"
        except (OSError, ValueError):
            pass
    level_label = "approaching limit" if level == "warn" else "near limit — may slow down"
    msg = f"{icon} Claude {type_label} at {pct} — {level_label}{resets}"
    log.warning(f"Rate limit {level}: {info.rate_limit_type} at {pct}")
    try:
        if source == "telegram" and chat_id:
            _cid = _safe_int_chat_id(chat_id)
            if _cid is not None:
                from integrations.telegram import send_message as tg_send
                _rl_kwargs: dict = {"chat_id": _cid}
                if message_thread_id is not None:
                    _rl_kwargs["message_thread_id"] = message_thread_id
                await tg_send(msg, **_rl_kwargs)
        elif source == "teams" and chat_id:
            from integrations.teams import get_client as _teams_get
            _tc = _teams_get()
            if _tc:
                await _tc.send_message(chat_id, msg, text_format="plain")
    except Exception as e:
        log.warning(f"Rate limit alert send failed: {e}")



def _total_input_tokens(usage: dict) -> int:
    """Compute total input tokens including cached tokens.

    SDK reports input_tokens=1 when prompt caching is active. The real context
    size is input_tokens + cache_read_input_tokens + cache_creation_input_tokens.

    NOTE: ResultMessage.usage is CUMULATIVE across all API calls within a single
    query(). For accurate per-call context size, use get_context_usage() (post-query)
    or _read_jsonl_context_tokens() (used by PreCompact hook mid-query).
    This function is still useful for rough logging where cumulative IS acceptable.
    """
    return (usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0))


# JSONL session transcript directory (SDK writes per-call usage here)
_JSONL_DIR = os.path.expanduser("~/.claude/projects/-app")


def _read_jsonl_context_tokens(session_id: str) -> int:
    """Read per-call context tokens from the last assistant entry in JSONL transcript.

    The JSONL session file contains per-API-call usage in each ``assistant`` entry.
    Unlike ResultMessage.usage (which is cumulative across all calls in a query),
    this gives the actual context window usage for the most recent API call —
    i.e. how full the context window really is.

    Returns 0 if the file doesn't exist or no valid entry is found.
    """
    if not session_id or "/" in session_id or "\\" in session_id or ".." in session_id:
        return 0
    jsonl_path = os.path.join(_JSONL_DIR, f"{session_id}.jsonl")
    try:
        with open(jsonl_path, "rb") as f:
            # Seek to end, read last 64KB (plenty for several entries)
            f.seek(0, 2)
            file_size = f.tell()
            chunk_size = min(file_size, 65536)
            f.seek(file_size - chunk_size)
            data = f.read(chunk_size).decode("utf-8", errors="replace")

        # Parse lines in reverse to find last assistant entry with usage
        lines = data.strip().split("\n")
        for line in reversed(lines):
            try:
                entry = json.loads(line)
                if entry.get("type") == "assistant" and "message" in entry:
                    usage = entry["message"].get("usage", {})
                    tokens = (usage.get("input_tokens", 0)
                              + usage.get("cache_read_input_tokens", 0)
                              + usage.get("cache_creation_input_tokens", 0))
                    if tokens > 0:
                        return tokens
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
        return 0
    except FileNotFoundError:
        return 0
    except Exception as e:
        log.warning(f"Failed to read JSONL tokens for {session_id[:12]}...: {e}")
        return 0


def _build_compaction_message(pre_tokens: int = 0, post_tokens: int = 0, model: str = "") -> str:
    """Build compaction notification text with token stats. Always shows all data."""
    info_parts = []
    if pre_tokens > 0:
        pre_pct = pre_tokens * 100 // _context_window_for_model(model)
        info_parts.append(f"{pre_tokens // 1000}k before ({pre_pct}%)")
    if post_tokens > 0:
        post_pct = post_tokens * 100 // _context_window_for_model(model)
        info_parts.append(f"{post_tokens // 1000}k after ({post_pct}%)")
    if pre_tokens > 0 and post_tokens > 0:
        delta = pre_tokens - post_tokens
        if delta > 0:
            info_parts.append(f"{delta // 1000}k freed")
        else:
            info_parts.append(f"{abs(delta) // 1000}k increased")
    info = f" ({', '.join(info_parts)})" if info_parts else ""
    return f"\U0001f9f9 Context compacted — older messages summarized{info}."


def _parse_compaction_session_key(session_key: str) -> tuple[str, str] | None:
    """Extract (source, chat_id) from session_key. Returns None if unparseable."""
    parts = session_key.split(":")
    if len(parts) < 2:
        log.warning(f"Cannot parse session_key for compaction notification: {session_key}")
        return None
    return parts[0], parts[1]


def _extract_thread_id(session_key: str) -> int | None:
    """Extract forum topic thread_id from session_key format source:chat_id:thread_id.

    Returns None if session_key has no thread_id component or if it's not a valid int.
    Safe for all session_key formats: telegram:123, telegram:123:456, system:123, etc.
    """
    parts = session_key.split(":")
    if len(parts) >= 3:
        try:
            return int(parts[2])
        except (ValueError, TypeError):
            return None
    return None


async def _notify_compaction(session_key: str, pre_tokens: int = 0,
                             post_tokens: int = 0, model: str = "") -> int | None:
    """Send compaction notification with pre/post token stats.

    Always sends notification when called — no filtering.
    pre_tokens from PreCompact hook (JSONL), post_tokens from JSONL drop detection.
    """
    try:

        if ":task-" in session_key:
            log.info(f"Compaction in task session {session_key} — notifying parent chat")

        parsed = _parse_compaction_session_key(session_key)
        if not parsed:
            return None
        source, chat_id = parsed

        msg = _build_compaction_message(pre_tokens, post_tokens, model=model)
        log.info(f"Sending compaction notification to {source}:{chat_id}")

        result_msg_id = None
        _cid = _safe_int_chat_id(chat_id)
        _comp_tid = _extract_thread_id(session_key)
        if source == "telegram" and _cid is not None:
            from integrations.telegram import send_message as tg_send
            _comp_kwargs: dict = {"chat_id": _cid}
            if _comp_tid is not None:
                _comp_kwargs["message_thread_id"] = _comp_tid
            result = await tg_send(msg, **_comp_kwargs)
            if result:
                result_msg_id = result.get("message_id")
        elif source == "teams":
            from integrations.teams import get_client as _teams_get
            _tc = _teams_get()
            if _tc:
                await _tc.send_message(chat_id, msg, text_format="plain")
        else:
            log.debug(f"Skipping compaction notification for source: {source}")

        # Mark chat activity so parallel tasks detect compaction-message interleaving
        from bot.hooks import mark_chat_activity
        mark_chat_activity(chat_id, "__compaction__")
        return result_msg_id
    except Exception as e:
        log.warning(f"Compaction notification failed for {session_key}: {e}")
        return None



def _build_error_detail(error: Exception, session_key: str, is_admin: bool) -> str:
    """Build user-facing error message with admin-aware detail level.

    Admins get actionable diagnostics (error type, session key, hint).
    Non-admins get a clean retry message.
    """
    err_str = str(error).lower()
    is_timeout = "timeout" in err_str or "timed out" in err_str
    is_connection = "connection" in err_str or "refused" in err_str or "eof" in err_str
    is_auth = "auth" in err_str or "token" in err_str or "401" in err_str

    if not is_admin:
        if is_timeout:
            return ("\u26a0\ufe0f SDK initialization timed out (MCP servers slow to start). "
                    "Retry by sending your message again.")
        return ("\u26a0\ufe0f Internal error while processing your message. "
                "Retry by sending your message again.")

    # Admin gets structured diagnostics
    err_type = type(error).__name__
    parts = [f"\u26a0\ufe0f <b>Error:</b> <code>{err_type}</code>"]

    if is_timeout:
        parts.append("\U0001f550 <b>Cause:</b> SDK/MCP init timeout (>180s)")
        parts.append("\U0001f4a1 <b>Try:</b> send message again, or <code>diagnose</code>")
    elif is_connection:
        parts.append("\U0001f50c <b>Cause:</b> SDK subprocess connection failed")
        parts.append("\U0001f4a1 <b>Try:</b> send message again (new subprocess)")
    elif is_auth:
        parts.append("\U0001f512 <b>Cause:</b> Authentication/token error")
        parts.append("\U0001f4a1 <b>Try:</b> check credentials, or <code>diagnose</code>")
    else:
        err_preview = str(error)[:150].replace("<", "&lt;").replace(">", "&gt;")
        parts.append(f"\U0001f4cb <b>Detail:</b> {err_preview}")
        parts.append("\U0001f4a1 <b>Try:</b> send message again, or <code>diagnose</code>")

    parts.append(f"\U0001f3f7\ufe0f <code>{session_key}</code>")
    return "\n".join(parts)


# ============================================================
# HIGH-LEVEL FUNCTIONS (called from main.py)
# ============================================================

async def process_incoming(
    source: str,
    user_name: str,
    user_id: str,
    text: str,
    message_id: str = "",
    media_info: dict | None = None,
    on_stream_chunk: Callable | None = None,
    on_thinking_start: Callable | None = None,
    on_tool_status: Callable | None = None,
    chat_id: str = "",
    model: str = MODEL_PRIMARY,
    task_id: str = "",
    other_tasks_context: str = "",
    is_external_chat: bool = False,
    message_thread_id: int | None = None,
) -> str:
    """Process an incoming message from any platform.

    Returns the final response text (may be empty if Claude sent via tool).
    """
    from bot.storage.memory import store_message

    # Per-chat model + OAuth: use chat-specific overrides if set, else defaults
    admin = is_admin_user(source, user_id) if source and user_id else False
    # Session key: include thread_id for forum topics (topics = separate chats).
    # Harness/OAuth lookup uses the SAME composite key — topic → group → default.
    main_session_key = f"{source}:{chat_id or 'default'}"
    if message_thread_id is not None:
        main_session_key = f"{source}:{chat_id}:{message_thread_id}"
    # Model resolution: per-chat override → harness model → global default.
    # get_model_override_only returns None when no override, so we resolve
    # the harness model here to ensure model is always a valid string.
    from config import get_model_override_only as _get_override_only
    model = _get_override_only(main_session_key)
    if model is None:
        from bot.harness import get_harness_for_chat as _get_harness_fn, resolve_field as _resolve_fn
        _h = _get_harness_fn(main_session_key)
        model = _resolve_fn(_h, "model", main_session_key) or MODEL_PRIMARY
    from config import get_oauth_for_chat as _get_oauth
    oauth_label = _get_oauth(main_session_key)

    # RBAC credential gate (CB-31): non-admin users must have the OAuth profile
    # in their harness allowed_credentials. Prevents unauthorized inference on
    # the org's shared OAuth token.
    if not admin:
        try:
            from bot.harness import get_harness_for_chat as _get_h_oauth, resolve_field as _rf_oauth, is_credential_unrestricted as _is_unr_oauth
            _h_oauth = _get_h_oauth(main_session_key)
            _ac_oauth = _rf_oauth(_h_oauth, "allowed_credentials", main_session_key) or []
            if not _is_unr_oauth(_ac_oauth):
                _cred_id = f"claude:{oauth_label}"
                if _cred_id not in _ac_oauth and oauth_label not in _ac_oauth:
                    log.warning("OAuth DENIED: user=%s label=%s not in allowed_credentials=%s",
                                user_id, oauth_label, _ac_oauth)
                    # Send denial directly — returning a string relies on salvage
                    # logic in handlers.py which doesn't fire for early returns.
                    _deny_msg = (
                        "Access denied: your OAuth profile is not authorized.\n\n"
                        "To get access:\n"
                        "• Run /claude-auth <label> in this DM to set up your own profile\n"
                        "• Or ask your admin to grant you shared access"
                    )
                    if source == "telegram" and chat_id:
                        try:
                            from integrations.telegram import send_message as _deny_send
                            _deny_cid = int(chat_id)
                            _deny_kwargs: dict = {"chat_id": _deny_cid}
                            if message_thread_id is not None:
                                _deny_kwargs["message_thread_id"] = message_thread_id
                            await _deny_send(_deny_msg, **_deny_kwargs)
                        except (ValueError, TypeError):
                            pass
                        except Exception as _de:
                            log.debug("Failed to send OAuth denial: %s", _de)
                    return _deny_msg
        except Exception as e:
            log.warning("OAuth credential check FAILED (denying): %s", e)
            _fail_msg = "Access denied: credential check failed. Please try again or contact your admin."
            if source == "telegram" and chat_id:
                try:
                    from integrations.telegram import send_message as _fail_send
                    _fail_cid = int(chat_id)
                    _fail_kw: dict = {"chat_id": _fail_cid}
                    if message_thread_id is not None:
                        _fail_kw["message_thread_id"] = message_thread_id
                    await _fail_send(_fail_msg, **_fail_kw)
                except (ValueError, TypeError):
                    pass
                except Exception as _fe:
                    log.debug("Failed to send credential-check denial: %s", _fe)
            return _fail_msg

    # Pre-check: verify OAuth token is available before creating SDK session.
    # Without a valid token, the SDK subprocess fails silently. Better to tell
    # the user upfront and guide them to /claude-auth.
    from config import get_oauth_token as _get_token_check, CLAUDE_CREDENTIALS_PATH
    _pre_token = _get_token_check(oauth_label)
    if not _pre_token and not CLAUDE_CREDENTIALS_PATH.exists() and not CLAUDE_CODE_OAUTH_TOKEN:
        log.warning("No Claude OAuth token available for session %s (label=%s)", main_session_key, oauth_label)
        _notoken_msg = (
            "I don't have Claude API access configured for this session.\n\n"
            "Run /claude-auth <label> to set up your Claude access.\n"
            "Example: /claude-auth main\n\n"
            "Or ask your admin to grant you access to a shared profile."
        )
        if source == "telegram" and chat_id:
            try:
                from integrations.telegram import send_message as _nt_send
                _nt_cid = int(chat_id)
                _nt_kw: dict = {"chat_id": _nt_cid}
                if message_thread_id is not None:
                    _nt_kw["message_thread_id"] = message_thread_id
                await _nt_send(_notoken_msg, **_nt_kw)
            except (ValueError, TypeError):
                pass
            except Exception as _ne:
                log.debug("Failed to send no-token message: %s", _ne)
        return _notoken_msg

    # Store incoming message (with chat_id for data isolation)
    try:
        _store_tid = str(message_thread_id) if message_thread_id is not None else ""
        await store_message(source, user_name, text, "user", user_id, message_id,
                            chat_id=chat_id, message_thread_id=_store_tid)
    except Exception as e:
        log.warning(f"Failed to store incoming message: {e}")

    # Session key: always use main session per chat (no fork sessions).
    # Forum topics get their own session via composite key (topics = separate chats).
    session_key = main_session_key

    # Build the user prompt
    media_line = ""
    if media_info:
        mt = media_info.get("media_type", "file")
        fname = media_info.get("filename", "") or (media_info.get("local_path") or "").rsplit("/", 1)[-1]
        # Sanitize filename to prevent prompt injection via crafted filenames
        fname = fname.replace("[", "(").replace("]", ")").replace("\n", " ").strip()
        file_size = media_info.get("file_size", 0)
        size_str = f"{file_size / 1024 / 1024:.1f}MB" if file_size >= 1024 * 1024 else f"{file_size / 1024:.0f}KB" if file_size > 0 else ""
        size_tag = f" ({size_str})" if size_str else ""
        description = media_info.get("description")
        if description:
            # Large file: Gemini description pre-generated, no file path (prevents SDK crash)
            media_line = f"\n[{mt.upper()}: {fname}{size_tag} — AI description: {description}]"
            media_line += "\nNote: This file was too large for inline reading. Use search_media to find it if you need to send it."
        else:
            path = media_info.get("prompt_path") or media_info.get("local_path", "")
            media_line = f"\n[{mt.upper()} attached: {path}{size_tag}]"
        transcript = media_info.get("transcript")
        if transcript:
            media_line += f"\nVoice transcript: \"{transcript}\""

    _platform_tags = {"telegram": "[Telegram]", "teams": "[Teams]"}
    platform_tag = _platform_tags.get(source, f"[{source}]")
    if source == "telegram":
        # Corporate mode: chat_id must be provided. No primary group fallback.
        reply_instr = f'Reply via telegram_send_message with parse_mode="HTML" and chat_id={chat_id}.'
    elif source == "teams":
        reply_instr = f'Reply via teams_send_message with chat_id="{chat_id}".'
    else:
        reply_instr = (
            f'Reply via telegram_send_message with parse_mode="HTML" and chat_id={chat_id}.'
        )

    # Check if this will be a fresh session (no prior context)
    session_reset_note = ""
    if not task_id:
        # For main sessions: detect if we're creating a brand-new session
        # (not resuming from prior conversation). If so, tell the model to
        # proactively load recent context to restore continuity.
        existing_mc = client_pool.get_client(session_key)
        if not existing_mc:
            # No in-memory client — check if we can resume from DB
            has_saved = await client_pool.load_session_id(session_key)
            if not has_saved:
                session_reset_note = (
                    "\n\n⚠️ SESSION RESET: Your conversation session was reset "
                    "(due to bot restart, re-authentication, or error recovery). "
                    "You have NO prior conversation context. "
                    "IMMEDIATELY call `get_recent_conversation` to load recent messages "
                    "and restore context before processing this message. "
                    "This is critical for continuity — do it FIRST."
                )
                log.info(f"Fresh session detected for {session_key}, injecting context-restore instruction")

    # Check if session was recently compacted — inject context-reload hint
    compaction_note = ""
    if not task_id and not session_reset_note:
        from bot.hooks import compacted_sessions
        if session_key in compacted_sessions:
            compacted_sessions.discard(session_key)
            compaction_note = (
                "\n\n⚠️ CONTEXT COMPACTED: Your conversation context was recently compacted "
                "(summarized by the SDK). You may have lost detailed message history. "
                "Call `get_recent_conversation` to reload recent messages if you need "
                "specific details from prior discussions."
            )
            log.info(f"Post-compaction context hint injected for {session_key}")

    other_ctx_line = f"\n\n{other_tasks_context}" if other_tasks_context else ""

    # Session awareness (simplified — no fork sessions, sequential per chat)
    session_awareness = ""
    user_prompt = (
        f"{session_awareness}"
        f"NEW MESSAGE {platform_tag} from {user_name} "
        f"(user ID: {user_id}, message ID: {message_id}):\n"
        f"{text}{media_line}\n\n"
        f"IMPORTANT: A 'Thinking...' status was already sent automatically. "
        f"Do NOT send any status/acknowledgment message. "
        f"Your FIRST send_message call must be the actual reply with real content.\n"
        f"You MUST reply to every message — never ignore or skip. "
        f"Even for short messages like 'test' or 'hi', respond conversationally.\n"
        f"Process and reply. {reply_instr}"
        f"{session_reset_note}"
        f"{compaction_note}"
        f"{other_ctx_line}"
    )

    # Detect language from original user text (before enrichment).
    # Falls back to recent messages from the same user when text is short or ambiguous.
    detected_lang = await _detect_language_with_fallback(
        text, user_id=user_id, chat_id=chat_id, source=source
    )
    if detected_lang:
        log.debug(f"Language detected: {detected_lang} for {session_key}")

    # Reset send tracking and turn counter (explicit session_key — no globals)
    set_send_tool_called(False, session_key)
    set_intentional_silence(False, session_key=session_key)
    set_tool_use_counter(0, session_key=session_key)
    # Deploy gating: /deploy command sets confirmed flag; /deploy force|no-qa set variants
    _deploy_confirmed = check_deploy_approval(text) if text else False
    set_deploy_state(
        session_key,
        _deploy_confirmed,
        force=check_deploy_force(text) if text else False,
        noqa=check_deploy_noqa(text) if text else False,
    )
    # Outbound action gate (FB-73): /send command approves email/phone sends
    from bot.core.session_state import set_outbound_approved
    set_outbound_approved(session_key, check_outbound_approval(text) if text else False)
    # Extend-budget gating: admin-only MCP tool that raises the session's
    # max_budget_usd cap mid-conversation. Never auto-invoked — requires the
    # user's current message to contain "extend budget" / "увеличь бюджет".
    set_extend_budget_state(session_key, check_extend_budget_approval(text) if text else False)

    # Stream response
    final_text = ""
    all_text_blocks: list[str] = []  # accumulate ALL TextBlock text for fact extraction
    thinking_notified = False
    compaction_notified = False  # track whether compaction notification was sent
    tools_used = []
    result_usage: dict | None = None  # captured from ResultMessage for rate limit check
    result_num_turns: int = 0
    result_cost: float | None = None
    query_output_tokens = 0  # intra-query output token accumulator
    # Output token cap: harness override → model-based default
    from bot.harness import get_harness_for_chat, resolve_field
    _h_out = get_harness_for_chat(session_key)
    _h_cap = resolve_field(_h_out, "output_token_cap", session_key)
    # 0 = unlimited (skip cap enforcement); None = use model default
    if _h_cap is not None and _h_cap > 0:
        query_output_cap = _h_cap
    elif _h_cap == 0:
        query_output_cap = 0  # unlimited — cap check skipped below
    else:
        query_output_cap = QUERY_OUTPUT_CAP_HEAVY if "opus" in model else QUERY_OUTPUT_CAP_DEFAULT
    query_interrupted = False  # set True if we call interrupt()
    seen_msg_ids: set[str] = set()  # dedup parallel tool call usage
    query_start = time.time()

    # Store query start time in session state for PostToolUse heartbeat nudges
    try:
        from bot.core.session_state import _get_or_create_state as _gocs_hb
        _hb_state = _gocs_hb(session_key)
        _hb_state["query_start_time"] = query_start
        _hb_state["heartbeat_count"] = 0
    except Exception as _exc:
        log.debug("Suppressed: %s", _exc)
    try:
        async for message in client_pool.query(
            session_key=session_key,
            prompt=user_prompt,
            model=model,
            source=source,
            user_id=user_id,
            user_name=user_name,
            on_tool_status=on_tool_status,
            is_external_chat=is_external_chat,
            detected_language=detected_lang,
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ThinkingBlock):
                        if not thinking_notified and on_thinking_start:
                            thinking_notified = True
                            try:
                                await on_thinking_start()
                            except Exception as e:
                                log.warning(f"Thinking start callback error: {e}")
                    elif isinstance(block, TextBlock):
                        final_text = block.text
                        if block.text:
                            all_text_blocks.append(block.text)
                        if on_stream_chunk:
                            try:
                                await on_stream_chunk(final_text)
                            except Exception as e:
                                log.warning(f"Stream chunk callback error: {e}")
                    elif isinstance(block, ToolUseBlock):
                        # Track tool calls for status updates
                        bare_name = block.name.split("__")[-1]
                        tools_used.append(bare_name)
                        if bare_name in SEND_TOOLS:
                            set_send_tool_called(True, session_key)
                        if on_tool_status:
                            try:
                                await on_tool_status([bare_name], block.input)
                            except Exception as e:
                                log.warning(f"Tool status callback error: {e}")
                # --- Intra-query output token tracking ---
                # Deduplicate by message_id (parallel tool calls share same ID+usage)
                msg_id = getattr(message, 'message_id', None)
                if msg_id and msg_id not in seen_msg_ids:
                    seen_msg_ids.add(msg_id)
                    if message.usage:
                        query_output_tokens += message.usage.get("output_tokens", 0)
                        if not query_interrupted and query_output_cap > 0 and query_output_tokens > query_output_cap:
                            query_interrupted = True
                            log.warning(f"Per-query output cap hit: {query_output_tokens}/{query_output_cap} "
                                        f"for {session_key} — interrupting")
                            mc = client_pool.get_client(session_key)
                            if mc and mc.client:
                                mc.client.interrupt()
                # Immediate compaction detection: read JSONL for context drop.
                # Verified from real data: JSONL entries are written before
                # AssistantMessage is emitted (observed in session f496fb8f,
                # entries [249]→[250] show 178k→57k drop between consecutive
                # assistant entries within the same query).
                if not compaction_notified:
                    from bot.hooks import pending_compaction, compacted_sessions
                    compact_info = pending_compaction.get(session_key)
                    if compact_info:
                        pre_tokens = compact_info.get("approx_tokens", 0)
                        if pre_tokens > 0:
                            mc = client_pool.get_client(session_key)
                            post_tokens = _read_jsonl_context_tokens(mc.session_id) if mc else 0
                            if post_tokens > 0 and post_tokens < pre_tokens:
                                pending_compaction.pop(session_key, None)
                                log.info(f"Compaction detected (JSONL drop) for {session_key}: "
                                         f"pre {pre_tokens // 1000}k → post {post_tokens // 1000}k tokens")
                                if ":task-" not in session_key:
                                    compacted_sessions.add(session_key)
                                await _notify_compaction(session_key, pre_tokens, post_tokens, model=model)
                                compaction_notified = True
            elif isinstance(message, RateLimitEvent):
                info = message.rate_limit_info
                try:
                    from config_overrides import get_oauth_for_chat
                    _rl_oauth = get_oauth_for_chat(session_key)
                except Exception:
                    _rl_oauth = "default"
                _update_rate_limit_state(info, oauth_label=_rl_oauth)
                if info.utilization is not None:
                    _rl_tid = _extract_thread_id(session_key)
                    if info.utilization >= RATE_LIMIT_ALERT_PCT:
                        if admin:
                            asyncio.create_task(_rate_limit_alert(session_key, info, "alert", source, chat_id, message_thread_id=_rl_tid))
                    elif info.utilization >= RATE_LIMIT_WARN_PCT:
                        if admin:
                            asyncio.create_task(_rate_limit_alert(session_key, info, "warn", source, chat_id, message_thread_id=_rl_tid))
            # --- Background task lifecycle messages ---
            # SDK emits Task* messages while ResultMessage is held back during
            # background task execution (run_in_background=true on Bash/Agent).
            # Reuse on_tool_status to update last_sdk_activity (prevents false
            # zombie detection) and show bg task status in the streaming placeholder.
            elif isinstance(message, TaskStartedMessage):
                log.info(f"Background task started: id={message.task_id} "
                         f"type={message.task_type} desc={message.description[:100]!r}")
                if on_tool_status:
                    try:
                        await on_tool_status([f"bg:{message.description[:30]}"], {})
                    except Exception as e:
                        log.warning(f"BG task start callback error: {e}")
            elif isinstance(message, TaskProgressMessage):
                _tool = message.last_tool_name or "running"
                log.debug(f"Background task progress: id={message.task_id} "
                          f"tool={_tool} desc={message.description[:100]!r}")
                if on_tool_status:
                    try:
                        await on_tool_status([f"bg:{_tool[:30]}"], {})
                    except Exception as e:
                        log.warning(f"BG task progress callback error: {e}")
            elif isinstance(message, TaskNotificationMessage):
                log.info(f"Background task done: id={message.task_id} "
                         f"status={message.status} summary={message.summary[:100]!r}")
                if on_tool_status:
                    try:
                        _status_label = f"bg:done({message.status})"
                        await on_tool_status([_status_label], {})
                    except Exception as e:
                        log.warning(f"BG task notification callback error: {e}")
            elif isinstance(message, ResultMessage):
                elapsed = time.time() - query_start
                cost_str = f"${message.total_cost_usd:.4f}" if message.total_cost_usd else "?"
                model_label = "Sonnet" if "sonnet" in model else "Opus"
                # Track usage for compaction notifications
                mc = client_pool.get_client(session_key)
                if mc and message.usage:
                    mc.last_usage = message.usage
                usage_str = ""
                if message.usage:
                    _inp = message.usage.get("input_tokens", 0)
                    out = message.usage.get("output_tokens", 0)
                    cache_read = message.usage.get("cache_read_input_tokens", 0)
                    cache_create = message.usage.get("cache_creation_input_tokens", 0)
                    # Rough ctx estimate from cumulative usage (for log line only).
                    # Accurate per-call ctx is saved by post-query get_context_usage().
                    total_ctx = _total_input_tokens(message.usage)
                    ctx_pct = total_ctx * 100 // _context_window_for_model(model) if total_ctx > 0 else 0
                    usage_str = f" | ctx~{total_ctx // 1000}k/{ctx_pct}% tokens_out={out} turns={message.num_turns or 0}"
                    if cache_read or cache_create:
                        usage_str += f" cache_read={cache_read} cache_create={cache_create}"
                effort_label = get_effort_level()
                log.info(
                    f"Query complete: {session_key} | {model_label} effort={effort_label} | {elapsed:.1f}s | "
                    f"turns={message.num_turns} cost={cost_str} | "
                    f"tools=[{', '.join(tools_used)}] | "
                    f"sent_via_tool={get_send_tool_called(session_key)} | "
                    f"result={len(message.result or '')} chars{usage_str}"
                )
                result_usage = message.usage
                result_num_turns = message.num_turns or 0
                result_cost = message.total_cost_usd
                # Cache session-cumulative cost on mc so LIVE CONTEXT header
                # can show accrued spend + remaining budget. The SDK reports
                # total_cost_usd as running total within the current CLI
                # subprocess (resets when extend_budget spawns a fresh CLI).
                if message.total_cost_usd is not None:
                    mc.session_cost_usd = float(message.total_cost_usd)
                if message.result:
                    final_text = message.result
                # Persist usage to SQLite + anomaly check
                await _safe_store_usage(
                    session_key=session_key, model=model,
                    cost_usd=message.total_cost_usd,
                    usage=message.usage, num_turns=message.num_turns,
                    duration_s=elapsed, query_preview=(text or "")[:100],
                    oauth_profile=oauth_label,
                )
                if message.total_cost_usd and message.total_cost_usd > QUERY_COST_WARN:
                    log.warning(f"Expensive query: {session_key} cost=${message.total_cost_usd:.2f} "
                                f"turns={message.num_turns} model={model}")
                # Only warn about turn limit if one is actually set (not unlimited)
                _effective_max = mc.max_turns_cap
                if _effective_max > 0 and (message.num_turns or 0) >= _effective_max - 5:
                    log.warning(f"Near max_turns: {session_key} turns={message.num_turns}/{_effective_max}")

                # --- Session budget cap detection ---
                if message.subtype == "error_max_budget_usd":
                    main_key = session_key.split(":task-")[0] if ":task-" in session_key else session_key
                    log.warning(f"Session budget exhausted for {main_key}: "
                                f"${message.total_cost_usd} (subtype={message.subtype})")
                    await client_pool.reset_session(main_key)
                    budget_msg = ("\u26a0\ufe0f Session budget reached. Session has been reset.\n"
                                  "Send your message again to continue with a fresh session.")
                    try:
                        _cid = _safe_int_chat_id(chat_id)
                        _budget_tid = _extract_thread_id(session_key)
                        if source == "telegram" and _cid is not None:
                            from integrations.telegram import send_message as tg_send
                            _bk: dict = {"chat_id": _cid}
                            if _budget_tid is not None:
                                _bk["message_thread_id"] = _budget_tid
                            await tg_send(budget_msg, **_bk)
                    except Exception as e:
                        log.warning(f"Budget cap notification failed: {e}")

                # --- Per-query output cap notification ---
                if query_interrupted:
                    cap_msg = (f"\u26a0\ufe0f Output limit reached ({query_output_tokens:,} tokens). "
                               f"Send \"continue\" to resume where I left off.")
                    try:
                        _cid = _safe_int_chat_id(chat_id)
                        _cap_tid = _extract_thread_id(session_key)
                        if source == "telegram" and _cid is not None:
                            from integrations.telegram import send_message as tg_send
                            _ck: dict = {"chat_id": _cid}
                            if _cap_tid is not None:
                                _ck["message_thread_id"] = _cap_tid
                            await tg_send(cap_msg, **_ck)
                    except Exception as e:
                        log.warning(f"Output cap notification failed: {e}")

        # Execute deferred session reset (from switch_model during this query)
        if session_key in _pending_session_resets:
            _pending_session_resets.discard(session_key)
            main_key = session_key.split(":task-")[0] if ":task-" in session_key else session_key
            log.info(f"Executing deferred session reset for model switch: {main_key}")
            await client_pool.reset_session(main_key)

        # Execute deferred budget-driven disconnect (from extend_budget during
        # this query). Unlike reset_session, this preserves SQLite session_id
        # + the budget override — next query respawns CLI with --resume UUID +
        # the new max_budget_usd, conversation context restored via JSONL replay.
        if session_key in _pending_budget_disconnects:
            _pending_budget_disconnects.discard(session_key)
            main_key = session_key.split(":task-")[0] if ":task-" in session_key else session_key
            log.info(f"Executing deferred budget disconnect: {main_key}")
            await client_pool._disconnect_client(main_key)

        # Post-query: log context usage from SDK API (informational only).
        # Compaction notification already sent during streaming (JSONL drop detection).
        mc = client_pool.get_client(session_key)
        if mc and mc.client:
            try:
                ctx_usage = await mc.client.get_context_usage()
                ctx_total = ctx_usage.get("totalTokens", 0)
                ctx_max = ctx_usage.get("rawMaxTokens", 0)
                ctx_pct_sdk = ctx_usage.get("percentage", 0)
                if ctx_total > 0:
                    log.info(f"Context usage (SDK): {session_key} | "
                             f"total={ctx_total // 1000}k/{ctx_pct_sdk:.0f}% "
                             f"max={ctx_max // 1000}k")
            except Exception as ctx_err:
                log.debug(f"get_context_usage() unavailable for {session_key}: {ctx_err}")
        # Clean up pending_compaction if streaming handler missed it
        # (e.g. JSONL unavailable or pre_tokens was 0)
        from bot.hooks import pending_compaction
        leftover = pending_compaction.pop(session_key, None)
        if leftover:
            log.warning(f"Compaction for {session_key} was not reported during streaming "
                        f"(pre={leftover.get('approx_tokens', 0)}) — JSONL may have been unavailable")
    except Exception as e:
        # Fatal error during query — clear session to prevent pollution on retry
        log.error(f"Fatal error in query for {session_key}: {e}", exc_info=True)
        await client_pool.disconnect_client(session_key)
        if ":task-" not in session_key:
            await client_pool.clear_session_id(session_key)
        # Notify user about the failure with admin-aware diagnostics
        admin = is_admin_user(source, user_id) if source and user_id else False
        error_msg = _build_error_detail(e, session_key, admin)
        parse_mode = "HTML" if admin else None
        try:
            _cid = _safe_int_chat_id(chat_id)
            _err_tid = _extract_thread_id(session_key)
            if source == "telegram" and _cid is not None:
                from integrations.telegram import send_message as tg_send
                _ek: dict = {"chat_id": _cid}
                if parse_mode:
                    _ek["parse_mode"] = parse_mode
                if _err_tid is not None:
                    _ek["message_thread_id"] = _err_tid
                await tg_send(error_msg, **_ek)
        except Exception as notify_err:
            log.warning(f"Failed to notify user about fatal error: {notify_err}")
    finally:
        # Always disconnect task-specific (ephemeral) sessions to avoid leaking subprocesses
        if ":task-" in session_key:
            await client_pool.disconnect_client(session_key)

    # --- Rate limit detection ---
    # SDK returns rate limit as normal ResultMessage.result text (not exception).
    # Robust detection requires ALL 3 signals (eliminates false positives):
    #   1. Text matches known rate limit patterns
    #   2. Zero tokens (no API call was actually made)
    #   3. No tools were called (real responses always have tokens > 0)
    if final_text and _RATE_LIMIT_PATTERNS.search(final_text):
        inp_tokens = result_usage.get("input_tokens", -1) if result_usage else -1
        out_tokens = result_usage.get("output_tokens", -1) if result_usage else -1
        is_rate_limit = (
            inp_tokens == 0
            and out_tokens == 0
            and not tools_used
        )
        if is_rate_limit:
            elapsed = time.time() - query_start
            log.warning(
                f"RATE_LIMIT detected for {session_key} "
                f"(tokens=0/0, turns={result_num_turns}, "
                f"elapsed={elapsed:.1f}s): {final_text}"
            )
            # PRESERVE session — don't reset. Rate limits are transient;
            # destroying the session loses valuable conversation context.
            # Disconnect the CLI subprocess (will reconnect on next query)
            # but keep the session_id so it can resume.
            await client_pool.disconnect_client(session_key)
            # Return verbatim SDK message — do NOT store as bot response
            return final_text

    # --- Suspicious response detection ---
    # Check for responses that indicate session corruption, API anomalies, or caching.
    # Debug-only: extensive logging + admin notification. No retries.
    elapsed_final = time.time() - query_start
    diagnosis = _check_suspicious_response(
        elapsed=elapsed_final,
        num_turns=result_num_turns,
        tools_used=tools_used,
        send_tool_called=get_send_tool_called(session_key),
        result_text=final_text,
        usage=result_usage,
        session_key=session_key,
        model=model,
        cost=result_cost,
    )
    if diagnosis:
        # Fire-and-forget notification — don't block response delivery
        asyncio.create_task(
            _notify_suspicious_response(diagnosis, session_key, source, chat_id)
        )
        # Safety net: disconnect client so next query gets a clean connection.
        # The buffer drain (above) is the primary fix; this catches any edge cases
        # where stale data still gets through (e.g. messages arriving between drain
        # and query() call). Fire-and-forget to avoid blocking response delivery.
        async def _disconnect_suspicious(sk: str):
            try:
                log.warning(f"Auto-disconnecting {sk} after suspicious response "
                            f"(safety net for buffer contamination)")
                await client_pool.disconnect_client(sk)
            except Exception as disc_err:
                log.warning(f"Failed to disconnect {sk} after suspicious: {disc_err}")
        asyncio.create_task(_disconnect_suspicious(session_key))

    # --- Turn-limit safety net ---
    # Detect: model hit the turn cap without sending a final summary.
    # The model may have sent an early status message (e.g. "Starting Fora work...")
    # but then burned 60+ turns on browser/tool ops without a final reply.
    # Detection: no send tool in the TAIL of tools_used (last ~10 tool calls ≈ 5 turns).
    # Only fires when query is DONE (turns >= MAX_TURNS) — model has no more chances.
    _TAIL_CHECK = 10  # last ~10 tool calls to check for recent send
    if (result_num_turns >= MAX_TURNS and not final_text
            and source and chat_id and tools_used):
        tail = tools_used[-_TAIL_CHECK:]
        recent_send = any(t.startswith(("telegram_send_", "teams_send_")) for t in tail)
        if not recent_send:
            turn_limit_msg = ("\u26a0\ufe0f Ran out of processing turns without sending results. "
                              "Reply \u2018continue\u2019 to resume where I left off.")
            log.warning(f"Turn-limit safety net: {session_key} hit {result_num_turns}/{MAX_TURNS} turns, "
                        f"no send in last {len(tail)} tools ({tail[-3:]}) — notifying {source}:{chat_id}")
            try:
                _cid = _safe_int_chat_id(chat_id)
                _tl_tid = _extract_thread_id(session_key)
                if source == "telegram" and _cid is not None:
                    from integrations.telegram import send_message as tg_send
                    _tk: dict = {"chat_id": _cid}
                    if _tl_tid is not None:
                        _tk["message_thread_id"] = _tl_tid
                    await tg_send(turn_limit_msg, **_tk)
            except Exception as e:
                log.error(f"Turn-limit safety net send failed: {e}")

    # --- Budget exhaustion safety net ---
    # Detect: model likely hit the per-query USD budget cap without sending results.
    # Extract memory updates from ALL text blocks (intermediate + final).
    # The model may emit FACTS_UPDATE in an intermediate turn (before a tool call),
    # then produce a different final response. Using only final_text would lose those facts.
    # Combine all_text_blocks to catch FACTS_UPDATE from every turn.
    facts_text = "\n".join(all_text_blocks) if all_text_blocks else final_text
    if facts_text:
        try:
            source_msg_id = int(message_id) if message_id else None
        except (ValueError, TypeError):
            source_msg_id = None
        try:
            # Pass source_chat_id for data isolation — facts created in external
            # chats are tagged with that chat's ID so they can't pollute admin/shared data
            # Pass allowed_domains for cross-domain fact injection prevention
            from bot.hooks import get_access_context
            _ac = get_access_context(session_key)
            _allowed_doms = _ac.allowed_domains if _ac else ()
            # CB-94: Pass write_domain for auto-routing facts to shared domain
            _write_dom = ""
            try:
                from bot.core.session_state import _get_or_create_state as _gocs_wd
                _write_dom = _gocs_wd(session_key).get("write_domain", "")
            except Exception as _exc:
                log.debug("Suppressed: %s", _exc)
            await _persist_memory_updates(
                facts_text, source_msg_id=source_msg_id, source_chat_id=chat_id,
                source=source, allowed_domains=_allowed_doms, write_domain=_write_dom,
            )
        except Exception as e:
            log.warning(f"Failed to extract/persist memory updates: {e}")

    # Strip FACTS_UPDATE blocks from display text (not meant for chat)
    raw_text = final_text
    if final_text:
        final_text = strip_facts_update(final_text)

    # If model output was entirely FACTS_UPDATE (no display text remains)
    # and no send tool was called — log it but do NOT set intentional silence.
    # The user should always get a response; main.py Branch 4 handles this
    # by salvaging or showing a brief acknowledgment.
    if raw_text and not (final_text or "").strip() and not get_send_tool_called(session_key):
        log.warning("Model output was entirely FACTS_UPDATE with no reply — user will see salvage/ack")

    # Fallback send: if Claude returned text but never called a send tool
    sent_reply = get_send_tool_called(session_key)
    # Filter non-reply garbage text (model sometimes outputs these instead of replying)
    text_lower = final_text.strip().lower() if final_text else ""
    if text_lower in _GARBAGE_RESPONSES:
        log.warning(f"Claude returned garbage response, suppressing: {final_text[:80]}")
        final_text = ""
    elif final_text and not sent_reply and any(re.search(p, text_lower) for p in _get_silence_patterns()):
        # Check if the original message is actually tagging the bot by its own name
        # or @handle. If so, the user is addressing the bot — don't suppress.
        _orig_stripped = text.strip().lower() if text else ""
        _is_bot_tag = False
        try:
            from integrations.telegram import get_my_names
            _bot_first, _bot_handle = get_my_names()
            _bot_names = [n.lower() for n in (_bot_first, _bot_handle) if n]
            _is_bot_tag = any(n in _orig_stripped for n in _bot_names)
        except Exception as _exc:
            log.debug("Suppressed: %s", _exc)
        if _is_bot_tag:
            log.info(f"Silence pattern matched but message tags the bot "
                     f"({_bot_names}) — NOT suppressing")
        else:
            log.warning(f"Claude explained why not replying — suppressing: {final_text[:80]}")
            final_text = ""
            set_intentional_silence(True, session_key=session_key)
    # Fallback send for non-streaming sources (WA). TG streaming has on_stream_chunk set,
    # so this fallback is intentionally skipped — TG salvage is handled in main.py's
    # handle_telegram_message finally block (checks response_text when tools ran but no send).
    if final_text and not sent_reply and not on_stream_chunk:
        log.warning(f"Claude didn't send reply via tool — fallback sending: {final_text[:80]}")
        try:
            if source == "telegram":
                _cid = _safe_int_chat_id(chat_id)
                _fb_tid = _extract_thread_id(session_key)
                if _cid:
                    from integrations.telegram import send_message as tg_send
                    _fk: dict = {"chat_id": _cid}
                    if _fb_tid is not None:
                        _fk["message_thread_id"] = _fb_tid
                    await tg_send(final_text, **_fk)
                else:
                    log.error("Fallback send: no valid TG chat_id")
            elif source == "teams":
                if chat_id:
                    from integrations.teams import get_client as _get_teams
                    _tc = _get_teams()
                    if _tc:
                        await _tc.send_message(chat_id, final_text)
                else:
                    log.error("Fallback send: no valid Teams chat_id")
            else:
                log.warning(f"Fallback send: unsupported source '{source}'")
        except Exception as e:
            log.error(f"Fallback send failed: {e}")

    # Store clean bot response (without FACTS_UPDATE blocks)
    if final_text:
        try:
            _resp_tid = str(message_thread_id) if message_thread_id is not None else ""
            await store_message(source, "Bot", final_text, "assistant",
                                chat_id=chat_id, message_thread_id=_resp_tid)
        except Exception as e:
            log.warning(f"Failed to store bot response: {e}")

    return final_text


async def process_system_task(prompt: str, model: str | None = None,
                              chat_id: str = "",
                              system_prompt: str = "",
                              max_budget_usd: float = 0,
                              allowed_tools: list[str] | None = None,
                              session_key_suffix: str = "",
                              project_dir: str = "",
                              harness_label: str = "") -> str:
    """Process a system-initiated task (proactive, catchup, email check, PM agent).

    Uses a fresh 'system:...' session each time — disconnected after use
    to prevent context accumulation and ensure the latest CLI context limits
    (e.g. 1M for Opus 4.6) are applied.

    chat_id: originating chat whose AccessContext the task inherits.
    Empty = admin-level access (all facts visible).  When set, the task
    sees facts scoped to that chat (admin DM → all, primary group → shared,
    external chat → own data only).

    system_prompt: optional system prompt override. When provided, used as
    the SDK system prompt (enables Claude API prompt caching on fixed prefixes).
    The `prompt` becomes the user message only.

    max_budget_usd: optional per-task budget cap. When >0, passed to the SDK
    session to enforce a hard spend limit on this agent dispatch.

    allowed_tools: optional tool allowlist for this task. When provided,
    sets the harness allowed_tools for this session, restricting which MCP
    tools the agent can use (prefix matching). Used by PM to enforce
    read-only access for Architect/TL/Security roles.

    session_key_suffix: optional suffix for unique session keys. PM uses
    this for per-ticket+role isolation (e.g. 'pm-42-architect').

    project_dir: explicit working directory for this task. When set,
    overrides the default CWD resolution so agents write to the correct
    project folder. Used by PM to ensure all pipeline agents share the
    same workspace as the calling coding chat.

    harness_label: harness to assign to the agent session. When set,
    the agent inherits model, effort, max_turns, project_dir from the
    calling chat's harness. Role-specific allowed_tools override the
    harness allowed_tools. Cleaned up in finally.
    """
    base_key = f"system:{chat_id}" if chat_id else "system:ephemeral"
    session_key = f"{base_key}:{session_key_suffix}" if session_key_suffix else base_key
    # model=None means "inherit from harness" — resolved in _build_options.
    # Don't resolve here to preserve the "inherit" signal.

    # Assign harness to agent session — inherits model, effort, max_turns,
    # project_dir. Canonical default for system tasks is "admin" harness
    # (inherits Opus 1M, high effort, admin data_scope).
    # Per-role allowed_tools override the harness defaults (set below).
    if not harness_label:
        harness_label = "admin"  # system tasks use admin harness by default
    from bot.harness import set_chat_harness
    try:
        set_chat_harness(session_key, harness_label)
    except ValueError:
        log.warning("process_system_task: unknown harness '%s', skipping", harness_label)
        harness_label = ""  # disable cleanup in finally

    # Apply per-task tool restrictions via harness override
    if allowed_tools is not None:
        from bot.harness import set_chat_override
        set_chat_override(session_key, "allowed_tools", allowed_tools)

    # Apply per-task project directory override (defense-in-depth validation)
    if project_dir:
        if ".." in project_dir or not project_dir.startswith("/app/data/"):
            log.warning("process_system_task: rejected unsafe project_dir=%s", project_dir)
            project_dir = ""  # fall back to default CWD
        else:
            from bot.harness import set_chat_override
            set_chat_override(session_key, "project_dir", project_dir)

    # Apply per-task budget cap
    if max_budget_usd > 0:
        _session_budget_overrides[session_key] = max_budget_usd

    # Disconnect any stale session so query() creates a fresh one
    # with the current CLI's context window settings.
    await client_pool.disconnect_client(session_key)

    # Resolve output_token_cap from harness (same enforcement as main chat query)
    from bot.harness import get_harness_for_chat, resolve_field as _resolve_field
    _h_otc = get_harness_for_chat(session_key)
    _h_cap = _resolve_field(_h_otc, "output_token_cap", session_key)
    if _h_cap is not None and _h_cap > 0:
        _output_cap = _h_cap
    elif _h_cap == 0:
        _output_cap = 0  # unlimited
    else:
        _output_cap = QUERY_OUTPUT_CAP_HEAVY if (model and "opus" in model) else QUERY_OUTPUT_CAP_DEFAULT
    _output_tokens = 0
    _seen_ids: set[str] = set()

    final_text = ""
    try:
        async for message in client_pool.query(
            session_key=session_key,
            prompt=prompt,
            model=model,
            system_prompt=system_prompt or None,
        ):
            text_content = _extract_text(message)
            if text_content:
                final_text = text_content
            # Output token cap enforcement
            if _output_cap > 0 and hasattr(message, "usage"):
                _mid = getattr(message, "id", "") or ""
                if _mid and _mid not in _seen_ids:
                    _seen_ids.add(_mid)
                    _ot = (message.usage or {}).get("output_tokens", 0) or 0  # H2 fix: usage is dict, not object
                    _output_tokens += _ot
                    if _output_tokens > _output_cap:
                        _mc = client_pool._clients.get(session_key)
                        if _mc:
                            _mc.client.interrupt()
                        log.warning("System task %s: output token cap exceeded (%d > %d)",
                                    session_key, _output_tokens, _output_cap)
    finally:
        # Always disconnect after use — system tasks are one-shot,
        # no benefit from session persistence (and it causes stale
        # metadata + context accumulation issues).
        try:
            await client_pool.disconnect_client(session_key)
        except Exception as e:
            log.error(f"Failed to disconnect ephemeral session: {e}")
        # Clean up per-task overrides
        if allowed_tools is not None:
            from bot.harness import clear_chat_override
            clear_chat_override(session_key, "allowed_tools")
        if project_dir:
            from bot.harness import clear_chat_override
            clear_chat_override(session_key, "project_dir")
        if harness_label:
            from bot.harness import clear_chat_harness
            clear_chat_harness(session_key)
        _session_budget_overrides.pop(session_key, None)

    return final_text


# ============================================================
# HELPERS
# ============================================================

def _extract_text(message) -> str:
    """Extract text content from a SDK message object."""
    if hasattr(message, 'content') and isinstance(message.content, str):
        return message.content
    if hasattr(message, 'content') and isinstance(message.content, list):
        parts = []
        for block in message.content:
            if hasattr(block, 'text'):
                parts.append(block.text)
            elif isinstance(block, dict) and block.get('type') == 'text':
                parts.append(block.get('text', ''))
        return "\n".join(parts)
    if hasattr(message, 'text'):
        return message.text
    return ""




