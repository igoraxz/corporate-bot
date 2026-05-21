# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Harness — per-chat configuration profiles.

A harness is a named set of runtime parameters (model, effort, turns, budget,
skills, prompt filtering) that can be assigned to any chat. Unset fields
in a harness fall back to the "default" harness values.

Storage (JCS-backed):
  data/config/harnesses.json       — harness definitions {label: {fields...}}
  data/config/chat_harnesses.json  — chat assignments {chat_key: {harness, overrides}}

Both files are managed by JsonConfigStore instances: thread-safe locking,
atomic writes (tmp+rename), mtime-based auto-reload, .prev.json backups.

Migration: on first load, generates "default" harness from current globals
and migrates model_overrides.json entries into chat assignments.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

from bot.config_store import JsonConfigStore

log = logging.getLogger(__name__)

# Lazy import to avoid circular dependency with config
_DATA_DIR: Path | None = None


def _data_dir() -> Path:
    global _DATA_DIR
    if _DATA_DIR is None:
        from config import DATA_DIR
        _DATA_DIR = DATA_DIR
    return _DATA_DIR


# ── Constants ──────────────────────────────────────────────────────────
# CB-129: Explicit wildcard sentinel for unrestricted credential access.
# Use ["*"] instead of None to avoid confusion with "not set / absent".
CREDENTIAL_WILDCARD = "*"  # when present in allowed_credentials list, means unrestricted


def is_credential_unrestricted(allowed_credentials: list[str]) -> bool:
    """Check if credential list grants unrestricted access (wildcard present)."""
    return CREDENTIAL_WILDCARD in allowed_credentials


# ── Harness schema ──────────────────────────────────────────────────────

# Fields removed in Sprint R4 (security now handled by RBAC engine):
# data_scope, allowed_tools, allowed_emails,
# knowledge_domains, write_domain, sandbox, no_network,
# custom_instructions, facts_visible, facts_categories, project_dir.
# Old harness JSON files with these fields are silently ignored (backward compat).
# NOTE: claude_md_modules was RESTORED (CB-16) — behavioral field.
# NOTE: allowed_credentials RESTORED (CB-19) — credential access gating in hooks.py Layer 1b3.
_REMOVED_SECURITY_FIELDS = frozenset({
    "data_scope", "allowed_tools", "allowed_emails",
    "knowledge_domains", "sandbox", "no_network",
    "custom_instructions", "facts_visible",
    "facts_categories", "project_dir",
    # NOTE: write_domain REMOVED from this set (CB-118) — now a harness behavioral field
    "reference_domains",  # CB-5: removed — now derived at runtime (domains - write_domain - core)
})


@dataclass
class Harness:
    """A named configuration profile (behavior fields only — security via RBAC)."""
    label: str = "default"
    model: str | None = None              # e.g. "claude-sonnet-4-6", "claude-opus-4-6[1m]"
    effort: str | None = None             # low / medium / high / max
    max_turns: int | None = None          # max tool iterations per query
    max_budget_usd: float | None = None   # session budget cap (0 = no cap)
    output_token_cap: int | None = None   # per-query output token limit
    # CB-118: Two filter lists with domain:component notation
    prompt_modules: list[str] | None = None    # filters domain-contributed system prompt modules. Same notation as claude_md_modules.
    claude_md: str | None = None          # path to alternative CLAUDE.md (null = default)
    claude_md_modules: list[str] | None = None  # filters auto-load skills into CLAUDE.md. Supports domain:component notation.
    # SDK isolation
    settings_json: str | None = None       # path to per-harness .claude/settings.json (null = default)
    # Timeout configuration
    query_timeout_s: int | None = None     # seconds before zombie detection (default 900 = 15 min)
    # Credential access gating (Layer 1b3). Default-deny: [] blocks all credentials.
    # Admin harnesses use ["*"] (wildcard = unrestricted). Non-admin harnesses MUST
    # explicitly list allowed credential IDs (e.g., ["claude:main", "google:user@co.com"]).
    allowed_credentials: list[str] = field(default_factory=list)  # [] = deny-all, ["*"] = unrestricted (admin only)
    # CB-118: Knowledge system config — harness declares which domains are active.
    # Domains = source of truth for available skills/modules.
    # claude_md_modules (above) is a FILTER on domain offerings, not the source.
    domains: list[str] | None = None       # which knowledge domains active ("core" always implicit)
    write_domain: str | None = None        # FACTS_UPDATE auto-route target domain
    # reference_domains REMOVED (CB-5): derived at runtime as domains - write_domain - core.
    # Old JSON files with this field are silently ignored via from_dict().
    # CB-5: Admin management model — harness references an RBAC role.
    # When a harness is assigned to a chat, the default_role is auto-assigned via RBAC.
    # Harness REFERENCES role (not IS role) — entities stay properly separated.
    default_role: str | None = None        # RBAC role auto-assigned when this harness is set on a chat
    # CB-5: PM project routing — harness default, chat can override
    pm_project_id: int | None = None       # PM project for this harness (None = auto-derive from chat_key)
    # Performance
    fast_mode: bool | None = None          # SDK fast mode (Opus 4.6 only, 2.5x speed, 6x cost)

    def to_dict(self) -> dict:
        """Serialize to JSON-safe dict, omitting None fields."""
        d = asdict(self)
        # Exclude internal class attributes (not JSON-serializable)
        d.pop("_PRESERVE_NONE_FIELDS", None)
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict) -> "Harness":
        """Create from dict, ignoring unknown keys and removed security fields.

        Backward compat: old harness JSON with removed security fields is silently
        accepted (fields are dropped, not errors).
        """
        clean_data = {k: v for k, v in data.items() if k not in _REMOVED_SECURITY_FIELDS}
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in clean_data.items() if k in known}
        # CB-129: Coerce null allowed_credentials to [] (deny-all, fail-closed).
        # Prevents TypeError from corrupted/legacy JSON before migration runs.
        if "allowed_credentials" in filtered and filtered["allowed_credentials"] is None:
            filtered["allowed_credentials"] = []
        return cls(**filtered)


# ── Default harness values ──────────────────────────────────────────────

def _build_default_params() -> Harness:
    """Build default harness params — fallback values for resolve_field().

    This is NOT a harness that gets assigned to chats. It's the fallback
    source when a canonical harness field is None. Every chat gets one of
    the canonical harnesses (admin, external_chat, coding).
    Security fields (data_scope, allowed_tools, etc.) removed in R4 —
    access control is handled by RBAC engine.
    """
    from config import MODEL_PRIMARY, MAX_BUDGET_DEFAULT, QUERY_OUTPUT_CAP_DEFAULT
    return Harness(
        label="__defaults__",
        model=MODEL_PRIMARY,
        effort="medium",
        max_turns=75,
        max_budget_usd=MAX_BUDGET_DEFAULT,
        output_token_cap=QUERY_OUTPUT_CAP_DEFAULT,
        claude_md=None,
        query_timeout_s=900,    # 15 min zombie detection
    )


