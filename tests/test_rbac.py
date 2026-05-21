# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Unit tests for RBAC — Role-Based Access Control.

Tests:
- Policy.matches(): exact, prefix, wildcard, type mismatch
- PolicyEngine.check_access(): deny overrides allow, default deny, role inheritance
- PolicyEngine.get_allowed_domains(): specific domains, wildcard, empty
- PolicyEngine.get_execution_scope(): none/sandbox/full upgrade logic
- Subject role collection: user + team + chat roles merged
- Expired assignments filtered out
- Store CRUD: roles, policies, assignments, audit log
- Seed: builtin roles created idempotently
"""

import asyncio
from datetime import datetime, timezone, timedelta

import pytest
import pytest_asyncio

from bot.rbac.models import AccessDecision, Policy, Subject


# ============================================================
# 1. Policy.matches() — pure Python, no DB
# ============================================================

class TestPolicyMatches:
    def test_exact_match(self):
        p = Policy(privilege="use", object_type="tools", object_id="Bash")
        assert p.matches("use", "tools", "Bash") is True

    def test_privilege_mismatch(self):
        p = Policy(privilege="read", object_type="tools", object_id="Bash")
        assert p.matches("use", "tools", "Bash") is False

    def test_object_type_mismatch(self):
        p = Policy(privilege="use", object_type="credentials", object_id="Bash")
        assert p.matches("use", "tools", "Bash") is False

    def test_object_id_mismatch(self):
        p = Policy(privilege="use", object_type="tools", object_id="Write")
        assert p.matches("use", "tools", "Bash") is False

    def test_privilege_wildcard(self):
        p = Policy(privilege="*", object_type="tools", object_id="Bash")
        assert p.matches("use", "tools", "Bash") is True
        assert p.matches("admin", "tools", "Bash") is True
        assert p.matches("execute", "tools", "Bash") is True

    def test_object_type_wildcard(self):
        p = Policy(privilege="use", object_type="*", object_id="Bash")
        assert p.matches("use", "tools", "Bash") is True
        assert p.matches("use", "credentials", "Bash") is True

    def test_object_id_wildcard(self):
        p = Policy(privilege="use", object_type="tools", object_id="*")
        assert p.matches("use", "tools", "Bash") is True
        assert p.matches("use", "tools", "Write") is True
        assert p.matches("use", "tools", "anything") is True

    def test_all_wildcards(self):
        p = Policy(privilege="*", object_type="*", object_id="*")
        assert p.matches("admin", "credentials", "secret_key") is True

    def test_prefix_match(self):
        p = Policy(privilege="use", object_type="tools", object_id="telegram_*")
        assert p.matches("use", "tools", "telegram_send_message") is True
        assert p.matches("use", "tools", "telegram_send_photo") is True
        assert p.matches("use", "tools", "whatsapp_send_message") is False

    def test_prefix_exact_boundary(self):
        """Prefix 'deploy_*' should NOT match 'deploy' (no trailing chars)."""
        p = Policy(privilege="use", object_type="tools", object_id="deploy_*")
        # "deploy_" prefix requires at least "deploy_" — "deploy" is shorter
        assert p.matches("use", "tools", "deploy") is False
        assert p.matches("use", "tools", "deploy_bot") is True
        assert p.matches("use", "tools", "deploy_staging") is True

    def test_empty_prefix(self):
        """Prefix '*' at position 0 means object_id='*' — wildcard."""
        p = Policy(privilege="use", object_type="tools", object_id="*")
        assert p.matches("use", "tools", "anything") is True


# ============================================================
# 2. Subject — pure Python
# ============================================================

class TestSubject:
    def test_frozen(self):
        s = Subject(user_id="u1", chat_id="c1")
        with pytest.raises(AttributeError):
            s.user_id = "u2"  # type: ignore[misc]

    def test_defaults(self):
        s = Subject()
        assert s.user_id == ""
        assert s.team_ids == ()
        assert s.chat_id == ""
        assert s.source == ""

    def test_with_teams(self):
        s = Subject(user_id="u1", team_ids=("eng", "devops"))
        assert len(s.team_ids) == 2


# ============================================================
# 3. AccessDecision — pure Python
# ============================================================

class TestAccessDecision:
    def test_allowed(self):
        d = AccessDecision(allowed=True, reason="ok", policy_id=1, role_id="admin")
        assert d.allowed is True
        assert d.policy_id == 1

    def test_denied_default(self):
        d = AccessDecision(allowed=False, reason="default deny")
        assert d.allowed is False
        assert d.policy_id is None
        assert d.role_id == ""


# ============================================================
# 4. DB-backed tests — schema, store, engine, seed
# ============================================================

@pytest_asyncio.fixture
async def rbac_db():
    """Initialize the RBAC schema in the test DB."""
    from bot.rbac.schema import init_rbac_db
    await init_rbac_db()
    yield
    # Cleanup: drop tables so tests are isolated
    from bot.storage.db import open_db
    async with open_db() as db:
        await db.executescript("""
            DELETE FROM rbac_access_log;
            DELETE FROM rbac_assignments;
            DELETE FROM rbac_policies;
            DELETE FROM rbac_roles;
        """)
        await db.commit()


@pytest.fixture
def fresh_engine():
    """Return a fresh PolicyEngine with no cached state."""
    from bot.rbac.engine import PolicyEngine
    return PolicyEngine(cache_ttl=0)  # TTL=0 forces refresh every check


# --- Store CRUD tests ---

class TestStoreCRUD:
    @pytest.mark.asyncio
    async def test_create_and_get_role(self, rbac_db):
        from bot.rbac.store import create_role, get_role
        r = await create_role("test_role", "Test Role", "A test role")
        assert r["role_id"] == "test_role"
        assert r["name"] == "Test Role"

        fetched = await get_role("test_role")
        assert fetched is not None
        assert fetched["role_id"] == "test_role"

    @pytest.mark.asyncio
    async def test_get_role_not_found(self, rbac_db):
        from bot.rbac.store import get_role
        assert await get_role("nonexistent") is None

    @pytest.mark.asyncio
    async def test_list_roles(self, rbac_db):
        from bot.rbac.store import create_role, list_roles
        await create_role("r1", "Role 1")
        await create_role("r2", "Role 2")
        roles = await list_roles()
        role_ids = [r["role_id"] for r in roles]
        assert "r1" in role_ids
        assert "r2" in role_ids

    @pytest.mark.asyncio
    async def test_delete_role(self, rbac_db):
        from bot.rbac.store import create_role, delete_role, get_role
        await create_role("deletable", "Deletable")
        assert await delete_role("deletable") is True
        assert await get_role("deletable") is None

    @pytest.mark.asyncio
    async def test_delete_builtin_blocked(self, rbac_db):
        from bot.rbac.store import create_role, delete_role
        await create_role("builtin_role", "Builtin", is_builtin=True)
        with pytest.raises(ValueError, match="Cannot delete builtin"):
            await delete_role("builtin_role")

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, rbac_db):
        from bot.rbac.store import delete_role
        assert await delete_role("ghost") is False

    @pytest.mark.asyncio
    async def test_add_and_get_policies(self, rbac_db):
        from bot.rbac.store import create_role, add_policy, get_policies_for_role
        await create_role("pol_role", "Policy Role")
        pid = await add_policy("pol_role", "use", "tools", "Bash", "allow")
        assert pid > 0

        policies = await get_policies_for_role("pol_role")
        assert len(policies) == 1
        assert policies[0].privilege == "use"
        assert policies[0].object_type == "tools"
        assert policies[0].object_id == "Bash"
        assert policies[0].effect == "allow"

    @pytest.mark.asyncio
    async def test_remove_policy(self, rbac_db):
        from bot.rbac.store import create_role, add_policy, remove_policy, get_policies_for_role
        await create_role("rm_role", "Remove")
        pid = await add_policy("rm_role", "read", "chat_data", "*")
        assert await remove_policy(pid) is True
        assert await get_policies_for_role("rm_role") == []

    @pytest.mark.asyncio
    async def test_assign_and_get(self, rbac_db):
        from bot.rbac.store import create_role, assign_role, get_assignments_for_subject
        await create_role("assign_role", "Assign")
        aid = await assign_role("user", "user_123", "assign_role", "admin")
        assert aid > 0

        assignments = await get_assignments_for_subject("user", "user_123")
        assert len(assignments) == 1
        assert assignments[0]["role_id"] == "assign_role"
        assert assignments[0]["granted_by"] == "admin"

    @pytest.mark.asyncio
    async def test_revoke_role(self, rbac_db):
        from bot.rbac.store import create_role, assign_role, revoke_role, get_assignments_for_subject
        await create_role("rev_role", "Revoke")
        await assign_role("user", "u1", "rev_role")
        assert await revoke_role("user", "u1", "rev_role") is True
        assert await get_assignments_for_subject("user", "u1") == []

    @pytest.mark.asyncio
    async def test_revoke_nonexistent(self, rbac_db):
        from bot.rbac.store import revoke_role
        assert await revoke_role("user", "ghost", "ghost_role") is False

    @pytest.mark.asyncio
    async def test_get_all_assignments(self, rbac_db):
        from bot.rbac.store import create_role, assign_role, get_all_assignments
        await create_role("all_role", "All")
        await assign_role("user", "u1", "all_role")
        await assign_role("team", "t1", "all_role")
        all_a = await get_all_assignments()
        assert len(all_a) >= 2

    @pytest.mark.asyncio
    async def test_log_access(self, rbac_db):
        from bot.rbac.store import log_access
        from bot.storage.db import open_db
        await log_access("u1", "use", "tools", "Bash", "allowed", 1, "admin")
        async with open_db() as db:
            rows = await db.execute_fetchall("SELECT * FROM rbac_access_log")
        assert len(rows) >= 1
        assert rows[0]["subject_id"] == "u1"
        assert rows[0]["effect"] == "allowed"


# --- PolicyEngine tests ---

class TestPolicyEngine:
    @pytest.mark.asyncio
    async def test_no_roles_default_deny(self, rbac_db, fresh_engine):
        subject = Subject(user_id="nobody")
        decision = await fresh_engine.check_access(
            subject, "use", "tools", "Bash", log_decision=False
        )
        assert decision.allowed is False
        assert "No roles assigned" in decision.reason

    @pytest.mark.asyncio
    async def test_allow_with_matching_policy(self, rbac_db, fresh_engine):
        from bot.rbac.store import create_role, add_policy, assign_role
        await create_role("allow_role", "Allow Role")
        await add_policy("allow_role", "use", "tools", "Bash", "allow")
        await assign_role("user", "u1", "allow_role")

        subject = Subject(user_id="u1")
        decision = await fresh_engine.check_access(
            subject, "use", "tools", "Bash", log_decision=False
        )
        assert decision.allowed is True
        assert decision.role_id == "allow_role"

    @pytest.mark.asyncio
    async def test_deny_overrides_allow(self, rbac_db, fresh_engine):
        from bot.rbac.store import create_role, add_policy, assign_role
        # Role with both allow and deny on same resource
        await create_role("mixed_role", "Mixed")
        await add_policy("mixed_role", "use", "tools", "Bash", "allow")
        await add_policy("mixed_role", "use", "tools", "Bash", "deny")
        await assign_role("user", "u_mixed", "mixed_role")

        subject = Subject(user_id="u_mixed")
        decision = await fresh_engine.check_access(
            subject, "use", "tools", "Bash", log_decision=False
        )
        assert decision.allowed is False
        assert "Denied" in decision.reason

    @pytest.mark.asyncio
    async def test_deny_from_second_role_overrides(self, rbac_db, fresh_engine):
        from bot.rbac.store import create_role, add_policy, assign_role
        # Role A allows, Role B denies — deny wins
        await create_role("allow_r", "Allow")
        await add_policy("allow_r", "use", "tools", "deploy_bot", "allow")
        await create_role("deny_r", "Deny")
        await add_policy("deny_r", "use", "tools", "deploy_bot", "deny")
        await assign_role("user", "u_both", "allow_r")
        await assign_role("user", "u_both", "deny_r")

        subject = Subject(user_id="u_both")
        decision = await fresh_engine.check_access(
            subject, "use", "tools", "deploy_bot", log_decision=False
        )
        assert decision.allowed is False

    @pytest.mark.asyncio
    async def test_no_matching_policy_default_deny(self, rbac_db, fresh_engine):
        from bot.rbac.store import create_role, add_policy, assign_role
        await create_role("limited_role", "Limited")
        await add_policy("limited_role", "read", "chat_data", "*", "allow")
        await assign_role("user", "u_limited", "limited_role")

        subject = Subject(user_id="u_limited")
        decision = await fresh_engine.check_access(
            subject, "use", "tools", "Bash", log_decision=False
        )
        assert decision.allowed is False
        assert "No matching policy" in decision.reason

    @pytest.mark.asyncio
    async def test_wildcard_privilege_matches(self, rbac_db, fresh_engine):
        from bot.rbac.store import create_role, add_policy, assign_role
        await create_role("superadmin", "Super")
        await add_policy("superadmin", "*", "*", "*", "allow")
        await assign_role("user", "u_super", "superadmin")

        subject = Subject(user_id="u_super")
        for priv in ("use", "read", "write", "admin", "execute"):
            decision = await fresh_engine.check_access(
                subject, priv, "tools", "Bash", log_decision=False
            )
            assert decision.allowed is True, f"Failed for privilege={priv}"

    @pytest.mark.asyncio
    async def test_team_role_inheritance(self, rbac_db, fresh_engine):
        from bot.rbac.store import create_role, add_policy, assign_role
        await create_role("team_role", "Team Role")
        await add_policy("team_role", "read", "knowledge_domain", "eng-runbooks", "allow")
        await assign_role("team", "engineering", "team_role")

        # User has no direct role, but is a member of the "engineering" team
        subject = Subject(user_id="u_eng", team_ids=("engineering",))
        decision = await fresh_engine.check_access(
            subject, "read", "knowledge_domain", "eng-runbooks", log_decision=False
        )
        assert decision.allowed is True

    @pytest.mark.asyncio
    async def test_chat_role(self, rbac_db, fresh_engine):
        from bot.rbac.store import create_role, add_policy, assign_role
        await create_role("chat_role", "Chat Role")
        await add_policy("chat_role", "use", "tools", "search_media", "allow")
        await assign_role("chat", "chat_42", "chat_role")

        subject = Subject(user_id="u_any", chat_id="chat_42")
        decision = await fresh_engine.check_access(
            subject, "use", "tools", "search_media", log_decision=False
        )
        assert decision.allowed is True

    @pytest.mark.asyncio
    async def test_user_team_chat_roles_merged(self, rbac_db, fresh_engine):
        """Roles from user + team + chat are all considered together."""
        from bot.rbac.store import create_role, add_policy, assign_role
        # User role: read chat_data
        await create_role("user_r", "User R")
        await add_policy("user_r", "read", "chat_data", "*", "allow")
        await assign_role("user", "u_merge", "user_r")
        # Team role: read knowledge
        await create_role("team_r", "Team R")
        await add_policy("team_r", "read", "knowledge_domain", "hr-docs", "allow")
        await assign_role("team", "hr_team", "team_r")
        # Chat role: use tools
        await create_role("chat_r", "Chat R")
        await add_policy("chat_r", "use", "tools", "memory_search", "allow")
        await assign_role("chat", "c_merge", "chat_r")

        subject = Subject(user_id="u_merge", team_ids=("hr_team",), chat_id="c_merge")

        # Should have access from user role
        d1 = await fresh_engine.check_access(
            subject, "read", "chat_data", "*", log_decision=False
        )
        assert d1.allowed is True
        # Should have access from team role
        d2 = await fresh_engine.check_access(
            subject, "read", "knowledge_domain", "hr-docs", log_decision=False
        )
        assert d2.allowed is True
        # Should have access from chat role
        d3 = await fresh_engine.check_access(
            subject, "use", "tools", "memory_search", log_decision=False
        )
        assert d3.allowed is True

    @pytest.mark.asyncio
    async def test_expired_assignment_filtered(self, rbac_db, fresh_engine):
        from bot.rbac.store import create_role, add_policy, assign_role
        await create_role("exp_role", "Expiring")
        await add_policy("exp_role", "use", "tools", "Bash", "allow")
        # Assignment expired 1 hour ago
        expired_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        await assign_role("user", "u_expired", "exp_role", expires_at=expired_ts)

        subject = Subject(user_id="u_expired")
        decision = await fresh_engine.check_access(
            subject, "use", "tools", "Bash", log_decision=False
        )
        assert decision.allowed is False
        assert "No roles assigned" in decision.reason

    @pytest.mark.asyncio
    async def test_non_expired_assignment_active(self, rbac_db, fresh_engine):
        from bot.rbac.store import create_role, add_policy, assign_role
        await create_role("future_role", "Future")
        await add_policy("future_role", "use", "tools", "Bash", "allow")
        # Assignment expires 1 hour from now — still active
        future_ts = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        await assign_role("user", "u_future", "future_role", expires_at=future_ts)

        subject = Subject(user_id="u_future")
        decision = await fresh_engine.check_access(
            subject, "use", "tools", "Bash", log_decision=False
        )
        assert decision.allowed is True

    @pytest.mark.asyncio
    async def test_cache_invalidation(self, rbac_db):
        from bot.rbac.engine import PolicyEngine
        from bot.rbac.store import create_role, add_policy, assign_role
        engine = PolicyEngine(cache_ttl=60)  # Long TTL

        await create_role("cache_role", "Cached")
        await add_policy("cache_role", "use", "tools", "Read", "allow")
        await assign_role("user", "u_cache", "cache_role")

        subject = Subject(user_id="u_cache")
        d1 = await engine.check_access(subject, "use", "tools", "Read", log_decision=False)
        assert d1.allowed is True

        # Add a deny policy — but cache won't see it
        await add_policy("cache_role", "use", "tools", "Read", "deny")
        d2 = await engine.check_access(subject, "use", "tools", "Read", log_decision=False)
        assert d2.allowed is True  # Still cached as allowed

        # Invalidate cache — now deny should win
        engine.invalidate_cache()
        d3 = await engine.check_access(subject, "use", "tools", "Read", log_decision=False)
        assert d3.allowed is False

    @pytest.mark.asyncio
    async def test_access_logging(self, rbac_db, fresh_engine):
        from bot.rbac.store import create_role, add_policy, assign_role
        from bot.storage.db import open_db
        await create_role("log_role", "Logged")
        await add_policy("log_role", "use", "tools", "Bash", "allow")
        await assign_role("user", "u_log", "log_role")

        subject = Subject(user_id="u_log")
        await fresh_engine.check_access(
            subject, "use", "tools", "Bash",
            session_key="sess_1", tool_name="Bash",
            log_decision=True,
        )

        async with open_db() as db:
            rows = await db.execute_fetchall(
                "SELECT * FROM rbac_access_log WHERE subject_id = 'u_log'"
            )
        assert len(rows) >= 1
        assert rows[0]["effect"] == "allowed"
        assert rows[0]["session_key"] == "sess_1"
        assert rows[0]["tool_name"] == "Bash"


# --- get_allowed_domains tests ---

class TestGetAllowedDomains:
    @pytest.mark.asyncio
    async def test_no_roles_empty(self, rbac_db, fresh_engine):
        subject = Subject(user_id="nobody")
        domains = await fresh_engine.get_allowed_domains(subject)
        assert domains == []

    @pytest.mark.asyncio
    async def test_specific_domains(self, rbac_db, fresh_engine):
        from bot.rbac.store import create_role, add_policy, assign_role
        await create_role("domain_r", "Domain")
        await add_policy("domain_r", "read", "knowledge_domain", "eng-runbooks")
        await add_policy("domain_r", "read", "knowledge_domain", "hr-docs")
        await assign_role("user", "u_dom", "domain_r")

        subject = Subject(user_id="u_dom")
        domains = await fresh_engine.get_allowed_domains(subject)
        assert set(domains) == {"eng-runbooks", "hr-docs"}

    @pytest.mark.asyncio
    async def test_wildcard_domain(self, rbac_db, fresh_engine):
        from bot.rbac.store import create_role, add_policy, assign_role
        await create_role("admin_r", "Admin")
        await add_policy("admin_r", "read", "knowledge_domain", "*")
        await assign_role("user", "u_admin", "admin_r")

        subject = Subject(user_id="u_admin")
        domains = await fresh_engine.get_allowed_domains(subject)
        assert domains == ["*"]

    @pytest.mark.asyncio
    async def test_admin_privilege_includes_domains(self, rbac_db, fresh_engine):
        from bot.rbac.store import create_role, add_policy, assign_role
        await create_role("admin_priv", "Admin Priv")
        await add_policy("admin_priv", "admin", "knowledge_domain", "*")
        await assign_role("user", "u_adm_priv", "admin_priv")

        subject = Subject(user_id="u_adm_priv")
        domains = await fresh_engine.get_allowed_domains(subject)
        assert domains == ["*"]

    @pytest.mark.asyncio
    async def test_deny_domains_excluded(self, rbac_db, fresh_engine):
        """Deny policies are skipped in get_allowed_domains (effect != allow)."""
        from bot.rbac.store import create_role, add_policy, assign_role
        await create_role("deny_dom_r", "Deny Domain")
        await add_policy("deny_dom_r", "read", "knowledge_domain", "secret-docs", "deny")
        await add_policy("deny_dom_r", "read", "knowledge_domain", "public-docs", "allow")
        await assign_role("user", "u_deny_dom", "deny_dom_r")

        subject = Subject(user_id="u_deny_dom")
        domains = await fresh_engine.get_allowed_domains(subject)
        assert domains == ["public-docs"]

    @pytest.mark.asyncio
    async def test_dedup_domains(self, rbac_db, fresh_engine):
        from bot.rbac.store import create_role, add_policy, assign_role
        await create_role("dup_r", "Dup")
        await add_policy("dup_r", "read", "knowledge_domain", "eng-runbooks")
        await add_policy("dup_r", "read", "knowledge_domain", "eng-runbooks")
        await assign_role("user", "u_dup", "dup_r")

        subject = Subject(user_id="u_dup")
        domains = await fresh_engine.get_allowed_domains(subject)
        assert domains == ["eng-runbooks"]  # No duplicates


# --- get_execution_scope tests ---

class TestGetExecutionScope:
    @pytest.mark.asyncio
    async def test_no_roles_none(self, rbac_db, fresh_engine):
        subject = Subject(user_id="nobody")
        scope = await fresh_engine.get_execution_scope(subject)
        assert scope == {"filesystem": "none", "network": "none"}

    @pytest.mark.asyncio
    async def test_sandbox_filesystem(self, rbac_db, fresh_engine):
        from bot.rbac.store import create_role, add_policy, assign_role
        await create_role("sandbox_r", "Sandbox")
        await add_policy("sandbox_r", "execute", "filesystem", "sandbox")
        await assign_role("user", "u_sb", "sandbox_r")

        subject = Subject(user_id="u_sb")
        scope = await fresh_engine.get_execution_scope(subject)
        assert scope["filesystem"] == "sandbox"
        assert scope["network"] == "none"

    @pytest.mark.asyncio
    async def test_full_filesystem(self, rbac_db, fresh_engine):
        from bot.rbac.store import create_role, add_policy, assign_role
        await create_role("full_r", "Full")
        await add_policy("full_r", "execute", "filesystem", "full")
        await assign_role("user", "u_full", "full_r")

        subject = Subject(user_id="u_full")
        scope = await fresh_engine.get_execution_scope(subject)
        assert scope["filesystem"] == "full"

    @pytest.mark.asyncio
    async def test_full_overrides_sandbox(self, rbac_db, fresh_engine):
        """Full beats sandbox when both are granted."""
        from bot.rbac.store import create_role, add_policy, assign_role
        await create_role("upgrade_r", "Upgrade")
        await add_policy("upgrade_r", "execute", "filesystem", "sandbox")
        await add_policy("upgrade_r", "execute", "filesystem", "full")
        await assign_role("user", "u_upgrade", "upgrade_r")

        subject = Subject(user_id="u_upgrade")
        scope = await fresh_engine.get_execution_scope(subject)
        assert scope["filesystem"] == "full"

    @pytest.mark.asyncio
    async def test_sandboxed_network(self, rbac_db, fresh_engine):
        from bot.rbac.store import create_role, add_policy, assign_role
        await create_role("netbox_r", "NetBox")
        await add_policy("netbox_r", "execute", "network", "sandboxed")
        await assign_role("user", "u_net", "netbox_r")

        subject = Subject(user_id="u_net")
        scope = await fresh_engine.get_execution_scope(subject)
        assert scope["network"] == "sandboxed"
        assert scope["filesystem"] == "none"

    @pytest.mark.asyncio
    async def test_full_network(self, rbac_db, fresh_engine):
        from bot.rbac.store import create_role, add_policy, assign_role
        await create_role("fullnet_r", "Full Net")
        await add_policy("fullnet_r", "execute", "network", "full")
        await assign_role("user", "u_fullnet", "fullnet_r")

        subject = Subject(user_id="u_fullnet")
        scope = await fresh_engine.get_execution_scope(subject)
        assert scope["network"] == "full"

    @pytest.mark.asyncio
    async def test_admin_wildcard_gives_full(self, rbac_db, fresh_engine):
        from bot.rbac.store import create_role, add_policy, assign_role
        await create_role("admin_scope_r", "Admin Scope")
        await add_policy("admin_scope_r", "*", "*", "*")
        await assign_role("user", "u_admin_scope", "admin_scope_r")

        subject = Subject(user_id="u_admin_scope")
        scope = await fresh_engine.get_execution_scope(subject)
        assert scope["filesystem"] == "full"
        assert scope["network"] == "full"

    @pytest.mark.asyncio
    async def test_deny_exec_not_upgraded(self, rbac_db, fresh_engine):
        """Deny policies don't contribute to execution scope upgrades."""
        from bot.rbac.store import create_role, add_policy, assign_role
        await create_role("deny_exec_r", "Deny Exec")
        await add_policy("deny_exec_r", "execute", "filesystem", "full", "deny")
        await assign_role("user", "u_deny_exec", "deny_exec_r")

        subject = Subject(user_id="u_deny_exec")
        scope = await fresh_engine.get_execution_scope(subject)
        assert scope["filesystem"] == "none"  # Deny doesn't grant


