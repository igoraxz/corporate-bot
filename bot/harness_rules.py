# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Harness auto-assignment rules engine for corporate deployment.

Maps Teams conversations to harnesses based on configurable rules.
Rules are evaluated in priority order; first match wins.

Rule format:
{
    "id": "rule-1",
    "priority": 10,       # lower = higher priority
    "match": {
        "conversation_type": "channel",     # personal | groupChat | channel
        "team_name": "Engineering*",        # glob pattern
        "channel_name": "*runbooks*",       # glob pattern
        "tenant_id": "abc-123-...",         # exact match
        "user_aad_id": "...",               # for DM rules (exact)
    },
    "harness": "engineering",               # harness label to assign
    "knowledge_domains": ["eng-runbooks"],   # optional: override harness domains
}

Match fields are AND-ed (all specified fields must match).
Glob patterns use fnmatch (*, ?, [seq]).
Empty/missing match fields are ignored (don't restrict).
"""

import fnmatch
import logging

from bot.config_store import JsonConfigStore

log = logging.getLogger(__name__)


# ============================================================
# Rules Store (JSON-backed, atomic writes)
# ============================================================

_rules_store: JsonConfigStore | None = None


def _get_rules_store() -> JsonConfigStore:
    """Get or create the harness rules config store (singleton)."""
    global _rules_store
    if _rules_store is None:
        from config import DATA_DIR, CONFIG_DIR
        store_path = (CONFIG_DIR if CONFIG_DIR.exists() else DATA_DIR) / "harness_rules.json"
        _rules_store = JsonConfigStore("harness_rules", store_path)
    return _rules_store


def get_all_rules() -> list[dict]:
    """Get all rules, sorted by priority."""
    store = _get_rules_store()
    rules = list(store.all().values())
    rules.sort(key=lambda r: r.get("priority", 100))
    return rules


def add_rule(rule: dict) -> str:
    """Add or update a rule. Returns rule ID."""
    store = _get_rules_store()
    rule_id = rule.get("id", "")
    if not rule_id:
        # Generate ID
        existing = store.all()
        rule_id = f"rule-{len(existing) + 1}"
        rule["id"] = rule_id
    store.set(rule_id, rule)
    log.info("Harness rule added/updated: %s → %s", rule_id, rule.get("harness", "?"))
    return rule_id


def remove_rule(rule_id: str) -> bool:
    """Remove a rule by ID."""
    store = _get_rules_store()
    if rule_id in store:
        store.delete(rule_id)
        log.info("Harness rule removed: %s", rule_id)
        return True
    return False


# ============================================================
# Rule Matching
# ============================================================

def match_rule(
    conversation_type: str = "",
    team_name: str = "",
    channel_name: str = "",
    tenant_id: str = "",
    user_aad_id: str = "",
    chat_title: str = "",
) -> dict | None:
    """Find the first matching rule for a Teams conversation.

    Args:
        conversation_type: "personal", "groupChat", "channel"
        team_name: Teams team display name (if available)
        channel_name: Teams channel display name (if available)
        tenant_id: Azure AD tenant ID
        user_aad_id: User's Azure AD object ID (for DM rules)
        chat_title: Chat/channel title (fallback for name matching)

    Returns: Matching rule dict, or None if no rule matches.
    """
    rules = get_all_rules()

    for rule in rules:
        match_criteria = rule.get("match", {})
        if not match_criteria:
            continue

        matched = True

        # Check each criterion (AND logic — all must match)
        if "conversation_type" in match_criteria:
            if match_criteria["conversation_type"] != conversation_type:
                matched = False
                continue

        if "team_name" in match_criteria and matched:
            pattern = match_criteria["team_name"]
            if not fnmatch.fnmatch(team_name.lower(), pattern.lower()):
                # Also try chat_title as fallback
                if not fnmatch.fnmatch(chat_title.lower(), pattern.lower()):
                    matched = False
                    continue

        if "channel_name" in match_criteria and matched:
            pattern = match_criteria["channel_name"]
            if not fnmatch.fnmatch(channel_name.lower(), pattern.lower()):
                if not fnmatch.fnmatch(chat_title.lower(), pattern.lower()):
                    matched = False
                    continue

        if "tenant_id" in match_criteria and matched:
            if match_criteria["tenant_id"] != tenant_id:
                matched = False
                continue

        if "user_aad_id" in match_criteria and matched:
            if match_criteria["user_aad_id"] != user_aad_id:
                matched = False
                continue

        if matched:
            log.debug("Harness rule matched: %s → %s", rule.get("id"), rule.get("harness"))
            return rule

    return None


def resolve_harness_for_teams_chat(
    parsed: dict,
    default_harness: str = "teamwork",  # CB-5: canonical harness for group chats
) -> str:
    """Resolve which harness to assign to a new Teams conversation.

    Uses the rules engine to match, falls back to default.

    Args:
        parsed: Parsed message dict from Teams activity handler
        default_harness: Fallback harness if no rule matches

    Returns: Harness label to assign.
    """
    rule = match_rule(
        conversation_type=parsed.get("teams_conversation_type", ""),
        tenant_id=parsed.get("teams_tenant_id", ""),
        user_aad_id=parsed.get("user_id", ""),
        chat_title=parsed.get("chat_title", ""),
    )

    if rule:
        return rule.get("harness", default_harness)

    # Default logic: DMs get personal harness, channels get default (CB-5)
    conv_type = parsed.get("teams_conversation_type", "")
    if conv_type == "personal":
        return "personal"

    return default_harness


# ============================================================
# Admin helper: seed default corporate rules
# ============================================================

def seed_default_rules():
    """Create default corporate harness rules if none exist.

    Called at startup to bootstrap a sensible default config.
    """
    store = _get_rules_store()
    if store.all():
        return  # Already has rules

    defaults = [
        {
            "id": "dm-default",
            "priority": 100,
            "match": {"conversation_type": "personal"},
            "harness": "personal",  # CB-5: canonical personal harness
        },
        {
            "id": "group-default",
            "priority": 200,
            "match": {"conversation_type": "groupChat"},
            "harness": "teamwork",  # CB-5: canonical teamwork harness
        },
        {
            "id": "channel-default",
            "priority": 300,
            "match": {"conversation_type": "channel"},
            "harness": "teamwork",  # CB-5: canonical teamwork harness
        },
    ]

    for rule in defaults:
        store.set(rule["id"], rule)

    log.info("Seeded %d default harness assignment rules", len(defaults))
