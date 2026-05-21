# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""RBAC — Role-Based Access Control for the corporate bot.

4 core components:
- models: Subject, Policy, AccessDecision dataclasses
- schema: SQLite DDL + migration
- store: CRUD for roles, policies, assignments
- engine: PolicyEngine — evaluates access decisions with caching

Usage:
    from bot.rbac import get_engine
    engine = get_engine()
    decision = await engine.check_access(subject, "use", "tools", "Bash")
"""

from bot.rbac.engine import PolicyEngine

_engine: PolicyEngine | None = None


def get_engine() -> PolicyEngine:
    """Get the singleton PolicyEngine instance."""
    global _engine
    if _engine is None:
        _engine = PolicyEngine()
    return _engine