# ── Canonical Harnesses ───────────────────────────────────────────────
# These 3 harnesses (admin, external_chat, coding) are auto-created at
# startup if absent. They define the baseline config for each chat type.
# Admins can modify them via manage_harness — the startup logic only
# creates, never overwrites. Team-specific harnesses are created by
# admin via manage_harness.
#
# Resolution: system tasks → admin, external chats → external_chat,
# coding sessions → coding. Teams channels → admin-configured per channel.

# CB-118: Hardcoded skill lists REMOVED. Domains are the source of truth.
# Skills come from canonical domain configs (_CANONICAL_DOMAINS in domains.py).
# Harness claude_md_modules is now a FILTER (allowlist), not the primary list.
# None = accept all from domains. Explicit list = restrict to these only.
#
# For external_chat: explicit allowlist restricts to minimal core skills.
# For admin/coding/teamwork: None = accept all domain-offered skills.
_EXTERNAL_SKILLS_FILTER = [
    "bot-features", "skills-catalog", "bot-commands", "rich-content",
]

_CANONICAL_HARNESSES: dict[str, dict] = {
    # CB-5: Admin management model — 6 canonical harnesses.
    # Each has default_role for auto-RBAC assignment.
    # reference_domains removed — derived as (domains - write_domain - core).
    # Core domain is always implicit (injected by runtime).
    "admin": {
        "label": "admin",
        "model": "claude-opus-4-6[1m]",
        "effort": "high",
        "max_turns": 0,
        "query_timeout_s": 1800,
        "allowed_credentials": ["*"],
        "claude_md_modules": [
            "bot-features", "skills-catalog", "bot-commands", "rich-content",
            "media-guide", "preference-learning", "proactive-guide", "todo-guide",
            "ms365-guide",
            "bot-architecture", "bot-patterns", "bot-pitfalls", "bot-deploy",
            "bot-staging", "bot-backup",
        ],  # Explicit filter: 15 skills (excludes coding-specific)
        "domains": ["admin", "teamwork"],
        "write_domain": "admin",
        "default_role": "bot_admin",
        "pm_project_id": 2,  # Global Corporate Bot project
    },
    "coding": {
        "label": "coding",
        "model": "claude-opus-4-6[1m]",
        "effort": "high",
        "max_turns": 0,
        "query_timeout_s": 0,
        "allowed_credentials": ["*"],
        "claude_md_modules": None,  # Accept ALL from domains
        "domains": ["admin", "teamwork"],
        "write_domain": "admin",
        "default_role": "bot_admin",
        "pm_project_id": 2,  # Global Corporate Bot project
    },
    "teamwork": {
        "label": "teamwork",
        "model": "claude-sonnet-4-6",
        "effort": "medium",
        "max_budget_usd": 0,  # No cap — env var DEFAULT is a fallback for unset harnesses only
        "query_timeout_s": 1800,
        "allowed_credentials": [],
        "claude_md_modules": None,  # Accept ALL from core + teamwork
        "domains": ["teamwork"],
        "write_domain": "teamwork",
        "default_role": "teamwork",
    },
    "project-coding": {
        "label": "project-coding",
        "model": "claude-opus-4-6[1m]",
        "effort": "high",
        "max_turns": 0,
        "max_budget_usd": 0,
        "query_timeout_s": 1800,
        "allowed_credentials": [],
        "claude_md_modules": None,  # Accept ALL from core + teamwork
        "domains": ["teamwork"],
        "write_domain": "teamwork",
        "default_role": "teamwork",
    },
    "personal": {
        "label": "personal",
        "model": "claude-sonnet-4-6",
        "effort": "medium",
        "max_budget_usd": 0,
        "query_timeout_s": 600,
        "allowed_credentials": [],
        "claude_md_modules": None,  # Accept ALL from core
        "domains": [],
        "write_domain": "",
        "default_role": "employee",
    },
    "external_chat": {
        "label": "external_chat",
        "model": "claude-sonnet-4-6",
        "query_timeout_s": 600,
        "allowed_credentials": [],
        "claude_md_modules": _EXTERNAL_SKILLS_FILTER,  # Explicit filter — minimal only
        "domains": [],
        "write_domain": "",
        "default_role": "external",
    },
}


# ── Storage (JCS-backed) ───────────────────────────────────────────────

_HARNESSES_FILE = "harnesses.json"
_CHAT_HARNESSES_FILE = "chat_harnesses.json"

# JCS instances (lazy-initialized via _ensure_loaded)
_harness_store: JsonConfigStore | None = None
_assignment_store: JsonConfigStore | None = None
_init_done = False
_init_lock = threading.Lock()


def _config_dir() -> Path:
    return _data_dir() / "config"


def _harnesses_path() -> Path:
    return _config_dir() / _HARNESSES_FILE


def _chat_harnesses_path() -> Path:
    return _config_dir() / _CHAT_HARNESSES_FILE


