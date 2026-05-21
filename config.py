# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Configuration for Corporate Bot (Claude Agent SDK).

Loads organization-specific data from org_config.json (mounted as volume).
All secrets come from environment variables (.env file).
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def _safe_int(var_name: str, default: int, min_val: int | None = None, max_val: int | None = None) -> int:
    """Parse int env var with range validation. Logs warning on invalid input."""
    raw = os.environ.get(var_name, "")
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        log.warning("Config: %s=%r is not a valid integer, using default %d", var_name, raw, default)
        return default
    if min_val is not None and val < min_val:
        log.warning("Config: %s=%d below minimum %d, using %d", var_name, val, min_val, min_val)
        return min_val
    if max_val is not None and val > max_val:
        log.warning("Config: %s=%d above maximum %d, using %d", var_name, val, max_val, max_val)
        return max_val
    return val


def _safe_float(var_name: str, default: float, min_val: float | None = None, max_val: float | None = None) -> float:
    """Parse float env var with range validation. Logs warning on invalid input."""
    raw = os.environ.get(var_name, "")
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        log.warning("Config: %s=%r is not a valid float, using default %.1f", var_name, raw, default)
        return default
    if min_val is not None and val < min_val:
        return min_val
    if max_val is not None and val > max_val:
        return max_val
    return val


# === ENVIRONMENT ===
BOT_ENV = os.environ.get("BOT_ENV", "production")
IS_STAGING = BOT_ENV == "staging"

# === STAGING QA ===
_compose_project = os.environ.get("COMPOSE_PROJECT_NAME", "corporate-bot")
STAGING_URL = os.environ.get("STAGING_URL", f"http://{_compose_project}-staging:8000")
STAGING_TEST_TOKEN = os.environ.pop("STAGING_TEST_TOKEN", "")
STAGING_QA_TIMEOUT = _safe_int("STAGING_QA_TIMEOUT", 90, min_val=10, max_val=3600)

# === PATHS ===
BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))

# Git commit hash (for health endpoint, deploy audit, etc.)
# Priority: 1) GIT_COMMIT env var (baked at Docker build time — most accurate)
#           2) /host-repo/.git/HEAD (live repo — may be NEWER than running code)
GIT_COMMIT = os.environ.get("GIT_COMMIT", "")
_GIT_COMMIT_SOURCE = "build" if GIT_COMMIT else ""
if not GIT_COMMIT:
    try:
        _head = Path("/host-repo/.git/HEAD").read_text().strip()
        if _head.startswith("ref: "):
            _ref_path = Path("/host-repo/.git") / _head[5:]
            GIT_COMMIT = _ref_path.read_text().strip()[:12]
        else:
            GIT_COMMIT = _head[:12]
        _GIT_COMMIT_SOURCE = "repo"
    except Exception:
        GIT_COMMIT = "unknown"
        _GIT_COMMIT_SOURCE = "unknown"


_undeployed_cache: tuple[float, bool] = (0.0, False)  # (timestamp, result)
_UNDEPLOYED_CACHE_TTL = 30  # seconds


def has_undeployed_changes() -> bool:
    """Detect if /host-repo/ has code changes not in the running /app/ image.

    Compares MD5 of core Python files between the running container (/app/)
    and the git repo (/host-repo/). Any mismatch means repo has changes
    that haven't been deployed (Docker image not rebuilt).

    Results are cached for 30s to avoid re-reading 14 files on every query.
    """
    import hashlib
    import time

    global _undeployed_cache
    now = time.monotonic()
    if now - _undeployed_cache[0] < _UNDEPLOYED_CACHE_TTL:
        return _undeployed_cache[1]

    _key_files = [
        "bot/agent.py", "bot/hooks.py", "bot/mcp_tools.py",
        "bot/storage/memory.py", "bot/storage/rag.py", "main.py", "config.py",
        "config_overrides.py", "bot/tool_labels.py",
        "bot/core/access.py", "bot/core/session_state.py",
        "bot/core/language.py", "bot/core/safety.py",
        "bot/core/fact_persistence.py",
        "bot/pipeline/task_manager.py", "bot/pipeline/streaming.py",
        "bot/pipeline/staging.py", "bot/commands.py", "bot/relay/ux.py",
        "bot/pipeline/handlers.py", "bot/pipeline/dispatch.py",
        "bot/storage/facts.py", "bot/storage/media_cache.py",
        "bot/desktop_relay.py",
        "bot/relay/core.py", "bot/relay/ssh.py", "bot/relay/registry.py",
        "bot/mcp/__init__.py", "bot/mcp/telegram.py",
        "bot/mcp/teams.py", "bot/mcp/services.py", "bot/mcp/deploy.py",
        "integrations/telegram.py", "integrations/tg_parser.py",
        "integrations/teams.py",
    ]
    result = False
    for f in _key_files:
        try:
            app_hash = hashlib.md5(Path(f"/app/{f}").read_bytes()).hexdigest()
            repo_hash = hashlib.md5(Path(f"/host-repo/{f}").read_bytes()).hexdigest()
            if app_hash != repo_hash:
                log.debug("has_undeployed_changes: %s differs (app=%s repo=%s)", f, app_hash[:8], repo_hash[:8])
                result = True
                break
        except Exception as exc:
            log.debug("has_undeployed_changes: skipping %s: %s", f, exc)
            continue
    _undeployed_cache = (now, result)
    return result