# --- Seed tests ---

class TestSeed:
    @pytest.mark.asyncio
    async def test_seed_creates_builtin_roles(self, rbac_db):
        from bot.rbac.seed import seed_builtin_roles
        from bot.rbac.store import get_role, get_policies_for_role
        await seed_builtin_roles()

        admin = await get_role("bot_admin")
        assert admin is not None
        assert admin["is_builtin"] is True
        admin_policies = await get_policies_for_role("bot_admin")
        assert len(admin_policies) >= 1
        # bot_admin should have wildcard policy
        assert any(p.privilege == "*" and p.object_type == "*" for p in admin_policies)

        employee = await get_role("employee")
        assert employee is not None
        emp_policies = await get_policies_for_role("employee")
        assert len(emp_policies) > 5  # Multiple allow + deny policies

        # guest role removed — only bot_admin + employee remain

    @pytest.mark.asyncio
    async def test_seed_idempotent(self, rbac_db):
        from bot.rbac.seed import seed_builtin_roles
        from bot.rbac.store import get_policies_for_role
        await seed_builtin_roles()
        policies_first = await get_policies_for_role("bot_admin")

        # Run again — should not duplicate
        await seed_builtin_roles()
        policies_second = await get_policies_for_role("bot_admin")
        assert len(policies_first) == len(policies_second)

    @pytest.mark.asyncio
    async def test_seed_admin_full_access(self, rbac_db, fresh_engine):
        """bot_admin with seeded policies should have full access."""
        from bot.rbac.seed import seed_builtin_roles
        from bot.rbac.store import assign_role
        await seed_builtin_roles()
        await assign_role("user", "u_admin_seed", "bot_admin")

        subject = Subject(user_id="u_admin_seed")
        decision = await fresh_engine.check_access(
            subject, "admin", "credentials", "secret_key", log_decision=False
        )
        assert decision.allowed is True

    @pytest.mark.asyncio
    async def test_seed_employee_deny_deploy(self, rbac_db, fresh_engine):
        """Employee should be denied deploy_bot."""
        from bot.rbac.seed import seed_builtin_roles
        from bot.rbac.store import assign_role
        await seed_builtin_roles()
        await assign_role("user", "u_emp_seed", "employee")

        subject = Subject(user_id="u_emp_seed")
        decision = await fresh_engine.check_access(
            subject, "use", "tools", "deploy_bot", log_decision=False
        )
        assert decision.allowed is False

    @pytest.mark.asyncio
    async def test_seed_employee_sandbox_scope(self, rbac_db, fresh_engine):
        """Employee should get filesystem=sandbox execution scope."""
        from bot.rbac.seed import seed_builtin_roles
        from bot.rbac.store import assign_role
        await seed_builtin_roles()
        await assign_role("user", "u_emp_sandbox", "employee")

        subject = Subject(user_id="u_emp_sandbox")
        scope = await fresh_engine.get_execution_scope(subject)
        assert scope["filesystem"] == "sandbox"
        assert scope["network"] == "sandboxed"