def _ensure_loaded() -> None:
    """Lazy-init JCS stores on first access. Thread-safe via JCS internal locking.

    JCS handles: mtime-based auto-reload, atomic writes, .prev.json backup.
    This function only needs to run one-time init/migration logic.
    """
    global _harness_store, _assignment_store, _init_done
    if _init_done:
        return
    with _init_lock:
        if _init_done:
            return  # double-check under lock

        _harness_store = JsonConfigStore("harnesses", _harnesses_path())
        _assignment_store = JsonConfigStore("chat_harnesses", _chat_harnesses_path())

        # Migrate old str→dict format in assignments
        for k, v in list(_assignment_store.all().items()):
            if isinstance(v, str):
                _assignment_store.set(k, {"harness": v, "overrides": {}})

        # Migration: remove __defaults__ pseudo-harness from JCS (CB-119)
        if "__defaults__" in _harness_store:
            _harness_store.delete("__defaults__")
            log.info("Removed __defaults__ pseudo-harness from JCS (CB-119)")
        # Migration: remove old "default" harness entry if present
        if "default" in _harness_store:
            _harness_store.delete("default")
            log.info("Removed legacy 'default' harness entry")

        # Ensure canonical harnesses exist AND have required CB-5 fields.
        # Creates new canonical harnesses. For existing ones, patches in any
        # missing fields from the canonical definition (e.g., default_role,
        # domains, write_domain added in CB-5) without overwriting admin-customized values.
        _CB5_FIELDS = {"default_role", "domains", "write_domain", "pm_project_id"}
        for _canon_label, _canon_fields in _CANONICAL_HARNESSES.items():
            if _canon_label not in _harness_store:
                _harness_store.set(_canon_label, _canon_fields)
                log.info("Created canonical harness: %s", _canon_label)
            else:
                # CB-5 migration: patch missing fields on existing canonical harnesses
                _existing = _harness_store.get(_canon_label)
                if isinstance(_existing, dict):
                    _patched_fields = []
                    for _fld in _CB5_FIELDS:
                        if _fld not in _existing and _fld in _canon_fields:
                            _existing[_fld] = _canon_fields[_fld]
                            _patched_fields.append(_fld)
                    if _patched_fields:
                        _harness_store.set(_canon_label, _existing)
                        log.info("CB-5 migration: patched %s on harness '%s': %s",
                                 len(_patched_fields), _canon_label, _patched_fields)

        # CB-32+CB-129 Migration: replace None-as-wildcard with explicit values.
        # Admin/coding harnesses: None → ["*"] (explicit unrestricted sentinel).
        # Non-admin harnesses: None/missing → [] (deny-all).
        _admin_labels = frozenset({"admin", "coding"})
        _patched = 0
        for _hl, _hd in list(_harness_store.all().items()):
            if _hl.startswith("__") or not isinstance(_hd, dict):
                continue
            _ac = _hd.get("allowed_credentials")
            if _hl in _admin_labels:
                # CB-129: admin/coding with None → ["*"]
                if _ac is None:
                    _hd["allowed_credentials"] = [CREDENTIAL_WILDCARD]
                    _harness_store.set(_hl, _hd)
                    _patched += 1
            else:
                # CB-32: non-admin with None/missing → []
                if _ac is None or "allowed_credentials" not in _hd:
                    _hd["allowed_credentials"] = []
                    _harness_store.set(_hl, _hd)
                    _patched += 1
        if _patched:
            log.info("CB-129 migration: patched allowed_credentials on %d harness(es)", _patched)

        # Migration: import model_overrides.json if no chat assignments exist yet
        if len(_assignment_store) == 0:
            _migrate_from_model_overrides()

        # Clean up stale system:* keys (ephemeral PM/scheduled task sessions)
        stale_system = [k for k in _assignment_store if k.startswith("system:")]
        if stale_system:
            for k in stale_system:
                _assignment_store.delete(k)
            log.info("Removed %d stale system:* harness assignment(s): %s", len(stale_system), stale_system)

        _init_done = True  # set LAST after all init complete


def _migrate_from_model_overrides() -> None:
    """One-time migration: create harnesses from existing model_overrides.json."""
    from config import get_chat_model_overrides, ALLOWED_MODELS
    overrides = get_chat_model_overrides()
    if not overrides:
        return

    migrated = 0
    all_harnesses = _harness_store.all()
    for chat_key, model in overrides.items():
        if model not in ALLOWED_MODELS:
            continue
        # Check if a harness with this model already exists
        matching = None
        for label, data in all_harnesses.items():
            if data.get("model") == model and label != "default":
                matching = label
                break
        if not matching:
            # Create a new harness for this model
            label = model.replace("claude-", "").replace("-", "_").replace("[1m]", "_1m")
            h = Harness(label=label, model=model)
            _harness_store.set(label, h.to_dict())
            all_harnesses[label] = h.to_dict()  # update local copy
            matching = label

        _assignment_store.set(chat_key, {"harness": matching, "overrides": {}})
        migrated += 1

    if migrated:
        log.info("Migrated %d model override(s) to harness assignments", migrated)


# ── Public API ──────────────────────────────────────────────────────────

def _to_harness(label: str, data: dict) -> Harness:
    """Convert a JCS dict entry to a Harness object."""
    d = dict(data)
    d["label"] = label
    return Harness.from_dict(d)


def get_harness(label: str) -> Harness | None:
    """Get a harness by label. Returns None if not found."""
    _ensure_loaded()
    raw = _harness_store.get(label)
    if raw is None:
        return None
    return _to_harness(label, raw)


def get_default_params() -> Harness:
    """Get default harness params — fallback values for resolve_field().

    NOT a real harness. Internal plumbing only. Every chat gets a canonical
    harness (admin/shared/external_chat/coding), never this.
    """
    return _build_default_params()


def get_harness_for_chat(chat_key: str) -> Harness:
    """Get the effective harness for a chat (assigned or default).

    For forum topics (key format 'source:chat_id:thread_id'), falls back to the
    parent group harness ('source:chat_id') if no topic-specific assignment exists.
    """
    _ensure_loaded()
    assignment = _assignment_store.get(chat_key, {})
    # Fallback for forum topics: topic key → parent group key → default
    if not assignment:
        parts = chat_key.split(":")
        if len(parts) == 3:
            parent_key = f"{parts[0]}:{parts[1]}"
            assignment = _assignment_store.get(parent_key, {})
    label = assignment.get("harness", "") if isinstance(assignment, dict) else ""
    if label:
        raw = _harness_store.get(label)
        if raw is not None:
            return _to_harness(label, raw)
        log.warning("Chat %s assigned to non-existent harness '%s'", chat_key, label)
    # CB-5: Unassigned chats get sensible canonical defaults
    # DMs → personal, groups → teamwork, external → external_chat
    parts = chat_key.split(":")
    is_dm = False
    is_external = False
    if len(parts) >= 2:
        from bot.core.access import is_private_chat
        try:
            is_dm = is_private_chat(parts[1], source=parts[0])
        except Exception:
            pass
        # Check chat registry for external/silent flag (security: Finding 1)
        try:
            from bot.chat_registry import get_entry
            _reg_entry = get_entry(chat_key)
            if _reg_entry and (_reg_entry.get("is_external") or _reg_entry.get("mode") == "silent"):
                is_external = True
        except Exception:
            pass
    if is_external:
        fallback_label = "external_chat"
    elif is_dm:
        fallback_label = "personal"
    else:
        fallback_label = "teamwork"
    fallback_raw = _harness_store.get(fallback_label)
    if fallback_raw is not None:
        log.debug("Chat %s unassigned, using fallback harness '%s'", chat_key, fallback_label)
        return _to_harness(fallback_label, fallback_raw)
    # Ultimate fallback: built-in defaults (should never reach here in normal operation)
    return get_default_params()