# --- Config vs Credentials isolation ---
# State configs (git-versioned, non-sensitive): harnesses, external chats, etc.
CONFIG_DIR = DATA_DIR / "config"
# Credentials (never git-tracked): OAuth tokens, Google/MS365 creds, auth state
CREDENTIALS_DIR = DATA_DIR / "credentials"

PROMPTS_DIR = DATA_DIR / "prompts"
TMP_DIR = DATA_DIR / "tmp"
DB_PATH = DATA_DIR / "conversations.db"
PM_DB_PATH = DATA_DIR / "pm.db"
GOOGLE_WORKSPACE_CREDS_DIR = CREDENTIALS_DIR / "google-workspace-creds"

# Derived: Google Workspace accounts (from credential filenames)
def _get_google_accounts() -> list[str]:
    creds_dir = GOOGLE_WORKSPACE_CREDS_DIR
    if creds_dir.exists():
        return sorted(
            f.stem for f in creds_dir.glob("*.json")
            if f.stem and "@" in f.stem and f.stem != "gmail-api"
        )
    return []

GOOGLE_ACCOUNTS = _get_google_accounts()

# MS365 email (from env or empty)
MS365_EMAIL = os.environ.get("MS365_EMAIL", "")
MEDIA_CACHE_DIR = DATA_DIR / "media_cache"
CLAUDE_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"

# OAuth profiles directory (unified: bot SDK + Mac relay)
OAUTH_PROFILES_DIR = CREDENTIALS_DIR / "oauth_profiles"
# Display label for the built-in file-based OAuth profile (~/.claude/.credentials.json).
# "default" is always accepted as an alias for backward compat.
OAUTH_DEFAULT_LABEL = os.environ.get("OAUTH_DEFAULT_LABEL", "default")

# Ensure directories exist. PermissionError is tolerated (warning, not crash)
# because Docker bind mounts can create intermediate dirs as root, and existing
# volumes may have root-owned dirs from prior runs. The bot starts in degraded
# mode and the admin sees the warning. Dockerfile pre-creates dirs for new volumes.
for d in [DATA_DIR, CONFIG_DIR, CREDENTIALS_DIR, PROMPTS_DIR, TMP_DIR,
          GOOGLE_WORKSPACE_CREDS_DIR, MEDIA_CACHE_DIR, OAUTH_PROFILES_DIR]:
    try:
        d.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        logging.getLogger(__name__).warning(
            "Cannot create %s (PermissionError). Fix: "
            "docker run --rm -v <volume>:/data alpine chown -R 10001:10001 /data/credentials",
            d,
        )

# === ORGANIZATION CONFIG (loaded from JSON file) ===
ORG_CONFIG_PATH = Path(os.environ.get("ORG_CONFIG_PATH", DATA_DIR / "org_config.json"))

