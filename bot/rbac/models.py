# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""RBAC data models — subjects, policies, decisions."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Subject:
    """Who is requesting access."""
    user_id: str = ""              # Azure AD Object ID or TG/WA user ID
    team_ids: tuple[str, ...] = () # Team/group memberships (for team inheritance)
    chat_id: str = ""              # Current conversation ID
    source: str = ""               # "teams" | "telegram" | "whatsapp"


@dataclass(frozen=True)
class AccessDecision:
    """Result of a policy evaluation."""
    allowed: bool
    reason: str
    policy_id: int | None = None   # Which policy decided (None = default deny)
    role_id: str = ""              # Which role the policy belongs to


@dataclass
class Policy:
    """A single privilege grant or denial."""
    policy_id: int = 0
    role_id: str = ""
    privilege: str = ""            # admin | read | write | use | execute
    object_type: str = ""          # knowledge_domain | credentials | tools | chat_data | filesystem | network
    object_id: str = "*"           # specific ID, prefix with *, or "*" for all
    effect: str = "allow"          # allow | deny
    conditions: dict = field(default_factory=dict)

    def matches(self, privilege: str, object_type: str, object_id: str) -> bool:
        """Check if this policy matches the requested access."""
        # Privilege match: exact or wildcard
        if self.privilege != "*" and self.privilege != privilege:
            return False
        # Object type match: exact or wildcard
        if self.object_type != "*" and self.object_type != object_type:
            return False
        # Object ID match: exact, prefix (ends with *), or wildcard
        if self.object_id != "*":
            if self.object_id.endswith("*"):
                prefix = self.object_id[:-1]
                if not object_id.startswith(prefix):
                    return False
            elif self.object_id != object_id:
                return False
        return True


# Valid privilege values
PRIVILEGES = frozenset({"admin", "read", "write", "use", "execute", "*"})

# Valid object types
OBJECT_TYPES = frozenset({
    "knowledge_domain", "credentials", "tools", "commands", "chat_data",
    "domain_data", "filesystem", "network", "pm_project", "*",
})

# Valid effects
EFFECTS = frozenset({"allow", "deny"})