def get_chat_harness_label(chat_key: str) -> str:
    """Get the harness label assigned to a chat, or empty string if not assigned.

    Falls back to parent group for forum topics (source:chat:thread → source:chat).
    Returns empty string when no explicit assignment exists.
    """
    _ensure_loaded()
    assignment = _assignment_store.get(chat_key, {})
    if not assignment:
        parts = chat_key.split(":")
        if len(parts) == 3:
            parent_key = f"{parts[0]}:{parts[1]}"
            assignment = _assignment_store.get(parent_key, {})
    return assignment.get("harness", "") if isinstance(assignment, dict) else ""


def get_chat_overrides(chat_key: str) -> dict:
    """Get per-chat field overrides (empty dict if none).

    Falls back to parent group for forum topics (source:chat:thread → source:chat).
    """
    _ensure_loaded()
    assignment = _assignment_store.get(chat_key, {})
    if not assignment:
        parts = chat_key.split(":")
        if len(parts) == 3:
            parent_key = f"{parts[0]}:{parts[1]}"
            assignment = _assignment_store.get(parent_key, {})
    return assignment.get("overrides", {}) if isinstance(assignment, dict) else {}


def _schedule_chat_reset(chat_key: str) -> None:
    """Schedule an SDK session reset for a chat. Best-effort, never raises."""
    try:
        from bot.agent import client_pool
        if client_pool:
            client_pool.schedule_session_reset(chat_key)
            log.debug("Scheduled session reset for %s (harness/override change)", chat_key)
    except Exception:
        pass  # Pool not initialized yet (startup) or import cycle


def set_chat_override(chat_key: str, field: str, value) -> None:
    """Set a per-chat field override. Takes priority over harness definition.

    Automatically schedules an SDK session reset so the change takes effect
    immediately (tools, skills, model, etc. are loaded at session start).
    """
    _ensure_loaded()
    # CB-129: Validate wildcard in allowed_credentials — only admin/coding harnesses allowed.
    if field == "allowed_credentials" and isinstance(value, list) and CREDENTIAL_WILDCARD in value:
        _assignment = _assignment_store.get(chat_key) or {}
        _h_label = _assignment.get("harness", "") if isinstance(_assignment, dict) else ""
        if _h_label not in ("admin", "coding"):
            raise ValueError("Wildcard '*' in allowed_credentials is only allowed for "
                             "admin/coding harnesses. Use an explicit list.")
    existing = _assignment_store.get(chat_key) or {"harness": "", "overrides": {}}
    existing = dict(existing)  # copy for mutation
    if "overrides" not in existing:
        existing["overrides"] = {}
    existing["overrides"][field] = value
    _assignment_store.set(chat_key, existing)
    # CB-5 Finding 7: Redact sensitive override values in logs
    _SENSITIVE_FIELDS = frozenset({"allowed_credentials", "allowed_tools"})
    _log_val = f"[{len(value)} items]" if field in _SENSITIVE_FIELDS and isinstance(value, list) else value
    log.info("Chat %s override set: %s=%s", chat_key, field, _log_val)
    _schedule_chat_reset(chat_key)


def clear_chat_override(chat_key: str, field: str | None = None) -> None:
    """Clear a specific per-chat override, or all overrides if field is None.

    Automatically schedules an SDK session reset so the change takes effect.
    """
    _ensure_loaded()
    existing = _assignment_store.get(chat_key)
    if existing is None:
        return
    existing = dict(existing)  # copy for mutation
    if field is None:
        existing["overrides"] = {}
    else:
        existing.get("overrides", {}).pop(field, None)
    _assignment_store.set(chat_key, existing)
    _schedule_chat_reset(chat_key)


def resolve_field(harness: Harness, field_name: str, chat_key: str | None = None) -> Any:
    """Resolve a harness field with optional per-chat override.

    Priority: chat override → harness field → default harness.
    """
    # 1. Check per-chat override first
    if chat_key:
        overrides = get_chat_overrides(chat_key)
        if field_name in overrides:
            return overrides[field_name]
    # 2. Check harness field
    val = getattr(harness, field_name, None)
    if val is not None:
        return val
    # 3. Fall back to default harness
    default = get_default_params()
    return getattr(default, field_name, None)


def set_chat_harness(chat_key: str, label: str) -> None:
    """Assign a harness to a chat. Preserves existing overrides."""
    _ensure_loaded()
    if label not in _harness_store:
        raise ValueError(f"Unknown harness: '{label}'. Use list_harnesses() to see available.")
    existing = _assignment_store.get(chat_key, {})
    overrides = existing.get("overrides", {}) if isinstance(existing, dict) else {}
    _assignment_store.set(chat_key, {"harness": label, "overrides": overrides})
    log.info("Chat %s assigned to harness '%s'", chat_key, label)


def derive_reference_domains(harness: Harness) -> list[str]:
    """Derive read-only reference domains from harness config (CB-5).

    reference_domains = domains - write_domain - core (all derived, not stored).
    Core is always implicit and never listed as a reference domain.
    """
    domains = harness.domains or []
    write = harness.write_domain or ""
    return [d for d in domains if d != write and d != "core"]


async def assign_chat_harness(chat_key: str, label: str) -> Harness:
    """Unified ONE-STEP harness assignment (CB-5).

    1. Sets harness in config store (sync)
    2. Auto-assigns RBAC role from harness.default_role (async)
    3. Returns the harness for caller convenience

    After calling, check harness._rbac_status for assignment outcome:
    - "assigned" = role auto-assigned successfully
    - "skipped_admin" = skipped because chat has admin-granted role
    - "skipped_no_role" = harness has no default_role
    - "failed" = RBAC assignment failed (logged as warning)

    This is the primary API for chat registration. Use set_chat_harness()
    only when RBAC assignment is not needed (system tasks, migrations).
    """
    set_chat_harness(chat_key, label)
    harness = get_harness_for_chat(chat_key)
    harness._rbac_status = "skipped_no_role"  # type: ignore[attr-defined]

    # Auto-assign RBAC role from harness.default_role
    if harness.default_role:
        try:
            from bot.rbac.store import assign_role, get_assignments_for_subject
            # Determine RBAC subject: parse chat_key for subject_type + subject_id
            parts = chat_key.split(":")
            if len(parts) >= 2:
                # Group chat or topic: subject = chat:<chat_id>
                # DM: subject = user:<source>:<user_id>
                from bot.core.access import is_private_chat
                if is_private_chat(parts[1], source=parts[0]):
                    subject_type = "user"
                    subject_id = chat_key  # full key for user subjects
                else:
                    subject_type = "chat"
                    # For topics (3 parts), use parent chat_id for RBAC
                    subject_id = parts[1]

                # Check if chat already has an explicit admin-granted role
                existing = await get_assignments_for_subject(subject_type, subject_id)
                admin_granted = any(
                    a.get("granted_by", "").startswith("user:")
                    for a in existing
                )
                if not admin_granted:
                    await assign_role(
                        subject_type, subject_id,
                        harness.default_role,
                        granted_by=f"harness:{label}",
                    )
                    # Invalidate RBAC cache so the new role is immediately visible
                    try:
                        from bot.rbac import get_engine
                        get_engine().invalidate_cache()
                    except Exception:
                        pass
                    harness._rbac_status = "assigned"  # type: ignore[attr-defined]
                    log.info("Auto-assigned role '%s' to %s:%s (harness:%s)",
                             harness.default_role, subject_type, subject_id, label)
                else:
                    harness._rbac_status = "skipped_admin"  # type: ignore[attr-defined]
                    log.debug("Skipped auto-role for %s — has admin-granted role", chat_key)
        except Exception as e:
            harness._rbac_status = "failed"  # type: ignore[attr-defined]
            log.warning("Failed to auto-assign RBAC role for %s: %s", chat_key, e)

    return harness