def _load_org_config() -> dict:
    """Load org_config.json. Returns empty dict if missing."""
    if ORG_CONFIG_PATH.exists():
        try:
            return json.loads(ORG_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            log.error(f"Failed to load org config from {ORG_CONFIG_PATH}: {e}")
    return {}

_org_cfg = _load_org_config()
ORG_CONFIG = _org_cfg  # Public accessor for org config data

# Organization identity
ORG_NAME = _org_cfg.get("org_name", "")
BOT_NAME = _org_cfg.get("bot_name", "Corporate Bot")
ORG_TIMEZONE = _org_cfg.get("timezone", "Europe/London")

# Build ORG_CONTEXT from config (used in system prompt)
def _build_org_context() -> dict:
    """Build the ORG_CONTEXT dict from org_config.json.

    Supports both old schema (members.parents/children) and new schema
    (members.admins/team_members). New schema keys take priority.
    """
    members = _org_cfg.get("members", {})
    parents = members.get("admins", members.get("parents", []))
    children = members.get("team_members", members.get("children", []))
    other = members.get("other", [])

    ctx: dict = {"organization": {}, "location": _org_cfg.get("location", "")}

    for p in parents:
        role = p.get("role", "parent")
        entry: dict = {"name": p.get("name", "")}
        if "email" in p:
            entry["email"] = p["email"]
        if "emails" in p:
            entry["emails"] = p["emails"]
        if "telegram_id" in p:
            entry["telegram_id"] = p["telegram_id"]
        if "telegram_username" in p:
            entry["telegram_username"] = p["telegram_username"]
        ctx["organization"][role] = entry

    if children:
        ctx["organization"]["team_members"] = children

    for o in other:
        role = o.get("role", "other")
        ctx[role] = {"name": o.get("name", "")}

    return ctx

ORG_CONTEXT = _build_org_context()

# Extract admin info for convenience (supports both old and new schema)
_members = _org_cfg.get("members", {})
_parents = _members.get("admins", _members.get("parents", []))

def _get_parent_names() -> list[str]:
    return [p.get("name", "").split()[0] for p in _parents if p.get("name")]

PARENT_NAMES = _get_parent_names()

# Primary email (for sending)
_email_cfg = _org_cfg.get("email", {})
PRIMARY_EMAIL = _email_cfg.get("primary_address", "")
PRIMARY_EMAIL_USER = _email_cfg.get("primary_user_name", "")

# Phone agent config
_phone_cfg = _org_cfg.get("phone_agent", {})
PHONE_ORG_SURNAME = _phone_cfg.get("org_surname", ORG_NAME)
PHONE_DEFAULT_GENDER = _phone_cfg.get("default_gender", "male")
PHONE_VAPI_MODEL = _phone_cfg.get("vapi_model", "claude-sonnet-4-20250514")  # Vapi uses Anthropic model IDs

# Fact categories — example list for prompts (NOT a validation constraint).
# The model can use any category string; these are just common examples.
FACT_CATEGORIES: list[str] = _org_cfg.get("fact_categories", [
    "documents", "projects", "travel", "operations", "hr",
    "finance", "credentials", "contact", "bot", "office",
    "people", "events", "general",
])

# Organization member names for graph subject extraction.
# Used to auto-derive subject from fact key prefixes (e.g. "john_passport" → subject=john).
def _build_org_member_names() -> list[str]:
    """Extract all known organization member names/aliases for subject derivation.

    Supports both old schema (members.parents/children) and new schema
    (members.admins/team_members). New schema keys take priority.
    """
    names = []
    members = _org_cfg.get("members", {})
    for p in members.get("admins", members.get("parents", [])):
        name = p.get("name", "").split()[0].lower()
        if name:
            names.append(name)
    for c in members.get("team_members", members.get("children", [])):
        name = c.get("name", "").lower()
        if name:
            names.append(name)
        # Also add aliases
        for alias in c.get("aliases", []):
            names.append(alias.lower())
    for o in members.get("other", []):
        name = o.get("name", "").split()[0].lower()
        if name:
            names.append(name)
        role = o.get("role", "").lower().replace(" ", "_")
        if role:
            names.append(role)
    return sorted(set(names), key=len, reverse=True)  # longest first for greedy match

ORG_MEMBER_NAMES: list[str] = _build_org_member_names()

# Goals
DEFAULT_GOALS = _org_cfg.get("goals", [
    "Plan team time effectively — offsites, events, outings",
    "Keep everyone productive and reduce friction",
    "Support professional development and growth",
    "Stay on top of key events, deadlines, and deliverables",
])

# Telegram username validation: 5-32 chars, alphanumeric + underscore only
_TG_USERNAME_RE = re.compile(r'^[a-zA-Z0-9_]{5,32}$')

# Parent TG IDs — used by main.py to fetch live usernames at startup
PARENT_TG_IDS: list[int] = [
    p["telegram_id"] for p in _parents if p.get("telegram_id")
]


def _safe_tg_username(username: str) -> str:
    """Return username if valid per TG rules, else empty string."""
    return username if _TG_USERNAME_RE.match(username) else ""


# Build user tag map for prompts (telegram deep links) — static fallback
def _build_user_tags() -> dict[str, str]:
    """Build {Name: '<a href=\"tg://user?id=...\">Name</a>'} for prompt templates."""
    tags = {}
    for p in _parents:
        name = p.get("name", "").split()[0]
        tg_id = p.get("telegram_id")
        if name and tg_id:
            tags[name] = f'<a href="tg://user?id={tg_id}">{name}</a>'
    return tags

USER_TAGS = _build_user_tags()

# Build authorized user descriptions for prompts
def _build_authorized_users_desc() -> str:
    """Build a description like 'John (123456789) and Jane (987654321)'."""
    parts = []
    for p in _parents:
        name = p.get("name", "").split()[0]
        tg_id = p.get("telegram_id")
        if name and tg_id:
            parts.append(f"{name} ({tg_id})")
    return " and ".join(parts)

AUTHORIZED_USERS_DESC = _build_authorized_users_desc()

# DEPRECATED (Sprint B2): WA authorized desc — kept as empty string for backward compat.
WA_AUTHORIZED_DESC = ""

# Build reply tag rules for prompts — static fallback (updated at runtime by update_user_tags_from_tg)
def _build_reply_tag_rules() -> str:
    """Build reply tag rules using tg://user?id= deep links (static fallback)."""
    lines = []
    for p in _parents:
        name = p.get("name", "").split()[0]
        tg_id = p.get("telegram_id")
        if name and tg_id:
            lines.append(f'  {name}: <a href="tg://user?id={tg_id}">{name}</a>')
    return "\n".join(lines)


REPLY_TAG_RULES = _build_reply_tag_rules()


def update_user_tags_from_tg(tg_user_info: list[dict]) -> None:
    """Refresh REPLY_TAG_RULES and USER_TAGS with live Telegram usernames.

    Called at startup after Pyrogram connects. Each dict must have 'tg_id' and 'username'.
    Falls back to tg://user?id= deep link for users without a valid username.
    """
    global REPLY_TAG_RULES, USER_TAGS
    username_map = {
        u["tg_id"]: _safe_tg_username(u.get("username") or "")
        for u in tg_user_info
    }
    tag_lines: list[str] = []
    tags: dict[str, str] = {}
    for p in _parents:
        name = p.get("name", "").split()[0]
        tg_id = p.get("telegram_id")
        if not name or not tg_id:
            continue
        username = username_map.get(tg_id, "")
        if username:
            tag = f'<a href="https://t.me/{username}">@{username}</a>'
        else:
            tag = f'<a href="tg://user?id={tg_id}">{name}</a>'
        tag_lines.append(f"  {name}: {tag}")
        tags[name] = tag
    REPLY_TAG_RULES = "\n".join(tag_lines)
    USER_TAGS = tags
    log.info("[config] TG user tags refreshed from live data")

# Build member summary for identity prompt
def _build_members_summary() -> str:
    """Build member summary from org config."""
    parent_names = PARENT_NAMES
    children = _org_cfg.get("members", {}).get("children", [])
    child_names = [c.get("name", "") for c in children if c.get("name")]

    parts = []
    if parent_names:
        parts.append(", ".join(parent_names))
    if child_names:
        parts.append(f"and their {'child' if len(child_names) == 1 else f'{len(child_names)} children'} ({', '.join(child_names)})")
    return " ".join(parts)

MEMBERS_SUMMARY = _build_members_summary()

# === API KEYS & SERVICE CREDENTIALS ===
# Read and REMOVE from os.environ — prevents SDK subprocess from inheriting secrets.
# The model structurally cannot access these: they exist only as Python module variables,
# not in the process environment. os.environ.pop() is the structural guarantee.
CLAUDE_CODE_OAUTH_TOKEN = os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", "")
GEMINI_API_KEY = os.environ.pop("GEMINI_API_KEY", "")

# Google Workspace OAuth (app-level secrets — will migrate to credential_store)
GOOGLE_OAUTH_CLIENT_ID = os.environ.pop("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.pop("GOOGLE_OAUTH_CLIENT_SECRET", "")

# === TELEGRAM (Pyrogram MTProto userbot) ===
TG_API_ID = int(os.environ.get("TG_API_ID", "") or "0")
TG_API_HASH = os.environ.pop("TG_API_HASH", "")
TG_SESSION_DIR = str(CREDENTIALS_DIR / "tg_session")
TG_SESSION_NAME = os.environ.get("TG_SESSION_NAME", "corporate_bot")
# Ensure session directory exists
Path(TG_SESSION_DIR).mkdir(parents=True, exist_ok=True)

# REMOVED: TG_CHAT_ID was the "primary group" for notification fallback.
# Corporate bot has NO default chat — all messages require explicit chat_id.
# Restart notifications go to ADMIN_USERS DMs. Kept as 0 for backward compat
# (function signatures still reference it as default parameter).
TG_CHAT_ID = 0

# Legacy Bot API config (kept for backward compat, not required for userbot)
TG_BOT_TOKEN = os.environ.pop("TG_BOT_TOKEN", "")
TG_WEBHOOK_URL = os.environ.get("TG_WEBHOOK_URL", "")
TG_WEBHOOK_SECRET = os.environ.pop("TG_WEBHOOK_SECRET", "")

# Bot user IDs to ignore (both the old bot and MCP bot)
TG_BOT_USER_ID = int(os.environ.get("TG_BOT_USER_ID", "") or "0")
TG_MCP_BOT_USER_ID = int(os.environ.get("TG_MCP_BOT_USER_ID", "") or "0")

# === LEGACY REGISTRIES — REMOVED ===
# TG_ALLOWED_USERS, TG_ALLOWED_CHATS, TG_EXTERNAL_CHATS removed in CB-28.
# Replaced by unified chat_registry (bot/chat_registry.py) + RBAC engine.
# Kept as empty stubs for backward compat (imports that reference them won't crash).
TG_ALLOWED_USERS: dict[int, str] = {}    # REMOVED — use chat_registry + RBAC
TG_ALLOWED_CHATS: set[int] = set()       # REMOVED — use chat_registry.is_registered()
# TG_EXTERNAL_CHATS kept as JCS (read-only) for one more migration cycle.
# migrate_from_legacy() reads from it to seed chat_registry on first boot.
from bot.config_store import JsonConfigStore
TG_EXTERNAL_CHATS = JsonConfigStore(
    "tg_external_chats", CONFIG_DIR / "external_chats.json",
    key_type=int,
)

def save_external_chats():
    """No-op stub — legacy compat."""
    pass

def promote_chat_to_admin_level(chat_id: int) -> None:
    """No-op stub — legacy compat. Use manage_chat + RBAC instead."""
    pass

# Populate legacy stubs from org_config for boot migration seed
# (chat_registry.migrate_from_legacy reads these during first boot)
_tg_users_str = os.environ.get("TG_ALLOWED_USERS", "")
for _pair in _tg_users_str.split(","):
    if ":" in _pair:
        _uid, _name = _pair.strip().split(":", 1)
        try:
            TG_ALLOWED_USERS[int(_uid)] = _name
        except ValueError:
            pass
if not TG_ALLOWED_USERS:
    for p in _parents:
        tg_id = p.get("telegram_id")
        name = p.get("name", "").split()[0]
        if tg_id and name:
            TG_ALLOWED_USERS[tg_id] = name

# === UNIFIED CHAT REGISTRY ===
# Single source of truth for all registered chats.
# Auth: chat_registry.is_authorized(). RBAC: role-based permissions.
from bot.chat_registry import init as _init_chat_registry
_init_chat_registry(CONFIG_DIR)


# Backward compat alias (Sprint 17 rename)
promote_chat_to_primary = promote_chat_to_admin_level


# === MS TEAMS BOT (optional — omit to disable) ===
TEAMS_ENABLED = os.environ.get("TEAMS_ENABLED", "").lower() in ("true", "1", "yes")
TEAMS_APP_ID = os.environ.get("TEAMS_APP_ID", "")
TEAMS_APP_SECRET = os.environ.pop("TEAMS_APP_SECRET", "")
TEAMS_TENANT_ID = os.environ.get("TEAMS_TENANT_ID", "")
# Optional tenant allowlist — restrict which Azure AD tenants can interact.
# Comma-separated. Own tenant is always allowed. Empty = own tenant only.
TEAMS_ALLOWED_TENANTS = [t.strip() for t in os.environ.get("TEAMS_ALLOWED_TENANTS", "").split(",") if t.strip()]
# Default harness for new Teams chats (CB-5: canonical harness names)
TEAMS_DEFAULT_HARNESS = os.environ.get("TEAMS_DEFAULT_HARNESS", "teamwork")

# External Teams chats — explicitly registered Teams conversations.
# Format: {conversation_id: {"title": str, "mode": "active_plus", "added_at": str,
#          "conversation_type": str, "tenant_id": str}}
TEAMS_EXTERNAL_CHATS = JsonConfigStore(
    "teams_external_chats", CONFIG_DIR / "teams_external_chats.json",
)

# === CORPORATE AUTH (per-user SSO + token vault) ===
# Token encryption key (Fernet symmetric — generate with:
#   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
TOKEN_ENCRYPTION_KEY = os.environ.pop("TOKEN_ENCRYPTION_KEY", "")

# MS365 Graph API config (for per-user OBO flow)
MS365_CLIENT_ID = os.environ.get("MS365_CLIENT_ID", "") or TEAMS_APP_ID  # public ID, not a secret
MS365_CLIENT_SECRET = os.environ.pop("MS365_CLIENT_SECRET", "") or TEAMS_APP_SECRET
MS365_TENANT_ID = os.environ.get("MS365_TENANT_ID", "") or TEAMS_TENANT_ID
MS365_SCOPES = [s.strip() for s in os.environ.get("MS365_SCOPES", "Mail.Read,Calendars.ReadWrite,User.Read").split(",")]

# Bot Admin AAD IDs (comma-separated Azure AD Object IDs with full bot access)
BOT_ADMIN_AAD_IDS: set[str] = set(
    filter(None, os.environ.get("BOT_ADMIN_AAD_IDS", "").split(","))
)

# GitHub config
GITHUB_TOKEN = os.environ.pop("GITHUB_TOKEN", "")  # PAT for repo content fetch (domain sync)
GITHUB_WEBHOOK_SECRET = os.environ.pop("GITHUB_WEBHOOK_SECRET", "")
GITHUB_OAUTH_CLIENT_ID = os.environ.get("GITHUB_OAUTH_CLIENT_ID", "")  # OAuth App client ID (device flow)

# === DATA ACCESS ISOLATION ===
# DEPRECATED: Corporate bot has no primary group. Access is harness-scoped.
# Employees see only their own chat data; cross-chat knowledge comes from
# knowledge domains, not shared chat history. Kept as empty set for backward
# compat (25+ imports reference this variable).
PRIMARY_GROUP_CHATS: set[str] = set()

# === VAPI (Phone Calls) ===
VAPI_API_KEY = os.environ.pop("VAPI_API_KEY", "")
VAPI_PHONE_NUMBER_ID = os.environ.get("VAPI_PHONE_NUMBER_ID", "")
VAPI_VOICE_ID = os.environ.get("VAPI_VOICE_ID", "")
WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL", "")

# === REPORT HOSTING ===
REPORTS_PORT = _safe_int("REPORTS_PORT", 8210, min_val=1024, max_val=65535)
REPORTS_DIR = DATA_DIR / "reports"
REPORTS_BASE_URL = os.environ.get("REPORTS_BASE_URL", "")  # e.g. "https://reports.example.com"
REPORTS_MAX_AGE_DAYS = int(os.environ.get("REPORTS_MAX_AGE_DAYS", "365"))

# === ADDITIONAL SECRETS (centralized for env stripping) ===
CAMOFOX_API_KEY = os.environ.pop("CAMOFOX_API_KEY", "")
# MS365 MCP uses a different env var name than the Teams bot config
MS365_MCP_CLIENT_ID = os.environ.get("MS365_MCP_CLIENT_ID", "") or MS365_CLIENT_ID
MS365_MCP_CLIENT_SECRET = os.environ.pop("MS365_MCP_CLIENT_SECRET", "") or MS365_CLIENT_SECRET
MS365_MCP_TENANT_ID = os.environ.get("MS365_MCP_TENANT_ID", "") or MS365_TENANT_ID
TAILSCALE_OAUTH_CLIENT_SECRET = os.environ.pop("TAILSCALE_OAUTH_CLIENT_SECRET", "")

# === ADMIN USERS — BOOTSTRAP ONLY ===
# Used by RBAC seed to auto-assign bot_admin role on first boot.
# Runtime auth checks use RBAC (bot.rbac.engine). Do NOT use is_admin_user()
# for runtime authorization — it exists only for RBAC bootstrap and fallback.
_admin_str = os.environ.get("ADMIN_USERS", "")  # format: "telegram:123456789,teams:aad_object_id"
ADMIN_USERS = set()
for _pair in _admin_str.split(","):
    if ":" in _pair:
        _source, _uid = _pair.strip().split(":", 1)
        ADMIN_USERS.add((_source, _uid))

# Auto-populate admins from org config if env var was empty
if not ADMIN_USERS:
    for p in _parents:
        if p.get("is_admin"):
            tg_id = p.get("telegram_id")
            teams_id = p.get("teams_aad_id") or p.get("teams_id")
            if tg_id:
                ADMIN_USERS.add(("telegram", str(tg_id)))
            if teams_id:
                ADMIN_USERS.add(("teams", str(teams_id)))

# Root admin — the primary admin identity for system tasks and scheduler.
# Format: "source:user_id" (e.g. "telegram:123456789"). Falls back to first ADMIN_USERS entry.
_root_admin_str = os.environ.get("ROOT_ADMIN", "")
if _root_admin_str and ":" in _root_admin_str:
    ROOT_ADMIN: tuple[str, str] = tuple(_root_admin_str.split(":", 1))  # type: ignore[assignment]
elif ADMIN_USERS:
    ROOT_ADMIN = next(iter(sorted(ADMIN_USERS)))
else:
    ROOT_ADMIN = ("", "0")  # CB-132: no platform assumption — forces explicit config
    log.warning("ROOT_ADMIN not configured and ADMIN_USERS empty — set ROOT_ADMIN env var")
log.info(f"Root admin: {ROOT_ADMIN[0]}:{ROOT_ADMIN[1]}")


def is_admin_user(source: str, user_id: str) -> bool:
    """Check if a (source, user_id) pair is an admin user.

    BOOTSTRAP ONLY — used by RBAC seed and as fallback when RBAC engine
    is not yet loaded. Runtime auth checks should use RBAC.
    """
    return (source, str(user_id)) in ADMIN_USERS

# Admin direct chat IDs for TG notifications (restart, shutdown, alerts)
# Derived from ADMIN_USERS — no separate env var needed
TG_ADMIN_CHAT_IDS: list[int] = []
for _src, _uid in ADMIN_USERS:
    if _src == "telegram":
        try:
            TG_ADMIN_CHAT_IDS.append(int(_uid))
        except (ValueError, TypeError):
            logging.getLogger(__name__).warning(f"Invalid TG admin ID, skipping: {_uid}")

# === CLAUDE AGENT SDK CONFIG ===
ALLOWED_MODELS = {
    "claude-opus-4-6", "claude-sonnet-4-6",
    "claude-opus-4-6[1m]", "claude-sonnet-4-6[1m]",
    "claude-opus-4-7", "claude-opus-4-7[1m]",
}
_model_primary = os.environ.get("MODEL_PRIMARY", "claude-sonnet-4-6")
MODEL_PRIMARY = _model_primary  # backward compat for imports that captured at load time
# Session-level USD caps — SDK max_budget_usd is cumulative across query() calls
# on a resumed session. 0 = no cap (SDK default — unlimited). Non-zero = hard cap.
# Low caps cause session resets → expensive JSONL replays ($5-15 per reset).
MAX_BUDGET_DEFAULT = float(os.environ.get("MAX_BUDGET_DEFAULT", "0"))      # sonnet session (0 = no cap)
MAX_BUDGET_HEAVY = float(os.environ.get("MAX_BUDGET_HEAVY", "0"))          # opus session (0 = no cap)

# Per-query output token caps — custom enforcement via client.interrupt().
# Tracks output_tokens from AssistantMessage.usage intra-query.
QUERY_OUTPUT_CAP_DEFAULT = _safe_int("QUERY_OUTPUT_CAP_DEFAULT", 25000, min_val=0)  # sonnet
QUERY_OUTPUT_CAP_HEAVY = _safe_int("QUERY_OUTPUT_CAP_HEAVY", 50000, min_val=0)  # opus

# Rate limit alert thresholds (from RateLimitEvent.utilization, 0.0-1.0)
RATE_LIMIT_WARN_PCT = float(os.environ.get("RATE_LIMIT_WARN_PCT", "0.75"))
RATE_LIMIT_ALERT_PCT = float(os.environ.get("RATE_LIMIT_ALERT_PCT", "0.90"))
RATE_LIMIT_ALERT_COOLDOWN_S = 1800  # don't re-alert same threshold within 30 min

# Per-query anomaly thresholds (logging only, not kill)
QUERY_COST_WARN = float(os.environ.get("QUERY_COST_WARN", "2.0"))


def get_model_primary() -> str:
    """Get the global default model."""
    return _model_primary


def set_model_primary(model: str) -> str:
    """Switch the global default model. Returns the new model name."""
    global _model_primary
    if model not in ALLOWED_MODELS:
        raise ValueError(f"Unknown model: {model}. Allowed: {', '.join(sorted(ALLOWED_MODELS))}")
    _model_primary = model
    return model


# === Per-chat overrides (model, OAuth, effort) ===
# Implementation lives in config_overrides.py; re-exported here for backward compat.
from config_overrides import (  # noqa: F401, E402
    # Model overrides
    get_model_for_chat,
    get_model_override_only,
    set_model_for_chat,
    clear_model_for_chat,
    get_chat_model_overrides,
    # OAuth overrides
    is_default_oauth,
    validate_oauth_label,
    get_default_oauth_label,
    set_default_oauth_label,
    get_oauth_for_chat,
    set_oauth_for_chat,
    clear_oauth_for_chat,
    get_chat_oauth_overrides,
    get_oauth_profile_path,
    list_oauth_profiles,
    add_oauth_profile,
    remove_oauth_profile,
    get_oauth_token,
    # Effort level
    ALLOWED_EFFORTS,
    get_effort_level,
    set_effort_level,
)

# Initialize persisted overrides (loads JSON files from DATA_DIR).
# Must happen after all config constants are defined.
from config_overrides import init as _init_overrides  # noqa: E402
_init_overrides()

# Agent loop limits
MAX_TURNS = 75  # max tool-call iterations per query
MAX_TOOL_RESULT_CHARS = 30000  # truncate tool results above this
TOOL_TIMEOUT = 90  # seconds per individual tool execution
# TG send/edit/delete tools must outlast Telegram FloodWait pre-sleeps.
# A single FloodWait from TG can be 100-300s; the MCP timeout has to accommodate
# that or the model sees a TIMEOUT error and the message is dropped on the floor.
TG_TOOL_TIMEOUT = int(os.environ.get("TG_TOOL_TIMEOUT", "600"))  # 10 min — comfortably outlasts typical FloodWait
# Linux MAX_ARG_STRLEN = 131,072 bytes. SDK passes system prompt as a single
# CLI argument. Keep safety margin for shell escaping + other CLI overhead.
SYSTEM_PROMPT_MAX_BYTES = _safe_int("SYSTEM_PROMPT_MAX_BYTES", 120000, min_val=0)  # Connection resilience
CONNECT_MAX_RETRIES = int(os.environ.get("CONNECT_MAX_RETRIES", "2"))  # retry count for SDK connect()
CONNECT_BACKOFF_BASE = float(os.environ.get("CONNECT_BACKOFF_BASE", "3.0"))  # seconds; doubles each retry

# === CONTEXT MANAGEMENT ===
SESSION_TIMEOUT = 31536000  # seconds (365 days — effectively never expire; sessions reset only on fatal errors/re-auth)
SESSION_FILE_RETENTION = 604800  # seconds (7 days) — keep orphaned JSONL/dir session files for debugging before deletion
CONTEXT_MSG_TRUNCATE = _safe_int("CONTEXT_MSG_TRUNCATE", 2000, min_val=0)  # max chars per older message in context (truncated at line boundary)
CONTEXT_FULL_TAIL = _safe_int("CONTEXT_FULL_TAIL", 8, min_val=0)  # last N messages shown in full (never truncated, even if >CONTEXT_MSG_TRUNCATE)

# === DATABASE ===
DB_BUSY_TIMEOUT = _safe_int("DB_BUSY_TIMEOUT", 5000, min_val=100)  # ms; SQLite retries on lock for this long

# === RAG (Retrieval-Augmented Generation) ===
RAG_CHUNK_SIZE = 7    # messages per chunk (window size for semantic search) — used as max msgs per chunk
RAG_MAX_CHUNK_CHARS = 2000       # character budget per chunk — chunks stop growing when this is reached
RAG_CHUNK_OVERLAP_MSGS = 2       # overlap N messages between consecutive chunks for context continuity
RAG_LONG_MSG_OVERLAP_CHARS = 200 # character overlap when splitting a single long message into sub-chunks
RAG_EMBEDDING_MODEL = "gemini-embedding-001"
RAG_EMBEDDING_DIM = 3072
RAG_EMBEDDING_BATCH_SIZE = 100  # Gemini supports up to 100 texts per batch
RAG_SEARCH_BATCH_SIZE = _safe_int("RAG_SEARCH_BATCH_SIZE", 50000, min_val=0)  # chunks per batch in paginated search (~586 MB at 50K)

# Bot startup timestamp (UTC) — used for stale message detection and deploy_status
BOT_STARTED = datetime.now(timezone.utc)

# === BOT BEHAVIOR ===
PROACTIVE_HOUR = _safe_int("PROACTIVE_HOUR", 7, min_val=0)
PROACTIVE_MINUTE = _safe_int("PROACTIVE_MINUTE", 10, min_val=0)
EMAIL_POLL_INTERVAL_H = _safe_int("EMAIL_POLL_INTERVAL_H", 3, min_val=0)
STREAM_EDIT_INTERVAL = 3.0  # seconds between streaming edits (TG rate limit ~20/min)
# Maximum characters to propagate from a replied-to message into the SDK prompt.
# Previously per-site caps ranged 300-500, which silently clipped long bot
# replies (code reviews, plans) when users replied to them. All SDK variants
# (Sonnet/Opus, 200k/1M) easily absorb 50k; safety cap prevents pathological
# payloads. Shared by integrations/telegram.py and main.py (primary TG, WA, relay dispatch).
REPLY_QUOTE_MAX_CHARS = _safe_int("REPLY_QUOTE_MAX_CHARS", 50000, min_val=0)
PLACEHOLDER_DELAY = float(os.environ.get("PLACEHOLDER_DELAY", "3.0"))  # seconds before showing "Thinking..." (avoids flash on fast cached responses)
MAX_MEDIA_SIZE_MB = 20
MEDIA_RETENTION_DAYS = int(os.environ.get("MEDIA_RETENTION_DAYS", "36135"))  # ~99 years (unlimited by default; bot monitors disk and recommends cleanup)
MEDIA_DESCRIPTION_ENABLED = os.environ.get("MEDIA_DESCRIPTION_ENABLED", "true").lower() in ("true", "1", "yes")
MEDIA_DESCRIPTION_BACKFILL_LIMIT = _safe_int("MEDIA_DESCRIPTION_BACKFILL_LIMIT", 20, min_val=0)  # max items per startup
MEDIA_DESCRIBE_MAX_SIZE_MB = _safe_int("MEDIA_DESCRIBE_MAX_SIZE_MB", 100, min_val=0)  # max file size to auto-describe (MB)
MEDIA_DESCRIBE_INLINE_SIZE_MB = _safe_int("MEDIA_DESCRIBE_INLINE_SIZE_MB", 20, min_val=0)  # inline vs File API threshold (MB)
MEDIA_DESCRIBE_TIMEOUT_S = _safe_int("MEDIA_DESCRIBE_TIMEOUT_S", 120, min_val=1)  # timeout per description call (seconds)
MEDIA_DESCRIBE_MAX_CONCURRENT = _safe_int("MEDIA_DESCRIBE_MAX_CONCURRENT", 3, min_val=0)  # max concurrent Gemini calls
VOICE_TRANSCRIBE_TIMEOUT_S = int(os.environ.get("VOICE_TRANSCRIBE_TIMEOUT_S", "60"))  # timeout for synchronous voice transcription (seconds)
VOICE_TRANSCRIBE_MAX_CHARS = _safe_int("VOICE_TRANSCRIBE_MAX_CHARS", 20000, min_val=0)  # max transcript chars (~10 min speech with translation)
MEDIA_INLINE_READ_MAX_KB = _safe_int("MEDIA_INLINE_READ_MAX_KB", 500, min_val=0)  # files above this size (KB) get Gemini description instead of file path in prompt (prevents SDK 1MB JSON buffer crash)

# === LOCALE: Extra command keywords (pipe-separated regex fragments) ===
# Default: English-only. Set via env vars to add locale-specific keywords.
# Example for Russian: LOCALE_CANCEL_WORDS="отмена|стоп|хватит|отменить|прекрати"
LOCALE_CANCEL_WORDS = os.environ.get("LOCALE_CANCEL_WORDS", "")
LOCALE_CANCEL_PREFIX = os.environ.get("LOCALE_CANCEL_PREFIX", "")  # e.g. "отмени эту|останови эту"
LOCALE_DIAGNOSE_WORDS = os.environ.get("LOCALE_DIAGNOSE_WORDS", "")  # e.g. "диагностика|что случилось|проверь бота"
LOCALE_DROP_WORDS = os.environ.get("LOCALE_DROP_WORDS", "")  # e.g. "положи трубку|бросай|сбрось|повесь трубку"
LOCALE_CONTINUE_WORDS = os.environ.get("LOCALE_CONTINUE_WORDS", "")  # e.g. "продолжай|продолжить|дальше"

# === TRIAGE: FAST vs PRIMARY ===
QUICK_TIMEOUT = 30
LONG_STATUS_INTERVAL = 180
LONG_SOFT_TIMEOUT = 600
MAX_PARALLEL_TASKS = 1  # sequential per chat — messages queue and inject into main session
MAX_TOTAL_TASKS = 15    # global cap across all chats
TASK_STALE_TIMEOUT_S = int(os.environ.get("TASK_STALE_TIMEOUT_S", "900"))  # seconds of no SDK activity before zombie detection (15 min)

# === WEB SERVER ===
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = _safe_int("PORT", 8000, min_val=1024)