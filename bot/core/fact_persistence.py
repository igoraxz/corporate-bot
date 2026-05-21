# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""FACTS_UPDATE extraction, parsing, and persistence.

Extracted from bot/agent.py (Phase 0 refactor).
"""

import json
import logging
import re

log = logging.getLogger(__name__)

_FACTS_UPDATE_PREFIX_RE = re.compile(r'\s*FACTS_UPDATE:\s*')

_TIMING_KEY_PATTERNS = re.compile(
    r'(flight|dep|arr|depart|arriv|checkin|checkout|check_in|check_out|'
    r'reservation|booking|appointment|transfer|pickup|dropoff|ferry|train|bus)',
    re.IGNORECASE,
)
_TIME_VALUE_PATTERN = re.compile(r'\b([01]?\d|2[0-3]):([0-5]\d)\b')


def _find_facts_blocks(text: str) -> list[tuple[int, int]]:
    """Find all FACTS_UPDATE: {...} spans using brace-counting for nested JSON.

    Returns list of (start, end) index pairs covering the full block
    including any leading whitespace.
    """
    spans = []
    for m in _FACTS_UPDATE_PREFIX_RE.finditer(text):
        brace_start = m.end()
        if brace_start >= len(text) or text[brace_start] != '{':
            continue
        depth = 0
        i = brace_start
        while i < len(text):
            ch = text[i]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    spans.append((m.start(), i + 1))
                    break
            elif ch == '\\':
                i += 1  # skip escaped char
            i += 1
    return spans


def strip_facts_update(text: str, streaming: bool = False) -> str:
    """Remove FACTS_UPDATE: {...} blocks from display text.

    Uses brace-counting to correctly handle nested JSON (extended format).
    These are internal memory instructions -- not meant for chat display.
    The raw text is still parsed by _persist_memory_updates() separately.

    When streaming=True, also strips partial/incomplete FACTS_UPDATE blocks
    (e.g. "FACTS_UPDATE: {" mid-output before the closing brace arrives).
    """
    # Strip complete FACTS_UPDATE blocks (balanced braces)
    spans = _find_facts_blocks(text)
    if spans:
        parts = []
        prev = 0
        for start, end in spans:
            parts.append(text[prev:start])
            prev = end
        parts.append(text[prev:])
        text = ''.join(parts)
    # In streaming mode, also strip partial/incomplete FACTS_UPDATE at end of text
    if streaming:
        m = _FACTS_UPDATE_PREFIX_RE.search(text)
        if m:
            text = text[:m.start()]
    return text.strip()


async def _check_timing_fact_conflicts(key: str, value: str):
    """Log warning if a new timing fact conflicts with existing atomic facts.

    Safety net: detects when a consolidated fact contains a time that contradicts
    an existing atomic fact for the same entity (e.g. flight number).
    Non-blocking -- facts still save, but conflicts are visible in logs.
    """
    from bot.storage.memory import search_message_facts
    # Only check facts that look timing-related
    if not _TIMING_KEY_PATTERNS.search(key):
        return
    new_times = set(_TIME_VALUE_PATTERN.findall(value))
    if not new_times:
        return
    # Extract entity identifier from key (e.g. "ja781" from "jetsmart_igu_scl_flight")
    # Search for related facts
    key_parts = key.replace('_', ' ')
    try:
        existing = await search_message_facts(key_parts, limit=10)
        for efact in existing:
            if efact.get('fact_key') == key:
                continue  # skip self
            existing_times = set(_TIME_VALUE_PATTERN.findall(efact.get('fact_value', '')))
            if existing_times and new_times and existing_times != new_times:
                # Check if any times in new fact conflict with existing
                # Check entity overlap using word boundary matching to avoid false positives
                ekey = efact.get('fact_key', '')
                ekey_parts = set(ekey.split('_'))
                key_parts_set = set(p for p in key.split('_') if len(p) > 3)
                overlap_entity = bool(key_parts_set & ekey_parts)
                if overlap_entity:
                    log.warning(
                        f"\u26a0\ufe0f TIMING FACT CONFLICT: new fact '{key}'={value[:80]} "
                        f"has times {new_times} but existing fact "
                        f"'{efact.get('fact_key', '?')}'={efact.get('fact_value', '')[:80]} "
                        f"has times {existing_times}"
                    )
    except Exception as e:
        log.debug(f"Timing conflict check failed (non-blocking): {e}")


async def _persist_memory_updates(text: str, source_msg_id: int | None = None,
                                   source_chat_id: str = "",
                                   source: str = "",
                                   allowed_domains: tuple[str, ...] = (),
                                   write_domain: str = ""):
    """Extract and persist FACTS_UPDATE from response text.

    FACTS_UPDATE writes to message_facts table (RAG-adjacent structured facts).
    source_chat_id: chat that generated this fact (for data access isolation).
    allowed_domains: session's allowed domains — domain_id in FACTS_UPDATE is
    validated against this list to prevent cross-domain fact injection.
    write_domain: the chat's configured write domain (CB-94). Facts without
    explicit domain tag are auto-routed here. Empty = all facts chat-local.

    Supports two value formats:
      Simple:   {"key": "value"}                    -> category auto-guessed
      Extended: {"key": {"value": "...", "category": "training", "doc_pointer": "/path"}}
    """
    from bot.storage.memory import save_message_fact

    for start, end in _find_facts_blocks(text):
        # Extract just the JSON part (after 'FACTS_UPDATE:' prefix)
        block = text[start:end]
        brace_idx = block.index('{')
        json_text = block[brace_idx:]
        try:
            data = json.loads(json_text)
            for key, raw_value in data.items():
                if key.startswith("_"):
                    continue
                # Parse extended format: {"value": "...", "category": "...", "subject": "...",
                #                        "doc_pointer": "...", "domain": "..."}
                category = ""
                doc_pointer = ""
                subject = ""
                domain_id = ""
                if isinstance(raw_value, dict):
                    value = str(raw_value.get("value", ""))
                    category = raw_value.get("category", "")
                    subject = raw_value.get("subject", "")
                    doc_pointer = raw_value.get("doc_pointer", "") or raw_value.get("doc", "")
                    domain_id = raw_value.get("domain", "") or raw_value.get("domain_id", "")
                else:
                    value = str(raw_value)
                if not value:
                    continue
                # CB-94: Auto-route to write_domain when domain field is omitted.
                # If model explicitly specifies a domain, validate it.
                # If model omits domain and write_domain is set, auto-apply.
                if not domain_id and write_domain:
                    # Auto-route to write_domain (the chat's configured shared domain)
                    # Exception: credentials and chat-specific categories stay local
                    from bot.storage.facts import _guess_fact_category
                    _auto_cat = category or _guess_fact_category(key)
                    # Sensitive categories stay chat-local — never auto-route to shared domain
                    _LOCAL_ONLY_CATEGORIES = frozenset({
                        "credentials", "bot", "health", "finance",
                        "contact", "documents", "personal",
                    })
                    if _auto_cat not in _LOCAL_ONLY_CATEGORIES:
                        domain_id = write_domain
                # Validate domain_id: must be in session's allowed_domains + valid format
                if domain_id:
                    from bot.storage.domains import _validate_domain_id
                    if not _validate_domain_id(domain_id):
                        log.warning("FACTS_UPDATE: invalid domain_id format '%s', ignoring domain tag", domain_id[:50])
                        domain_id = ""
                    elif allowed_domains and domain_id not in allowed_domains:
                        log.warning("FACTS_UPDATE: domain_id '%s' not in session allowed_domains %s, ignoring domain tag", domain_id, allowed_domains)
                        domain_id = ""
                # B5d: RBAC write-safety check for shared domains.
                # DM sessions have READ-ONLY access to shared domains.
                # Write to a shared domain requires explicit RBAC "write" permission
                # on "domain_data" for the session's chat_id.
                if domain_id and not source_chat_id:
                    # No source_chat_id = system task or internal caller.
                    # Deny domain writes without explicit chat context (fail-closed, F5).
                    log.warning("FACTS_UPDATE: domain_id '%s' denied — no source_chat_id for RBAC check", domain_id)
                    domain_id = ""
                if domain_id and source_chat_id:
                    try:
                        from bot.rbac import get_engine
                        from bot.core.access import build_rbac_subject
                        engine = get_engine()
                        # Flat permission model: DM = user subject, group = chat subject
                        # CB-133: Inherit source from ROOT_ADMIN if not provided
                        _src = source
                        if not _src:
                            from config import ROOT_ADMIN
                            _src = ROOT_ADMIN[0] or "telegram"
                        _fact_subject = build_rbac_subject(_src, f"{_src}:{source_chat_id}", source_chat_id)
                        write_decision = await engine.check_access(
                            _fact_subject,
                            "write", "domain_data", domain_id,
                        )
                        if not write_decision.allowed:
                            log.warning(
                                "FACTS_UPDATE: domain_id '%s' write denied by RBAC "
                                "for chat %s -- storing as chat-local + flagging admin",
                                domain_id, source_chat_id[:20],
                            )
                            # Flag admin about misconfigured domain access (one-time)
                            await _flag_admin_domain_denial(
                                source_chat_id, domain_id, "write",
                            )
                            domain_id = ""  # Fall back to chat-local fact
                    except Exception:
                        # RBAC unavailable -- default to chat-local (safe)
                        log.debug("RBAC domain write check unavailable, defaulting to chat-local")
                        domain_id = ""
                # Safety net: check for timing fact conflicts before saving
                await _check_timing_fact_conflicts(key, value)
                try:
                    await save_message_fact(
                        key, value,
                        source_msg_id=source_msg_id,
                        category=category,
                        doc_pointer=doc_pointer,
                        subject=subject,
                        source_chat_id=source_chat_id,
                        domain_id=domain_id,
                    )
                    _domain_tag = f" domain={domain_id}" if domain_id else ""
                    log.debug(f"Persisted fact: [{subject or 'auto'}] {key}={value[:50]} cat={category or 'auto'}{_domain_tag}")
                except Exception as e:
                    log.warning(f"Failed to persist fact '{key}' to message_facts: {e}")
        except json.JSONDecodeError:
            pass


# One-time flags to prevent spamming admin with repeated denial notifications
_domain_denial_flagged: set[str] = set()


async def _flag_admin_domain_denial(
    source_chat_id: str,
    denied_domain: str,
    operation: str,
):
    """Notify admin when RBAC denies a domain operation configured in chat registry.

    One-time per session (suppresses repeat alerts for same chat+domain).
    """
    flag_key = f"{source_chat_id}:{denied_domain}:{operation}"
    if flag_key in _domain_denial_flagged:
        return  # Already flagged this session
    _domain_denial_flagged.add(flag_key)

    log.warning(
        "DOMAIN_DENIAL: chat=%s op=%s domain=%s — RBAC denied, falling back to local",
        source_chat_id[:30], operation, denied_domain,
    )

    # CB-133: Alert admin on their platform (Telegram or Teams)
    try:
        from config import ROOT_ADMIN
        _alert_source, _alert_uid = ROOT_ADMIN
        _alert_msg = (
            f"<b>\u26a0\ufe0f Domain access denied</b>\n"
            f"Chat: <code>{source_chat_id[:40]}</code>\n"
            f"Operation: {operation}\n"
            f"Domain: <code>{denied_domain}</code>\n"
            f"Fallback: storing as chat-local\n\n"
            f"<i>Fix: assign RBAC {operation} permission for this chat on domain '{denied_domain}'</i>"
        )
        if _alert_source == "telegram":
            from integrations.telegram_botapi import send_message as tg_send
            await tg_send(_alert_msg, chat_id=int(_alert_uid), parse_mode="HTML")
        elif _alert_source == "teams":
            from integrations.teams import get_client as _get_teams
            _tc = _get_teams()
            if _tc:
                await _tc.send_message(_alert_uid, _alert_msg.replace("<b>", "**").replace("</b>", "**")
                                       .replace("<code>", "`").replace("</code>", "`")
                                       .replace("<i>", "*").replace("</i>", "*"))
        else:
            log.warning("Cannot send domain denial alert: ROOT_ADMIN source is '%s' "
                        "(not telegram or teams). Set ROOT_ADMIN env var.", _alert_source)
    except Exception as e:
        log.warning("Failed to flag admin about domain denial: %s", e)