def clear_chat_harness(chat_key: str) -> None:
    """Remove harness assignment, reverting to default."""
    _ensure_loaded()
    _assignment_store.delete(chat_key)


def list_harnesses() -> dict[str, Harness]:
    """Get all defined harnesses."""
    _ensure_loaded()
    return {label: _to_harness(label, data) for label, data in _harness_store.all().items()}


def get_chat_assignments() -> dict[str, str]:
    """Get all chat→harness label assignments (for display)."""
    _ensure_loaded()
    return {k: (v.get("harness", "") if isinstance(v, dict) else v)
            for k, v in _assignment_store.all().items()}


def get_chats_by_harness(label: str) -> list[str]:
    """Get all chat_keys assigned to a given harness label."""
    _ensure_loaded()
    return [k for k, v in _assignment_store.all().items()
            if (v.get("harness", "") if isinstance(v, dict) else v) == label]


_FIELD_TYPES: dict[str, type | tuple] = {
    "max_turns": int, "max_budget_usd": (int, float), "output_token_cap": int, "query_timeout_s": int,
    "claude_md_modules": (list, type(None)),
    "prompt_modules": (list, type(None)),
    "fast_mode": (bool, type(None)),
    # CB-5: Knowledge + role + PM fields
    "default_role": (str, type(None)),
    "domains": (list, type(None)),
    "write_domain": (str, type(None)),
    "allowed_credentials": list,
    "pm_project_id": (int, type(None)),
}


def _validate_fields(fields: dict, *, target_label: str = "") -> None:
    """Validate field types. Raises ValueError on mismatch."""
    # Silently drop removed security fields (backward compat with old callers)
    for rf in _REMOVED_SECURITY_FIELDS:
        fields.pop(rf, None)
    for k, v in fields.items():
        if k in _FIELD_TYPES and v is not None:
            expected = _FIELD_TYPES[k]
            if not isinstance(v, expected):
                exp_name = expected.__name__ if isinstance(expected, type) else str(expected)
                raise ValueError(f"Field '{k}' must be {exp_name}, got {type(v).__name__}")
    # Path fields: reject traversal
    for path_field in ("claude_md", "settings_json"):
        val = fields.get(path_field)
        if val and (".." in str(val) or str(val).startswith("/")):
            raise ValueError(f"{path_field} must be a relative path without '..'")
    # CB-5 Finding 2: Restrict default_role to non-admin roles (prevent privilege escalation)
    # bot_admin can only be set on canonical harnesses, not via create/update.
    _dr = fields.get("default_role")
    if _dr and _dr == "bot_admin":
        raise ValueError("default_role='bot_admin' cannot be set via create/update. "
                         "Only canonical admin/coding harnesses have bot_admin role.")
    # CB-129: Reject wildcard "*" in allowed_credentials for non-admin harnesses.
    # Only admin/coding harnesses may use ["*"] (unrestricted access).
    if "allowed_credentials" in fields:
        _ac_val = fields["allowed_credentials"]
        _label = target_label or fields.get("label", "")
        if _ac_val is None:
            raise ValueError("allowed_credentials=None is no longer valid. "
                             "Use ['*'] for unrestricted or [] for deny-all.")
        if isinstance(_ac_val, list) and CREDENTIAL_WILDCARD in _ac_val and _label not in ("admin", "coding"):
            raise ValueError("Wildcard '*' in allowed_credentials is only allowed for "
                             "admin/coding harnesses. Use an explicit list.")



def create_harness(label: str, copy_from: str | None = None, **fields) -> Harness:
    """Create a new harness, optionally copying from another.

    If copy_from is specified, copies all fields from the source harness
    as defaults, then applies any explicit fields on top. Also copies
    the project dir contents (settings.json, CLAUDE.md) from the source.
    """
    _ensure_loaded()
    if label in _harness_store:
        raise ValueError(f"Harness '{label}' already exists. Use update_harness().")
    if not label or not label.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"Invalid label: '{label}'. Use alphanumeric + hyphens/underscores.")
    # Copy fields from source harness if specified
    if copy_from:
        source_raw = _harness_store.get(copy_from)
        if not source_raw:
            raise ValueError(f"Source harness '{copy_from}' not found for copy_from.")
        # Use source fields as base, override with explicit fields
        base = dict(source_raw)
        base.pop("label", None)
        base.update({k: v for k, v in fields.items() if k != "label"})
        fields = base

    _validate_fields(fields, target_label=label)
    fields["label"] = label
    h = Harness.from_dict(fields)
    _harness_store.set(label, h.to_dict())
    # Create project dir with settings.json for SDK isolation
    if label != "default":
        try:
            ensure_harness_project_dir(label)
            # Copy project dir contents from source if copy_from specified
            if copy_from:
                _copy_project_dir(copy_from, label)
                # Re-enforce settings — copy_from may have overwritten
                # settings.json with source's different permissions
                _write_harness_settings(
                    _projects_base() / label / ".claude" / "settings.json", label
                )
        except Exception as e:
            log.warning("Failed to create project dir for harness '%s': %s", label, e)
    log.info("Created harness '%s': %s", label, h.to_dict())
    return h


