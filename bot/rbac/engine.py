# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""PolicyEngine — evaluates RBAC access decisions with in-memory caching.

The engine loads all roles, policies, and assignments from SQLite into memory
and serves access checks from cache with a configurable TTL (default 5s).
Deny policies always override allow policies (deny-wins). Default is deny.
"""

import logging
import time
from datetime import datetime, timezone

from bot.rbac.models import AccessDecision, Policy, Subject

log = logging.getLogger(__name__)


class PolicyEngine:
    """Evaluates RBAC policies. In-memory cache refreshed from SQLite."""

    def __init__(self, cache_ttl: float = 5.0):
        self._role_policies: dict[str, list[Policy]] = {}  # role_id -> policies
        self._subject_roles: dict[str, set[str]] = {}      # "type:id" -> role_ids
        self._cache_ts: float = 0
        self._cache_ttl: float = cache_ttl
        self._initialized: bool = False

    async def _refresh_cache(self) -> None:
        """Reload all roles, policies, assignments from DB if TTL expired."""
        now = time.time()
        if now - self._cache_ts < self._cache_ttl and self._initialized:
            return

        from bot.rbac.store import get_all_assignments, get_policies_for_role, list_roles

        roles = await list_roles()
        new_role_policies: dict[str, list[Policy]] = {}
        for r in roles:
            policies = await get_policies_for_role(r["role_id"])
            new_role_policies[r["role_id"]] = policies

        assignments = await get_all_assignments()
        new_subject_roles: dict[str, set[str]] = {}
        for a in assignments:
            # Filter expired assignments
            if a.get("expires_at"):
                try:
                    exp = datetime.fromisoformat(
                        a["expires_at"].replace("Z", "+00:00")
                    )
                    if exp < datetime.now(timezone.utc):
                        continue  # Expired
                except Exception:
                    log.warning("Malformed expires_at '%s' — treating as expired (fail-closed)", a["expires_at"])
                    continue  # Malformed = expired (fail-closed)

            key = f"{a['subject_type']}:{a['subject_id']}"
            if key not in new_subject_roles:
                new_subject_roles[key] = set()
            new_subject_roles[key].add(a["role_id"])

        self._role_policies = new_role_policies
        self._subject_roles = new_subject_roles
        self._cache_ts = now
        self._initialized = True

    async def check_access(
        self,
        subject: Subject,
        privilege: str,
        object_type: str,
        object_id: str = "*",
        session_key: str = "",
        tool_name: str = "",
        log_decision: bool = True,
    ) -> AccessDecision:
        """Evaluate access for a subject. Deny overrides allow. Default deny."""
        await self._refresh_cache()

        # Collect all roles for this subject
        role_ids = self._get_role_ids(subject)

        if not role_ids:
            decision = AccessDecision(allowed=False, reason="No roles assigned")
            if log_decision:
                await self._log(subject, privilege, object_type, object_id,
                                decision, session_key, tool_name)
            return decision

        # Collect all matching policies across all roles
        matching_allows: list[Policy] = []
        matching_denies: list[Policy] = []

        for rid in role_ids:
            for policy in self._role_policies.get(rid, []):
                if policy.matches(privilege, object_type, object_id):
                    if policy.effect == "deny":
                        matching_denies.append(policy)
                    else:
                        matching_allows.append(policy)

        # Deny overrides allow
        if matching_denies:
            p = matching_denies[0]
            decision = AccessDecision(
                allowed=False,
                reason=f"Denied by policy on role '{p.role_id}'",
                policy_id=p.policy_id,
                role_id=p.role_id,
            )
        elif matching_allows:
            p = matching_allows[0]
            decision = AccessDecision(
                allowed=True,
                reason=f"Allowed by role '{p.role_id}'",
                policy_id=p.policy_id,
                role_id=p.role_id,
            )
        else:
            decision = AccessDecision(
                allowed=False,
                reason="No matching policy (default deny)",
            )

        if log_decision:
            await self._log(subject, privilege, object_type, object_id,
                            decision, session_key, tool_name)
        return decision

    async def get_allowed_domains(self, subject: Subject) -> list[str]:
        """Get all knowledge domain IDs this subject can read.

        Returns ["*"] if the subject has wildcard domain access.
        Deny-overrides-allow: explicit deny on a domain removes it from the list.
        """
        await self._refresh_cache()
        allowed: set[str] = set()
        denied: set[str] = set()
        wildcard_allow = False
        wildcard_deny = False
        role_ids = self._get_role_ids(subject)
        for rid in role_ids:
            for p in self._role_policies.get(rid, []):
                if p.object_type not in ("knowledge_domain", "*"):
                    continue
                if p.privilege not in ("read", "admin", "*"):
                    continue
                if p.effect == "deny":
                    if p.object_id == "*":
                        wildcard_deny = True
                    else:
                        denied.add(p.object_id)
                elif p.effect == "allow":
                    if p.object_id == "*":
                        wildcard_allow = True
                    else:
                        allowed.add(p.object_id)
        # Deny overrides allow
        if wildcard_deny:
            return []
        if wildcard_allow:
            if not denied:
                return ["*"]  # Unrestricted
            # Wildcard allow + specific denies: cannot use ["*"] sentinel because
            # consumer can't subtract denies from it. Enumerate all active domains
            # from DB and subtract denied ones.
            try:
                from bot.storage.domains import list_domains
                all_domains = await list_domains()
                all_ids = {d["domain_id"] for d in all_domains if d.get("status") == "active"}
                return list((all_ids | allowed) - denied)
            except Exception:
                # Fallback: return allowed - denied (may miss some domains, but safe)
                return list(allowed - denied) if allowed else []
        return list(allowed - denied)

    async def get_execution_scope(self, subject: Subject) -> dict:
        """Get filesystem + network execution scope for a subject.

        Returns {"filesystem": "none"|"sandbox"|"full",
                 "network": "none"|"sandboxed"|"full"}.
        Upgrade logic: none < sandbox/sandboxed < full.
        Deny-overrides-allow: explicit deny forces scope to "none".
        """
        await self._refresh_cache()
        result = {"filesystem": "none", "network": "none"}
        fs_denied = False
        net_denied = False
        role_ids = self._get_role_ids(subject)

        for rid in role_ids:
            for p in self._role_policies.get(rid, []):
                if p.privilege not in ("execute", "admin", "*"):
                    continue

                # Check denies first
                if p.effect == "deny":
                    if p.object_type in ("filesystem", "*"):
                        fs_denied = True
                    if p.object_type in ("network", "*"):
                        net_denied = True
                    continue

                if p.effect != "allow":
                    continue

                if p.object_type in ("filesystem", "*") and not fs_denied:
                    if p.object_id in ("full", "*"):
                        result["filesystem"] = "full"
                    elif p.object_id == "sandbox" and result["filesystem"] == "none":
                        result["filesystem"] = "sandbox"

                if p.object_type in ("network", "*") and not net_denied:
                    if p.object_id in ("full", "*"):
                        result["network"] = "full"
                    elif p.object_id == "sandboxed" and result["network"] == "none":
                        result["network"] = "sandboxed"

        # Deny overrides: force to "none" if denied
        if fs_denied:
            result["filesystem"] = "none"
        if net_denied:
            result["network"] = "none"

        return result

    def _get_role_ids(self, subject: Subject) -> set[str]:
        """Collect all role IDs for a subject (user + teams + chat)."""
        role_ids: set[str] = set()
        if subject.user_id:
            role_ids.update(
                self._subject_roles.get(f"user:{subject.user_id}", set())
            )
        for tid in subject.team_ids:
            role_ids.update(
                self._subject_roles.get(f"team:{tid}", set())
            )
        if subject.chat_id:
            role_ids.update(
                self._subject_roles.get(f"chat:{subject.chat_id}", set())
            )
        return role_ids

    async def _log(
        self,
        subject: Subject,
        privilege: str,
        object_type: str,
        object_id: str,
        decision: AccessDecision,
        session_key: str,
        tool_name: str,
    ) -> None:
        """Log access decision (fire-and-forget — never blocks)."""
        try:
            from bot.rbac.store import log_access
            await log_access(
                subject_id=subject.user_id or subject.chat_id,
                privilege=privilege,
                object_type=object_type,
                object_id=object_id,
                effect="allowed" if decision.allowed else "denied",
                policy_id=decision.policy_id,
                role_id=decision.role_id,
                session_key=session_key,
                tool_name=tool_name,
            )
        except Exception as e:
            log.warning("RBAC audit log write failed: %s", type(e).__name__)

    def invalidate_cache(self) -> None:
        """Force cache refresh on next check."""
        self._cache_ts = 0


async def is_domain_admin(user_id: str, domain_id: str) -> bool:
    """Check if user is an admin for a specific domain.

    Checks via RBAC first (admin privilege on knowledge_domain object).
    Falls back to legacy domain config admins list for backward compat.

    Migrated from bot/core/roles.py in Sprint R4.
    """
    # RBAC check: does the user have "admin" privilege on this domain?
    try:
        from bot.rbac import get_engine as _get_engine
        engine = _get_engine()
        decision = await engine.check_access(
            Subject(user_id=user_id),
            "admin",
            "knowledge_domain",
            domain_id,
            log_decision=False,
        )
        if decision.allowed:
            return True
    except Exception:
        pass  # RBAC unavailable — fall through to legacy

    # Legacy fallback: check domain config admins list
    try:
        from bot.storage.domains import get_domain
        import yaml
        domain = await get_domain(domain_id)
        if not domain:
            return False
        config = yaml.safe_load(domain.get("config_yaml", "{}"))
        domain_cfg = config.get("domain", config)
        access = domain_cfg.get("access", {})
        admins = access.get("admins", [])
        return user_id in admins
    except Exception:
        return False