def update_harness(label: str, **fields) -> Harness:
    """Update fields on an existing harness."""
    _ensure_loaded()
    raw = _harness_store.get(label)
    if raw is None:
        raise ValueError(f"Harness '{label}' not found.")
    _validate_fields(fields, target_label=label)
    h = _to_harness(label, raw)
    for k, v in fields.items():
        if hasattr(h, k) and k != "label":
            setattr(h, k, v)
    _harness_store.set(label, h.to_dict())
    # Regenerate project dir when SDK-config or module fields change
    _needs_project_update = (
        "fast_mode" in fields or "model" in fields
        or "claude_md_modules" in fields
    )
    if _needs_project_update:
        # Invalidate cache so project dir is rebuilt (settings.json + CLAUDE.md)
        _cwd_cache.discard(label)
        if label != "default":
            try:
                ensure_harness_project_dir(label)
            except Exception as e:
                log.warning("Failed to update project dir for harness '%s': %s", label, e)
        else:
            _write_harness_settings(Path("/app/.claude/settings.json"), "default")
            _cwd_cache.add("default")
    log.info("Updated harness '%s': %s", label, fields)
    return h


def delete_harness(label: str) -> None:
    """Delete a harness. Cannot delete canonical harnesses. Reassigns affected chats."""
    _ensure_loaded()
    # CB-5 F9: Protect all canonical harnesses (not just "default" which no longer exists)
    if label in _CANONICAL_HARNESSES:
        raise ValueError(f"Cannot delete canonical harness '{label}'.")
    if label not in _harness_store:
        raise ValueError(f"Harness '{label}' not found.")
    _harness_store.delete(label)
    # Reassign any chats using this harness to default
    reassigned = []
    for chat_key, assigned in list(_assignment_store.all().items()):
        h_label = assigned.get("harness") if isinstance(assigned, dict) else assigned
        if h_label == label:
            _assignment_store.delete(chat_key)
            reassigned.append(chat_key)
    if reassigned:
        log.info("Deleted harness '%s', reassigned %d chat(s) to default", label, len(reassigned))
    else:
        log.info("Deleted harness '%s'", label)


def format_harness(h: Harness, compact: bool = False) -> str:
    """Format a harness for display (behavior fields only — security via RBAC)."""
    if compact:
        parts = []
        if h.model:
            model_short = h.model.replace("claude-", "")
            parts.append(model_short)
            if "[1m]" in h.model:
                parts.append("ctx=1M")
        if h.effort and h.effort != "medium":
            parts.append(f"effort={h.effort}")
        if h.max_turns and h.max_turns != 75:
            parts.append(f"turns={h.max_turns}")
        if h.max_budget_usd:
            parts.append(f"budget=${h.max_budget_usd}")
        if h.output_token_cap:
            parts.append(f"out={h.output_token_cap // 1000}k")
        if h.fast_mode:
            parts.append("fast(6x)")
        return ", ".join(parts) if parts else "(defaults)"

    lines = [f"<b>{h.label}</b>"]
    d = h.to_dict()
    for k, v in d.items():
        if k == "label":
            continue
        if isinstance(v, list):
            v = ", ".join(str(i) for i in v) if v else "[]"
        lines.append(f"  {k}: {v}")
    return "\n".join(lines)


# ── Per-harness project dirs (SDK settings isolation) ───────────────────

_HARNESS_PROJECTS_DIR = "harness-projects"


def _projects_base() -> Path:
    return _data_dir() / _HARNESS_PROJECTS_DIR


_cwd_cache: set[str] = set()  # labels whose project dirs have been created


def get_harness_cwd(label: str, chat_key: str | None = None) -> str:
    """Get the cwd path for a harness's SDK subprocess.

    R4: project_dir removed from Harness — auto from label for all named harnesses.
    Per-chat project_dir overrides still supported (via chat_harnesses.json overrides).
    Caches dir creation to avoid repeated filesystem stat calls.
    """
    # Check per-chat override first
    if chat_key:
        overrides = get_chat_overrides(chat_key)
        if overrides.get("project_dir"):
            pd = overrides["project_dir"]
            _ensure_project_dir_setup(pd, label)
            return pd

    # Every named harness gets its own project dir (auto from label)
    project_dir = _projects_base() / label
    if label not in _cwd_cache:
        ensure_harness_project_dir(label)
        _cwd_cache.add(label)
    return str(project_dir)


_SDK_DANGEROUS_TOOLS = ["Bash", "Write", "Edit"]


def _write_harness_settings(settings_path: Path, label: str,
                            force_admin: bool = False) -> None:
    """Write settings.json for a harness.

    Sprint R4: Security fields removed from Harness — RBAC controls access.
    All harnesses get full SDK tool access (no SDK-level deny).
    RBAC + hooks enforce tool restrictions at runtime.
    Fast mode (Opus 4.6 only) is injected when harness.fast_mode is True.
    """
    _ensure_loaded()
    raw = _harness_store.get(label)
    h = _to_harness(label, raw) if raw else None
    # R4: all harnesses get full SDK tool visibility.
    # RBAC engine handles tool access control at runtime.
    settings: dict = {}
    # Fast mode: inject "fastMode": true when enabled AND model contains "opus-4".
    # SDK silently ignores fastMode for non-opus models, but we guard here
    # to avoid unexpected cost spikes if model is later changed without
    # clearing fast_mode.
    if h and h.fast_mode:
        model = h.model or ""
        if "opus-4" in model:
            settings["fastMode"] = True
            log.info("fast_mode enabled for harness '%s' (model=%s)", label, model)
        else:
            log.warning("fast_mode ignored for harness '%s': requires opus-4-6 model (got '%s')", label, model or "default/sonnet")
    try:
        settings_path.write_text(json.dumps(settings, indent=2))
    except OSError as e:
        log.warning("Failed to write settings.json for harness '%s': %s", label, e)
        return
    info = "full access (RBAC-enforced)"
    if settings.get("fastMode"):
        info += ", fastMode=on"
    log.info("settings.json for '%s': %s", label, info)


# Strict module/skill name validation: lowercase alphanum + hyphens + underscores (no path traversal).
_MODULE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Cache for loaded module content (populated by load_module_content).
# M23 fix: track mtime to invalidate on disk edits (e.g., admin self-upgrade).
_module_cache: dict[str, str] = {}
_module_mtime: dict[str, float] = {}  # name → file mtime at cache time


def _strip_yaml_frontmatter(text: str) -> str:
    """Strip YAML frontmatter (--- ... ---) from skill/module files."""
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            return text[end + 3:].strip()
    return text


def load_module_content(name: str) -> str:
    """Load a skill/module's content by name (cached after first read).

    Search order: .claude/skills/<name>/SKILL.md first, then data/claude_md_modules/<name>.md
    as legacy fallback. YAML frontmatter is stripped from skill files.
    Names validated against strict allowlist regex to prevent path traversal.
    Returns content string or empty string on failure.
    """
    # M23 fix: check mtime to invalidate stale cache entries
    if name in _module_cache:
        cached_mtime = _module_mtime.get(name, 0)
        if cached_mtime > 0:
            # Re-resolve path to check mtime
            _sp = Path("/app/.claude/skills") / name / "SKILL.md"
            _md = _data_dir() / "claude_md_modules" / f"{name}.md"
            _check = _sp if _sp.exists() else (_md if _md.exists() else None)
            if _check:
                try:
                    if _check.stat().st_mtime <= cached_mtime:
                        return _module_cache[name]
                    # File changed — fall through to reload
                    log.info("Module cache invalidated (disk change): %s", name)
                except OSError:
                    return _module_cache[name]
            else:
                return _module_cache[name]
        else:
            return _module_cache[name]  # empty-string cache (module not found)
    if not _MODULE_NAME_RE.match(name):
        log.warning("Invalid module name rejected: %r", name)
        _module_cache[name] = ""
        return ""
    skills_dir = Path("/app/.claude/skills")
    modules_dir = _data_dir() / "claude_md_modules"
    # Try skills dir first (canonical location)
    skill_path = skills_dir / name / "SKILL.md"
    mod_path = None
    is_skill = False
    if skill_path.exists():
        mod_path = skill_path
        is_skill = True
    else:
        # Legacy fallback: check modules dir
        legacy_path = modules_dir / f"{name}.md"
        if legacy_path.exists():
            mod_path = legacy_path
        else:
            log.warning("Skill/module not found: %s (checked %s and %s)", name, skill_path, legacy_path)
            _module_cache[name] = ""
            return ""
    try:
        content = mod_path.read_text(encoding="utf-8")
        if is_skill:
            content = _strip_yaml_frontmatter(content)
        _module_cache[name] = content
        try:
            _module_mtime[name] = mod_path.stat().st_mtime
        except OSError:
            _module_mtime[name] = 0
        log.info("Loaded module '%s' (%d chars) from %s", name, len(content), mod_path)
    except Exception as e:
        log.warning("Failed to load module '%s': %s", name, e)
        _module_cache[name] = ""
    return _module_cache[name]


def build_claude_md(modules: list[str] | None, base_claude_md: str | None = None) -> str | None:
    """Compose a CLAUDE.md from a list of skill/module names.

    Skills loaded via load_module_content() (.claude/skills/ first,
    data/claude_md_modules/ legacy fallback, with frontmatter stripping).
    If base_claude_md is set, it's prepended (for harnesses with a custom CLAUDE.md).
    Returns the composed content string, or None if no modules specified.
    """
    if not modules:
        return None
    parts: list[str] = []
    # Prepend base CLAUDE.md if specified
    if base_claude_md:
        if ".." in base_claude_md or base_claude_md.startswith("/"):
            log.warning("Rejected absolute/traversal base_claude_md: %s", base_claude_md)
        else:
            try:
                base_path = Path(base_claude_md)
                if base_path.exists():
                    parts.append(base_path.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("Failed to read base CLAUDE.md %s: %s", base_claude_md, exc)
    # Load each module via shared loader
    for mod_name in modules:
        content = load_module_content(mod_name)
        if content:
            parts.append(content)
    if not parts:
        return None
    return "\n\n".join(parts)


def write_composed_claude_md(project_dir: str, modules: list[str] | None,
                              base_claude_md: str | None = None,
                              initialize_only: bool = False) -> bool:
    """Write a composed CLAUDE.md to a project directory.

    Args:
        initialize_only: if True, only write if CLAUDE.md doesn't exist yet.
            Used for custom project_dir to avoid overwriting user customizations.

    Returns True if the file was written/updated, False if no modules or no change.
    """
    composed = build_claude_md(modules, base_claude_md)
    if composed is None:
        return False
    target = Path(project_dir) / "CLAUDE.md"
    # For custom project dirs: only write on first creation (user owns it after)
    if initialize_only and target.exists():
        return False
    # Check if content changed to avoid unnecessary writes
    if target.exists():
        try:
            existing = target.read_text(encoding="utf-8")
            if existing == composed:
                return False  # No change
        except Exception:
            pass
    try:
        target.write_text(composed, encoding="utf-8")
        log.info("Composed CLAUDE.md (%d bytes, %d modules) -> %s",
                 len(composed), len(modules or []), target)
        return True
    except Exception as exc:
        log.error("Failed to write composed CLAUDE.md to %s: %s", target, exc)
        return False


def _ensure_project_dir_setup(path: str, label: str) -> None:
    """Ensure a custom project dir has .claude/settings.json + skills symlink + CLAUDE.md."""
    cache_key = f"custom:{path}"
    if cache_key in _cwd_cache:
        return
    p = Path(path)
    claude_dir = p / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    # Symlink skills and agents if not present
    for subdir in ("skills", "agents"):
        link = claude_dir / subdir
        target = Path("/app/.claude") / subdir
        if not link.exists() and target.exists():
            try:
                link.symlink_to(target)
            except OSError:
                pass
    # Write settings.json
    _write_harness_settings(claude_dir / "settings.json", label)
    # Custom project dirs don't get auto-composed CLAUDE.md — user owns the file.
    # Standard harness project dirs get composition via ensure_harness_project_dir().
    _cwd_cache.add(cache_key)


def ensure_harness_project_dir(label: str) -> Path:
    """Create/update the project dir for a harness.

    Structure:
      data/harness-projects/<label>/
        .claude/
          settings.json  ← per-harness SDK permissions
          skills/        ← symlink to /app/.claude/skills
    """
    base = _projects_base() / label
    claude_dir = base / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    # Symlink skills and agents (so SDK discovers them for all harnesses)
    for subdir in ("skills", "agents"):
        link = claude_dir / subdir
        target = Path("/app/.claude") / subdir
        if not link.exists() and target.exists():
            try:
                link.symlink_to(target)
                log.info("Created %s symlink: %s -> %s", subdir, link, target)
            except OSError as e:
                log.warning("Failed to create %s symlink for harness '%s': %s", subdir, label, e)

    # Write settings.json
    settings_path = claude_dir / "settings.json"
    _write_harness_settings(settings_path, label)

    # CB-118: Compose CLAUDE.md from domain-sourced modules (not harness static list).
    # Uses compose_effective_modules() — same function as system prompt path.
    # Domains are the source of truth. Harness claude_md_modules = allowlist filter.
    h = get_harness(label)
    _modules = None
    try:
        import asyncio
        from bot.storage.domains import get_domain_knowledge_configs, compose_effective_modules

        # Get domain configs for this harness's domains (CB-5: harness is source of truth)
        _h_domains = getattr(h, "domains", None) or []
        if not _h_domains:
            # No explicit domains — use write_domain + derived refs
            _h_wd = getattr(h, "write_domain", None) or ""
            _h_domains = ["core"]
            if _h_wd and _h_wd != "core":
                _h_domains.append(_h_wd)

        # Run async domain lookup in sync context (ensure_harness_project_dir is sync)
        _loop = asyncio.get_event_loop()
        if _loop.is_running():
            # Already in async context — can't use run_until_complete
            # Use harness claude_md_modules as bridge until next query rebuilds
            _modules = h.claude_md_modules if h else None
        else:
            _domain_cfgs = _loop.run_until_complete(get_domain_knowledge_configs(_h_domains))
            if _domain_cfgs:
                _h_filter = h.claude_md_modules if h else None
                _composition = compose_effective_modules(_domain_cfgs, harness_filter=_h_filter)
                _modules = _composition.auto_load_skills
    except Exception as _comp_err:
        log.warning("CB-118: CLAUDE.md composition from domains failed for '%s': %s — using harness static list", label, _comp_err)
        _modules = h.claude_md_modules if h else None

    if _modules:
        write_composed_claude_md(str(base), _modules, base_claude_md=h.claude_md if h else None)
        log.info("Composed CLAUDE.md for harness '%s': %d modules (domain-sourced)", label, len(_modules))
    else:
        # No modules → remove stale CLAUDE.md so SDK falls back to /app/CLAUDE.md
        _stale_cmd = base / "CLAUDE.md"
        if _stale_cmd.exists():
            try:
                _stale_cmd.unlink()
                log.info("Removed stale CLAUDE.md from harness '%s' (no modules)", label)
            except OSError:
                pass

    # Initialize local git repo for commit history tracking (PM↔git linkage).
    # Local-only — no remote. Enables commit_hash references in PM tickets.
    git_dir = base / ".git"
    if not git_dir.exists():
        import subprocess
        try:
            r = subprocess.run(
                ["git", "init", "--initial-branch=main"],
                cwd=str(base), capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                log.warning("git init failed in %s: %s", base, r.stderr.strip())
            else:
                # Set minimal git config so commits work
                subprocess.run(
                    ["git", "config", "user.email", "bot@local"],
                    cwd=str(base), capture_output=True, timeout=5,
                )
                subprocess.run(
                    ["git", "config", "user.name", "Corporate Bot"],
                    cwd=str(base), capture_output=True, timeout=5,
                )
                log.info("Initialized git repo in harness project dir: %s", base)
        except Exception as e:
            log.warning("Failed to init git in %s: %s", base, e)

    return base


def get_system_agent_cwd(allowed_tools: list[str] | None = None) -> str:
    """Get a CWD for system agent sessions (PM dispatch) with correct SDK permissions.

    When allowed_tools includes Bash/Write/Edit, returns a project dir with
    admin settings.json (full access). Otherwise uses the default /app.
    This avoids the default harness's SDK-level deny on dangerous tools.
    """
    needs_admin = allowed_tools and any(
        t in allowed_tools for t in _SDK_DANGEROUS_TOOLS
    )
    if not needs_admin:
        return "/app"
    # Use a shared PM-agents project dir with admin settings
    pm_dir = _projects_base() / "pm-agents"
    cache_key = "pm-agents-admin"
    # Also register the custom:{path} key so _ensure_project_dir_setup
    # (called from get_harness_cwd per-chat override path) recognizes
    # this dir as already set up and doesn't overwrite settings.json.
    custom_cache_key = f"custom:{pm_dir}"
    if cache_key not in _cwd_cache:
        claude_dir = pm_dir / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        # Symlink skills and agents
        for subdir in ("skills", "agents"):
            link = claude_dir / subdir
            target = Path("/app/.claude") / subdir
            if not link.exists() and target.exists():
                try:
                    link.symlink_to(target)
                except OSError:
                    pass
        # Write permissive settings.json
        _write_harness_settings(claude_dir / "settings.json", "default",
                                force_admin=True)
        _cwd_cache.add(cache_key)
        _cwd_cache.add(custom_cache_key)
    return str(pm_dir)


def _copy_project_dir(source_label: str, target_label: str) -> None:
    """Copy project dir contents from source harness to target.

    Copies: CLAUDE.md, settings.json, and any other files in .claude/.
    Does NOT copy skills symlink (recreated by ensure_harness_project_dir).
    Source can be "default" (copies from /app/.claude/).
    """
    import shutil
    if source_label == "default":
        src = Path("/app")
    else:
        src = _projects_base() / source_label

    dst = _projects_base() / target_label

    # Copy CLAUDE.md if source has one
    src_claude_md = src / "CLAUDE.md"
    if src_claude_md.exists():
        dst_claude_md = dst / "CLAUDE.md"
        if not dst_claude_md.exists():
            shutil.copy2(str(src_claude_md), str(dst_claude_md))
            log.info("Copied CLAUDE.md from '%s' to '%s'", source_label, target_label)

    # Copy settings.json from source .claude/ (overwrite auto-generated)
    src_settings = src / ".claude" / "settings.json"
    dst_settings = dst / ".claude" / "settings.json"
    if src_settings.exists():
        shutil.copy2(str(src_settings), str(dst_settings))
        log.info("Copied .claude/settings.json from '%s' to '%s'", source_label, target_label)

    # Copy any other non-symlink files in .claude/ (rules, agents, etc.)
    src_claude = src / ".claude"
    dst_claude = dst / ".claude"
    if src_claude.exists():
        for item in src_claude.iterdir():
            if item.name == "skills" or item.is_symlink():
                continue  # skills are always symlinked, not copied
            dst_item = dst_claude / item.name
            if not dst_item.exists():
                if item.is_dir():
                    shutil.copytree(str(item), str(dst_item), symlinks=True)
                else:
                    shutil.copy2(str(item), str(dst_item))
                log.info("Copied .claude/%s from '%s' to '%s'", item.name, source_label, target_label)


def ensure_all_harness_dirs() -> None:
    """Create/update project dirs for all non-default harnesses.
    Called on startup to ensure dirs exist.
    """
    _ensure_loaded()
    for label in _harness_store:
        if label != "default":
            try:
                ensure_harness_project_dir(label)
            except Exception as e:
                log.warning("Failed to create project dir for harness '%s': %s", label, e)
